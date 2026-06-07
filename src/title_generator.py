from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .cost_log import estimate_cost_usd, extract_image_modality_tokens
from .image_resolve import download_first_image_url, open_pil
from .title_prompts import build_title_prompt, normalize_product_category, parse_generated_title


def primary_image_url(product: dict[str, Any]) -> str:
    url = str(product.get("featured_image_url") or product.get("primary_image_url") or "").strip()
    if url:
        return url
    for media in product.get("media") or []:
        if not isinstance(media, dict):
            continue
        if str(media.get("content_type") or "IMAGE").upper() != "IMAGE":
            continue
        candidate = str(media.get("url") or "").strip()
        if candidate:
            return candidate
    return ""


def enrich_product_record(product: dict[str, Any]) -> dict[str, Any]:
    out = dict(product)
    out["canonical_category"] = normalize_product_category(
        category=str(product.get("category") or ""),
        product_type=str(product.get("product_type") or ""),
        title=str(product.get("title") or ""),
    )
    out["primary_image_url"] = primary_image_url(out)
    skus = product.get("skus") or []
    out["sku"] = str(skus[0] if skus else "")
    return out


def fetch_products_for_quotas(
    client,
    quotas: dict[str, int],
    *,
    query: str | None = "status:active",
    page_size: int = 50,
    max_pages: int = 40,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    Scan Shopify products until category quotas are filled or catalog is exhausted.
    Returns (selected_products, remaining_unfilled_quotas).
    """
    requested = {k: max(0, int(v)) for k, v in quotas.items() if int(v or 0) > 0}
    remaining = dict(requested)
    selected: list[dict[str, Any]] = []
    after: str | None = None

    for _ in range(max_pages):
        if not any(remaining.values()):
            break
        result = client.list_products(first=page_size, after=after, query=query)
        for raw in result.get("products") or []:
            prod = enrich_product_record(raw)
            bucket = str(prod.get("canonical_category") or "other")
            need = int(remaining.get(bucket, 0) or 0)
            if need <= 0:
                continue
            if not str(prod.get("primary_image_url") or "").strip():
                continue
            selected.append(prod)
            remaining[bucket] = need - 1
            if not any(remaining.values()):
                break

        page_info = result.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = str(page_info.get("endCursor") or "") or None
        if not after:
            break

    return selected, remaining


def fetch_all_products(
    client,
    *,
    query: str | None = "status:active",
    page_size: int = 50,
    max_pages: int = 200,
) -> list[dict[str, Any]]:
    """Fetch all Shopify products matching query (paginated)."""
    selected: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(max_pages):
        result = client.list_products(first=page_size, after=after, query=query)
        for raw in result.get("products") or []:
            prod = enrich_product_record(raw)
            if not str(prod.get("primary_image_url") or "").strip():
                continue
            selected.append(prod)
        page_info = result.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = str(page_info.get("endCursor") or "") or None
        if not after:
            break
    return selected


def generate_title_from_image(
    cfg,
    *,
    image_url: str = "",
    image_path: Path | None = None,
    category_key: str,
    cache_dir: Path,
    current_title: str = "",
    product_type: str = "",
    sku: str = "",
    model: str = "models/gemini-2.5-flash",
    avoid_titles: list[str] | None = None,
) -> tuple[str, float, str, dict]:
    """
    Vision-based title generation from a product image URL.
    Returns (title, estimated_cost_usd, error_message, meta).
    meta includes usage_metadata, response_id, model_version, cost_str, model.
    """
    api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    empty_meta: dict = {"usage_metadata": None, "response_id": "", "model_version": "", "cost_str": "", "model": model}
    if not api_key:
        return "", 0.0, "Missing GOOGLE_API_KEY / GEMINI_API_KEY", empty_meta

    local_path: Path | None = None
    if image_path and Path(image_path).is_file():
        local_path = Path(image_path)
    elif image_url:
        local_path = download_first_image_url([image_url], cache_dir)
    if not local_path:
        return "", 0.0, "No image available for title generation", empty_meta

    try:
        from google import genai
        from google.genai import types

        prompt = build_title_prompt(
            category_key=category_key,
            current_title=current_title,
            product_type=product_type,
            sku=sku,
            avoid_titles=avoid_titles,
        )
        image = open_pil(local_path)
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        resp = client.models.generate_content(model=model, contents=[prompt, image])
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            try:
                cand = (resp.candidates or [])[0]
                parts = (cand.content.parts or [])
                text = "\n".join([p.text for p in parts if getattr(p, "text", None)]).strip()
            except Exception:
                text = ""

        title = parse_generated_title(text)
        if not title:
            return "", 0.0, "Model returned empty title", empty_meta

        usage = getattr(resp, "usage_metadata", None)
        usage_dict = usage.model_dump() if hasattr(usage, "model_dump") else (usage if isinstance(usage, dict) else None)
        prompt_tokens = int((usage_dict or {}).get("prompt_token_count") or 0)
        cand_tokens = int((usage_dict or {}).get("candidates_token_count") or 0)
        img_prompt, img_cand = extract_image_modality_tokens(usage_dict)
        est = estimate_cost_usd(
            getattr(cfg, "pricing_usd_per_million_tokens", {}) or {},
            model,
            prompt_tokens,
            cand_tokens,
            image_prompt_tokens=img_prompt,
            image_candidates_tokens=img_cand,
        )
        cost_f = float(est) if est else 0.0
        meta = {
            "usage_metadata": usage_dict,
            "response_id": str(getattr(resp, "response_id", "") or ""),
            "model_version": str(getattr(resp, "model_version", "") or ""),
            "cost_str": est or f"{cost_f:.6f}",
            "model": model,
        }
        return title, cost_f, "", meta
    except Exception as e:
        return "", 0.0, str(e), empty_meta
