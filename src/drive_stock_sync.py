"""Rebuild local stock export and upload to Drive (no bulk downloads)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from build_stock_export import build_export
from src.drive_client import upload_or_update_file
from src.drive_review_config import DriveReviewConfig
from src.drive_review_log import get_logger
from src.review_store import ReviewStore
from src.title_store import TitleStore


def resolve_stock_path(cfg: DriveReviewConfig) -> Path:
    """Use local Stock.xlsx — same data as Drive-hosted spreadsheet."""
    path = cfg.local_stock_path
    if not path.is_file():
        raise FileNotFoundError(f"Local Stock.xlsx not found: {path}")
    return path


def rebuild_enriched_xlsx(
    cfg: DriveReviewConfig,
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

    stock_path = resolve_stock_path(cfg)
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


def replace_drive_xlsx(cfg: DriveReviewConfig, service, *, local_path: Path | None = None) -> dict[str, Any]:
    local_path = local_path or cfg.enriched_xlsx_path
    if not local_path.is_file():
        raise FileNotFoundError(local_path)
    result = upload_or_update_file(
        service=service,
        local_path=local_path,
        parent_id="",
        name=local_path.name,
        file_id=cfg.drive_xlsx_file_id,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
    log.info("Rebuilding stock_enriched.xlsx from local outputs/ + %s...", cfg.local_stock_path)
    enriched = rebuild_enriched_xlsx(cfg, review_store=review_store)
    out: dict[str, Any] = {"enriched_path": str(enriched), "stock_source": str(resolve_stock_path(cfg))}
    if replace_source:
        log.info("Uploading rebuilt XLSX to Drive (file %s)...", cfg.drive_xlsx_file_id)
        out["drive_replace"] = replace_drive_xlsx(cfg, service, local_path=enriched)
    log.info("XLSX rebuild and replace complete.")
    return out
