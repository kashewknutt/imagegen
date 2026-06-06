from __future__ import annotations

import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import streamlit as st
from PIL import Image

from src.config import load_config
from src.genai_client import GenAiImageClient
from src.pipeline import (
    PROMPT_1,
    PROMPT_2,
    generate_pair,
    generate_single_replacement,
    write_missing_report,
    approve_many,
    skip,
    load_entries_and_state,
    prepare_work_item_for_path,
    prepare_work_item_for_paths,
    prepare_work_item_from_url,
)
from src.folder_ingest import iter_groups
from src.name_group import base_key_from_path
from src.cost_log import append_cost_row, make_generate_row, estimate_cost_usd, extract_image_modality_tokens
from src.xlsx_ingest import iter_rows as xlsx_iter_rows, index_by_sku as xlsx_index_by_sku, list_sheets as xlsx_list_sheets

from src.image_resolve import SUPPORTED_EXTS
from src.lease import try_acquire_lease, list_active_leases
from src.upload_store import UploadStore
from src.shopify_client import ShopifyClient, ShopifyConfig
from src.shopify_token_cache import load_cached_token, save_cached_token, CachedToken
from src.shopify_settings import load_shopify_settings, save_shopify_settings, ShopifySettings
from src.drive_client import ensure_client_secret_saved, get_drive_service, list_videos_for_sku, download_file_to_cache
from src.title_prompts import TITLE_CATEGORIES
from src.title_generator import fetch_products_for_quotas, generate_title_from_image

log = logging.getLogger(__name__)


def _usage_to_dict(usage) -> dict | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    # google-genai types are often pydantic models
    if hasattr(usage, "model_dump"):
        try:
            return usage.model_dump()
        except Exception:
            return None
    if hasattr(usage, "to_dict"):
        try:
            return usage.to_dict()
        except Exception:
            return None
    return None


def _log_ai_cost(
    *,
    cfg,
    sku: str,
    prompt_id: str,
    model: str,
    resp,
    status: str = "success",
    error: str = "",
) -> None:
    try:
        usage = _usage_to_dict(getattr(resp, "usage_metadata", None))
        response_id = str(getattr(resp, "response_id", "") or "")
        model_version = str(getattr(resp, "model_version", "") or "")
        prompt_tokens = int((usage or {}).get("prompt_token_count") or 0) if isinstance(usage, dict) else 0
        cand_tokens = int((usage or {}).get("candidates_token_count") or 0) if isinstance(usage, dict) else 0
        img_prompt, img_cand = extract_image_modality_tokens(usage if isinstance(usage, dict) else None)
        est = estimate_cost_usd(
            getattr(cfg, "pricing_usd_per_million_tokens", {}) or {},
            model,
            prompt_tokens,
            cand_tokens,
            image_prompt_tokens=img_prompt,
            image_candidates_tokens=img_cand,
        )
        append_cost_row(
            cfg.cost_log_csv,
            make_generate_row(
                key=sku,
                ref_tag="",
                prompt_id=prompt_id,
                attempt=0,
                model=model,
                mode="devapi",
                status=status,
                action="ai_meta",
                usage_metadata=usage if isinstance(usage, dict) else None,
                response_id=response_id,
                model_version=model_version,
                estimated_cost_usd=est,
                error=error,
            ),
        )
    except Exception:
        pass


def _load_image(path: Path) -> Image.Image:
    return Image.open(path).convert("RGB")


def _safe_open_image(path: Path) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


@contextmanager
def _bordered_container():
    try:
        with st.container(border=True):
            yield
    except TypeError:
        with st.container():
            yield


def _strip_html(html: str, max_len: int = 500) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _render_shopify_connection_sidebar(cfg) -> None:
    """Shared Shopify connection controls for Upload and Shopify Review tabs."""
    with st.sidebar:
        st.subheader("Shopify")
        settings_path = cfg.outputs_dir / ".shopify_settings.json"
        saved = load_shopify_settings(settings_path)
        if saved:
            st.session_state.setdefault("shopify_domain", saved.shop_domain)
            st.session_state.setdefault("shopify_client_id", saved.client_id)
            st.session_state.setdefault("shopify_api_version", saved.api_version)
        sd = st.text_input("Shop domain", value=st.session_state.get("shopify_domain", ""), placeholder="yourstore.myshopify.com")
        client_id = st.text_input("Client ID", value=st.session_state.get("shopify_client_id", ""), type="password")
        client_secret = st.text_input("Client secret", value=st.session_state.get("shopify_client_secret", ""), type="password")
        tok = st.text_input(
            "Access token (optional)",
            value=st.session_state.get("shopify_token", ""),
            type="password",
            help="If empty, click 'Get access token' to request one using Client ID/secret (client credentials grant).",
        )
        api_ver = st.text_input("API version", value=st.session_state.get("shopify_api_version", "2024-01"))
        st.session_state.shopify_domain = sd
        st.session_state.shopify_client_id = client_id
        st.session_state.shopify_client_secret = client_secret
        st.session_state.shopify_token = tok
        st.session_state.shopify_api_version = api_ver

        cache_path = cfg.outputs_dir / ".shopify_token_cache.json"
        cache_key = f"{sd}|{client_id}"
        cached = load_cached_token(cache_path, cache_key) if (sd and client_id) else None
        if cached and not tok.strip():
            st.session_state.shopify_token = cached.access_token
            tok = cached.access_token
        if cached:
            st.caption(f"Cached token valid for ~{int((cached.expires_at_epoch - time.time()) // 60)} min")

        if sd.strip():
            try:
                save_shopify_settings(
                    settings_path,
                    ShopifySettings(shop_domain=sd.strip(), client_id=client_id.strip(), api_version=(api_ver.strip() or "2024-01")),
                )
            except Exception:
                pass

        if st.button("Get access token", width="stretch", disabled=not (sd and client_id and client_secret)):
            try:
                data = ShopifyClient.oauth_token_client_credentials(shop_domain=sd, client_id=client_id, client_secret=client_secret)
                access_token = str(data.get("access_token") or "")
                expires_in = int(data.get("expires_in") or 0)
                scope = str(data.get("scope") or "")
                if not access_token:
                    raise RuntimeError(f"Token response missing access_token: {data}")
                st.session_state.shopify_token = access_token
                save_cached_token(
                    cache_path,
                    cache_key,
                    CachedToken(access_token=access_token, expires_at_epoch=time.time() + float(expires_in or 0), scope=scope),
                )
                st.success("Access token acquired and cached locally.")
            except Exception as e:
                st.error("Failed to get access token.")
                st.exception(e)

        if st.button("Test Shopify connection", width="stretch", disabled=not (sd and (tok or cached))):
            try:
                token_to_use = tok.strip() if tok.strip() else (cached.access_token if cached else "")
                name = ShopifyClient(ShopifyConfig(shop_domain=sd, admin_access_token=token_to_use, api_version=api_ver)).ping()
                st.success(f"Connected: {name}")
            except Exception as e:
                st.error("Shopify connection failed.")
                st.exception(e)


def _shopify_client_from_session(cfg) -> ShopifyClient | None:
    sd = str(st.session_state.get("shopify_domain") or "").strip()
    api_ver = str(st.session_state.get("shopify_api_version") or "2024-01").strip() or "2024-01"
    tok = str(st.session_state.get("shopify_token") or "").strip()
    client_id = str(st.session_state.get("shopify_client_id") or "").strip()
    if not sd:
        return None
    if not tok and client_id:
        cache_path = cfg.outputs_dir / ".shopify_token_cache.json"
        cached = load_cached_token(cache_path, f"{sd}|{client_id}")
        if cached:
            tok = cached.access_token
    if not tok:
        return None
    return ShopifyClient(ShopifyConfig(shop_domain=sd, admin_access_token=tok, api_version=api_ver))


def _ensure_genai_client(cfg) -> GenAiImageClient:
    if "client" not in st.session_state:
        st.session_state.selected_model = "models/gemini-3.1-flash-image-preview"
        st.session_state.client = GenAiImageClient(
            st.session_state.selected_model,
            cfg.min_seconds_between_requests,
            semaphore_dir=str(cfg.outputs_dir / "_semaphore"),
            max_inflight_generations=int(getattr(cfg, "max_inflight_generations", 4) or 4),
        )
    return st.session_state.client


def _build_shopify_product_query(*, search: str, status: str, product_type: str) -> str | None:
    parts: list[str] = []
    if search.strip():
        parts.append(search.strip())
    if status and status != "ALL":
        parts.append(f"status:{status.lower()}")
    if product_type.strip():
        parts.append(f"product_type:{product_type.strip()}")
    return " ".join(parts) if parts else None


def _read_state(path: Path) -> dict:
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _list_candidates_for_key(images_dir: Path, key: str) -> list[Path]:
    if not images_dir.exists():
        return []
    out: list[Path] = []
    for p in sorted(images_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if base_key_from_path(p) == key:
            out.append(p)
    return out


def _load_json_file(path: Path) -> dict:
    try:
        import json

        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json_file(path: Path, data: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


_PROMPT_RE = re.compile(r"^prompt(?P<prompt>[12])_v(?P<ver>\d+)\.(?P<ext>png|jpe?g)$", re.IGNORECASE)


def _list_output_versions(outputs_dir: Path, sku: str) -> dict[int, dict[str, Path]]:
    """
    Returns: {version: {"p1": Path?, "p2": Path?}}
    """
    out: dict[int, dict[str, Path]] = {}
    candidates = [
        outputs_dir / sku,
        outputs_dir / sku / sku,  # legacy nested layout
        outputs_dir / f"{sku}_2",  # legacy suffixed layout
    ]
    d = next((p for p in candidates if p.exists() and p.is_dir()), None)
    if d is None:
        return out
    for p in d.iterdir():
        if not p.is_file():
            continue
        m = _PROMPT_RE.match(p.name)
        if not m:
            continue
        ver = int(m.group("ver"))
        slot = "p1" if m.group("prompt") == "1" else "p2"
        out.setdefault(ver, {})[slot] = p
    return out


def _normalize_category(value: str) -> str:
    v = (value or "").strip()
    if v.lower() == "pandent":
        return "pendant"
    return v


def _parse_float(value: object) -> float | None:
    try:
        s = str(value).strip().replace(",", "")
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _gid_to_int(gid: str) -> int | None:
    # gid://shopify/Variant/1234567890 -> 1234567890
    try:
        s = str(gid or "").strip()
        if not s:
            return None
        return int(s.rsplit("/", 1)[-1])
    except Exception:
        return None


_SHOPIFY_PRODUCT_TYPES = [
    "Anklets",
    "Watch Accessories",
    "Watches",
    "Smart Watches",
    "Body Jewelry",
    "Bracelets",
    "Brooches & Lapel Pins",
    "Charms & Pendants",
    "Earrings",
    "Jewelry Sets",
    "Necklaces",
    "Rings",
]


def _map_to_shopify_product_type(category: str) -> str:
    c = (category or "").strip().lower()
    mapping = {
        "anklets": "Anklets",
        "watch accessories": "Watch Accessories",
        "watches": "Watches",
        "smart watches": "Smart Watches",
        "body jewelry": "Body Jewelry",
        "bracelets": "Bracelets",
        "bracelet": "Bracelets",
        "brooches": "Brooches & Lapel Pins",
        "brooches & lapel pins": "Brooches & Lapel Pins",
        "charms": "Charms & Pendants",
        "pendant": "Charms & Pendants",
        "pendants": "Charms & Pendants",
        "charms & pendants": "Charms & Pendants",
        "earring": "Earrings",
        "earrings": "Earrings",
        "jewelry sets": "Jewelry Sets",
        "necklace": "Necklaces",
        "necklaces": "Necklaces",
        "ring": "Rings",
        "rings": "Rings",
        "legwear": "Anklets",
        "chain": "Necklaces",
    }
    mapped = mapping.get(c) or category.strip().title()
    return mapped if mapped in _SHOPIFY_PRODUCT_TYPES else ""


def _collection_title(value: str) -> str:
    v = _normalize_category(value)
    return v.strip().title()


def _ai_suggest_tags(*, category: str, subcategory: str, metal_type: str, metal_color: str, made_for: str) -> list[str]:
    """
    Optional helper to propose human-friendly merchandising tags like:
      wedding, everyday, party, office, gifting, festive, etc.
    Uses Gemini Developer API key if present; otherwise returns [].
    """
    api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return []
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        prompt = (
            "You are helping tag an Indian jewellery product for Shopify.\n"
            "Return ONLY a comma-separated list of 5 to 10 short, human-friendly tags.\n"
            "No hashtags, no SKU, no metal names unless important.\n"
            "Prefer intents/occasions/styles like: wedding, everyday, office, party, festive, gifting, minimal, statement, traditional, modern.\n\n"
            f"Category: {category}\n"
            f"SubCategory: {subcategory}\n"
            f"Metal Type: {metal_type}\n"
            f"Metal Color: {metal_color}\n"
            f"Made For: {made_for}\n"
        )
        resp = client.models.generate_content(model="models/gemini-2.5-flash", contents=prompt)
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            # Try canonical candidate parts
            try:
                cand = (resp.candidates or [])[0]
                parts = (cand.content.parts or [])
                text = " ".join([p.text for p in parts if getattr(p, "text", None)]).strip()
            except Exception:
                text = ""
        tags = [t.strip() for t in text.split(",") if t.strip()]
        # De-dupe while preserving order
        out: list[str] = []
        seen = set()
        for t in tags:
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out[:10]
    except Exception:
        return []


def _ai_generate_title_description(
    cfg,
    *,
    category: str,
    subcategory: str,
    metal_type: str,
    metal_color: str,
    made_for: str,
    price: str,
    sku: str,
) -> tuple[str, str]:
    api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return "", ""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        model = "models/gemini-2.5-flash"
        prompt = (
            "Generate a Shopify product Title and Description for an Indian jewellery product.\n"
            "Rules:\n"
            "- Title must start with 'ZOCI'.\n"
            "- Do NOT include SKU in the title.\n"
            "- Use an Amazon-like concise title including jewellery type and key attributes.\n"
            "- Description: exactly ONE plain paragraph (3–4 lines). No bullet points. No headings. No markdown.\n"
            "- Do not invent gemstones or materials beyond what is provided.\n"
            "- Output format EXACTLY:\n"
            "TITLE: ...\n"
            "DESCRIPTION: ...\n"
            f"SKU: {sku}\n"
            f"Category: {category}\n"
            f"SubCategory: {subcategory}\n"
            f"Metal Type: {metal_type}\n"
            f"Metal Color: {metal_color}\n"
            f"Made For: {made_for}\n"
            f"Price: {price}\n"
        )
        resp = client.models.generate_content(model=model, contents=prompt)
        _log_ai_cost(cfg=cfg, sku=sku, prompt_id="title_desc", model=model, resp=resp)
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            try:
                cand = (resp.candidates or [])[0]
                parts = (cand.content.parts or [])
                text = "\n".join([p.text for p in parts if getattr(p, "text", None)]).strip()
            except Exception:
                text = ""
        if not text:
            return "", ""
        title = ""
        desc = ""
        if "TITLE:" in text:
            lines = [l.rstrip() for l in text.splitlines()]
            for i, l in enumerate(lines):
                if l.startswith("TITLE:"):
                    title = l.split("TITLE:", 1)[1].strip()
                if l.startswith("DESCRIPTION:"):
                    desc = l.split("DESCRIPTION:", 1)[1].strip()
                    # If model wrapped onto next lines, join remaining lines as continuation.
                    tail = "\n".join(lines[i + 1 :]).strip()
                    if tail:
                        desc = (desc + " " + tail).strip()
                    break
        return title, desc
    except Exception:
        try:
            _log_ai_cost(cfg=cfg, sku=sku, prompt_id="title_desc", model="models/gemini-2.5-flash", resp=type("R", (), {"usage_metadata": None})(), status="error", error="ai_generate_failed")
        except Exception:
            pass
        return "", ""


def _ai_classify_shopify_type(
    cfg,
    *,
    sku: str,
    category: str,
    subcategory: str,
    metal_type: str,
    metal_color: str,
    made_for: str,
) -> str:
    api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return ""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        model = "models/gemini-2.5-flash"
        labels = ", ".join(_SHOPIFY_PRODUCT_TYPES)
        prompt = (
            "Classify this jewellery product into EXACTLY ONE of the following Shopify product type labels:\n"
            f"{labels}\n\n"
            "Rules:\n"
            "- Output ONLY the label text, nothing else.\n"
            "- Choose the closest match.\n\n"
            f"Category: {category}\n"
            f"SubCategory: {subcategory}\n"
            f"Metal Type: {metal_type}\n"
            f"Metal Color: {metal_color}\n"
            f"Made For: {made_for}\n"
        )
        resp = client.models.generate_content(model=model, contents=prompt)
        _log_ai_cost(cfg=cfg, sku=sku, prompt_id="classify_type", model=model, resp=resp)
        label = (getattr(resp, "text", None) or "").strip()
        if label not in _SHOPIFY_PRODUCT_TYPES:
            # Try to salvage by exact match ignoring case.
            for x in _SHOPIFY_PRODUCT_TYPES:
                if x.lower() == label.lower():
                    return x
            return ""
        return label
    except Exception:
        return ""


def _ai_choose_taxonomy_category_gid(
    cfg,
    *,
    sku: str,
    candidates: list[dict[str, str]],
    category: str,
    subcategory: str,
    metal_type: str,
    metal_color: str,
    made_for: str,
) -> str:
    """
    Given Shopify taxonomy candidates (from Admin GraphQL), ask AI to pick the best category GID.
    Returns "" if not confident / no API key.
    """
    if not candidates:
        return ""
    api_key = (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return ""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        lines = []
        for i, c in enumerate(candidates[:20], start=1):
            cid = str(c.get("id") or "")
            name = str(c.get("fullName") or c.get("name") or "")
            lines.append(f"{i}. {cid} | {name}")
        prompt = (
            "Pick the best matching Shopify taxonomy category for this jewellery product.\n"
            "Rules:\n"
            "- Output ONLY the selected category id (the gid://... value) from the list.\n"
            "- Prefer Jewelry-related categories.\n\n"
            f"Product context:\n"
            f"- Category: {category}\n"
            f"- SubCategory: {subcategory}\n"
            f"- Metal Type: {metal_type}\n"
            f"- Metal Color: {metal_color}\n"
            f"- Made For: {made_for}\n\n"
            "Candidates:\n"
            + "\n".join(lines)
        )
        model = "models/gemini-2.5-flash"
        resp = client.models.generate_content(model=model, contents=prompt)
        _log_ai_cost(cfg=cfg, sku=sku, prompt_id="choose_taxonomy", model=model, resp=resp)
        text = (getattr(resp, "text", None) or "").strip()
        ids = {str(c.get("id") or "") for c in candidates}
        return text if text in ids else ""
    except Exception:
        return ""


def _shopify_metafield_value_for(def_name: str, *, product_type: str, subcategory: str, metal_type: str, metal_color: str, made_for: str) -> str | None:
    """
    Maps Shopify standard category metafield display names to values from our XLSX context.
    Only returns values for fields we can populate with reasonable confidence.
    """
    n = (def_name or "").strip().lower()
    if n == "color":
        return metal_color or None
    if n == "jewelry material":
        return metal_type or None
    if n == "target gender":
        mf = (made_for or "").strip().lower()
        if mf in {"women", "woman", "female", "f"}:
            return "female"
        if mf in {"men", "man", "male", "m"}:
            return "male"
        if mf:
            return mf
        return None
    if n == "age group":
        return "adult"
    if n == "jewelry type":
        return product_type or None
    if n in {"bracelet design", "necklace design"}:
        return subcategory or None
    # Not confidently mappable without more data
    # - Jewelry type (handled above)
    return None


def _render_gallery(cfg) -> None:
    st.subheader("Gallery (Approved)")
    from src.state_store import StateStore

    store = StateStore(cfg.state_path)
    state = _read_state(cfg.state_path)
    skus = state.get("skus") or {}
    rows = [v for v in skus.values() if isinstance(v, dict) and v.get("status") == "approved"]
    if not rows:
        st.info("No approved entries yet.")
        return

    rows.sort(key=lambda r: str(r.get("sku") or ""))
    per_page = st.selectbox("Items per page", [5, 10, 20], index=1, key="gallery_per_page")
    max_page = max(1, (len(rows) + per_page - 1) // per_page)
    page = st.number_input("Page", min_value=1, max_value=max_page, value=1, key="gallery_page")
    start = (page - 1) * per_page
    page_rows = rows[start : start + per_page]

    for rec in page_rows:
        sku = str(rec.get("sku") or "")
        version = int(rec.get("approved_version") or 0)
        refs = rec.get("selected_reference_paths") or []
        if not isinstance(refs, list) or not refs:
            st.markdown(f"### `{sku}` (v{version})")
            st.warning("No reference paths recorded.")
            continue

        with _bordered_container():
            header_cols = st.columns([3, 1.2, 1.2])
            with header_cols[0]:
                st.markdown(f"### `{sku}` (v{version})")
            with header_cols[1]:
                if st.button(
                    f"Open",
                    key=f"gallery_open::{sku}",
                    width="stretch",
                    type="primary",
                ):
                    st.session_state.pending_nav = "Generate"
                    st.session_state.override_key = sku
                    st.session_state.generated = {}
                    st.session_state.key = ""
                    st.rerun()
            with header_cols[2]:
                if st.button(
                    f"Needs Rework",
                    key=f"gallery_rework::{sku}",
                    width="stretch",
                ):
                    st_rec = store.get(sku)
                    store.update(
                        sku,
                        status="pending",
                        attempts=int(st_rec.attempts or 0) + 1,
                        last_temp_p1="",
                        last_temp_p2="",
                        last_error="",
                    )
                    st.session_state.pending_nav = "Generate"
                    st.session_state.override_key = sku
                    st.session_state.generated = {}
                    st.session_state.key = ""
                    st.rerun()
            for ref_path_str in refs:
                ref_path = Path(str(ref_path_str))
                out_dir = cfg.outputs_dir / sku
                p1 = out_dir / f"prompt1_v{version}.{cfg.output_format}"
                p2 = out_dir / f"prompt2_v{version}.{cfg.output_format}"

                c1, c2, c3 = st.columns(3)
                with c1:
                    st.caption(f"Original: {ref_path.name}")
                    img = _safe_open_image(ref_path)
                    if img is None:
                        st.warning("Missing/unreadable original")
                    else:
                        st.image(img, width="stretch")
                with c2:
                    st.caption("Prompt 1")
                    if p1.exists():
                        st.image(_load_image(p1), width="stretch")
                    else:
                        st.warning("Missing prompt1 output")
                with c3:
                    st.caption("Prompt 2")
                    if p2.exists():
                        st.image(_load_image(p2), width="stretch")
                    else:
                        st.warning("Missing prompt2 output")


def _render_shopify_review(cfg) -> None:
    st.subheader("Shopify Inventory Review")
    st.caption("Browse live Shopify products, view all images per product, and delete or replace images inline.")

    _render_shopify_connection_sidebar(cfg)
    client = _shopify_client_from_session(cfg)
    if client is None:
        st.warning("Connect Shopify in the sidebar (domain + token), then return here.")
        return

    with st.sidebar:
        st.divider()
        st.subheader("Generation")
        gen_client = _ensure_genai_client(cfg)
        st.caption(f"Model: {getattr(gen_client, 'model', cfg.model)}")

    f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
    with f1:
        search = st.text_input("Search", value=st.session_state.get("shopify_review_search", ""), placeholder="title, SKU, handle...")
        st.session_state.shopify_review_search = search
    with f2:
        status = st.selectbox("Status", ["ALL", "ACTIVE", "DRAFT", "ARCHIVED"], index=0, key="shopify_review_status")
    with f3:
        product_type_filter = st.text_input("Product type", value=st.session_state.get("shopify_review_product_type", ""))
        st.session_state.shopify_review_product_type = product_type_filter
    with f4:
        page_size = st.selectbox("Per page", [5, 10, 15, 20], index=1, key="shopify_review_page_size")

    filter_key = f"{search}|{status}|{product_type_filter}|{page_size}"
    if st.session_state.get("shopify_review_filter_key") != filter_key:
        st.session_state.shopify_review_filter_key = filter_key
        st.session_state.shopify_review_cursors = [None]
        st.session_state.shopify_review_page_idx = 0

    if "shopify_review_cursors" not in st.session_state:
        st.session_state.shopify_review_cursors = [None]
    if "shopify_review_page_idx" not in st.session_state:
        st.session_state.shopify_review_page_idx = 0

    cursors: list = st.session_state.shopify_review_cursors
    page_idx = int(st.session_state.shopify_review_page_idx)
    after_cursor = cursors[page_idx] if page_idx < len(cursors) else None

    query = _build_shopify_product_query(search=search, status=status, product_type=product_type_filter)

    try:
        result = client.list_products(first=int(page_size), after=after_cursor, query=query)
    except Exception as e:
        st.error("Failed to fetch products from Shopify.")
        st.exception(e)
        return

    products = result.get("products") or []
    page_info = result.get("pageInfo") or {}
    has_next = bool(page_info.get("hasNextPage"))
    if has_next:
        end_cursor = str(page_info.get("endCursor") or "")
        if end_cursor and page_idx + 1 >= len(cursors):
            cursors.append(end_cursor)
            st.session_state.shopify_review_cursors = cursors

    nav1, nav2, nav3, nav4 = st.columns([1, 1, 1, 3])
    with nav1:
        if st.button("Refresh", width="stretch"):
            st.session_state.shopify_review_refresh = int(st.session_state.get("shopify_review_refresh", 0)) + 1
            st.rerun()
    with nav2:
        if st.button("Previous", width="stretch", disabled=page_idx <= 0):
            st.session_state.shopify_review_page_idx = max(0, page_idx - 1)
            st.rerun()
    with nav3:
        if st.button("Next", width="stretch", disabled=not has_next):
            st.session_state.shopify_review_page_idx = page_idx + 1
            st.rerun()
    with nav4:
        st.caption(f"Page {page_idx + 1} — {len(products)} product(s)")

    if not products:
        st.info("No products found for the current filters.")
        return

    st.caption(f"Showing {len(products)} product(s) on this page.")

    gen_client = _ensure_genai_client(cfg)

    for prod in products:
        product_id = str(prod.get("id") or "")
        title = str(prod.get("title") or "")
        category = str(prod.get("category") or prod.get("product_type") or "")
        description = _strip_html(str(prod.get("description_html") or ""), max_len=800)
        handle = str(prod.get("handle") or "")
        skus = prod.get("skus") or []
        media = prod.get("media") or []

        with _bordered_container():
            header = st.columns([4, 1])
            with header[0]:
                st.markdown(f"### {title}")
                sku_text = ", ".join(skus) if skus else "—"
                st.caption(f"Category: `{category}` | Handle: `{handle}` | SKU: `{sku_text}` | Status: `{prod.get('status', '')}`")
            with header[1]:
                st.caption(f"{len(media)} image(s)")

            if description:
                st.markdown(description)
            else:
                st.caption("No description.")

            if not media:
                st.warning("No images on this product.")
                continue

            for idx, m in enumerate(media):
                media_id = str(m.get("id") or "")
                media_url = str(m.get("url") or "")
                media_alt = str(m.get("alt") or "")
                if not media_id:
                    continue

                repl_key = f"review_replacement::{product_id}::{media_id}"
                action_key = f"{product_id}::{media_id}"

                st.markdown("---")
                img_col, act_col = st.columns([1, 2])
                with img_col:
                    st.caption(f"Image {idx + 1}" + (f" — {media_alt}" if media_alt else ""))
                    if media_url:
                        st.image(media_url, width=220)
                    else:
                        st.warning("Image URL unavailable")

                with act_col:
                    style = st.radio(
                        "Replacement style",
                        ["product", "lifestyle"],
                        horizontal=True,
                        key=f"style::{action_key}",
                        format_func=lambda x: "Product shot" if x == "product" else "Lifestyle",
                    )
                    prompt_state_key = f"prompt::{action_key}"
                    style_cache_key = f"prompt_style_cache::{action_key}"
                    default_prompt = PROMPT_2 if style == "product" else PROMPT_1
                    if st.session_state.get(style_cache_key) != style:
                        st.session_state[prompt_state_key] = default_prompt
                        st.session_state[style_cache_key] = style
                    if prompt_state_key not in st.session_state:
                        st.session_state[prompt_state_key] = default_prompt
                    prompt_override = st.text_area(
                        "Prompt (editable for this generation only)",
                        key=prompt_state_key,
                        height=120,
                    )
                    reset_cols = st.columns([1, 3])
                    with reset_cols[0]:
                        if st.button("Reset prompt", key=f"reset_prompt::{action_key}"):
                            st.session_state[prompt_state_key] = default_prompt
                            st.rerun()
                    btn_cols = st.columns(3)
                    with btn_cols[0]:
                        if st.button("Delete", key=f"del::{action_key}", type="secondary"):
                            try:
                                with st.spinner("Deleting image..."):
                                    client.delete_product_media(product_id=product_id, media_ids=[media_id])
                                if repl_key in st.session_state:
                                    del st.session_state[repl_key]
                                st.success("Image deleted.")
                                st.rerun()
                            except Exception as e:
                                st.error("Delete failed.")
                                st.exception(e)
                    with btn_cols[1]:
                        if st.button("Generate replacement", key=f"gen::{action_key}", type="primary"):
                            if not media_url:
                                st.error("Cannot generate: image URL missing.")
                            else:
                                try:
                                    work_key = f"review_{handle or product_id}_{media_id[-8:]}"
                                    extra = f"Product title: {title}\nCategory: {category}"
                                    with st.spinner("Generating replacement image..."):
                                        work = prepare_work_item_from_url(cfg, work_key, media_url)
                                        out_path, meta = generate_single_replacement(
                                            cfg,
                                            gen_client,
                                            work,
                                            attempt=1,
                                            prompt_style=style,
                                            extra_context=extra,
                                            prompt_override=prompt_override,
                                            output_suffix=f"replace_{idx}",
                                        )
                                    st.session_state[repl_key] = {"path": str(out_path), "meta": meta, "style": style}
                                    st.success("Replacement generated. Review below, then click Replace in Shopify.")
                                    st.rerun()
                                except Exception as e:
                                    st.error("Generation failed.")
                                    st.exception(e)
                    with btn_cols[2]:
                        repl = st.session_state.get(repl_key)
                        replace_disabled = not (isinstance(repl, dict) and repl.get("path") and Path(str(repl["path"])).exists())
                        if st.button("Replace in Shopify", key=f"replace::{action_key}", disabled=replace_disabled):
                            repl_path = Path(str(repl["path"]))
                            try:
                                mime = "image/jpeg" if repl_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
                                with st.spinner("Uploading replacement and removing old image..."):
                                    client.replace_product_image(
                                        product_id=product_id,
                                        old_media_id=media_id,
                                        file_bytes=repl_path.read_bytes(),
                                        filename=repl_path.name,
                                        mime_type=mime,
                                        alt=media_alt or title,
                                    )
                                if repl_key in st.session_state:
                                    del st.session_state[repl_key]
                                st.success("Image replaced in Shopify.")
                                st.rerun()
                            except Exception as e:
                                st.error("Replace failed.")
                                st.exception(e)

                repl = st.session_state.get(repl_key)
                if isinstance(repl, dict) and repl.get("path"):
                    repl_path = Path(str(repl["path"]))
                    if repl_path.exists():
                        st.caption(f"Generated candidate ({repl.get('style', 'product')})")
                        st.image(_load_image(repl_path), width=220)


def _log_title_generation_cost(
    cfg,
    *,
    key: str,
    model: str,
    cost: float,
    status: str = "success",
    error: str = "",
) -> None:
    try:
        append_cost_row(
            cfg.cost_log_csv,
            make_generate_row(
                key=key,
                ref_tag="",
                prompt_id="vision_title",
                attempt=0,
                model=model,
                mode="devapi",
                status=status,
                action="ai_meta",
                usage_metadata=None,
                response_id="",
                model_version="",
                estimated_cost_usd=f"{cost:.6f}" if cost else "",
                error=error,
            ),
        )
    except Exception:
        pass


def _render_title_generator(cfg) -> None:
    st.subheader("Title Generator")
    st.caption("Generate image-based product titles from live Shopify inventory, review them, then bulk-update Shopify.")

    _render_shopify_connection_sidebar(cfg)
    client = _shopify_client_from_session(cfg)
    if client is None:
        st.warning("Connect Shopify in the sidebar (domain + token), then return here.")
        return

    try:
        import pandas as pd
    except Exception:
        st.error("Missing pandas dependency; reinstall requirements.")
        return

    model = "models/gemini-2.5-flash"

    with st.expander("Category quotas", expanded=True):
        quota_cols = st.columns(4)
        quotas: dict[str, int] = {}
        for i, cat in enumerate(TITLE_CATEGORIES):
            with quota_cols[i % 4]:
                quotas[cat] = int(
                    st.number_input(
                        cat.title(),
                        min_value=0,
                        max_value=50,
                        value=int(st.session_state.get(f"title_quota_{cat}", 0) or 0),
                        key=f"title_quota_{cat}",
                    )
                )

    f1, f2 = st.columns([2, 1])
    with f1:
        search = st.text_input("Shopify search filter (optional)", value=st.session_state.get("title_gen_search", ""))
        st.session_state.title_gen_search = search
    with f2:
        status = st.selectbox("Status", ["ACTIVE", "DRAFT", "ARCHIVED", "ALL"], index=0, key="title_gen_status")

    query_parts: list[str] = []
    if search.strip():
        query_parts.append(search.strip())
    if status != "ALL":
        query_parts.append(f"status:{status.lower()}")
    shopify_query = " ".join(query_parts) if query_parts else None

    total_requested = sum(quotas.values())
    action_cols = st.columns([1, 1, 2])
    with action_cols[0]:
        select_clicked = st.button("Select products", type="secondary", disabled=total_requested <= 0)
    with action_cols[1]:
        generate_clicked = st.button("Generate titles", type="primary", disabled=total_requested <= 0)
    with action_cols[2]:
        st.caption(f"Requested total: {total_requested}")

    if select_clicked:
        with st.spinner("Scanning Shopify catalog for category quotas..."):
            selected, remaining = fetch_products_for_quotas(client, quotas, query=shopify_query)
        rows = []
        for prod in selected:
            rows.append(
                {
                    "selected": True,
                    "product_id": prod.get("id", ""),
                    "sku": prod.get("sku", ""),
                    "category": prod.get("canonical_category", ""),
                    "current_title": prod.get("title", ""),
                    "generated_title": "",
                    "new_title": "",
                    "cost_usd": "",
                    "status": "selected",
                    "image_url": prod.get("primary_image_url", ""),
                    "product_type": prod.get("product_type", ""),
                }
            )
        st.session_state.title_gen_rows = rows
        st.session_state.title_gen_remaining = remaining
        st.session_state.title_gen_version = int(st.session_state.get("title_gen_version", 0)) + 1
        if not rows:
            st.warning("No matching products with images were found for the requested quotas.")
        else:
            unfilled = {k: v for k, v in (remaining or {}).items() if int(v or 0) > 0}
            if unfilled:
                st.info(f"Selected {len(rows)} products. Unfilled quotas: {unfilled}")
            else:
                st.success(f"Selected {len(rows)} products.")

    if generate_clicked:
        rows = list(st.session_state.get("title_gen_rows") or [])
        if not rows:
            with st.spinner("Selecting products and generating titles..."):
                selected, remaining = fetch_products_for_quotas(client, quotas, query=shopify_query)
                st.session_state.title_gen_remaining = remaining
                rows = [
                    {
                        "selected": True,
                        "product_id": p.get("id", ""),
                        "sku": p.get("sku", ""),
                        "category": p.get("canonical_category", ""),
                        "current_title": p.get("title", ""),
                        "generated_title": "",
                        "new_title": "",
                        "cost_usd": "",
                        "status": "selected",
                        "image_url": p.get("primary_image_url", ""),
                        "product_type": p.get("product_type", ""),
                    }
                    for p in selected
                ]
        if not rows:
            st.warning("No products available to generate titles for.")
        else:
            progress = st.progress(0.0, text="Generating titles...")
            for i, row in enumerate(rows):
                progress.progress((i + 1) / max(1, len(rows)), text=f"Generating {i + 1}/{len(rows)}...")
                image_url = str(row.get("image_url") or "")
                if not image_url:
                    row["status"] = "error: missing image"
                    continue
                title, cost, err = generate_title_from_image(
                    cfg,
                    image_url=image_url,
                    category_key=str(row.get("category") or "other"),
                    cache_dir=cfg.download_cache_dir,
                    current_title=str(row.get("current_title") or ""),
                    product_type=str(row.get("product_type") or ""),
                    sku=str(row.get("sku") or ""),
                    model=model,
                )
                if err:
                    row["status"] = f"error: {err}"
                    _log_title_generation_cost(cfg, key=str(row.get("sku") or row.get("product_id") or ""), model=model, cost=0.0, status="error", error=err)
                else:
                    row["generated_title"] = title
                    row["new_title"] = title
                    row["cost_usd"] = f"{cost:.6f}"
                    row["status"] = "generated"
                    _log_title_generation_cost(cfg, key=str(row.get("sku") or row.get("product_id") or ""), model=model, cost=cost)
            st.session_state.title_gen_rows = rows
            st.session_state.title_gen_version = int(st.session_state.get("title_gen_version", 0)) + 1
            st.success(f"Generated titles for {sum(1 for r in rows if r.get('status') == 'generated')} product(s).")

    rows = list(st.session_state.get("title_gen_rows") or [])
    if not rows:
        st.info("Set category quotas, then click Select products or Generate titles.")
        return

    remaining = st.session_state.get("title_gen_remaining") or {}
    if remaining:
        unfilled = {k: v for k, v in remaining.items() if int(v or 0) > 0}
        if unfilled:
            st.caption(f"Unfilled quotas from last selection: {unfilled}")

    df = pd.DataFrame(rows)
    display_cols = [
        "selected",
        "sku",
        "category",
        "current_title",
        "generated_title",
        "new_title",
        "cost_usd",
        "status",
        "product_id",
        "image_url",
        "product_type",
    ]
    for col in display_cols:
        if col not in df.columns:
            df[col] = ""
    df = df[display_cols]

    st.subheader("Review generated titles")
    edited = st.data_editor(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "selected": st.column_config.CheckboxColumn("Update?", default=True),
            "sku": st.column_config.TextColumn("SKU", disabled=True),
            "category": st.column_config.TextColumn("Category", disabled=True),
            "current_title": st.column_config.TextColumn("Current title", disabled=True),
            "generated_title": st.column_config.TextColumn("Generated title", disabled=True),
            "new_title": st.column_config.TextColumn("Title to apply"),
            "cost_usd": st.column_config.TextColumn("Cost (USD)", disabled=True),
            "status": st.column_config.TextColumn("Status", disabled=True),
            "product_id": None,
            "image_url": None,
            "product_type": None,
        },
        disabled=["sku", "category", "current_title", "generated_title", "cost_usd", "status"],
        key=f"title_gen_editor_{int(st.session_state.get('title_gen_version', 0))}",
    )

    total_cost = 0.0
    for val in edited.get("cost_usd", []):
        try:
            total_cost += float(val or 0.0)
        except Exception:
            pass
    st.caption(f"Total generation cost shown: ${total_cost:.4f}")

    preview_cols = st.columns(min(4, len(edited)))
    for i, (_, row) in enumerate(edited.head(4).iterrows()):
        with preview_cols[i % len(preview_cols)]:
            url = str(row.get("image_url") or "")
            if url:
                st.image(url, width=140)
            st.caption(str(row.get("sku") or ""))

    if st.button("Update selected titles in Shopify", type="primary"):
        to_update = edited[edited["selected"] == True]  # noqa: E712
        if to_update.empty:
            st.warning("No rows selected for update.")
            return

        ok = 0
        failed: list[str] = []
        success_ids: set[str] = set()
        for _, row in to_update.iterrows():
            product_id = str(row.get("product_id") or "").strip()
            new_title = str(row.get("new_title") or "").strip()
            sku = str(row.get("sku") or product_id)
            if not product_id or not new_title:
                failed.append(f"{sku}: missing product_id or title")
                continue
            try:
                with st.spinner(f"Updating {sku}..."):
                    client.product_update_title(product_id=product_id, title=new_title)
                ok += 1
                success_ids.add(product_id)
            except Exception as e:
                failed.append(f"{sku}: {e}")

        updated_rows = edited.to_dict(orient="records")
        for row in updated_rows:
            pid = str(row.get("product_id") or "")
            if pid in success_ids:
                row["current_title"] = str(row.get("new_title") or "")
                row["status"] = "updated"
        st.session_state.title_gen_rows = updated_rows
        st.session_state.title_gen_version = int(st.session_state.get("title_gen_version", 0)) + 1

        st.success(f"Updated {ok} title(s) in Shopify.")
        if failed:
            st.error("Some updates failed:")
            for msg in failed:
                st.write(f"- {msg}")


def _render_costs(cfg) -> None:
    st.subheader("Costs / Calls")
    try:
        import pandas as pd
    except Exception:
        st.error("Missing pandas dependency; reinstall requirements.")
        return

    if not cfg.cost_log_csv.exists():
        st.info(f"No cost log yet: {cfg.cost_log_csv}")
        return

    df = pd.read_csv(cfg.cost_log_csv)
    if df.empty:
        st.info("Cost log is empty.")
        return

    total = len(df)
    success = int((df.get("status") == "success").sum()) if "status" in df.columns else 0
    error = int((df.get("status") == "error").sum()) if "status" in df.columns else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows logged", total)
    c2.metric("Success", success)
    c3.metric("Errors", error)
    c4.metric("Success rate", f"{(success / max(1,total))*100:.1f}%")

    if "estimated_cost_usd" in df.columns:
        df["estimated_cost_usd_num"] = pd.to_numeric(df["estimated_cost_usd"], errors="coerce").fillna(0.0)
        st.metric("Total estimated cost (USD)", f"{float(df['estimated_cost_usd_num'].sum()):.4f}")
        nz = df[df["estimated_cost_usd_num"] > 0]
        st.metric("Average cost (non-zero rows)", f"{float(nz['estimated_cost_usd_num'].mean() or 0.0):.6f}")

        if {"action", "status"}.issubset(df.columns):
            gen = df[(df["action"] == "generate") & (df["status"] == "success")].copy()
            st.subheader("Last 10 successful calls")
            st.dataframe(gen.tail(10), width="stretch")

        if "ts_utc" in df.columns:
            df["ts"] = pd.to_datetime(df["ts_utc"], errors="coerce")
            st.subheader("Cost over time")
            st.line_chart(df.sort_values("ts").set_index("ts")["estimated_cost_usd_num"], height=220)

        if "model" in df.columns:
            st.subheader("Cost by model")
            by_model = df.groupby("model")["estimated_cost_usd_num"].sum().sort_values(ascending=False)
            st.bar_chart(by_model, height=220)
    else:
        st.info("Cost estimates not enabled. Fill `pricing_usd_per_million_tokens` in config.yaml to estimate USD costs.")

    st.subheader("Raw log")
    st.dataframe(df.tail(200), width="stretch")


def _render_upload(cfg) -> None:
    st.subheader("Upload to Shopify (Queue)")

    _render_shopify_connection_sidebar(cfg)

    with st.sidebar:
        st.divider()
        st.subheader("Google Drive (videos)")
        gdrive_folder_id = st.text_input("Drive folder ID", value=st.session_state.get("gdrive_folder_id", ""))
        st.session_state.gdrive_folder_id = gdrive_folder_id
        client_secret_path = cfg.outputs_dir / ".gdrive_client_secret.json"
        token_path = cfg.outputs_dir / ".gdrive_token.json"
        uploaded = st.file_uploader("OAuth client JSON", type=["json"], key="gdrive_client_json")
        if uploaded is not None:
            try:
                ensure_client_secret_saved(dest_path=client_secret_path, uploaded_bytes=uploaded.getvalue())
                st.success("Saved OAuth client JSON.")
            except Exception as e:
                st.error("Failed to save client JSON.")
                st.exception(e)
        gdrive_ready = client_secret_path.exists()
        if st.button("Connect Google Drive", width="stretch", disabled=not gdrive_ready):
            try:
                _ = get_drive_service(client_secret_path=client_secret_path, token_path=token_path)
                st.success("Google Drive connected (token saved).")
            except Exception as e:
                st.error("Google Drive connection failed.")
                st.exception(e)

    # Upload uses ONLY the Total sheet as source-of-truth.
    all_sheets = ["Total"]
    rows = xlsx_iter_rows(cfg.xlsx_path, all_sheets)
    sku_map = xlsx_index_by_sku(rows, sku_column="SKU")

    # Eligible SKUs are those with prompt2_vN present in outputs.
    eligible: list[str] = []
    sku_versions: dict[str, dict[int, dict[str, Path]]] = {}
    for sku in sku_map.keys():
        versions = _list_output_versions(cfg.outputs_dir, sku)
        if not versions:
            continue
        has_p2 = any("p2" in v for v in versions.values())
        if not has_p2:
            continue
        eligible.append(sku)
        sku_versions[sku] = versions

    eligible.sort()
    store = UploadStore(cfg.outputs_dir / "upload_state.json")
    store.ensure_skus(eligible)

    with st.expander("Upload debug", expanded=False):
        st.write({"xlsx_sheets_found": len(all_sheets), "xlsx_rows": len(rows), "sku_map": len(sku_map), "eligible_with_outputs": len(eligible)})
        leases_dir = cfg.outputs_dir / "_leases"
        try:
            st.write({"leases_dir": str(leases_dir), "leases_files": len(list(leases_dir.glob('*.json')) if leases_dir.exists() else [])})
        except Exception:
            pass
        st.write("Eligible sample:", eligible[:10])

    # Select an actionable SKU (pending preferred), respecting the global lease cap.
    if "session_id" not in st.session_state:
        st.session_state.session_id = uuid.uuid4().hex
    leases_dir = cfg.outputs_dir / "_leases"
    max_parallel = int(getattr(cfg, "max_parallel_sessions", 4) or 4)
    lease_ttl = int(getattr(cfg, "lease_ttl_seconds", 3600) or 3600)

    override = st.session_state.get("upload_override_sku")
    sku_to_open: str | None = None
    if override and override in eligible:
        lease = try_acquire_lease(leases_dir, override, str(st.session_state.session_id), ttl_seconds=lease_ttl, max_concurrent=max_parallel)
        if lease:
            st.session_state.lease_key = override
            sku_to_open = override
    else:
        for sku in eligible:
            if store.get(sku).status != "pending":
                continue
            lease = try_acquire_lease(leases_dir, sku, str(st.session_state.session_id), ttl_seconds=lease_ttl, max_concurrent=max_parallel)
            if lease:
                st.session_state.lease_key = sku
                sku_to_open = sku
                break

    if not sku_to_open:
        st.info("No upload-eligible SKUs available (either none exist or all are leased/uploaded).")
        return

    xr = sku_map.get(sku_to_open)
    row_vals = getattr(xr, "values", {}) if xr else {}
    category = _normalize_category(str(row_vals.get("category") or ""))
    subcategory = str(row_vals.get("subCategory") or "").strip()
    metal_type = str(row_vals.get("metalType") or "").strip()
    metal_color = str(row_vals.get("metalColor") or "").strip()
    made_for = str(row_vals.get("madeFor") or "").strip()
    # NOTE (Stock.xlsx / Total):
    # - `price_2` is the selling price column.
    # - cost per item is derived from Labour + (rate * weight) (weight in grams).
    price_sell = str(row_vals.get("price_2") or "").strip()
    labour = _parse_float(row_vals.get("Labour"))
    rate = _parse_float(row_vals.get("rate"))
    weight_g = _parse_float(row_vals.get("weight"))
    qty = int(_parse_float(row_vals.get("quantity")) or 0)
    computed_cost = None
    if labour is not None and rate is not None:
        if weight_g is not None:
            computed_cost = labour + (rate * weight_g)
        else:
            computed_cost = labour + rate
    price_cost = f"{computed_cost:.2f}" if computed_cost is not None else ""
    # Fallback if price_2 missing.
    if not price_sell:
        price_sell = str(row_vals.get("price") or "").strip()

    st.markdown(f"### SKU: `{sku_to_open}`")
    st.caption(
        f"Category: `{category}` | Sub: `{subcategory}` | Metal: `{metal_type}` / `{metal_color}` | Made for: `{made_for}` | Sell: `{price_sell}` | Cost: `{price_cost}`"
    )

    with st.expander("Reference images (pics_raw)", expanded=False):
        refs = _list_candidates_for_key(cfg.images_dir, sku_to_open)
        if not refs:
            st.info("No reference images found in pics_raw for this SKU.")
        else:
            cols = st.columns(min(4, len(refs)))
            for i, p in enumerate(refs):
                with cols[i % len(cols)]:
                    img = _safe_open_image(p)
                    if img is None:
                        st.warning(p.name)
                    else:
                        st.image(img, caption=p.name, width="stretch")

    # Select additional pics_raw images to upload to Shopify.
    st.subheader("Select pics_raw to upload (optional)")
    pics = _list_candidates_for_key(cfg.images_dir, sku_to_open)
    rec = store.get_record(sku_to_open)
    pics_sel_key = f"upload_pics_raw_sel::{sku_to_open}"
    persisted_pics = rec.get("pics_raw_selected") or []
    if pics_sel_key not in st.session_state:
        if isinstance(persisted_pics, list) and persisted_pics:
            st.session_state[pics_sel_key] = [str(x) for x in persisted_pics]
        elif len(pics) == 1:
            st.session_state[pics_sel_key] = [pics[0].name]
        else:
            st.session_state[pics_sel_key] = []

    if not pics:
        st.caption("No pics_raw matches for this SKU.")
    else:
        cols = st.columns(min(4, len(pics)))
        current = set(st.session_state.get(pics_sel_key) or [])
        for i, p in enumerate(pics):
            with cols[i % len(cols)]:
                img = _safe_open_image(p)
                if img is None:
                    st.warning(p.name)
                else:
                    st.image(img, caption=p.name, width="stretch")
                checked = p.name in current
                if st.checkbox("Upload", value=checked, key=f"pick_pic::{sku_to_open}::{p.name}"):
                    current.add(p.name)
                else:
                    current.discard(p.name)
        st.session_state[pics_sel_key] = sorted(current)
        store.update(sku_to_open, pics_raw_selected=list(st.session_state[pics_sel_key]))

    # Drive videos (match by filename contains SKU)
    st.subheader("Select videos to upload (Google Drive)")
    drive_folder_id = str(st.session_state.get("gdrive_folder_id") or "").strip()
    drive_client_secret = cfg.outputs_dir / ".gdrive_client_secret.json"
    drive_token = cfg.outputs_dir / ".gdrive_token.json"
    drive_files: list = []
    drive_sel_key = f"upload_drive_videos_sel::{sku_to_open}"
    persisted_vids = rec.get("drive_video_selected") or []
    if drive_sel_key not in st.session_state:
        st.session_state[drive_sel_key] = list(persisted_vids) if isinstance(persisted_vids, list) else []
    if not (drive_folder_id and drive_client_secret.exists()):
        st.caption("Drive not configured (set Folder ID and connect in sidebar).")
    else:
        try:
            svc = get_drive_service(client_secret_path=drive_client_secret, token_path=drive_token)
            drive_files = list_videos_for_sku(service=svc, folder_id=drive_folder_id, sku=sku_to_open)
        except Exception as e:
            st.warning("Drive lookup failed.")
            st.exception(e)
            drive_files = []
        if not drive_files:
            st.caption("No matching Drive videos found for this SKU.")
        elif len(drive_files) == 1 and not st.session_state[drive_sel_key]:
            st.session_state[drive_sel_key] = [drive_files[0].id]
        else:
            for f in drive_files:
                col1, col2, col3 = st.columns([1, 3, 1])
                with col1:
                    st.checkbox("Upload video", value=(f.id in set(st.session_state[drive_sel_key] or [])), key=f"pick_vid::{sku_to_open}::{f.id}")
                with col2:
                    st.write(f"{f.name} ({f.size/1_000_000:.1f} MB)")
                with col3:
                    if st.button("Preview", key=f"preview_vid::{sku_to_open}::{f.id}", width="stretch"):
                        try:
                            cache = cfg.outputs_dir / "_gdrive_cache" / f"{f.id}.bin"
                            local_path = download_file_to_cache(service=svc, file_id=f.id, cache_path=cache)
                            st.video(local_path.read_bytes())
                        except Exception as e:
                            st.error("Preview failed.")
                            st.exception(e)
            selected = []
            for f in drive_files:
                if st.session_state.get(f"pick_vid::{sku_to_open}::{f.id}"):
                    selected.append(f.id)
            st.session_state[drive_sel_key] = selected
        store.update(sku_to_open, drive_video_selected=list(st.session_state[drive_sel_key]))

    versions = sku_versions.get(sku_to_open) or {}
    available_vers = sorted(versions.keys(), reverse=True)
    default_ver = available_vers[0] if available_vers else 1
    chosen_ver = st.selectbox("Select image version to upload", options=available_vers, index=0)

    p1 = versions.get(int(chosen_ver), {}).get("p1")
    p2 = versions.get(int(chosen_ver), {}).get("p2")
    c1, c2 = st.columns(2)
    with c1:
        st.caption("Prompt 2 (product photo)")
        if p2 and p2.exists():
            st.image(_load_image(p2), width="stretch")
        else:
            st.warning("No prompt2 image for this version.")
    with c2:
        st.caption("Prompt 1 (lifestyle try-on)")
        if p1 and p1.exists():
            st.image(_load_image(p1), width="stretch")
        else:
            st.warning("No prompt1 image for this version.")

    with st.expander("All generated outputs (outputs/{SKU})", expanded=False):
        out_dir = cfg.outputs_dir / sku_to_open
        if not out_dir.exists():
            st.info("No outputs directory for this SKU.")
        else:
            files = [p for p in sorted(out_dir.iterdir()) if p.is_file()]
            if not files:
                st.info("No files in outputs directory for this SKU.")
            else:
                cols = st.columns(min(4, len(files)))
                for i, fp in enumerate(files):
                    with cols[i % len(cols)]:
                        if fp.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                            img = _safe_open_image(fp)
                            if img is None:
                                st.warning(fp.name)
                            else:
                                st.image(img, caption=fp.name, width="stretch")
                        else:
                            st.write(fp.name)

    st.divider()
    st.subheader("If images aren’t good enough")
    if st.button("Regenerate images for this SKU", type="primary", width="stretch"):
        st.session_state.pending_nav = "Generate"
        st.session_state.override_key = sku_to_open
        st.session_state.generated = {}
        st.session_state.key = ""
        st.rerun()

    st.divider()
    st.subheader("Shopify upload (scaffold)")
    title_key = f"upload_title::{sku_to_open}"
    desc_key = f"upload_desc::{sku_to_open}"
    tags_key = f"upload_tags::{sku_to_open}"
    ai_done_key = f"upload_ai_done::{sku_to_open}"
    force_ai_key = f"upload_force_ai::{sku_to_open}"

    # Let the user force-refresh AI fields; do it via a rerun so we update Session State
    # BEFORE widgets are instantiated.
    if st.button("Regenerate title + description (AI)", width="stretch"):
        st.session_state[force_ai_key] = True
        st.rerun()

    # Auto-generate title/description (and default tags) on page load once per SKU.
    # This must run before creating widgets with these keys.
    need_ai = bool(st.session_state.get(force_ai_key)) or (not bool(st.session_state.get(ai_done_key)))
    if need_ai and (not str(st.session_state.get(title_key) or "").strip() or not str(st.session_state.get(desc_key) or "").strip()):
        t, d = _ai_generate_title_description(
            cfg,
            category=_collection_title(category),
            subcategory=subcategory,
            metal_type=metal_type,
            metal_color=metal_color,
            made_for=made_for,
            price=price_sell,
            sku=sku_to_open,
        )
        if t:
            st.session_state[title_key] = t
        if d:
            st.session_state[desc_key] = d
        # Default tags: include subCategory if present.
        if tags_key not in st.session_state:
            st.session_state[tags_key] = (subcategory.title() if subcategory else "")
        st.session_state[ai_done_key] = True
        st.session_state[force_ai_key] = False
    else:
        st.session_state.setdefault(title_key, "")
        st.session_state.setdefault(desc_key, "")
        if tags_key not in st.session_state:
            st.session_state[tags_key] = (subcategory.title() if subcategory else "")
        st.session_state.setdefault(ai_done_key, False)
        st.session_state.setdefault(force_ai_key, False)

    title = st.text_input("Title", key=title_key, placeholder="e.g. ZOCI Sterling Silver Bolo Bracelet for Women")
    desc = st.text_area("Description (HTML allowed)", key=desc_key, height=120)
    tags = st.text_input("Tags (comma-separated)", key=tags_key, placeholder="e.g. wedding, everyday, gifting")
    ccol1, ccol2, ccol3 = st.columns(3)
    with ccol1:
        add_to_category_collection = st.checkbox("Add to category collection", value=True)
    with ccol2:
        add_to_landing = st.checkbox("Add to landing_page", value=False)
    with ccol3:
        add_to_bestseller = st.checkbox("Add to bestseller", value=False)
    with st.container():
        if st.button("Suggest tags (AI)", width="stretch"):
            suggested = _ai_suggest_tags(
                category=_collection_title(category),
                subcategory=subcategory,
                metal_type=metal_type,
                metal_color=metal_color,
                made_for=made_for,
            )
            if suggested:
                st.session_state[tags_key] = ", ".join(suggested)
                st.rerun()
            else:
                st.warning("No API key set or tag suggestion failed.")

    # Ensure tags reflect collection choices for landing_page/bestseller smart collections.
    collection_tags: list[str] = []
    if add_to_landing:
        collection_tags.append("landing_page")
    if add_to_bestseller:
        collection_tags.append("bestseller")
    st.caption("Note: publishing to sales channels is not wired yet; products will be created as unpublished by default.")

    cache_path = cfg.outputs_dir / ".shopify_token_cache.json"
    cache_key = f"{sd}|{st.session_state.get('shopify_client_id','')}"
    cached = load_cached_token(cache_path, cache_key) if (sd and st.session_state.get("shopify_client_id")) else None
    token_to_use = tok.strip() if tok.strip() else (cached.access_token if cached else "")

    # --- Shopify taxonomy Category (UI + persisted choice) ---
    classify_key = f"shopify_type_classified::{sku_to_open}"
    if classify_key not in st.session_state:
        st.session_state[classify_key] = _ai_classify_shopify_type(
            cfg,
            sku=sku_to_open,
            category=_collection_title(category),
            subcategory=subcategory,
            metal_type=metal_type,
            metal_color=metal_color,
            made_for=made_for,
        )
    classified_label = str(st.session_state.get(classify_key) or "").strip()
    product_type = classified_label or _map_to_shopify_product_type(_collection_title(category))

    taxonomy_gid = str(store.get_record(sku_to_open).get("taxonomy_category_gid") or "").strip()
    taxonomy_candidates_key = f"taxonomy_candidates::{sku_to_open}"
    taxonomy_candidates: list[dict[str, str]] = list(st.session_state.get(taxonomy_candidates_key) or [])
    if sd and token_to_use and not taxonomy_candidates:
        try:
            tmp_client = ShopifyClient(ShopifyConfig(shop_domain=sd, admin_access_token=token_to_use, api_version=api_ver))
            taxonomy_candidates = tmp_client.taxonomy_search_categories(search=product_type or category, first=25)
            # Ask AI to choose the best candidate id from the returned list.
            chosen = _ai_choose_taxonomy_category_gid(
                cfg,
                sku=sku_to_open,
                candidates=taxonomy_candidates,
                category=_collection_title(category),
                subcategory=subcategory,
                metal_type=metal_type,
                metal_color=metal_color,
                made_for=made_for,
            )
            if chosen:
                taxonomy_gid = chosen
                store.update(sku_to_open, taxonomy_category_gid=taxonomy_gid)
            st.session_state[taxonomy_candidates_key] = taxonomy_candidates
        except Exception:
            taxonomy_candidates = []
            st.session_state[taxonomy_candidates_key] = []
    if taxonomy_candidates:
        opts = [(c.get("fullName") or c.get("name") or c.get("id") or "", c.get("id") or "") for c in taxonomy_candidates]
        labels = [o[0] for o in opts]
        ids = [o[1] for o in opts]
        default_id = taxonomy_gid if taxonomy_gid in ids else ids[0]
        choice_id = st.selectbox(
            "Shopify Category (taxonomy)",
            options=ids,
            format_func=lambda x: labels[ids.index(x)],
            index=ids.index(default_id),
            key=f"taxonomy_choice::{sku_to_open}",
        )
        taxonomy_gid = str(choice_id or "").strip()
        store.update(sku_to_open, taxonomy_category_gid=taxonomy_gid)
    else:
        st.caption("No taxonomy categories found (or not connected). Category will remain empty unless your API version supports it and search succeeds.")

    can_upload = bool(sd and token_to_use and title.strip() and p2 and p2.exists())
    if st.button("Create product + upload images", type="primary", width="stretch", disabled=not can_upload):
        try:
            client = ShopifyClient(ShopifyConfig(shop_domain=sd, admin_access_token=token_to_use, api_version=api_ver))
            with st.spinner("Creating product..."):
                prod = client.product_create(
                    title=title.strip(),
                    description_html=desc or "",
                    vendor="ZOCI",
                    product_type=product_type,
                    category_gid=taxonomy_gid or None,
                    tags=sorted(set([t.strip() for t in (tags.split(",") if tags else []) if t.strip()] + collection_tags)),
                )
            product_id = prod["id"]
            st.success(f"Created product: {prod.get('handle') or product_id}")

            # Populate inventory/cost/weight from Total (best-effort).
            variant_id_int = _gid_to_int(prod.get("variant_id") or "")
            inventory_item_id_int = _gid_to_int(prod.get("inventory_item_id") or "")
            if variant_id_int:
                with st.spinner("Setting SKU + price..."):
                    client.rest_variant_update(variant_id=variant_id_int, sku=sku_to_open, price=price_sell or None)
            if inventory_item_id_int:
                cost_f = _parse_float(price_cost)
                if cost_f is not None:
                    with st.spinner("Setting cost per item..."):
                        client.rest_inventory_item_cost(inventory_item_id=inventory_item_id_int, cost=cost_f)
            if variant_id_int:
                if weight_g is not None:
                    with st.spinner("Setting shipping weight..."):
                        client.rest_variant_weight(variant_id=variant_id_int, weight_kg=float(weight_g) / 1000.0)
            if inventory_item_id_int and qty:
                with st.spinner("Setting inventory quantity..."):
                    locations = client.rest_locations()
                    if locations:
                        location_id = int((locations[0] or {}).get("id") or 0)
                        if location_id:
                            client.rest_inventory_set(location_id=location_id, inventory_item_id=inventory_item_id_int, available=int(qty))

            # Set category metafields (standard Shopify definitions) when possible.
            if taxonomy_gid:
                try:
                    defs = client.metafield_definitions_for_category(category_gid=taxonomy_gid, first=50)
                    metas: list[dict[str, str]] = []
                    for d in defs:
                        val = _shopify_metafield_value_for(
                            d.get("name") or "",
                            product_type=product_type,
                            subcategory=subcategory,
                            metal_type=metal_type,
                            metal_color=metal_color,
                            made_for=made_for,
                        )
                        if val is None:
                            continue
                        metas.append(
                            {
                                "namespace": d["namespace"],
                                "key": d["key"],
                                "type": d["type"],
                                "value": str(val),
                            }
                        )
                    if metas:
                        with st.spinner("Setting category metafields..."):
                            client.product_update_metafields(product_id=product_id, metafields=metas)
                except Exception:
                    # Metafields are best-effort; do not fail upload if store lacks access/scopes.
                    pass

            media_urls: list[str] = []
            images_to_upload: list[tuple[Path, str]] = []
            # Ensure prompt2 first (hero), then prompt1 if present.
            if p2 and p2.exists():
                images_to_upload.append((p2, f"{sku_to_open} - Product"))
            if p1 and p1.exists():
                images_to_upload.append((p1, f"{sku_to_open} - Lifestyle"))
            # Add selected pics_raw (if any)
            selected_pics = [x for x in (st.session_state.get(pics_sel_key) or []) if isinstance(x, str)]
            for name in selected_pics:
                pp = cfg.images_dir / name
                if pp.exists():
                    images_to_upload.append((pp, f"{sku_to_open} - Reference"))

            for img_path, alt in images_to_upload:
                with st.spinner(f"Uploading {img_path.name}..."):
                    mime = "image/jpeg" if img_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
                    target = client.staged_upload_create(filename=img_path.name, mime_type=mime, resource="FILE", http_method="POST")
                    with st.expander(f"Staged target debug: {img_path.name}", expanded=False):
                        st.json(target)
                    client.upload_to_staged_target(target=target, filename=img_path.name, mime_type=mime, file_bytes=img_path.read_bytes())
                    file_id = client.file_create_from_staged(resource_url=str(target.get("resourceUrl") or target.get("url") or ""), alt=alt, content_type="IMAGE")
                    ready = client.file_poll_ready(file_id=file_id, max_tries=30, sleep_seconds=2.0)
                    cdn = str(ready.get("preview_url") or "").strip()
                    if not cdn:
                        raise RuntimeError(f"File did not become READY: {ready}")
                    media_urls.append(cdn)

            if media_urls:
                with st.spinner("Attaching media to product..."):
                    client.product_create_media(
                        product_id=product_id,
                        media=[{"mediaContentType": "IMAGE", "originalSource": u, "alt": sku_to_open} for u in media_urls],
                    )

            # Upload Drive videos (if selected)
            selected_video_ids = [x for x in (st.session_state.get(drive_sel_key) or []) if isinstance(x, str)]
            if selected_video_ids and drive_folder_id and drive_client_secret.exists():
                svc = get_drive_service(client_secret_path=drive_client_secret, token_path=drive_token)
                file_by_id = {f.id: f for f in (drive_files or [])}
                for vid in selected_video_ids:
                    f = file_by_id.get(vid)
                    if not f:
                        continue
                    cache = cfg.outputs_dir / "_gdrive_cache" / f"{f.id}.bin"
                    with st.spinner(f"Downloading video: {f.name}"):
                        local_path = download_file_to_cache(service=svc, file_id=f.id, cache_path=cache)
                    with st.spinner(f"Uploading video: {f.name}"):
                        target = client.staged_upload_create(
                            filename=f.name,
                            mime_type=f.mime_type or "video/mp4",
                            resource="VIDEO",
                            file_size=int(f.size),
                            http_method="POST",
                        )
                        client.upload_to_staged_target(
                            target=target,
                            filename=f.name,
                            mime_type=f.mime_type or "video/mp4",
                            file_bytes=local_path.read_bytes(),
                        )
                        file_id = client.file_create_from_staged(
                            resource_url=str(target.get("resourceUrl") or target.get("url") or ""),
                            alt=f"{sku_to_open} - Video",
                            content_type="VIDEO",
                        )
                        ready = client.file_poll_ready(file_id=file_id, max_tries=90, sleep_seconds=2.0)
                        # Attach to product using staged resourceUrl (video URL isn't exposed on File in some API versions).
                        resource_url = str(target.get("resourceUrl") or "").strip()
                        if not resource_url:
                            raise RuntimeError(f"Staged target missing resourceUrl for video: {target}")
                        client.product_create_media(
                            product_id=product_id,
                            media=[{"mediaContentType": "VIDEO", "originalSource": resource_url, "alt": sku_to_open}],
                        )

            # Collections (manual collections + add product).
            # Smart collections based on tags.
            # We ensure the collections exist; membership is automatic based on the product tags we set above.
            with st.spinner("Ensuring smart collections..."):
                def ensure_smart_collection_by_tag(title: str, tag: str) -> None:
                    found = client.collection_find_by_title(title=title)
                    if found and found.get("id"):
                        return
                    client.collection_create_smart_by_tag(title=title, tag=tag)

                if add_to_category_collection and category.strip():
                    # Use product type for category collection (no extra tags needed).
                    found = client.collection_find_by_title(title=_collection_title(category))
                    if not found or not found.get("id"):
                        client.collection_create_smart_by_product_type(title=_collection_title(category), product_type=_collection_title(category))
                if add_to_landing:
                    ensure_smart_collection_by_tag("landing_page", "landing_page")
                if add_to_bestseller:
                    ensure_smart_collection_by_tag("bestseller", "bestseller")

            store.update(sku_to_open, status="uploaded", product_id=product_id, handle=prod.get("handle") or "", last_error="")
            st.success("Uploaded successfully.")
            if st.session_state.get("lease_key") == sku_to_open:
                try:
                    (cfg.outputs_dir / "_leases" / f"{sku_to_open}.json").unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                st.session_state.lease_key = ""
            st.rerun()
        except Exception as e:
            store.update(sku_to_open, status="failed", last_error=str(e))
            st.error("Upload failed.")
            st.exception(e)


def _render_bulk_upload(cfg) -> None:
    st.subheader("Bulk Upload (Auto)")

    # Shopify creds/live token are stored in session_state by the Upload page sidebar.
    # Reuse the same state keys here.
    sd = str(st.session_state.get("shopify_domain") or "").strip()
    api_ver = str(st.session_state.get("shopify_api_version") or "2024-01").strip() or "2024-01"
    tok = str(st.session_state.get("shopify_token") or "").strip()
    client_id = str(st.session_state.get("shopify_client_id") or "").strip()
    cache_path = cfg.outputs_dir / ".shopify_token_cache.json"
    cache_key = f"{sd}|{client_id}"
    cached = load_cached_token(cache_path, cache_key) if (sd and client_id) else None
    token_to_use = tok if tok else (cached.access_token if cached else "")

    if not (sd and token_to_use):
        st.warning("Shopify is not configured. Open the Upload tab, connect Shopify, then return here.")
        return

    # Build eligible SKUs (Total sheet only) where BOTH prompt1+prompt2 exist.
    rows = xlsx_iter_rows(cfg.xlsx_path, ["Total"])
    sku_map = xlsx_index_by_sku(rows, sku_column="SKU")
    sku_versions: dict[str, dict[int, dict[str, Path]]] = {}
    eligible: list[str] = []
    for sku in sku_map.keys():
        versions = _list_output_versions(cfg.outputs_dir, sku)
        if not versions:
            continue
        ok = any(("p1" in v and "p2" in v) for v in versions.values())
        if not ok:
            continue
        eligible.append(sku)
        sku_versions[sku] = versions
    eligible.sort()

    store = UploadStore(cfg.outputs_dir / "upload_state.json")
    store.ensure_skus(eligible)

    uploaded = [s for s in eligible if store.get(s).status == "uploaded"]
    pending = [s for s in eligible if store.get(s).status == "pending"]
    failed = [s for s in eligible if store.get(s).status == "failed"]

    st.progress(0.0 if not eligible else (len(uploaded) / max(1, len(eligible))))
    st.caption(f"Eligible: {len(eligible)} | Uploaded: {len(uploaded)} | Pending: {len(pending)} | Failed: {len(failed)}")

    state_path = cfg.outputs_dir / "bulk_upload_state.json"
    state = _load_json_file(state_path)
    running = bool(state.get("running"))
    last_sku = str(state.get("last_sku") or "")

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Start / Resume", type="primary", width="stretch"):
            state["running"] = True
            _save_json_file(state_path, state)
            st.rerun()
    with c2:
        if st.button("Pause", width="stretch"):
            state["running"] = False
            _save_json_file(state_path, state)
            st.rerun()
    with c3:
        if st.button("Reset cursor (keep uploaded)", width="stretch"):
            state = {"running": False, "last_sku": ""}
            _save_json_file(state_path, state)
            st.rerun()

    if failed:
        if st.button("Retry failed (set back to pending)", width="stretch"):
            for s in failed:
                store.update(s, status="pending", last_error="")
            state["last_sku"] = ""
            state["running"] = False
            _save_json_file(state_path, state)
            st.success(f"Reset {len(failed)} failed SKUs to pending. Click Start / Resume.")
            st.stop()

    if not running:
        st.info("Bulk upload is paused.")
        return

    # Determine next pending SKU after cursor.
    queue = pending if not last_sku else [s for s in pending if s > last_sku]
    if not queue and pending:
        # Wraparound safety: if cursor is past end, restart from first pending.
        queue = pending
    if not queue:
        st.success("No pending SKUs left to bulk upload.")
        state["running"] = False
        _save_json_file(state_path, state)
        return

    sku = queue[0]
    xr = sku_map.get(sku)
    row_vals = getattr(xr, "values", {}) if xr else {}
    category = _normalize_category(str(row_vals.get("category") or ""))
    subcategory = str(row_vals.get("subCategory") or "").strip()
    metal_type = str(row_vals.get("metalType") or "").strip()
    metal_color = str(row_vals.get("metalColor") or "").strip()
    made_for = str(row_vals.get("madeFor") or "").strip()
    price_sell = str(row_vals.get("price_2") or "").strip() or str(row_vals.get("price") or "").strip()
    labour = _parse_float(row_vals.get("Labour"))
    rate = _parse_float(row_vals.get("rate"))
    weight_g = _parse_float(row_vals.get("weight"))
    qty = int(_parse_float(row_vals.get("quantity")) or 0)
    computed_cost = None
    if labour is not None and rate is not None:
        computed_cost = labour + ((rate * weight_g) if weight_g is not None else rate)
    price_cost = f"{computed_cost:.2f}" if computed_cost is not None else ""

    versions = sku_versions.get(sku) or {}
    ver = max(versions.keys()) if versions else 1
    p1 = versions.get(ver, {}).get("p1")
    p2 = versions.get(ver, {}).get("p2")
    if not (p1 and p2 and p1.exists() and p2.exists()):
        store.update(sku, status="failed", last_error="missing_prompt_images")
        state["last_sku"] = sku
        _save_json_file(state_path, state)
        st.warning(f"Skipping {sku}: missing prompt images for version {ver}.")
        st.rerun()
        return

    st.markdown(f"### Now uploading: `{sku}` (v{ver})")

    # Bulk upload rule: upload ALL pics_raw images for this SKU (no approval gating).
    pics = _list_candidates_for_key(cfg.images_dir, sku)
    rec = store.get_record(sku)
    selected_pics = list(pics)
    # Persist for record-keeping/resume.
    store.update(sku, pics_raw_selected=[p.name for p in selected_pics])

    # Auto title/desc/type/category (taxonomy best-effort)
    title, desc = _ai_generate_title_description(
        cfg,
        category=_collection_title(category),
        subcategory=subcategory,
        metal_type=metal_type,
        metal_color=metal_color,
        made_for=made_for,
        price=price_sell,
        sku=sku,
    )
    if not title:
        title = f"ZOCI {(_collection_title(category) or 'Jewellery')} {subcategory}".strip()
    if not desc:
        desc = f"{title} crafted in {metal_type} with a {metal_color} finish for {made_for}."

    classified_label = _ai_classify_shopify_type(
        cfg,
        sku=sku,
        category=_collection_title(category),
        subcategory=subcategory,
        metal_type=metal_type,
        metal_color=metal_color,
        made_for=made_for,
    )
    product_type = classified_label or _map_to_shopify_product_type(_collection_title(category))

    client = ShopifyClient(ShopifyConfig(shop_domain=sd, admin_access_token=token_to_use, api_version=api_ver))
    taxonomy_gid = str(rec.get("taxonomy_category_gid") or "").strip()
    if not taxonomy_gid:
        try:
            cands = client.taxonomy_search_categories(search=product_type or category, first=25)
            taxonomy_gid = _ai_choose_taxonomy_category_gid(
                cfg,
                sku=sku,
                candidates=cands,
                category=_collection_title(category),
                subcategory=subcategory,
                metal_type=metal_type,
                metal_color=metal_color,
                made_for=made_for,
            ) or (cands[0].get("id") if cands else "")
            if taxonomy_gid:
                store.update(sku, taxonomy_category_gid=taxonomy_gid)
        except Exception:
            taxonomy_gid = ""

    try:
        prod = client.product_create(
            title=title.strip(),
            description_html=desc.strip(),
            vendor="ZOCI",
            product_type=product_type,
            category_gid=taxonomy_gid or None,
            tags=sorted(set([subcategory.title()] if subcategory else [])),
        )
        product_id = prod["id"]
        variant_id_int = _gid_to_int(prod.get("variant_id") or "")
        inventory_item_id_int = _gid_to_int(prod.get("inventory_item_id") or "")
        if variant_id_int:
            client.rest_variant_update(variant_id=variant_id_int, sku=sku, price=price_sell or None)
            if weight_g is not None:
                client.rest_variant_weight(variant_id=variant_id_int, weight_kg=float(weight_g) / 1000.0)
        if inventory_item_id_int:
            cost_f = _parse_float(price_cost)
            if cost_f is not None:
                client.rest_inventory_item_cost(inventory_item_id=inventory_item_id_int, cost=cost_f)
            if qty:
                locs = client.rest_locations()
                if locs:
                    location_id = int((locs[0] or {}).get("id") or 0)
                    if location_id:
                        client.rest_inventory_set(location_id=location_id, inventory_item_id=inventory_item_id_int, available=int(qty))

        # Category metafields (best-effort)
        if taxonomy_gid:
            try:
                defs = client.metafield_definitions_for_category(category_gid=taxonomy_gid, first=50)
                metas: list[dict[str, str]] = []
                for d in defs:
                    val = _shopify_metafield_value_for(
                        d.get("name") or "",
                        product_type=product_type,
                        subcategory=subcategory,
                        metal_type=metal_type,
                        metal_color=metal_color,
                        made_for=made_for,
                    )
                    if val is None:
                        continue
                    metas.append({"namespace": d["namespace"], "key": d["key"], "type": d["type"], "value": str(val)})
                if metas:
                    client.product_update_metafields(product_id=product_id, metafields=metas)
            except Exception:
                pass

        # Upload images: prompt2, prompt1, then selected pics_raw.
        media_urls: list[str] = []
        upload_list: list[tuple[Path, str]] = [(p2, f"{sku} - Product"), (p1, f"{sku} - Lifestyle")]
        upload_list.extend([(pp, f"{sku} - Reference") for pp in selected_pics])
        for img_path, alt in upload_list:
            mime = "image/jpeg" if img_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            target = client.staged_upload_create(filename=img_path.name, mime_type=mime, resource="FILE", http_method="POST")
            client.upload_to_staged_target(target=target, filename=img_path.name, mime_type=mime, file_bytes=img_path.read_bytes())
            file_id = client.file_create_from_staged(resource_url=str(target.get("resourceUrl") or target.get("url") or ""), alt=alt, content_type="IMAGE")
            ready = client.file_poll_ready(file_id=file_id, max_tries=60, sleep_seconds=2.0)
            cdn = str(ready.get("preview_url") or "").strip()
            if cdn:
                media_urls.append(cdn)
        if media_urls:
            client.product_create_media(product_id=product_id, media=[{"mediaContentType": "IMAGE", "originalSource": u, "alt": sku} for u in media_urls])

        store.update(sku, status="uploaded", product_id=product_id, handle=prod.get("handle") or "", last_error="")
        state["last_sku"] = sku
        _save_json_file(state_path, state)
        st.success(f"Uploaded: {sku}")
        st.rerun()
    except Exception as e:
        store.update(sku, status="failed", last_error=str(e))
        state["last_sku"] = sku
        _save_json_file(state_path, state)
        st.error(f"Failed: {sku}")
        st.exception(e)
        st.rerun()


def _render_generate(cfg) -> None:
    store = None
    ordered_keys: list[str] = []
    key_to_images: dict[str, list[Path]] = {}
    sku_to_xlsx_row: dict[str, object] = {}
    xlsx_sheet_row_counts: dict[str, int] = {}
    xlsx_total_rows_all_sheets: int | None = None
    xlsx_all_sheets: list[str] = []

    if cfg.input_mode == "folder":
        groups = iter_groups(cfg.images_dir)
        ordered_keys = [g.key for g in groups]
        key_to_images = {g.key: g.images for g in groups}
        from src.state_store import StateStore

        store = StateStore(cfg.state_path)
        store.ensure_skus(ordered_keys)
    else:
        if cfg.input_mode == "xlsx":
            @st.cache_data(show_spinner=False)
            def _cached_xlsx_counts(xlsx_path_str: str) -> tuple[list[str], dict[str, int], int]:
                xlsx_path = Path(xlsx_path_str)
                sheets = xlsx_list_sheets(xlsx_path)
                counts: dict[str, int] = {}
                for sh in sheets:
                    counts[sh] = len(xlsx_iter_rows(xlsx_path, [sh]))
                return sheets, counts, sum(counts.values())

            try:
                xlsx_all_sheets, xlsx_sheet_row_counts, xlsx_total_rows_all_sheets = _cached_xlsx_counts(str(cfg.xlsx_path))
            except Exception:
                xlsx_all_sheets, xlsx_sheet_row_counts, xlsx_total_rows_all_sheets = [], {}, None

            xrows = xlsx_iter_rows(cfg.xlsx_path, cfg.xlsx_sheets)
            sku_map = xlsx_index_by_sku(xrows, sku_column="SKU")
            ordered_keys = list(sku_map.keys())
            sku_to_xlsx_row = sku_map
            from src.state_store import StateStore

            store = StateStore(cfg.state_path)
            store.ensure_skus(ordered_keys)
            for sku in ordered_keys:
                key_to_images[sku] = _list_candidates_for_key(cfg.images_dir, sku)
        else:
            entries, store = load_entries_and_state(cfg)
            ordered_keys = [e.sku for e in entries]
            for sku in ordered_keys:
                key_to_images[sku] = _list_candidates_for_key(cfg.images_dir, sku)

    if store is None:
        st.error("State store not initialized.")
        return

    if "session_id" not in st.session_state:
        st.session_state.session_id = uuid.uuid4().hex

    leases_dir = cfg.outputs_dir / "_leases"
    max_parallel = int(getattr(cfg, "max_parallel_sessions", 4) or 4)
    lease_ttl = int(getattr(cfg, "lease_ttl_seconds", 3600) or 3600)

    override_key = st.session_state.get("override_key")
    next_key: str | None = None

    # Refresh current lease heartbeat if we still hold it.
    cur_lease_key = st.session_state.get("lease_key")
    if cur_lease_key:
        p = leases_dir / f"{cur_lease_key}.json"
        if p.exists():
            try:
                import json

                data = json.loads(p.read_text(encoding="utf-8"))
                if str(data.get("session_id") or "") == str(st.session_state.session_id):
                    p.touch(exist_ok=True)
            except Exception:
                pass

    def _acquire_or_none(key: str) -> bool:
        lease = try_acquire_lease(
            leases_dir,
            key,
            str(st.session_state.session_id),
            ttl_seconds=lease_ttl,
            max_concurrent=max_parallel,
        )
        if lease is None:
            return False
        # If switching keys, release previous lease.
        prev = st.session_state.get("lease_key")
        if prev and prev != key:
            prev_path = leases_dir / f"{prev}.json"
            try:
                prev_path.unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                pass
        st.session_state.lease_key = key
        return True

    if override_key and override_key in ordered_keys:
        if not _acquire_or_none(str(override_key)):
            st.warning(f"`{override_key}` is currently being worked on in another window. Try again later.")
            active = list_active_leases(leases_dir, lease_ttl)
            st.caption(f"Active leases: {len(active)}/{max_parallel}")
            st.stop()
        next_key = str(override_key)
    else:
        # Prefer resuming pending items with attempts>0, but skip anything leased by another session.
        actionable: list[str] = []
        for sku in ordered_keys:
            rec = store.get(sku)
            if rec.status == "pending" and int(rec.attempts or 0) > 0:
                actionable.append(sku)
        for sku in ordered_keys:
            rec = store.get(sku)
            if rec.status == "pending" and int(rec.attempts or 0) <= 0:
                actionable.append(sku)

        for sku in actionable:
            if _acquire_or_none(sku):
                next_key = sku
                break

    if not next_key:
        active = list_active_leases(leases_dir, lease_ttl)
        if max_parallel > 0 and len(active) >= max_parallel:
            st.info(f"All {max_parallel} parallel slots are currently in use. Close a window or wait for leases to expire.")
            st.caption(f"Active leases: {len(active)}/{max_parallel}")
            st.stop()
        st.success("All SKUs are approved or skipped.")
        return

    # Progress bar (overall workbook scope when using xlsx; otherwise within input list)
    approved = 0
    skipped = 0
    pending = 0
    try:
        for k in ordered_keys:
            s = store.get(k).status
            if s == "approved":
                approved += 1
            elif s == "skipped":
                skipped += 1
            else:
                pending += 1
    except Exception:
        pass
    total = max(1, len(ordered_keys))
    done = approved + skipped
    st.progress(done / total, text=f"Progress: {done}/{total} done (approved: {approved}, skipped: {skipped}, pending: {pending})")

    if st.session_state.get("override_key") == next_key:
        st.info(f"Manual selection: showing `{next_key}` from Gallery.")

    st.caption(f"Key: `{next_key}`")
    if cfg.input_mode == "xlsx" and next_key in sku_to_xlsx_row:
        xr = sku_to_xlsx_row[next_key]
        try:
            values = xr.values  # type: ignore[attr-defined]
            st.subheader("Stock.xlsx row")
            st.caption(f"Sheet: {xr.sheet} | Row: {xr.row_index_1based}")  # type: ignore[attr-defined]
            st.dataframe([values], width="stretch")
        except Exception:
            pass

    if "client" not in st.session_state:
        # Default model for new tabs/sessions.
        st.session_state.selected_model = "models/gemini-3.1-flash-image-preview"
        st.session_state.client = GenAiImageClient(
            st.session_state.selected_model,
            cfg.min_seconds_between_requests,
            semaphore_dir=str(cfg.outputs_dir / "_semaphore"),
            max_inflight_generations=int(getattr(cfg, "max_inflight_generations", 4) or 4),
        )
        st.session_state.client_info = {
            "mode": getattr(st.session_state.client, "mode", "unknown"),
            "model": st.session_state.selected_model,
        }

    with st.sidebar:
        st.header("Run Info")
        st.json(st.session_state.get("client_info", {"model": cfg.model}))

        if "available_models" not in st.session_state:
            with st.spinner("Loading available models..."):
                st.session_state.available_models = st.session_state.client.list_models()

        if st.button("Refresh models", width="stretch"):
            with st.spinner("Refreshing..."):
                st.session_state.available_models = st.session_state.client.list_models()

        models_list = st.session_state.get("available_models") or []
        if models_list and isinstance(models_list, list) and isinstance(models_list[0], dict) and models_list[0].get("error"):
            st.error(f"Model listing failed: {models_list[0].get('error')}")
        image_models = [
            m["name"]
            for m in models_list
            if isinstance(m, dict)
            and isinstance(m.get("name"), str)
            and isinstance(m.get("supported_actions"), list)
            and "generateContent" in m.get("supported_actions")
            and (
                ("image" in m["name"].lower())
                or ("image" in str(m.get("display_name", "")).lower())
                or ("image" in str(m.get("description", "")).lower())
                or ("nano banana" in str(m.get("display_name", "")).lower())
            )
        ]
        image_models = sorted(set(image_models))

        st.subheader("Model")
        if image_models:
            default_idx = image_models.index(st.session_state.selected_model) if st.session_state.selected_model in image_models else 0
            chosen = st.selectbox("Select model for generation", options=image_models, index=default_idx)
            if chosen != st.session_state.selected_model:
                st.session_state.selected_model = chosen
                st.session_state.client = GenAiImageClient(
                    st.session_state.selected_model,
                    cfg.min_seconds_between_requests,
                    semaphore_dir=str(cfg.outputs_dir / "_semaphore"),
                    max_inflight_generations=int(getattr(cfg, "max_inflight_generations", 4) or 4),
                )
                st.session_state.client_info = {"mode": "devapi", "model": st.session_state.selected_model}
                st.success(f"Using model: {st.session_state.selected_model}")
        else:
            st.warning("No image-capable models returned from listing; using config.yaml model.")
            st.caption("If this persists, check your API key, network, or try Refresh models.")
            with st.expander("Model listing (raw)", expanded=False):
                st.json(models_list)

        st.divider()
        st.subheader("Progress")
        st.caption(f"Approved: {approved} | Skipped: {skipped} | Pending: {pending} | Total: {len(ordered_keys)}")
        if cfg.input_mode == "xlsx":
            pages = len(xlsx_all_sheets) if xlsx_all_sheets else len(getattr(cfg, "xlsx_sheets", []) or [])
            st.caption(f"XLSX pages (sheets): {pages}")
            if xlsx_total_rows_all_sheets is not None:
                st.caption(f"Total rows (all pages): {xlsx_total_rows_all_sheets}")
            # Current page stats
            try:
                xr = sku_to_xlsx_row.get(next_key)
                sheet = getattr(xr, "sheet", "") if xr is not None else ""
                if sheet:
                    st.caption(f"Current page: {sheet}")
                    if sheet in xlsx_sheet_row_counts:
                        st.caption(f"Rows in current page: {xlsx_sheet_row_counts[sheet]}")
            except Exception:
                pass

    if "attempt" not in st.session_state or st.session_state.get("key") != next_key:
        st.session_state.key = next_key
        st.session_state.attempt = int(store.get(next_key).attempts) + 1
        st.session_state.generated = {}

    # If the current SKU is pending but already has generated temp outputs, reload them so the user can approve.
    if not st.session_state.generated:
        st_rec = store.get(next_key)
        if st_rec.status == "pending" and int(st_rec.attempts or 0) > 0 and st_rec.selected_reference_paths:
            attempt = int(st_rec.attempts)
            loaded = {}
            for ref_path_str in (st_rec.selected_reference_paths or []):
                ref_path = Path(str(ref_path_str))
                temp_dir = cfg.outputs_dir / "_temp" / next_key
                p1 = temp_dir / f"prompt1_attempt{attempt}.{cfg.output_format}"
                p2 = temp_dir / f"prompt2_attempt{attempt}.{cfg.output_format}"
                if p1.exists() and p2.exists():
                    loaded[next_key] = {
                        "ref_paths": [str(Path(x)) for x in (st_rec.selected_reference_paths or [])],
                        "ref_path": str(ref_path),
                        "p1": str(p1),
                        "p2": str(p2),
                        "meta": {"attempt": attempt, "resumed": True},
                    }
            if loaded:
                st.session_state.generated = loaded
                st.session_state.attempt = attempt

    candidates = key_to_images.get(next_key) or []
    if not candidates:
        st.error("No images found for this key. Marking as skipped.")
        skip(store, next_key, "no_images_for_key")
        st.rerun()
        return

    st.subheader("Select reference images for generation")
    st.caption("Select one or more. The first selected image is treated as the PRIMARY reference for fidelity checks.")

    sel_key = f"selected_refs::{next_key}"
    if sel_key not in st.session_state:
        st.session_state[sel_key] = [str(candidates[0])]

    st.write("Select images (you can pick multiple):")
    cols = st.columns(min(4, len(candidates)))
    for i, p in enumerate(candidates):
        with cols[i % len(cols)]:
            img = _safe_open_image(p)
            if img is None:
                st.warning(p.name)
                st.caption("Unreadable image")
            else:
                st.image(img, caption=p.name, width="stretch")
            selected_list = list(st.session_state.get(sel_key) or [])
            is_selected = str(p) in selected_list
            btn_label = "Selected" if is_selected else "Select"
            if st.button(btn_label, key=f"pick::{next_key}::{p.name}", width="stretch"):
                if is_selected:
                    selected_list = [x for x in selected_list if x != str(p)]
                else:
                    selected_list.append(str(p))
                # Ensure at least one selection (fallback to first candidate)
                if not selected_list:
                    selected_list = [str(candidates[0])]
                st.session_state[sel_key] = selected_list
                st.rerun()

    selected_refs = [Path(p) for p in (st.session_state.get(sel_key) or [])]
    selected_refs = [p for p in selected_refs if p.exists()]
    if not selected_refs:
        selected_refs = [candidates[0]]
        st.session_state[sel_key] = [str(candidates[0])]
    st.caption("Current selection (order matters): " + ", ".join([f"`{p.name}`" for p in selected_refs]))

    extra_ctx = ""
    if cfg.input_mode == "xlsx" and next_key in sku_to_xlsx_row:
        try:
            row_vals = sku_to_xlsx_row[next_key].values  # type: ignore[attr-defined]
            fields = [
                ("category", row_vals.get("category")),
                ("subCategory", row_vals.get("subCategory")),
                ("metalType", row_vals.get("metalType")),
                ("metalColor", row_vals.get("metalColor")),
                ("madeFor", row_vals.get("madeFor")),
            ]
            extra_ctx = "\n".join([f"- {k}: {v}" for k, v in fields if str(v).strip() not in {"", "None"}])
        except Exception:
            extra_ctx = ""

    st.subheader("Product context (editable for this run)")
    st.caption("This does not edit your Stock.xlsx. It is only used as context for the current generation and resets for the next SKU.")
    ctx_key = f"product_ctx::{next_key}"
    # Populate with XLSX-derived context by default for this SKU, but allow edits during this session.
    if (ctx_key not in st.session_state) or (not str(st.session_state.get(ctx_key) or "").strip()):
        st.session_state[ctx_key] = extra_ctx if extra_ctx.strip() else ""
    editable_ctx = st.text_area("Product context", height=160, key=ctx_key)
    st.caption("If any context conflicts with the reference image (especially metal color/finish), the model must follow the reference image.")

    if st.button("Generate now", type="primary", width="stretch"):
        st.session_state.generated = {}
        error_placeholder = st.empty()
        with st.spinner("Generating..."):
            try:
                ref_paths = [Path(p) for p in selected_refs]
                ref_tag = ref_paths[0].stem
                work = prepare_work_item_for_paths(cfg, next_key, ref_paths)
                p1, p2, meta = generate_pair(
                    cfg,
                    st.session_state.client,
                    work,
                    st.session_state.attempt,
                    ref_tag=ref_tag,
                    extra_context=editable_ctx,
                )
                st.session_state.generated[next_key] = {
                    "ref_paths": [str(p) for p in ref_paths],
                    "ref_path": str(ref_paths[0]),
                    "p1": str(p1),
                    "p2": str(p2),
                    "meta": meta,
                }

                pm = meta.get("prompt_meta") or {}
                for prompt_id, bucket_key in [("prompt1", "p1"), ("prompt2", "p2")]:
                    m = pm.get(bucket_key) or {}
                    usage = (m.get("usage_metadata") or None) if isinstance(m, dict) else None
                    response_id = str(m.get("response_id") or "")
                    model_version = str(m.get("model_version") or "")
                    prompt_tokens = int((usage or {}).get("prompt_token_count") or 0) if isinstance(usage, dict) else 0
                    cand_tokens = int((usage or {}).get("candidates_token_count") or 0) if isinstance(usage, dict) else 0
                    img_prompt_tokens = int((m.get("image_prompt_tokens") or 0) if isinstance(m, dict) else 0)
                    img_cand_tokens = int((m.get("image_candidates_tokens") or 0) if isinstance(m, dict) else 0)
                    if isinstance(usage, dict):
                        ip, ic = extract_image_modality_tokens(usage)
                        img_prompt_tokens, img_cand_tokens = ip, ic
                    est = estimate_cost_usd(
                        cfg.pricing_usd_per_million_tokens,
                        str(getattr(st.session_state.client, "model", "")),
                        prompt_tokens,
                        cand_tokens,
                        image_prompt_tokens=img_prompt_tokens,
                        image_candidates_tokens=img_cand_tokens,
                    )
                    row = make_generate_row(
                        key=next_key,
                        ref_tag=ref_tag,
                        prompt_id=prompt_id,
                        attempt=int(st.session_state.attempt),
                        model=str(getattr(st.session_state.client, "model", cfg.model)),
                        mode="devapi",
                        status="success",
                        action="generate",
                        usage_metadata=usage if isinstance(usage, dict) else None,
                        response_id=response_id,
                        model_version=model_version,
                        estimated_cost_usd=est,
                        error="",
                    )
                    append_cost_row(cfg.cost_log_csv, row)
            except Exception as e:
                msg = str(e)
                log.exception("Generation failed for key=%s attempt=%s: %s", next_key, st.session_state.get("attempt"), msg)
                store.update(next_key, last_error=msg)
                st.session_state.generated = {}
                error_placeholder.error("Generation failed.")
                if hasattr(st.session_state, "client") and getattr(st.session_state.client, "last_raw_response", None) is not None:
                    with st.expander("Raw model response (debug)", expanded=True):
                        st.json(getattr(st.session_state.client, "last_raw_response"))
                with st.expander("Exception details", expanded=True):
                    st.write(f"Type: `{type(e).__name__}`")
                    st.exception(e)
                error_placeholder.code(msg)
                try:
                    row = make_generate_row(
                        key=next_key,
                        ref_tag=ref_tag if "ref_tag" in locals() else "",
                        prompt_id="unknown",
                        attempt=int(st.session_state.attempt),
                        model=str(getattr(st.session_state.client, "model", cfg.model)),
                        mode="devapi",
                        status="error",
                        action="generate",
                        usage_metadata=None,
                        response_id="",
                        model_version="",
                        estimated_cost_usd="",
                        error=msg,
                    )
                    append_cost_row(cfg.cost_log_csv, row)
                except Exception:
                    pass
                return

        store.update(next_key, attempts=st.session_state.attempt, selected_reference_paths=[str(p) for p in selected_refs], last_error="")
        st.rerun()

    if not st.session_state.generated:
        st.stop()

    st.subheader("Review generated outputs")
    for ref_tag, item in st.session_state.generated.items():
        ref_paths = item.get("ref_paths") or [item.get("ref_path")]
        ref_paths = [Path(str(p)) for p in ref_paths if p]
        st.markdown("### References")
        if ref_paths:
            ref_cols = st.columns(min(4, len(ref_paths)))
            for i, rp in enumerate(ref_paths):
                with ref_cols[i % len(ref_cols)]:
                    st.caption("PRIMARY" if i == 0 else "Context")
                    img = _safe_open_image(rp)
                    if img is None:
                        st.warning(rp.name)
                    else:
                        st.image(img, caption=rp.name, width="stretch")
        c2, c3 = st.columns(2)
        with c2:
            p1p = Path(item["p1"])
            if not p1p.exists():
                st.warning(f"Missing temp output: {p1p.name}")
            else:
                st.image(_load_image(p1p), width="stretch")
        with c3:
            p2p = Path(item["p2"])
            if not p2p.exists():
                st.warning(f"Missing temp output: {p2p.name}")
            else:
                st.image(_load_image(p2p), width="stretch")
        with st.expander(f"Metrics: {ref_tag}", expanded=False):
            st.json(item.get("meta", {}))

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        if st.button("Approve & Next", type="primary", width="stretch"):
            ref_to_temp = {ref_tag: (Path(item["p1"]), Path(item["p2"])) for ref_tag, item in st.session_state.generated.items()}
            approve_many(cfg, store, next_key, ref_to_temp)
            # Clear in-memory generated state so we never re-show this key after approval.
            st.session_state.generated = {}
            st.session_state.key = ""
            if st.session_state.get("lease_key") == next_key:
                try:
                    (cfg.outputs_dir / "_leases" / f"{next_key}.json").unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                st.session_state.lease_key = ""
            if st.session_state.get("override_key") == next_key:
                st.session_state.override_key = None
            try:
                append_cost_row(
                    cfg.cost_log_csv,
                    make_generate_row(
                        key=next_key,
                        ref_tag="",
                        prompt_id="",
                        attempt=int(st.session_state.attempt),
                        model=str(getattr(st.session_state.client, "model", cfg.model)),
                        mode="devapi",
                        status="success",
                        action="approve",
                        usage_metadata=None,
                        error="",
                    ),
                )
            except Exception:
                pass
            st.success("Saved and marked approved.")
            st.rerun()
    with c2:
        if st.button("Needs Rework (regenerate)", width="stretch"):
            try:
                append_cost_row(
                    cfg.cost_log_csv,
                    make_generate_row(
                        key=next_key,
                        ref_tag="",
                        prompt_id="",
                        attempt=int(st.session_state.attempt),
                        model=str(getattr(st.session_state.client, "model", cfg.model)),
                        mode="devapi",
                        status="success",
                        action="regenerate_click",
                        usage_metadata=None,
                        error="",
                    ),
                )
            except Exception:
                pass
            st.session_state.attempt += 1
            st.session_state.generated = {}
            st.rerun()
    with c3:
        if st.button("Skip SKU", width="stretch"):
            skip(store, next_key, "user_skipped")
            try:
                append_cost_row(
                    cfg.cost_log_csv,
                    make_generate_row(
                        key=next_key,
                        ref_tag="",
                        prompt_id="",
                        attempt=int(st.session_state.attempt),
                        model=str(getattr(st.session_state.client, "model", cfg.model)),
                        mode="devapi",
                        status="success",
                        action="skip",
                        usage_metadata=None,
                        error="user_skipped",
                    ),
                )
            except Exception:
                pass
            if st.session_state.get("lease_key") == next_key:
                try:
                    (cfg.outputs_dir / "_leases" / f"{next_key}.json").unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                st.session_state.lease_key = ""
            if st.session_state.get("override_key") == next_key:
                st.session_state.override_key = None
            st.rerun()


def main() -> None:
    cfg = load_config()
    st.set_page_config(page_title=cfg.page_title, layout="wide")
    st.title(cfg.page_title)
    try:
        st.set_option("runner.magicEnabled", False)
    except Exception:
        pass

    nav_options = ["Upload", "Bulk Upload", "Generate", "Gallery", "Shopify Review", "Title Generator", "Costs"]
    if "nav_widget" not in st.session_state:
        st.session_state.nav_widget = "Upload"
    if "pending_nav" in st.session_state:
        st.session_state.nav_widget = st.session_state.pop("pending_nav")

    with st.sidebar:
        st.subheader("View")
        st.radio(
            "Navigation",
            options=nav_options,
            label_visibility="collapsed",
            key="nav_widget",
            index=nav_options.index(st.session_state.nav_widget) if st.session_state.nav_widget in nav_options else 0,
        )
        st.session_state.view = st.session_state.nav_widget

    if st.session_state.view == "Upload":
        _render_upload(cfg)
    elif st.session_state.view == "Bulk Upload":
        _render_bulk_upload(cfg)
    elif st.session_state.view == "Gallery":
        _render_gallery(cfg)
    elif st.session_state.view == "Shopify Review":
        _render_shopify_review(cfg)
    elif st.session_state.view == "Title Generator":
        _render_title_generator(cfg)
    elif st.session_state.view == "Costs":
        _render_costs(cfg)
    else:
        _render_generate(cfg)


if __name__ == "__main__":
    main()
