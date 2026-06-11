"""Auto-fill review fields: category from Stock.xlsx, title from vision model."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.image_resolve import find_local_image
from src.media_workspace import SkuMediaIndex
from src.review_store import ReviewStore
from src.title_generator import generate_title_from_image
from src.title_prompts import normalize_product_category
from src.title_store import TitleStore
from src.xlsx_ingest import index_by_sku, iter_rows


def stock_row_for_sku(xlsx_path: Path, sku: str) -> dict[str, Any]:
    rows = index_by_sku(iter_rows(xlsx_path, ["Total"]), sku_column="SKU")
    row = rows.get(sku)
    return dict(row.values) if row else {}


def title_image_for_sku(
    *,
    outputs_dir: Path,
    images_dir: Path,
    sku: str,
    media_idx: SkuMediaIndex | None = None,
) -> Path | None:
    """Same image priority as generate_missing_titles / review: prompt2, prompt1, raw, DAIJE."""
    if media_idx is not None:
        if media_idx.latest_prompt2 and media_idx.latest_prompt2.is_file():
            return media_idx.latest_prompt2
        if media_idx.latest_prompt1 and media_idx.latest_prompt1.is_file():
            return media_idx.latest_prompt1
        if media_idx.raw_images:
            first = media_idx.raw_images[0]
            if first.is_file():
                return first

    generated = outputs_dir / sku / "prompt2_v1.jpg"
    if generated.is_file():
        return generated
    dslr = find_local_image(images_dir, sku, "")
    return dslr if dslr and dslr.is_file() else None


def _title_from_stores(
    *,
    sku: str,
    review_store: ReviewStore,
    title_store: TitleStore | None,
    shop_prod: dict | None,
) -> str:
    rec = review_store.get_record(sku)
    title = str(rec.get("title") or "").strip()
    if title:
        return title
    if title_store is not None:
        tr = title_store.get(sku)
        title = str(tr.get("new_title") or tr.get("generated_title") or "").strip()
        if title:
            return title
    if shop_prod:
        return str(shop_prod.get("title") or "").strip()
    return ""


def autofill_review_record(
    cfg,
    *,
    sku: str,
    review_store: ReviewStore,
    media_idx: SkuMediaIndex | None = None,
    shop_prod: dict | None = None,
    title_store: TitleStore | None = None,
    stock_path: Path | None = None,
    model: str = "models/gemini-2.5-flash",
) -> dict[str, Any]:
    """
    Fill missing category from Stock.xlsx and missing title via generate_title_from_image.
    Persists to review_store (and title_store when a title is generated).
    """
    stock_path = stock_path or cfg.xlsx_path
    outputs_dir = cfg.outputs_dir
    images_dir = getattr(cfg, "images_dir", None) or getattr(getattr(cfg, "base", None), "images_dir", Path("DAIJE"))
    cache_dir = getattr(cfg, "download_cache_dir", None) or getattr(getattr(cfg, "base", None), "download_cache_dir", outputs_dir / "_download_cache")

    rec = review_store.get_record(sku)
    stock_row = stock_row_for_sku(stock_path, sku)
    updates: dict[str, Any] = {}
    messages: list[str] = []

    category = str(rec.get("category") or rec.get("product_type") or "").strip()
    if not category:
        category = str(stock_row.get("category") or "").strip()
        if not category and shop_prod:
            category = str(shop_prod.get("product_type") or shop_prod.get("category") or "").strip()
        if category:
            updates["category"] = category
            updates["product_type"] = category
            messages.append("category from stock sheet")

    title = _title_from_stores(
        sku=sku,
        review_store=review_store,
        title_store=title_store,
        shop_prod=shop_prod,
    )
    if title and not str(rec.get("title") or "").strip():
        updates["title"] = title
        messages.append("title from title_gen_state")
    elif not title:
        image_path = title_image_for_sku(
            outputs_dir=outputs_dir,
            images_dir=images_dir,
            sku=sku,
            media_idx=media_idx,
        )
        if image_path is None:
            messages.append("title missing (no image for generation)")
        else:
            category_key = normalize_product_category(
                category=category or str(stock_row.get("category") or ""),
                product_type=str(rec.get("product_type") or category or ""),
                title="",
            )
            gen_title, cost, err, meta = generate_title_from_image(
                cfg.base if hasattr(cfg, "base") else cfg,
                image_path=image_path,
                category_key=category_key,
                cache_dir=cache_dir,
                sku=sku,
                model=model,
            )
            if err or not gen_title:
                messages.append(f"title generation failed: {err or 'empty'}")
            else:
                title = gen_title
                updates["title"] = title
                messages.append("title generated from image")
                if title_store is not None:
                    title_store.update(
                        sku,
                        sku=sku,
                        generated_title=title,
                        new_title=title,
                        cost_usd=f"{cost:.6f}",
                        status="generated",
                        model=str(meta.get("model") or model),
                    )

    if updates:
        review_store.update(sku, **updates)

    return {
        "updated": bool(updates),
        "title": title or str(review_store.get_record(sku).get("title") or "").strip(),
        "category": category or str(review_store.get_record(sku).get("category") or "").strip(),
        "messages": messages,
    }
