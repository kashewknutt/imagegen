"""Typo-folder audit and cleanup: local outputs/ workspace, push changes to Drive only."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.drive_outputs_sync import DriveOutputsSync
from src.drive_review_config import DriveReviewConfig
from src.drive_review_log import get_logger
from src.drive_stock_sync import rebuild_and_replace, resolve_stock_path
from src.review_store import ReviewStore
from src.typo_sku_cleanup import (
    AUDIT_JSON_NAME,
    AUDIT_MD_NAME,
    apply_typo_cleanup,
    audit_typo_folders,
    write_audit_report,
)


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def audit_drive_typo_folders(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    *,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Audit using local outputs/ and Stock.xlsx — no Drive downloads."""
    log = get_logger()
    t0 = time.monotonic()
    log.info("Auditing typo folders from local %s ...", cfg.outputs_dir)
    resolve_stock_path(cfg)
    audit = audit_typo_folders(
        outputs_dir=cfg.outputs_dir,
        xlsx_path=cfg.local_stock_path,
        review_store_path=cfg.review_state_path if cfg.review_state_path.is_file() else None,
    )
    audit["scan_mode"] = "local_outputs"
    audit["elapsed_seconds"] = round(time.monotonic() - t0, 1)
    summary = audit.get("summary") or {}
    log.info(
        "Audit complete in %.1fs: delete_safe=%s migrate=%s unresolved=%s",
        audit["elapsed_seconds"],
        summary.get("delete_safe", 0),
        summary.get("migrate", 0),
        summary.get("unresolved", 0),
    )
    return audit


def apply_drive_typo_cleanup(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    *,
    dry_run: bool = False,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    log = get_logger()
    resolve_stock_path(cfg)

    if dry_run:
        log.info("Dry-run: local audit only (no changes).")
        audit = audit_drive_typo_folders(cfg, sync, service, progress=progress)
        return {
            "dry_run": True,
            "audit_summary": audit.get("summary"),
            "entries": audit.get("entries"),
        }

    log.info("Applying typo cleanup on local %s ...", cfg.outputs_dir)
    results = apply_typo_cleanup(
        outputs_dir=cfg.outputs_dir,
        outputsv2_dir=cfg.base.outputsv2_dir,
        xlsx_path=cfg.local_stock_path,
        review_store_path=cfg.review_state_path,
        dry_run=False,
    )

    review_store = ReviewStore(cfg.review_state_path)

    deletions = results.get("deletions") or []
    orphan_deletions = results.get("orphan_deletions") or []
    migrations = results.get("migrations") or []
    push_skus: set[str] = set()

    for i, entry in enumerate(orphan_deletions, start=1):
        if not entry.get("deleted"):
            continue
        typo_sku = str(entry.get("typo_sku") or "")
        if progress:
            progress(f"Drive delete orphan {typo_sku}", i, len(orphan_deletions))
        log.info("Deleting orphan folder %s on Drive only", typo_sku)
        sync.delete_sku_folder_remote(typo_sku)

    for i, entry in enumerate(deletions, start=1):
        if not entry.get("deleted"):
            continue
        typo_sku = str(entry.get("typo_sku") or "")
        if progress:
            progress(f"Drive delete {typo_sku}", i, len(deletions))
        log.info("Deleting typo folder %s on Drive only", typo_sku)
        sync.delete_sku_folder_remote(typo_sku)

    for i, entry in enumerate(migrations, start=1):
        if entry.get("skipped"):
            continue
        real_sku = str(entry.get("real_sku") or "")
        typo_sku = str(entry.get("typo_sku") or "")
        if progress:
            progress(f"Push {real_sku} to Drive", i, len(migrations))
        if real_sku:
            log.info("Pushing migrated SKU %s to Drive...", real_sku)
            sync.push_sku(real_sku)
            push_skus.add(real_sku)
        if typo_sku:
            log.info("Deleting typo folder %s on Drive only", typo_sku)
            sync.delete_sku_folder_remote(typo_sku)

    log.info("Pushing review_state.json to Drive...")
    sync.sync_review_state_push()
    log.info("Rebuilding XLSX from local data and uploading to Drive...")
    rebuild_and_replace(cfg, service, review_store=review_store, replace_source=True)

    audit = audit_drive_typo_folders(cfg, sync, service)
    json_path, md_path = write_audit_report(audit, cfg.outputs_dir)
    for name in (AUDIT_JSON_NAME, AUDIT_MD_NAME, "typo_sku_cleanup_results.json"):
        local = cfg.outputs_dir / name
        if local.is_file():
            log.info("Uploading %s to Drive outputs root", name)
            sync.push_file_to_outputs_root(local, name)

    results["drive_sync_at_utc"] = _now_utc()
    results["audit_paths"] = [str(json_path), str(md_path)]
    results["pushed_skus"] = sorted(push_skus)
    log.info("Apply cleanup complete. Pushed %d SKU(s) to Drive.", len(push_skus))
    return results
