#!/usr/bin/env python3
"""Drive-backed SKU review app with Firestore leasing and tri-system sync."""
from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from pathlib import Path

import streamlit as st
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
from src.drive_stock_sync import rebuild_and_replace, refresh_stock_sheet, resolve_stock_path
from src.drive_sync_orchestrator import (
    sync_after_regenerate,
    sync_save_everything,
    validate_local_workspace,
)
from src.drive_review_log import setup_drive_review_logging
from src.drive_typo_cleanup import apply_drive_typo_cleanup, audit_drive_typo_folders
from src.firestore_leases import FirestoreLeaseManager
from src.genai_client import GenAiImageClient
from src.media_workspace import index_sku_media, refresh_manifest
from src.name_group import base_key_from_path
from src.pipeline import PROMPT_1, PROMPT_2, generate_to_workspace, prepare_work_item_for_path
from src.review_autofill import autofill_review_record
from src.review_store import ReviewStore
from src.title_store import TitleStore
from src.drive_outputs_tally import tally_drive_vs_local, write_tally_report
from src.shopify_client import ShopifyClient
from src.shopify_env import load_shopify_env, shopify_client_from_env
from src.shopify_media_sync import images_for_sku, media_paths_for_sku
from src.typo_sku_cleanup import write_audit_report
from src.xlsx_ingest import index_by_sku, iter_rows

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


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
            model=cfg.base.model,
        )
    st.session_state[key] = result
    if result.get("updated"):
        st.rerun()


def _ensure_genai(cfg: DriveReviewConfig) -> GenAiImageClient:
    if "genai_client" not in st.session_state:
        st.session_state.genai_client = GenAiImageClient(
            model=cfg.base.model,
            min_seconds_between_requests=cfg.base.min_seconds_between_requests,
        )
    return st.session_state.genai_client


def _shopify_sidebar(cfg: DriveReviewConfig) -> ShopifyClient | None:
    st.subheader("Shopify")
    env = load_shopify_env()
    if not env.configured:
        st.warning("Set `SHOPIFY_*` variables in `.env` (see `.env.example`).")
        return None
    st.caption(f"Shop: `{env.shop_domain}`")
    st.caption(f"API: `{env.api_version}`")
    try:
        client = shopify_client_from_env(cfg.outputs_dir)
    except Exception as e:
        st.error(str(e))
        return None
    if st.button("Test Shopify connection"):
        try:
            name = client.ping()
            st.success(f"Connected: {name}")
        except Exception as e:
            st.error(str(e))
    return client


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


def _shopify_product_for_sku(client: ShopifyClient, sku: str) -> dict | None:
    try:
        result = client.list_products(first=5, query=f"sku:{sku}")
        for prod in result.get("products") or []:
            skus = prod.get("skus") or []
            if sku in skus or str(prod.get("sku") or "") == sku:
                return prod
    except Exception:
        return None
    return None


def _acquire_next_sku(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    leases: FirestoreLeaseManager | None,
    *,
    skus: list[str],
    filter_status: set[str] | None = None,
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
        if leases is None:
            return sku
        lease = leases.try_acquire(sku, holder_id=holder, machine_id=machine, tab_id=tab)
        if lease:
            st.session_state.leased_sku = sku
            return sku
    return None


def _progress_callback(progress_bar, status_text):
    def _cb(message: str, current: int, total: int) -> None:
        pct = current / total if total else 0.0
        progress_bar.progress(min(1.0, pct), text=f"{message} ({current}/{total})")
        status_text.caption(f"{message} — {current}/{total}")
    return _cb


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
    shopify_client: ShopifyClient | None,
    leases: FirestoreLeaseManager | None,
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
    shop_prod = _shopify_product_for_sku(shopify_client, sku) if shopify_client else None
    _ensure_review_autofill(cfg, service, sku=sku, review_store=review_store, media_idx=media_idx, shop_prod=shop_prod)

    rec = review_store.get_record(sku)
    title_store = _title_store(cfg)
    title_rec = title_store.get(sku)
    default_title = (
        str(rec.get("title") or "").strip()
        or str(title_rec.get("new_title") or title_rec.get("generated_title") or "").strip()
        or str((shop_prod or {}).get("title") or "").strip()
    )
    default_category = (
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
    if shop_prod:
        st.caption(f"On Shopify: `{shop_prod.get('title')}` ({product_id})")
    else:
        st.caption("Not found on Shopify (or not connected).")

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
    prompt1_text = st.text_area("Prompt1", value=str(rec.get("prompt1_text") or PROMPT_1), height=120, key=f"p1text::{sku}")
    prompt2_text = st.text_area("Prompt2", value=str(rec.get("prompt2_text") or PROMPT_2), height=120, key=f"p2text::{sku}")

    ref_path = media_idx.raw_images[0] if media_idx.raw_images else None
    if ref_path is None:
        refs = _list_candidates(cfg.base.images_dir, sku)
        ref_path = refs[0] if refs else None

    gen = _ensure_genai(cfg)
    has_both_prompts = bool(media_idx.prompt1_versions and media_idx.prompt2_versions)
    g1, g2 = st.columns(2)
    with g1:
        if st.button("Regenerate prompt1", disabled=ref_path is None, key=f"rp1::{sku}"):
            work = prepare_work_item_for_path(cfg.base, sku, ref_path)
            out_path, _ = generate_to_workspace(
                cfg.base, gen, work, prompt_slot="prompt1", prompt_override=prompt1_text,
                extra_context=f"Title: {title}\nCategory: {category}",
            )
            result = sync_after_regenerate(cfg, sync, service, sku, prompt_slot="prompt1", review_store=review_store)
            st.success(f"Saved {out_path.name}")
            st.json(result)
            st.rerun()
    with g2:
        if st.button("Regenerate prompt2", disabled=ref_path is None, key=f"rp2::{sku}"):
            work = prepare_work_item_for_path(cfg.base, sku, ref_path)
            out_path, _ = generate_to_workspace(
                cfg.base, gen, work, prompt_slot="prompt2", prompt_override=prompt2_text,
                extra_context=f"Title: {title}\nCategory: {category}",
            )
            result = sync_after_regenerate(cfg, sync, service, sku, prompt_slot="prompt2", review_store=review_store)
            st.success(f"Saved {out_path.name}")
            st.json(result)
            st.rerun()

    save_help = (
        "Saves title/category/description/tags; uploads latest prompt1+prompt2 to Drive + Sheet. "
        "Skips raw/video on Drive if already there. Shopify gets generated images only."
    )
    if shopify_client and product_id:
        save_help += " Replaces Shopify product images with latest prompt1+prompt2."
    elif shopify_client:
        save_help += " (Shopify skipped — no product linked yet)"
    if st.button(
        "Save everything",
        type="primary",
        disabled=not has_both_prompts,
        key=f"save_all::{sku}",
        help=save_help,
    ):
        with st.spinner("Saving to Drive, Google Sheet, and Shopify..."):
            result = sync_save_everything(
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
                handle=str(rec.get("handle") or (shop_prod or {}).get("handle") or ""),
                shopify_client=shopify_client,
                shop_prod=shop_prod,
                review_store=review_store,
            )
        media_check = (result.drive_push or {}).get("media_check") or {}
        skipped = (result.drive_push or {}).get("skipped_existing_raw_videos") or []
        if media_check:
            st.caption(
                f"Drive check — videos: {len(media_check.get('videos_on_drive') or [])}/"
                f"{len(media_check.get('local_videos') or [])} on Drive, "
                f"raw: {len(media_check.get('raw_on_drive') or [])}/"
                f"{len(media_check.get('local_raw') or [])} on Drive"
                + (f" (skipped {len(skipped)} upload(s))" if skipped else "")
            )
        if result.errors:
            st.warning("Saved with warnings:")
            for err in result.errors:
                st.caption(f"• {err}")
        else:
            st.success("Saved everything — Drive, Google Sheet" + (", and Shopify" if result.shopify else "."))
        st.json(
            {
                "drive_push": result.drive_push,
                "review_state_pushed": result.review_state_pushed,
                "xlsx_replaced": result.xlsx_replaced,
                "shopify": result.shopify,
                "errors": result.errors,
            }
        )
        st.rerun()
    if not has_both_prompts:
        st.caption("Generate both prompt1 and prompt2 before Save everything.")

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


def _render_review_queue(cfg: DriveReviewConfig, sync: DriveOutputsSync, service, shopify_client, leases) -> None:
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
            sku = _acquire_next_sku(cfg, sync, leases, skus=filtered, filter_status=want)
            if sku:
                st.session_state.leased_sku = sku
                st.rerun()
            else:
                st.warning("No available SKU to lease.")
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

    pick = st.selectbox("Open SKU", [""] + filtered, index=0)
    sku = st.session_state.get("leased_sku") or (pick if pick else None)
    if sku:
        _render_review_sku(cfg, sync, service, sku=sku, shopify_client=shopify_client, leases=leases)


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

    with st.sidebar:
        _drive_sidebar(cfg)
        st.divider()
        shopify_client = _shopify_sidebar(cfg)
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

    tab_cleanup, tab_review, tab_tools = st.tabs(["Typo Cleanup", "Review Queue", "Tools"])
    with tab_cleanup:
        _render_cleanup(cfg, sync, service)
    with tab_review:
        _render_review_queue(cfg, sync, service, shopify_client, leases)
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
                    shopify_client=shopify_client,
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
