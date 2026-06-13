#!/usr/bin/env python3
"""Drive-backed SKU review app with Firestore leasing and tri-system sync."""
from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import streamlit as st
from google.genai import errors as genai_errors
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_stock_export import build_export
from src.drive_client import (
    DRIVE_OAUTH_PORT,
    ensure_client_secret_saved,
    format_drive_http_error,
    get_drive_service,
    oauth_gcp_project_id,
    oauth_redirect_uris,
)
from src.drive_outputs_sync import DriveOutputsSync
from src.drive_review_config import DriveReviewConfig, load_drive_review_config
from src.drive_gallery import (
    build_gallery_row,
    distinct_categories,
    filter_by_category,
    gallery_readiness_light,
    load_enriched_index,
    local_workspace_needs_drive_pull,
    paginate,
    required_stock_columns,
    scan_drive_metadata_for_skus,
    list_gallery_skus,
)
from src.drive_stock_sync import rebuild_and_replace, rebuild_enriched_xlsx, refresh_stock_sheet, resolve_stock_path
from src.drive_sync_orchestrator import (
    SyncResult,
    check_save_everything_media,
    sku_ready_to_save,
    sync_after_regenerate,
    sync_gallery_batch_save,
    sync_regenerate_local_only,
    sync_save_everything,
    validate_local_workspace,
)
from src.drive_review_log import setup_drive_review_logging
from src.drive_typo_cleanup import apply_drive_typo_cleanup, audit_drive_typo_folders
from src.firestore_leases import FirestoreLeaseManager
from src.genai_client import GenAiImageClient
from src.media_workspace import index_sku_media, refresh_manifest
from src.name_group import base_key_from_path
from src.pipeline import (
    PROMPT_1,
    PROMPT_2,
    default_prompt_for_category,
    generate_to_workspace,
    prepare_work_item_for_sku,
    reference_paths_for_sku,
)
from src.review_autofill import autofill_review_record
from src.review_store import ReviewStore
from src.title_store import TitleStore
from src.drive_outputs_tally import tally_drive_vs_local, write_tally_report
from src.drive_prompt_audit import audit_drive_prompt_images, write_prompt_audit_report
from src.shopify_client import ShopifyClient
from src.shopify_env import ShopifyConnectionState, ensure_shopify_connection, load_shopify_env
from src.shopify_product_dedup import lookup_shopify_product, shopify_products_by_sku
from src.shopify_media_sync import images_for_sku, media_paths_for_sku
from src.text_format import product_generation_context, title_case_category
from src.typo_sku_cleanup import write_audit_report
from src.xlsx_ingest import index_by_sku, iter_rows

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

GALLERY_EDITED_KEY = "gallery_edited_skus"
GALLERY_PULL_KEY = "gallery_pull_key"
GALLERY_DRIVE_META_CACHE = "gallery_drive_meta_cache"
GALLERY_COMPLETE_SKUS = "gallery_complete_skus"
GALLERY_COMPLETE_CACHE_KEY = "gallery_complete_cache_key"
GALLERY_ACTIVE_SEARCH_KEY = "gallery_active_search"


def _safe_open(path: Path) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def _machine_id() -> str:
    return socket.gethostname()


def _session_holder_id() -> str:
    if "drive_holder_id" not in st.session_state:
        st.session_state.drive_holder_id = uuid.uuid4().hex
    return str(st.session_state.drive_holder_id)


def _tab_id() -> str:
    if "drive_tab_id" not in st.session_state:
        st.session_state.drive_tab_id = uuid.uuid4().hex[:12]
    return str(st.session_state.drive_tab_id)


@st.cache_resource
def _load_cfg() -> DriveReviewConfig:
    return load_drive_review_config(
        ROOT / "drive_review/config.yaml",
        base_config_path=ROOT / "config.yaml",
    )


def _drive_service(cfg: DriveReviewConfig, *, write: bool = True):
    secret = cfg.drive_credentials_dir / "client_secret.json"
    token = cfg.drive_credentials_dir / ("token_write.json" if write else "token_read.json")
    return get_drive_service(client_secret_path=secret, token_path=token, write=write)


def _lease_manager(cfg: DriveReviewConfig) -> FirestoreLeaseManager | None:
    if not cfg.firestore_project_id:
        return None
    return FirestoreLeaseManager(
        project_id=cfg.firestore_project_id,
        collection=cfg.firestore_collection,
        ttl_seconds=cfg.lease_ttl_seconds,
        max_concurrent=cfg.max_parallel_leases,
    )


def _review_store(cfg: DriveReviewConfig) -> ReviewStore:
    return ReviewStore(cfg.review_state_path)


def _title_store(cfg: DriveReviewConfig) -> TitleStore:
    return TitleStore(cfg.outputs_dir / "title_gen_state.json")


def _ensure_review_autofill(
    cfg: DriveReviewConfig,
    service,
    *,
    sku: str,
    review_store: ReviewStore,
    media_idx,
    shop_prod: dict | None,
) -> None:
    """Fill missing category/title once per SKU per session, then rerun to refresh widgets."""
    key = f"drive_review_autofill::{sku}"
    if st.session_state.get(key):
        return
    rec = review_store.get_record(sku)
    needs_category = not str(rec.get("category") or rec.get("product_type") or "").strip()
    needs_title = not str(rec.get("title") or "").strip()
    title_store = _title_store(cfg)
    if not needs_title:
        tr = title_store.get(sku)
        needs_title = not str(tr.get("new_title") or tr.get("generated_title") or "").strip()
    if not needs_category and not needs_title:
        st.session_state[key] = {"skipped": True}
        return
    stock_path = resolve_stock_path(cfg, service)
    with st.spinner("Auto-filling title and category..."):
        result = autofill_review_record(
            cfg,
            sku=sku,
            review_store=review_store,
            media_idx=media_idx,
            shop_prod=shop_prod,
            title_store=title_store,
            stock_path=stock_path,
        )
    st.session_state[key] = result
    if result.get("updated"):
        st.rerun()


def _ensure_genai(cfg: DriveReviewConfig) -> GenAiImageClient:
    model = cfg.base.model
    key = f"genai_client::{model}"
    if key not in st.session_state:
        st.session_state[key] = GenAiImageClient(
            model=model,
            min_seconds_between_requests=cfg.base.min_seconds_between_requests,
            semaphore_dir=str(cfg.outputs_dir / "_semaphore"),
            max_inflight_generations=cfg.base.max_inflight_generations,
        )
    return st.session_state[key]


@dataclass
class _ShopifySession:
    connection: ShopifyConnectionState
    products_by_sku: dict[str, dict]

    @property
    def connected(self) -> bool:
        return bool(self.connection.connected)

    @property
    def client(self) -> ShopifyClient | None:
        return self.connection.client


def _load_shopify_product_index(
    client: ShopifyClient,
    *,
    review_store: ReviewStore | None = None,
) -> dict[str, dict]:
    return shopify_products_by_sku(client, active_only=False, review_store=review_store)


def _ensure_shopify_session(cfg: DriveReviewConfig, *, force_index: bool = False) -> _ShopifySession:
    """Auto-connect from .env and ping Shopify on every run."""
    env_path = ROOT / ".env"
    conn = ensure_shopify_connection(cfg.outputs_dir, env_path=env_path)
    st.session_state["shopify_connection"] = conn

    products_by_sku: dict[str, dict] = {}
    if conn.connected and conn.client:
        index_key = "shopify_products_by_sku"
        if force_index or index_key not in st.session_state:
            try:
                products_by_sku = _load_shopify_product_index(
                    conn.client,
                    review_store=_review_store(cfg),
                )
                st.session_state[index_key] = products_by_sku
                st.session_state["shopify_index_shop"] = conn.shop_domain
            except Exception as e:
                conn = ShopifyConnectionState(
                    connected=False,
                    shop_domain=conn.shop_domain,
                    error=f"Product index failed: {e}",
                )
                st.session_state["shopify_connection"] = conn
        elif st.session_state.get("shopify_index_shop") == conn.shop_domain:
            products_by_sku = st.session_state.get(index_key) or {}
        else:
            products_by_sku = _load_shopify_product_index(
                conn.client,
                review_store=_review_store(cfg),
            )
            st.session_state[index_key] = products_by_sku
            st.session_state["shopify_index_shop"] = conn.shop_domain

    return _ShopifySession(connection=conn, products_by_sku=products_by_sku)


def _shopify_sidebar(cfg: DriveReviewConfig, session: _ShopifySession) -> _ShopifySession:
    st.subheader("Shopify")
    conn = session.connection
    env = load_shopify_env(env_path=ROOT / ".env")

    if conn.connected:
        st.success(conn.status_label)
        st.caption(f"Shop domain: `{conn.shop_domain}` | API: `{env.api_version}`")
        st.caption(f"Products indexed: **{len(session.products_by_sku)}** SKUs")
    elif conn.missing_env:
        st.error(conn.status_label)
        for var in conn.missing_env:
            st.caption(f"• Missing `{var}` in `.env`")
    else:
        st.error(conn.status_label)

    if st.button("Refresh Shopify connection + index"):
        st.session_state.pop("shopify_products_by_sku", None)
        st.session_state.pop("shopify_index_shop", None)
        st.rerun()

    return session


def _drive_sidebar(cfg: DriveReviewConfig):
    st.subheader("Google Drive")
    secret_path = cfg.drive_credentials_dir / "client_secret.json"
    cfg.drive_credentials_dir.mkdir(parents=True, exist_ok=True)
    uploaded = st.file_uploader("OAuth client JSON", type=["json"], key="drive_client_json")
    if uploaded is not None:
        ensure_client_secret_saved(dest_path=secret_path, uploaded_bytes=uploaded.getvalue())
        st.success("Saved client secret.")
    if secret_path.exists():
        try:
            secret_data = json.loads(secret_path.read_text(encoding="utf-8"))
            client_kind = "installed" if "installed" in secret_data else ("web" if "web" in secret_data else "unknown")
        except Exception:
            client_kind = "unknown"
        oauth_project = oauth_gcp_project_id(secret_path)
        if oauth_project:
            st.caption(f"OAuth GCP project: `{oauth_project}` (Drive API must be enabled here)")
        if client_kind == "web":
            st.warning(
                "OAuth client is **Web application** type. Add these **Authorized redirect URIs** "
                f"in Google Cloud Console (Credentials -> your OAuth client): "
                f"`{oauth_redirect_uris()[0]}` and `{oauth_redirect_uris()[1]}`"
            )
        else:
            st.caption(f"OAuth client type: {client_kind} (Desktop/installed is recommended).")
    if st.button("Connect Google Drive (write)", disabled=not secret_path.exists()):
        try:
            _drive_service(cfg, write=True)
            st.success("Drive connected with write scope.")
        except Exception as e:
            st.error(str(e))
            if "redirect_uri_mismatch" in str(e).lower():
                st.info(
                    f"Register redirect URIs on port **{DRIVE_OAUTH_PORT}**: "
                    + ", ".join(f"`{u}`" for u in oauth_redirect_uris())
                )
    st.caption(f"OAuth callback port: `{DRIVE_OAUTH_PORT}` (override with env `DRIVE_OAUTH_PORT`)")
    st.caption(f"Outputs folder: `{cfg.drive_outputs_folder_id}`")
    st.caption(f"Stock sheet: `{cfg.stock_spreadsheet_id}`")


def _list_candidates(images_dir: Path, sku: str) -> list[Path]:
    if not images_dir.exists():
        return []
    out: list[Path] = []
    for p in sorted(images_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS and base_key_from_path(p) == sku:
            out.append(p)
    return out


def _acquire_next_sku(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    leases: FirestoreLeaseManager | None,
    *,
    skus: list[str],
    filter_status: set[str] | None = None,
    ready_to_save_only: bool = False,
) -> str | None:
    review_store = _review_store(cfg)
    holder = _session_holder_id()
    machine = cfg.machine_id or _machine_id()
    tab = _tab_id()
    for sku in skus:
        rec = review_store.get_record(sku)
        status = str(rec.get("review_status") or "pending_review")
        if filter_status and status not in filter_status:
            continue
        if ready_to_save_only and not sku_ready_to_save(cfg, sku, review_store=review_store):
            continue
        if leases is None:
            return sku
        current = leases.get(sku)
        if current and current.holder_id != holder:
            continue
        if current and current.holder_id == holder:
            return sku
        lease = leases.try_acquire(sku, holder_id=holder, machine_id=machine, tab_id=tab)
        if lease:
            return sku
    return None


def _select_next_open_sku(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    leases: FirestoreLeaseManager | None,
    *,
    skus: list[str],
    exclude: str | None = None,
) -> str | None:
    """First SKU ready to save (both prompts, open status), optionally skipping one."""
    if exclude and exclude in skus:
        idx = skus.index(exclude)
        candidates = [s for s in skus[idx + 1 :] + skus[:idx] if s != exclude]
    else:
        candidates = list(skus)
    return _acquire_next_sku(
        cfg,
        sync,
        leases,
        skus=candidates,
        filter_status=None,
        ready_to_save_only=True,
    )


def _advance_after_save(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    leases: FirestoreLeaseManager | None,
    *,
    skus: list[str],
    saved_sku: str,
) -> str | None:
    holder = _session_holder_id()
    if leases:
        leases.release(saved_sku, holder)
    next_sku = _select_next_open_sku(cfg, sync, leases, skus=skus, exclude=saved_sku)
    if next_sku and next_sku != saved_sku:
        st.session_state.leased_sku = next_sku
    else:
        st.session_state.pop("leased_sku", None)
        next_sku = None
    return next_sku


def _progress_callback(progress_bar, status_text):
    def _cb(message: str, current: int, total: int) -> None:
        pct = current / total if total else 0.0
        progress_bar.progress(min(1.0, pct), text=f"{message} ({current}/{total})")
        status_text.caption(f"{message} — {current}/{total}")
    return _cb


def _gallery_edited() -> dict[str, dict[str, int | None]]:
    return st.session_state.setdefault(GALLERY_EDITED_KEY, {})


def _mark_gallery_edited(sku: str, slot: str, version: int | None) -> None:
    edited = _gallery_edited()
    rec = edited.setdefault(sku, {"prompt1": None, "prompt2": None})
    rec[slot] = version


def _gallery_skip_prompt_slots(sku: str) -> frozenset[str]:
    rec = _gallery_edited().get(sku) or {}
    return frozenset(s for s in ("prompt1", "prompt2") if rec.get(s) is not None)


def _gallery_page_cache_key(page: int, cat_filter: str, page_skus: list[str]) -> str:
    return f"{page}:{cat_filter}:{','.join(page_skus)}"


def _cached_gallery_complete_skus(
    cfg: DriveReviewConfig,
    enriched_index: dict,
    *,
    req_cols: list[str],
    review_store: ReviewStore,
) -> list[str]:
    enriched_path = cfg.enriched_xlsx_path
    mtime = enriched_path.stat().st_mtime if enriched_path.is_file() else 0
    cache_key = f"{mtime}:{','.join(req_cols)}"
    if st.session_state.get(GALLERY_COMPLETE_CACHE_KEY) == cache_key:
        return list(st.session_state.get(GALLERY_COMPLETE_SKUS) or [])
    skus = list_gallery_skus(cfg, enriched_index, required_cols=req_cols, review_store=review_store)
    st.session_state[GALLERY_COMPLETE_CACHE_KEY] = cache_key
    st.session_state[GALLERY_COMPLETE_SKUS] = skus
    return skus


def _cached_drive_meta_for_page(
    sync: DriveOutputsSync,
    *,
    page_skus: list[str],
    drive_folders: dict[str, str],
    cache_key: str,
) -> dict[str, dict]:
    cache = st.session_state.setdefault(GALLERY_DRIVE_META_CACHE, {})
    if cache_key in cache:
        return cache[cache_key]
    meta = scan_drive_metadata_for_skus(sync, page_skus, drive_folders=drive_folders)
    cache[cache_key] = meta
    return meta


def _invalidate_gallery_caches() -> None:
    st.session_state.pop(GALLERY_PULL_KEY, None)
    st.session_state.pop(GALLERY_DRIVE_META_CACHE, None)
    st.session_state.pop(GALLERY_COMPLETE_CACHE_KEY, None)
    st.session_state.pop(GALLERY_COMPLETE_SKUS, None)


def _pull_gallery_page_from_drive(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    *,
    page_skus: list[str],
    drive_folders: dict[str, str],
    pull_key: str,
) -> None:
    if st.session_state.get(GALLERY_PULL_KEY) == pull_key:
        return
    need_pull = [
        sku
        for sku in page_skus
        if drive_folders.get(sku)
        and local_workspace_needs_drive_pull(
            cfg,
            sku,
            ref_paths=reference_paths_for_sku(cfg.base, sku) or _list_candidates(cfg.base.images_dir, sku),
        )
    ]
    if not need_pull:
        st.session_state[GALLERY_PULL_KEY] = pull_key
        return
    with st.spinner(f"Loading {len(need_pull)} workspace(s) from Drive..."):
        for sku in need_pull:
            sync.pull_sku_from_drive(
                sku,
                folder_id=drive_folders[sku],
                include_raw=True,
                include_videos=False,
                skip_prompt_slots=_gallery_skip_prompt_slots(sku),
            )
    st.session_state[GALLERY_PULL_KEY] = pull_key


def _regenerate_prompt_for_sku(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    *,
    sku: str,
    slot: str,
    prompt_text: str,
    gen_ctx: str,
    review_store: ReviewStore,
    gallery_local_only: bool = False,
) -> None:
    gen = _ensure_genai(cfg)
    try:
        work = prepare_work_item_for_sku(cfg.base, sku)
        out_path, meta = generate_to_workspace(
            cfg.base,
            gen,
            work,
            prompt_slot=slot,
            prompt_override=prompt_text,
            extra_context=gen_ctx,
        )
        if gallery_local_only:
            sync_regenerate_local_only(cfg, sku, prompt_slot=slot)
            _mark_gallery_edited(sku, slot, meta.get("workspace_version"))
            st.session_state[f"gallery_regen_msg::{sku}"] = (
                f"Regenerated locally: {out_path.name} (save at bottom when done)"
            )
            st.rerun()
        else:
            result = sync_after_regenerate(cfg, sync, service, sku, prompt_slot=slot, review_store=review_store)
            st.success(f"Saved {out_path.name}")
            st.json(result)
            st.rerun()
    except genai_errors.ClientError as exc:
        code = getattr(exc, "status_code", None)
        if code == 429:
            st.error(
                f"Gemini quota exceeded for `{cfg.base.model}`. "
                "Enable billing on your Google AI project or set `IMAGE_MODEL` in `.env`."
            )
        else:
            st.error(f"Gemini API error ({code}): {exc}")
    except Exception as exc:
        st.error(f"Generation failed: {exc}")


def _save_everything_for_sku(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    *,
    sku: str,
    title: str,
    category: str,
    description: str,
    tags: str,
    prompt1_text: str,
    prompt2_text: str,
    product_id: str,
    handle: str,
    shopify: _ShopifySession,
    shop_prod: dict | None,
    review_store: ReviewStore,
) -> SyncResult:
    return sync_save_everything(
        cfg,
        sync,
        service,
        sku=sku,
        title=title,
        category=category,
        description=description,
        tags=tags,
        prompt1_text=prompt1_text,
        prompt2_text=prompt2_text,
        product_id=product_id,
        handle=handle,
        shopify_client=shopify.client,
        shop_prod=shop_prod,
        review_store=review_store,
    )


def _show_save_result(
    result: SyncResult,
    *,
    sku: str,
    advance: bool = False,
    cfg: DriveReviewConfig | None = None,
    sync: DriveOutputsSync | None = None,
    leases=None,
    queue_skus: list[str] | None = None,
) -> None:
    if not (result.media_readiness or {}).get("ready"):
        st.error("Save blocked — fix missing media first.")
        for err in result.errors:
            st.caption(f"• {err}")
        st.json({"media_readiness": result.media_readiness, "errors": result.errors})
        return
    post = (result.drive_push or {}).get("post_push") or {}
    skipped = (result.drive_push or {}).get("skipped_existing_raw_videos") or []
    if post:
        st.caption(
            f"Drive — prompt1: {post.get('has_prompt1')}, prompt2: {post.get('has_prompt2')}, "
            f"raw: {len(post.get('raw_on_drive') or [])} on Drive, "
            f"videos: {len(post.get('videos_on_drive') or [])} on Drive"
            + (f" (skipped {len(skipped)} upload(s))" if skipped else "")
        )
    if result.shopify:
        st.caption(
            f"Shopify — images: {result.shopify.get('image_count', 0)}/"
            f"{result.shopify.get('expected_images', '?')}, "
            f"videos: {result.shopify.get('video_count', 0)}/"
            f"{result.shopify.get('expected_videos', 0)}"
        )
    if result.warnings:
        for w in result.warnings:
            st.caption(f"Note: {w}")
    if result.errors:
        st.warning("Saved with warnings:")
        for err in result.errors:
            st.caption(f"• {err}")
    else:
        st.success("Saved everything — Drive, Google Sheet" + (", and Shopify" if result.shopify else "."))
    if advance and cfg and sync:
        final_status = str(_review_store(cfg).get_record(sku).get("review_status") or "")
        if final_status == "uploaded":
            next_sku = _advance_after_save(cfg, sync, leases, skus=queue_skus or [], saved_sku=sku)
            if next_sku:
                st.info(f"Next open SKU: `{next_sku}`")
            else:
                st.info("No more open SKUs ready to save.")
        else:
            st.warning(f"SKU `{sku}` is still `{final_status}` — fix errors and save again.")
    st.session_state[f"gallery_last_save::{sku}"] = {
        "media_readiness": result.media_readiness,
        "drive_push": result.drive_push,
        "shopify": result.shopify,
        "warnings": result.warnings,
        "errors": result.errors,
    }


def _render_cleanup(cfg: DriveReviewConfig, sync: DriveOutputsSync, service) -> None:
    st.subheader("Typo Cleanup (Drive)")
    st.caption(
        f"Reads local `{cfg.outputs_dir}` and stock from Google Sheet `{cfg.stock_spreadsheet_id}`. "
        "**Apply** migrates locally, then pushes only changed SKUs + XLSX to Drive."
    )
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("Audit typo folders"):
            progress = st.progress(0, text="Starting audit...")
            status = st.empty()
            cb = _progress_callback(progress, status)
            audit = audit_drive_typo_folders(cfg, sync, service, progress=cb)
            json_path, md_path = write_audit_report(audit, cfg.outputs_dir)
            sync.push_file_to_outputs_root(json_path)
            sync.push_file_to_outputs_root(md_path)
            progress.progress(1.0, text="Audit complete")
            elapsed = audit.get("elapsed_seconds", "?")
            st.success(
                f"Audit done in {elapsed}s — "
                f"delete_safe={audit.get('summary', {}).get('delete_safe', 0)}, "
                f"delete_orphan={audit.get('summary', {}).get('delete_orphan', 0)}, "
                f"migrate={audit.get('summary', {}).get('migrate', 0)}, "
                f"unresolved={audit.get('summary', {}).get('unresolved', 0)}"
            )
            st.json(audit.get("summary") or {})
    with col2:
        if st.button("Apply cleanup (dry-run)"):
            progress = st.progress(0, text="Dry-run audit...")
            status = st.empty()
            results = apply_drive_typo_cleanup(cfg, sync, service, dry_run=True, progress=_progress_callback(progress, status))
            progress.progress(1.0, text="Dry-run complete")
            st.json(results)
    with col3:
        if st.button("Apply cleanup", type="primary"):
            st.info("Apply runs locally, then uploads only affected SKU folders to Drive.")
            progress = st.progress(0, text="Applying cleanup...")
            status = st.empty()
            results = apply_drive_typo_cleanup(cfg, sync, service, dry_run=False, progress=_progress_callback(progress, status))
            progress.progress(1.0, text="Cleanup complete")
            st.success("Drive typo cleanup applied.")
            st.json(results)


def _render_review_sku(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    *,
    sku: str,
    shopify: _ShopifySession,
    leases: FirestoreLeaseManager | None,
    queue_skus: list[str],
) -> None:
    review_store = _review_store(cfg)
    holder = _session_holder_id()
    if leases:
        current = leases.get(sku)
        if current and current.holder_id != holder:
            st.warning(f"SKU `{sku}` is leased by {current.machine_id}/{current.tab_id}.")
            return
        if current:
            leases.refresh(sku, holder)

    try:
        sync.ensure_local_sku(sku)
    except FileNotFoundError:
        st.error(f"No local workspace at `{cfg.outputs_dir / sku}`.")
        return
    sync.sync_review_state_local()

    media_idx = index_sku_media(outputs_dir=cfg.outputs_dir, sku=sku)
    shop_prod, shop_lookup_msg = lookup_shopify_product(
        shopify.products_by_sku,
        sku,
        review_store=review_store,
    )
    _ensure_review_autofill(cfg, service, sku=sku, review_store=review_store, media_idx=media_idx, shop_prod=shop_prod)

    rec = review_store.get_record(sku)
    title_store = _title_store(cfg)
    title_rec = title_store.get(sku)
    default_title = (
        str(rec.get("title") or "").strip()
        or str(title_rec.get("new_title") or title_rec.get("generated_title") or "").strip()
        or str((shop_prod or {}).get("title") or "").strip()
    )
    default_category = title_case_category(
        str(rec.get("category") or rec.get("product_type") or "").strip()
        or str((shop_prod or {}).get("product_type") or (shop_prod or {}).get("category") or "").strip()
    )
    autofill_info = st.session_state.get(f"drive_review_autofill::{sku}") or {}
    if autofill_info.get("messages"):
        st.caption("Auto-fill: " + "; ".join(autofill_info["messages"]))

    status = str(rec.get("review_status") or "pending_review")
    st.markdown(f"### {sku}")
    st.caption(f"Review: `{status}`")
    product_id = str(rec.get("product_id") or (shop_prod or {}).get("id") or "")
    conn = shopify.connection
    if not conn.connected:
        st.error(f"**Shopify not connected** — {conn.error or conn.status_label}")
        if conn.missing_env:
            st.caption("Add the missing variables to `.env` and click **Refresh Shopify connection + index**.")
    elif shop_prod:
        st.success(f"**Shopify connected** — `{shop_prod.get('title')}` (`{product_id}`)")
    else:
        st.warning(f"**Shopify connected** ({conn.shop_name}) — {shop_lookup_msg}")
        st.caption("Drive + Sheet will still save; Shopify media sync needs a product with this SKU on Shopify.")

    cols = st.columns(4)
    with cols[0]:
        st.metric("Raw", len(media_idx.raw_images))
    with cols[1]:
        st.metric("Prompt1", len(media_idx.prompt1_versions))
    with cols[2]:
        st.metric("Prompt2", len(media_idx.prompt2_versions))
    with cols[3]:
        st.metric("Videos", len(media_idx.videos))

    if media_idx.raw_images:
        st.image(_safe_open(media_idx.raw_images[0]), caption="Primary raw", width=200)
    if media_idx.latest_prompt1:
        st.image(_safe_open(media_idx.latest_prompt1), caption="Latest prompt1", width=200)
    if media_idx.latest_prompt2:
        st.image(_safe_open(media_idx.latest_prompt2), caption="Latest prompt2", width=200)

    title = st.text_input("Title", value=default_title, key=f"title::{sku}")
    category = st.text_input("Category", value=default_category, key=f"cat::{sku}")
    description = st.text_area("Description", value=str(rec.get("description") or ""), key=f"desc::{sku}")
    tags = st.text_input("Tags", value=str(rec.get("tags") or ""), key=f"tags::{sku}")
    default_p1 = default_prompt_for_category(PROMPT_1, category)
    default_p2 = default_prompt_for_category(PROMPT_2, category)
    prompt1_text = st.text_area(
        "Prompt1", value=str(rec.get("prompt1_text") or default_p1), height=120, key=f"p1text::{sku}",
    )
    prompt2_text = st.text_area(
        "Prompt2", value=str(rec.get("prompt2_text") or default_p2), height=120, key=f"p2text::{sku}",
    )

    ref_paths = reference_paths_for_sku(cfg.base, sku)
    if not ref_paths:
        ref_paths = _list_candidates(cfg.base.images_dir, sku)

    gen = _ensure_genai(cfg)
    st.caption(
        f"Image model: `{cfg.base.model}` | "
        f"Reference images for generation: **{len(ref_paths)}**"
        + (f" (`{', '.join(p.name for p in ref_paths[:4])}`"
           + (f" +{len(ref_paths) - 4} more" if len(ref_paths) > 4 else "")
           + ")" if ref_paths else " (none)")
    )
    gen_ctx = product_generation_context(title=title, category=category)
    has_both_prompts = bool(media_idx.prompt1_versions and media_idx.prompt2_versions)
    upload_paths = media_paths_for_sku(cfg.base, sku, review_store=review_store)
    has_raw = bool(upload_paths["raw"])
    can_save = has_both_prompts and has_raw
    readiness = check_save_everything_media(
        cfg, sync, sku, review_store=review_store, shop_prod=shop_prod,
    )
    with st.expander("Save readiness (generated + raw + videos)", expanded=not can_save):
        loc = readiness.get("local") or {}
        st.write(
            {
                "prompt1": loc.get("has_prompt1"),
                "prompt2": loc.get("has_prompt2"),
                "raw": loc.get("has_raw"),
                "videos": loc.get("has_videos"),
                "shopify_images_to_upload": loc.get("upload_image_count"),
                "shopify_videos_to_upload": loc.get("video_count"),
            }
        )
        shop_before = readiness.get("shopify_before") or {}
        if shop_before.get("connected"):
            st.caption(
                f"Shopify now: {shop_before.get('image_count', 0)} image(s), "
                f"{shop_before.get('video_count', 0)} video(s)"
            )
        if readiness.get("warnings"):
            for w in readiness["warnings"]:
                st.caption(f"Note: {w}")
    g1, g2 = st.columns(2)
    with g1:
        if st.button("Regenerate prompt1", disabled=not ref_paths, key=f"rp1::{sku}"):
            _regenerate_prompt_for_sku(
                cfg, sync, service,
                sku=sku, slot="prompt1", prompt_text=prompt1_text, gen_ctx=gen_ctx, review_store=review_store,
            )
    with g2:
        if st.button("Regenerate prompt2", disabled=not ref_paths, key=f"rp2::{sku}"):
            _regenerate_prompt_for_sku(
                cfg, sync, service,
                sku=sku, slot="prompt2", prompt_text=prompt2_text, gen_ctx=gen_ctx, review_store=review_store,
            )

    save_help = (
        "Validates prompt1, prompt2, raw images, and videos; pushes full workspace to Drive + Sheet. "
        "Skips raw/video on Drive if already there. Shopify gets prompt1+prompt2+raw+videos."
    )
    if conn.connected and product_id:
        save_help += " Replaces Shopify media with the full local set."
    elif conn.connected:
        save_help += " (Shopify skipped — no product with this SKU on Shopify yet)"
    elif not conn.connected:
        save_help += " (Shopify skipped — not connected)"
    if st.button(
        "Save everything",
        type="primary",
        disabled=not can_save,
        key=f"save_all::{sku}",
        help=save_help,
    ):
        with st.spinner("Saving to Drive, Google Sheet, and Shopify..."):
            result = _save_everything_for_sku(
                cfg, sync, service,
                sku=sku,
                title=title,
                category=category,
                description=description,
                tags=tags,
                prompt1_text=prompt1_text,
                prompt2_text=prompt2_text,
                product_id=product_id,
                handle=str(rec.get("handle") or (shop_prod or {}).get("handle") or ""),
                shopify=shopify,
                shop_prod=shop_prod,
                review_store=review_store,
            )
        _show_save_result(
            result,
            sku=sku,
            advance=True,
            cfg=cfg,
            sync=sync,
            leases=leases,
            queue_skus=queue_skus,
        )
        if (result.media_readiness or {}).get("ready"):
            st.rerun()
    if not can_save:
        missing = []
        if not has_both_prompts:
            missing.append("prompt1 and prompt2")
        if not has_raw:
            missing.append("raw/reference images")
        st.caption(f"Need {', '.join(missing)} before Save everything.")

    v1, v2, v3 = st.columns(3)
    with v1:
        if st.button("Mark verified", key=f"verify::{sku}"):
            review_store.mark_verified(sku)
            refresh_manifest(outputs_dir=cfg.outputs_dir, sku=sku, patch={"review_status": "verified"})
            sync.push_sku(sku)
            sync.sync_review_state_push()
            st.rerun()
    with v2:
        if st.button("Release lease", key=f"release::{sku}"):
            if leases:
                leases.release(sku, holder)
            st.session_state.pop("leased_sku", None)
            st.rerun()
    with v3:
        if st.button("Validate workspace", key=f"validate::{sku}"):
            st.json(validate_local_workspace(cfg, sku))

    with st.expander("Upload set preview"):
        imgs = images_for_sku(cfg.base, sku, review_store=review_store)
        paths = media_paths_for_sku(cfg.base, sku, review_store=review_store)
        st.write({"images": len(imgs), "videos": len(paths.get("videos") or [])})


def _resolve_gallery_search_sku(
    cfg: DriveReviewConfig,
    query: str,
    *,
    enriched_index: dict,
) -> str | None:
    q = (query or "").strip()
    if not q:
        return None
    if q in enriched_index:
        return q
    qu = q.upper()
    for sku in enriched_index:
        if sku.upper() == qu:
            return sku
    if (cfg.outputs_dir / qu).is_dir():
        return qu
    for p in cfg.outputs_dir.iterdir():
        if p.is_dir() and p.name.upper() == qu:
            return p.name
    return None


def _render_gallery_save_edited_footer(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    *,
    page_skus: list[str],
    shopify: _ShopifySession,
    review_store: ReviewStore,
) -> None:
    edited_all = _gallery_edited()
    edited_skus = {
        sku: slots
        for sku, slots in edited_all.items()
        if any(slots.get(s) is not None for s in ("prompt1", "prompt2"))
    }
    page_edited = {sku: slots for sku, slots in edited_skus.items() if sku in page_skus}
    st.divider()
    st.subheader("Save edited on this page")
    if page_edited:
        st.caption(
            f"**{len(page_edited)}** SKU(s) edited will upload only changed prompt images "
            f"(Drive + Shopify replace; raw/videos skipped)."
        )
        for sku, slots in sorted(page_edited.items()):
            parts = [f"{s} v{slots[s]}" for s in ("prompt1", "prompt2") if slots.get(s) is not None]
            st.caption(f"• `{sku}` — {', '.join(parts)}")
    else:
        st.caption("No edits yet. Regenerate images above, then save here.")

    save_col1, save_col2 = st.columns([1, 2])
    with save_col1:
        if st.button(
            f"Save edited ({len(page_edited)})",
            type="primary",
            key="gallery_save_edited_page",
            disabled=not page_edited,
        ):
            with st.spinner(f"Saving {len(page_edited)} edited SKU(s)..."):
                results = sync_gallery_batch_save(
                    cfg,
                    sync,
                    edited=page_edited,
                    shopify_client=shopify.client if shopify.connected else None,
                    products_by_sku=shopify.products_by_sku,
                    review_store=review_store,
                )
            ok = err = 0
            for result in results:
                if result.errors:
                    err += 1
                    st.warning(f"`{result.sku}`: {', '.join(result.errors)}")
                else:
                    ok += 1
            if ok:
                st.success(f"Saved {ok} SKU(s) — prompt images only.")
            for sku in page_edited:
                rec = edited_all.get(sku) or {}
                rec["prompt1"] = None
                rec["prompt2"] = None
                if not any(rec.get(s) is not None for s in ("prompt1", "prompt2")):
                    edited_all.pop(sku, None)
            st.session_state[GALLERY_EDITED_KEY] = edited_all
            st.session_state.pop(GALLERY_DRIVE_META_CACHE, None)
            st.rerun()
    with save_col2:
        session_edited_count = len(edited_skus)
        if session_edited_count > len(page_edited):
            st.caption(
                f"**{session_edited_count - len(page_edited)}** more edited SKU(s) elsewhere — "
                "navigate there to save them."
            )


def _render_gallery_media_sections(media_idx, *, compact: bool = False) -> None:
    """Show latest generated, all raw images, and videos for a SKU."""

    def _section(title: str) -> None:
        if compact:
            st.markdown(f"**{title}**")
        else:
            st.subheader(title)

    _section("Generated (latest)")
    g1, g2 = st.columns(2)
    with g1:
        if media_idx.latest_prompt1:
            ver = media_idx.prompt1_versions[-1][0]
            st.image(_safe_open(media_idx.latest_prompt1), caption=f"prompt1 v{ver}", width="stretch")
        else:
            st.caption("No prompt1 generated.")
    with g2:
        if media_idx.latest_prompt2:
            ver = media_idx.prompt2_versions[-1][0]
            st.image(_safe_open(media_idx.latest_prompt2), caption=f"prompt2 v{ver}", width="stretch")
        else:
            st.caption("No prompt2 generated.")

    _section(f"Raw images ({len(media_idx.raw_images)})")
    if media_idx.raw_images:
        ncol = min(3 if compact else 4, len(media_idx.raw_images))
        raw_cols = st.columns(ncol)
        for i, raw_path in enumerate(media_idx.raw_images):
            with raw_cols[i % ncol]:
                st.image(_safe_open(raw_path), caption=raw_path.name, width="stretch")
    else:
        st.caption("No raw images in local workspace.")

    _section(f"Videos ({len(media_idx.videos)})")
    if media_idx.videos:
        for vid_path in media_idx.videos:
            st.video(str(vid_path))
            st.caption(vid_path.name)
    else:
        st.caption("No videos in local workspace.")


def _render_gallery_sku_detail(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    *,
    sku: str,
    enriched_index: dict,
    drive_folders: dict[str, str],
    shopify: _ShopifySession,
    review_store: ReviewStore,
) -> None:
    pull_key = f"search:{sku}"
    _pull_gallery_page_from_drive(
        cfg, sync, page_skus=[sku], drive_folders=drive_folders, pull_key=pull_key,
    )
    media_idx = index_sku_media(outputs_dir=cfg.outputs_dir, sku=sku)
    shop_prod, _ = lookup_shopify_product(shopify.products_by_sku, sku, review_store=review_store)
    review_rec = review_store.get_record(sku)
    drive_meta = scan_drive_metadata_for_skus(sync, [sku], drive_folders=drive_folders).get(sku) or {}
    enriched_row = enriched_index.get(sku) or {}
    readiness = gallery_readiness_light(
        media_idx=media_idx,
        drive_meta=drive_meta,
        shop_prod=shop_prod,
    )
    row = build_gallery_row(
        sku,
        cfg=cfg,
        enriched_row=enriched_row,
        media_idx=media_idx,
        drive_meta=drive_meta,
        shop_prod=shop_prod,
        review_rec=review_rec,
        readiness=readiness,
        drive_folders=set(drive_folders.keys()),
    )
    ref_paths = reference_paths_for_sku(cfg.base, sku) or _list_candidates(cfg.base.images_dir, sku)

    st.markdown(f"### `{sku}`")
    if row.title:
        st.caption(f"**{row.title}** · {row.category}")
    if row.description:
        st.caption(row.description)

    s1, s2, s3, s4 = st.columns(4)
    with s1:
        st.caption(
            f"**Local** raw {row.local_raw} · vid {row.local_videos} · "
            f"p1 {'✓' if row.local_has_p1 else '✗'} · p2 {'✓' if row.local_has_p2 else '✗'}"
        )
    with s2:
        st.caption(
            f"**Drive** raw {row.drive_raw} · vid {row.drive_videos} · "
            f"p1 {'✓' if row.drive_has_p1 else '✗'} · p2 {'✓' if row.drive_has_p2 else '✗'}"
        )
    with s3:
        st.caption(
            f"**Shopify** img {row.shopify_images} · vid {row.shopify_videos} · "
            f"{'linked' if row.on_shopify else 'missing'}"
        )
    with s4:
        edited = _gallery_edited().get(sku) or {}
        edited_slots = [s for s in ("prompt1", "prompt2") if edited.get(s) is not None]
        if edited_slots:
            st.caption(f"**Edited** ({', '.join(edited_slots)})")
        else:
            st.caption(f"Review: `{row.review_status}`")

    b1, b2 = st.columns(2)
    rec = review_rec
    title = row.title or str(rec.get("title") or "")
    category = row.category or title_case_category(str(rec.get("category") or ""))
    prompt1_text = str(rec.get("prompt1_text") or default_prompt_for_category(PROMPT_1, category))
    prompt2_text = str(rec.get("prompt2_text") or default_prompt_for_category(PROMPT_2, category))
    gen_ctx = product_generation_context(title=title, category=category)
    with b1:
        if st.button("Regen p1", key=f"gallery_search_rp1::{sku}", disabled=not ref_paths):
            _regenerate_prompt_for_sku(
                cfg, sync, service,
                sku=sku, slot="prompt1", prompt_text=prompt1_text, gen_ctx=gen_ctx,
                review_store=review_store, gallery_local_only=True,
            )
    with b2:
        if st.button("Regen p2", key=f"gallery_search_rp2::{sku}", disabled=not ref_paths):
            _regenerate_prompt_for_sku(
                cfg, sync, service,
                sku=sku, slot="prompt2", prompt_text=prompt2_text, gen_ctx=gen_ctx,
                review_store=review_store, gallery_local_only=True,
            )

    regen_msg = st.session_state.pop(f"gallery_regen_msg::{sku}", None)
    if regen_msg:
        st.success(regen_msg)

    _render_gallery_media_sections(media_idx, compact=False)

    with st.expander("Details", expanded=False):
        st.write(row.readiness)


def _render_gallery_card(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    *,
    row,
    media_idx,
    shopify: _ShopifySession,
    review_store: ReviewStore,
    ref_paths: list[Path],
) -> None:
    sku = row.sku
    rec = review_store.get_record(sku)
    shop_prod, _ = lookup_shopify_product(shopify.products_by_sku, sku, review_store=review_store)
    title = row.title or str(rec.get("title") or "")
    category = row.category or title_case_category(str(rec.get("category") or ""))
    description = row.description or str(rec.get("description") or "")
    tags = str(rec.get("tags") or "")
    prompt1_text = str(rec.get("prompt1_text") or default_prompt_for_category(PROMPT_1, category))
    prompt2_text = str(rec.get("prompt2_text") or default_prompt_for_category(PROMPT_2, category))
    product_id = row.product_id or str((shop_prod or {}).get("id") or "")
    gen_ctx = product_generation_context(title=title, category=category)

    h1, h2, h3 = st.columns([2, 1, 1])
    with h1:
        st.markdown(f"**`{sku}`**")
    with h2:
        st.caption(f"Review: `{row.review_status}`")
    with h3:
        edited = _gallery_edited().get(sku) or {}
        edited_slots = [s for s in ("prompt1", "prompt2") if edited.get(s) is not None]
        if edited_slots:
            st.caption(f"**Edited** ({', '.join(edited_slots)})")
        else:
            st.caption("Ready" if row.save_ready else "Not ready")

    st.caption(f"**{title}** · {category}")
    if row.description:
        st.caption(row.description[:200] + ("…" if len(row.description) > 200 else ""))

    s1, s2, s3, s4 = st.columns(4)
    with s1:
        st.caption(
            f"**Local** raw {row.local_raw} · vid {row.local_videos} · "
            f"p1 {'✓' if row.local_has_p1 else '✗'} · p2 {'✓' if row.local_has_p2 else '✗'}"
        )
    with s2:
        st.caption(
            f"**Drive** raw {row.drive_raw} · vid {row.drive_videos} · "
            f"p1 {'✓' if row.drive_has_p1 else '✗'} · p2 {'✓' if row.drive_has_p2 else '✗'}"
        )
    with s3:
        st.caption(
            f"**Shopify** img {row.shopify_images} · vid {row.shopify_videos} · "
            f"{'linked' if row.on_shopify else 'missing'}"
        )
    with s4:
        st.caption(f"**Sheet** {'✓' if row.in_sheet else '✗'}")

    b1, b2 = st.columns(2)
    with b1:
        if st.button("Regen p1", key=f"gallery_rp1::{sku}", disabled=not ref_paths):
            _regenerate_prompt_for_sku(
                cfg, sync, service,
                sku=sku, slot="prompt1", prompt_text=prompt1_text, gen_ctx=gen_ctx,
                review_store=review_store, gallery_local_only=True,
            )
    with b2:
        if st.button("Regen p2", key=f"gallery_rp2::{sku}", disabled=not ref_paths):
            _regenerate_prompt_for_sku(
                cfg, sync, service,
                sku=sku, slot="prompt2", prompt_text=prompt2_text, gen_ctx=gen_ctx,
                review_store=review_store, gallery_local_only=True,
            )

    regen_msg = st.session_state.pop(f"gallery_regen_msg::{sku}", None)
    if regen_msg:
        st.success(regen_msg)

    _render_gallery_media_sections(media_idx, compact=True)

    with st.expander("Details", expanded=False):
        st.write(row.readiness)
        last = st.session_state.get(f"gallery_last_save::{sku}")
        if last:
            st.json(last)


def _render_gallery(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    shopify: _ShopifySession,
) -> None:
    st.subheader("Gallery — final batch review")
    st.caption(
        f"Loads prompts + raw from Drive, regenerate locally in batch, then **Save edited** at the bottom "
        f"to push only changed prompt images (raw/videos untouched)."
    )

    review_store = _review_store(cfg)
    enriched_path = cfg.enriched_xlsx_path

    if not enriched_path.is_file():
        st.warning(f"Enriched stock file missing: `{enriched_path}`")
        if st.button("Rebuild enriched xlsx", type="primary", key="gallery_rebuild_enriched"):
            with st.spinner("Rebuilding enriched export..."):
                rebuild_enriched_xlsx(cfg, service, review_store=review_store)
            st.success(f"Rebuilt `{enriched_path}`")
            st.rerun()
        return

    enriched_index, headers = load_enriched_index(enriched_path)
    if not enriched_index:
        st.error("Enriched file has no SKU rows.")
        return

    req_cols = required_stock_columns(headers, enriched_index)
    all_complete = _cached_gallery_complete_skus(
        cfg, enriched_index, req_cols=req_cols, review_store=review_store,
    )

    search_col1, search_col2, search_col3 = st.columns([3, 1, 1])
    with search_col1:
        search_query = st.text_input(
            "Search SKU",
            key="gallery_search_input",
            placeholder="e.g. DIARFHW26074",
        )
    with search_col2:
        search_clicked = st.button("Search", type="primary", key="gallery_search_btn")
    with search_col3:
        clear_search = st.button("Clear", key="gallery_search_clear")

    if clear_search:
        st.session_state.pop(GALLERY_ACTIVE_SEARCH_KEY, None)
        st.session_state.pop("gallery_search_input", None)
        st.session_state.pop(GALLERY_PULL_KEY, None)
        st.rerun()

    if search_clicked:
        resolved = _resolve_gallery_search_sku(cfg, search_query, enriched_index=enriched_index)
        if resolved:
            st.session_state[GALLERY_ACTIVE_SEARCH_KEY] = resolved
            st.session_state.pop(GALLERY_PULL_KEY, None)
            st.rerun()
        else:
            st.session_state.pop(GALLERY_ACTIVE_SEARCH_KEY, None)
            st.error(f"No SKU found for `{search_query.strip()}`")

    active_search = st.session_state.get(GALLERY_ACTIVE_SEARCH_KEY)
    if active_search:
        if "gallery_drive_folders" not in st.session_state:
            st.session_state["gallery_drive_folders"] = sync.list_sku_folders(refresh=False)
        drive_folders = st.session_state["gallery_drive_folders"]
        _render_gallery_sku_detail(
            cfg, sync, service,
            sku=active_search,
            enriched_index=enriched_index,
            drive_folders=drive_folders,
            shopify=shopify,
            review_store=review_store,
        )
        _render_gallery_save_edited_footer(
            cfg, sync, page_skus=[active_search], shopify=shopify, review_store=review_store,
        )
        return

    ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([1, 1, 1, 2])
    with ctrl1:
        page_size = st.selectbox("Page size", [20, 30, 50], index=0, key="gallery_page_size")
    categories = ["ALL"] + distinct_categories(enriched_index, all_complete)
    with ctrl2:
        cat_filter = st.selectbox("Category", categories, key="gallery_category")
    filtered = filter_by_category(all_complete, enriched_index, cat_filter)
    page = int(st.session_state.get("gallery_page") or 1)
    page_skus, total_pages, total_count = paginate(filtered, page=page, page_size=page_size)

    with ctrl3:
        if st.button("Refresh gallery data", key="gallery_refresh"):
            st.session_state.pop("gallery_drive_folders", None)
            _invalidate_gallery_caches()
            st.session_state["gallery_page"] = 1
            st.rerun()
    with ctrl4:
        st.caption(f"**{len(page_skus)}** on page · **{total_count}** complete SKUs · filter: `{cat_filter}`")

    nav1, nav2, nav3 = st.columns([1, 2, 1])
    with nav1:
        if st.button("← Prev", disabled=page <= 1, key="gallery_prev"):
            st.session_state["gallery_page"] = max(1, page - 1)
            st.rerun()
    with nav2:
        st.markdown(f"<div style='text-align:center'>Page **{page}** of **{total_pages or 1}**</div>", unsafe_allow_html=True)
    with nav3:
        if st.button("Next →", disabled=page >= (total_pages or 1), key="gallery_next"):
            st.session_state["gallery_page"] = min(total_pages or 1, page + 1)
            st.rerun()

    if not page_skus:
        st.info("No complete SKUs match the current filter.")
        return

    if "gallery_drive_folders" not in st.session_state:
        st.session_state["gallery_drive_folders"] = sync.list_sku_folders(refresh=False)
    drive_folders = st.session_state["gallery_drive_folders"]
    page_cache_key = _gallery_page_cache_key(page, cat_filter, page_skus)
    _pull_gallery_page_from_drive(
        cfg, sync, page_skus=page_skus, drive_folders=drive_folders, pull_key=page_cache_key,
    )
    drive_meta_by_sku = _cached_drive_meta_for_page(
        sync, page_skus=page_skus, drive_folders=drive_folders, cache_key=page_cache_key,
    )

    for sku in page_skus:
        enriched_row = enriched_index.get(sku) or {}
        media_idx = index_sku_media(outputs_dir=cfg.outputs_dir, sku=sku)
        shop_prod, _ = lookup_shopify_product(shopify.products_by_sku, sku, review_store=review_store)
        review_rec = review_store.get_record(sku)
        readiness = gallery_readiness_light(
            media_idx=media_idx,
            drive_meta=drive_meta_by_sku.get(sku),
            shop_prod=shop_prod,
        )
        row = build_gallery_row(
            sku,
            cfg=cfg,
            enriched_row=enriched_row,
            media_idx=media_idx,
            drive_meta=drive_meta_by_sku.get(sku),
            shop_prod=shop_prod,
            review_rec=review_rec,
            readiness=readiness,
            drive_folders=set(drive_folders.keys()),
        )
        ref_paths = reference_paths_for_sku(cfg.base, sku) or _list_candidates(cfg.base.images_dir, sku)
        with st.container(border=True):
            _render_gallery_card(
                cfg, sync, service,
                row=row,
                media_idx=media_idx,
                shopify=shopify,
                review_store=review_store,
                ref_paths=ref_paths,
            )

    _render_gallery_save_edited_footer(
        cfg, sync, page_skus=page_skus, shopify=shopify, review_store=review_store,
    )


def _render_review_queue(cfg: DriveReviewConfig, sync: DriveOutputsSync, service, shopify: _ShopifySession, leases) -> None:
    st.subheader("Review Queue")
    skus = sync.list_local_sku_dirs()
    review_store = _review_store(cfg)
    sync.sync_review_state_local()
    try:
        resolve_stock_path(cfg, service)
    except (FileNotFoundError, RuntimeError) as e:
        st.error(str(e))
        return

    status_filter = st.selectbox(
        "Review status filter",
        ["ALL", "pending_review", "approved", "uploaded", "verified", "failed"],
        index=1,
    )
    search = st.text_input("Search SKU")

    filtered = []
    for sku in skus:
        if search and search.lower() not in sku.lower():
            continue
        status = str(review_store.get_record(sku).get("review_status") or "pending_review")
        if status_filter != "ALL" and status != status_filter:
            continue
        filtered.append(sku)

    st.caption(f"{len(filtered)} SKU folder(s) in local `{cfg.outputs_dir}` (changes push to Drive on save)")

    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Lease next SKU", type="primary"):
            want = {"pending_review", "failed"} if status_filter in {"ALL", "pending_review"} else {status_filter}
            if status_filter == "ALL":
                want = None
            sku = _acquire_next_sku(
                cfg, sync, leases, skus=filtered, filter_status=want, ready_to_save_only=True,
            )
            if sku:
                st.session_state.leased_sku = sku
                st.rerun()
            else:
                st.warning("No available SKU ready to save.")
    with c2:
        if st.button("Refresh folder list"):
            st.rerun()
    with c3:
        if leases and st.button("Cleanup stale leases"):
            n = leases.cleanup_stale()
            st.success(f"Removed {n} stale lease(s).")

    if leases:
        active = leases.list_active()
        with st.expander(f"Active leases ({len(active)})", expanded=False):
            st.dataframe(
                [{"sku": l.key, "machine": l.machine_id, "tab": l.tab_id, "holder": l.holder_id[:8]} for l in active],
                width="stretch",
            )

    leased = st.session_state.get("leased_sku")
    if leased and not sku_ready_to_save(cfg, leased):
        if leases:
            leases.release(leased, _session_holder_id())
        leased = None
        st.session_state.pop("leased_sku", None)
    if not leased:
        auto = _select_next_open_sku(cfg, sync, leases, skus=filtered)
        if auto:
            st.session_state.leased_sku = auto

    options = [""] + filtered
    leased = st.session_state.get("leased_sku")
    pick_idx = options.index(leased) if leased in options else 0
    if leased and leased not in filtered:
        st.caption(f"Showing `{leased}` (status no longer matches filter). Change filter to ALL to see it in the list.")
    pick = st.selectbox("Open SKU", options, index=pick_idx)
    if pick:
        if pick != leased:
            st.session_state.leased_sku = pick
            if leases:
                holder = _session_holder_id()
                leases.try_acquire(
                    pick,
                    holder_id=holder,
                    machine_id=cfg.machine_id or _machine_id(),
                    tab_id=_tab_id(),
                )
    sku = st.session_state.get("leased_sku") or (pick if pick else None)
    if sku:
        _render_review_sku(
            cfg,
            sync,
            service,
            sku=sku,
            shopify=shopify,
            leases=leases,
            queue_skus=skus,
        )


def main() -> None:
    setup_drive_review_logging()
    cfg = _load_cfg()
    st.set_page_config(page_title="Drive Review", layout="wide")
    st.title("Drive-Backed SKU Review")
    st.caption(
        f"Local workspace: `{cfg.outputs_dir}` | Stock: Google Sheet `{cfg.stock_spreadsheet_id}` "
        f"| Push SKU folders + sheet updates on Shopify/Drive sync only."
    )

    cfg.outputs_dir.mkdir(parents=True, exist_ok=True)
    cfg.drive_credentials_dir.mkdir(parents=True, exist_ok=True)

    shopify_session = _ensure_shopify_session(cfg)

    with st.sidebar:
        _drive_sidebar(cfg)
        st.divider()
        shopify_session = _shopify_sidebar(cfg, shopify_session)
        st.divider()
        st.subheader("Firestore")
        st.caption(f"Project: `{cfg.firestore_project_id or '(not set)'}`")
        leases = _lease_manager(cfg) if cfg.firestore_project_id else None
        if not cfg.firestore_project_id:
            st.warning("Set `FIRESTORE_PROJECT_ID` in `.env` or `firestore_project_id` in config.yaml")

    secret = cfg.drive_credentials_dir / "client_secret.json"
    if not secret.exists():
        st.info("Upload Google OAuth client JSON in the sidebar to begin.")
        return

    try:
        service = _drive_service(cfg, write=True)
        sync = DriveOutputsSync(cfg, service)
        refresh_stock_sheet(cfg, service)
    except Exception as e:
        st.error("Connect Google Drive first.")
        st.exception(e)
        return

    tab_cleanup, tab_review, tab_gallery, tab_tools = st.tabs(
        ["Typo Cleanup", "Review Queue", "Gallery", "Tools"]
    )
    with tab_cleanup:
        _render_cleanup(cfg, sync, service)
    with tab_review:
        _render_review_queue(cfg, sync, service, shopify_session, leases)
    with tab_gallery:
        _render_gallery(cfg, sync, service, shopify_session)
    with tab_tools:
        st.subheader("Tools")
        st.caption(
            f"Stock sheet: [Google Sheets](https://docs.google.com/spreadsheets/d/{cfg.stock_spreadsheet_id}/edit) "
            f"(cached at `{cfg.local_stock_path}`) | Outputs: `{cfg.outputs_dir}`"
        )
        if st.button("Refresh stock sheet from Drive"):
            try:
                path = refresh_stock_sheet(cfg, service, force=True)
                st.success(f"Refreshed stock cache: `{path}`")
            except Exception as e:
                st.error(str(e))
        if st.button("Tally everything", type="primary"):
            progress = st.progress(0, text="Starting tally...")
            status = st.empty()
            try:
                tally = tally_drive_vs_local(
                    cfg,
                    sync,
                    shopify_client=shopify_session.client,
                    progress=_progress_callback(progress, status),
                )
                json_path, md_path = write_tally_report(tally, cfg.outputs_dir)
                sync.push_file_to_outputs_root(json_path)
                sync.push_file_to_outputs_root(md_path)
                progress.progress(1.0, text="Tally complete")
                s = tally.get("summary") or {}
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Local folders", s.get("local_sku_folders", 0))
                c2.metric("Drive folders", s.get("drive_sku_folders", 0))
                c3.metric("In both", s.get("in_both", 0))
                c4.metric("Mismatches", s.get("content_mismatches", 0))
                st.success(
                    f"Tally done in {s.get('elapsed_seconds', '?')}s — "
                    f"local_only={s.get('local_only', 0)}, drive_only={s.get('drive_only', 0)}, "
                    f"not_in_stock={s.get('local_not_in_stock', 0)}, needs_push={s.get('needs_push_to_drive', 0)}"
                )
                st.caption(f"Reports: `{json_path.name}`, `{md_path.name}` (uploaded to Drive)")
                with st.expander("Local only (not on Drive)"):
                    st.write(tally.get("local_only") or [])
                with st.expander("Drive only (not local)"):
                    st.write(tally.get("drive_only") or [])
                with st.expander("Not in Stock.xlsx (local folders)"):
                    st.write(tally.get("not_in_stock_local") or [])
                with st.expander("Needs push to Drive"):
                    st.write(tally.get("needs_push_to_drive") or [])
                with st.expander("Full summary"):
                    st.json(s)
            except Exception as e:
                st.error(str(e))
                st.exception(e)
        if st.button("Audit Drive prompt1+prompt2", type="primary"):
            progress = st.progress(0, text="Scanning Drive SKU folders...")
            status = st.empty()
            try:
                audit = audit_drive_prompt_images(
                    cfg,
                    sync,
                    progress=_progress_callback(progress, status),
                )
                json_path, md_path = write_prompt_audit_report(audit, cfg.outputs_dir)
                sync.push_file_to_outputs_root(json_path)
                sync.push_file_to_outputs_root(md_path)
                progress.progress(1.0, text="Audit complete")
                s = audit.get("summary") or {}
                st.success(
                    f"Drive audit done in {s.get('elapsed_seconds', '?')}s — "
                    f"folders={s.get('drive_sku_folders', 0)}, "
                    f"complete={s.get('complete_both_prompts', 0)}, "
                    f"missing_p1={s.get('missing_prompt1', 0)}, "
                    f"missing_p2={s.get('missing_prompt2', 0)}, "
                    f"missing_both={s.get('missing_both', 0)}"
                )
                st.caption(f"Reports: `{json_path.name}`, `{md_path.name}` (uploaded to Drive)")
                with st.expander("Missing prompt1"):
                    st.write(audit.get("missing_prompt1") or [])
                with st.expander("Missing prompt2"):
                    st.write(audit.get("missing_prompt2") or [])
                with st.expander("Missing both"):
                    st.write(audit.get("missing_both") or [])
            except Exception as e:
                st.error(str(e))
                st.exception(e)
        if st.button("Rebuild + upload XLSX to Drive"):
            out = rebuild_and_replace(cfg, service, review_store=_review_store(cfg))
            st.json(out)
        if st.button("Push review_state.json to Drive"):
            sync.sync_review_state_push()
            st.success("Pushed review_state.json")
        sku_push = st.text_input("Push single SKU folder to Drive", placeholder="e.g. DIAEFHW26021")
        if st.button("Push SKU to Drive", disabled=not sku_push.strip()):
            try:
                sync.ensure_local_sku(sku_push.strip())
                result = sync.push_sku(sku_push.strip())
                st.success(f"Pushed {len(result.get('uploaded') or [])} file(s).")
                st.json(result)
            except Exception as e:
                st.error(str(e))


if __name__ == "__main__":
    main()
