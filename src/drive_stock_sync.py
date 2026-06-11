"""Read stock from Google Sheets on Drive; write back only on Shopify/Drive sync."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_stock_export import build_export
from src.drive_client import XLSX_MIME, export_drive_file_to_cache, get_file_metadata, upload_or_update_file
from src.drive_review_config import DriveReviewConfig
from src.drive_review_log import get_logger
from src.review_store import ReviewStore
from src.title_store import TitleStore

STOCK_CACHE_FILENAME = "stock_sheet.xlsx"
STOCK_META_FILENAME = "stock_sheet_meta.json"


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def stock_cache_path(cfg: DriveReviewConfig) -> Path:
    return cfg.drive_cache_dir / STOCK_CACHE_FILENAME


def stock_meta_path(cfg: DriveReviewConfig) -> Path:
    return cfg.drive_cache_dir / STOCK_META_FILENAME


def _load_stock_meta(cfg: DriveReviewConfig) -> dict[str, Any]:
    path = stock_meta_path(cfg)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_stock_meta(cfg: DriveReviewConfig, *, file_id: str, modified_time: str, name: str = "") -> None:
    cfg.drive_cache_dir.mkdir(parents=True, exist_ok=True)
    stock_meta_path(cfg).write_text(
        json.dumps(
            {
                "file_id": file_id,
                "modified_time": modified_time,
                "name": name,
                "cached_at_utc": _now_utc(),
                "cache_path": str(stock_cache_path(cfg)),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def refresh_stock_sheet(cfg: DriveReviewConfig, service, *, force: bool = False) -> Path:
    """
    Export the canonical Google Sheet to a local cache file.
    Skips download when Drive modifiedTime matches cached metadata (unless force=True).
    """
    log = get_logger()
    file_id = cfg.stock_spreadsheet_id
    if not file_id:
        raise RuntimeError("drive_xlsx_file_id / stock spreadsheet ID is not configured")

    cache = stock_cache_path(cfg)
    meta = _load_stock_meta(cfg)
    remote = get_file_metadata(service=service, file_id=file_id)
    remote_mtime = str(remote.get("modifiedTime") or "")
    remote_name = str(remote.get("name") or "")

    if (
        not force
        and cache.is_file()
        and cache.stat().st_size > 0
        and meta.get("file_id") == file_id
        and meta.get("modified_time") == remote_mtime
    ):
        log.info("Stock sheet cache is current (%s)", remote_name or file_id)
        return cache

    log.info("Exporting stock sheet from Drive: %s (%s)", remote_name or file_id, file_id)
    export_drive_file_to_cache(service=service, file_id=file_id, cache_path=cache)
    _save_stock_meta(cfg, file_id=file_id, modified_time=remote_mtime, name=remote_name)
    log.info("Stock sheet cached at %s", cache)
    return cache


def resolve_stock_path(cfg: DriveReviewConfig, service, *, force_refresh: bool = False) -> Path:
    """Return cached export of the Google Sheet (refreshing from Drive when stale)."""
    if service is None:
        cache = stock_cache_path(cfg)
        if cache.is_file() and cache.stat().st_size > 0:
            return cache
        raise RuntimeError("Stock sheet not cached — connect Google Drive first.")
    return refresh_stock_sheet(cfg, service, force=force_refresh)


def rebuild_enriched_xlsx(
    cfg: DriveReviewConfig,
    service,
    *,
    review_store: ReviewStore | None = None,
) -> Path:
    review_store = review_store or ReviewStore(cfg.review_state_path)
    title_store = TitleStore(cfg.outputs_dir / "title_gen_state.json")
    product_names = review_store.product_names()
    for sku, rec in review_store.all_records().items():
        title = str(rec.get("title") or "").strip()
        if title and sku not in product_names:
            product_names[sku] = title

    prompt2_versions: dict[str, int | None] = {}
    for sku, rec in review_store.all_records().items():
        p2 = rec.get("approved_prompt2_version")
        prompt2_versions[sku] = int(p2) if p2 is not None else None

    products_path = Path("products_export_1.xlsx")
    if not products_path.is_file():
        products_path = cfg.base.xlsx_path.parent / "products_export_1.xlsx"

    stock_path = resolve_stock_path(cfg, service)
    build_export(
        stock_path=stock_path,
        products_path=products_path,
        outputs_dir=cfg.outputs_dir,
        output_path=cfg.enriched_xlsx_path,
        stock_sheets=cfg.base.xlsx_sheets,
        product_names=product_names,
        images_dir=cfg.base.images_dir,
        prompt2_versions=prompt2_versions,
    )
    return cfg.enriched_xlsx_path


def replace_drive_spreadsheet(
    cfg: DriveReviewConfig,
    service,
    *,
    local_path: Path | None = None,
) -> dict[str, Any]:
    local_path = local_path or cfg.enriched_xlsx_path
    if not local_path.is_file():
        raise FileNotFoundError(local_path)
    result = upload_or_update_file(
        service=service,
        local_path=local_path,
        parent_id="",
        name=local_path.name,
        file_id=cfg.stock_spreadsheet_id,
        mime_type=XLSX_MIME,
    )
    # Keep read cache aligned with what we just uploaded.
    cache = stock_cache_path(cfg)
    shutil.copy2(local_path, cache)
    _save_stock_meta(
        cfg,
        file_id=cfg.stock_spreadsheet_id,
        modified_time=result.modified_time,
        name=result.name,
    )
    return {"file_id": result.id, "name": result.name, "local_path": str(local_path)}


def rebuild_and_replace(
    cfg: DriveReviewConfig,
    service,
    *,
    review_store: ReviewStore | None = None,
    replace_source: bool = True,
) -> dict[str, Any]:
    log = get_logger()
    stock_source = resolve_stock_path(cfg, service)
    log.info("Rebuilding enriched export from Google Sheet cache + local outputs/ ...")
    enriched = rebuild_enriched_xlsx(cfg, service, review_store=review_store)
    out: dict[str, Any] = {
        "enriched_path": str(enriched),
        "stock_source": str(stock_source),
        "stock_spreadsheet_id": cfg.stock_spreadsheet_id,
    }
    if replace_source:
        log.info("Uploading enriched sheet to Google Sheets/Drive (%s)...", cfg.stock_spreadsheet_id)
        out["drive_replace"] = replace_drive_spreadsheet(cfg, service, local_path=enriched)
    log.info("Sheet rebuild and replace complete.")
    return out
