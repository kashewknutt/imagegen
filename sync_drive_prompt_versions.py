#!/usr/bin/env python3
"""
Sync local latest prompt1/prompt2 to Drive + Shopify; validate enriched XLSX thumbnail paths.

Local outputs/{SKU}/ is the source of truth for latest generated versions.

Phase 1 — Drive:
  For each local SKU, ensure Drive has only the latest prompt1/prompt2 files.
  Prune stale versions and upload missing latest files.

Phase 2 — Shopify:
  Compare local latest versions to review_store approved versions (last upload).
  Replace prompt1/prompt2 images on Shopify when stale (raw/videos untouched).

Phase 3 — Enriched XLSX:
  Validate ``thumbnail image path`` in local stock_enriched.xlsx and the Drive sheet
  against expected ``{SKU}/prompt2_vN.ext`` from local latest prompt2.

Usage:
  python sync_drive_prompt_versions.py --dry-run
  python sync_drive_prompt_versions.py
  python sync_drive_prompt_versions.py --sku DIARFHW26074
  python sync_drive_prompt_versions.py --skip-shopify
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.drive_client import get_drive_service
from src.drive_outputs_sync import DriveOutputsSync
from src.drive_prompt_version_sync import run_prompt_version_sync, write_sync_report
from src.drive_review_config import load_drive_review_config
from src.review_store import ReviewStore
from src.shopify_env import ensure_shopify_connection
from src.shopify_product_dedup import shopify_products_by_sku

log = logging.getLogger("sync_prompt_versions")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Sync latest prompt versions to Drive, Shopify, and XLSX")
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not upload or replace")
    parser.add_argument("--sku", action="append", help="Limit to SKU(s)")
    parser.add_argument("--skip-drive", action="store_true")
    parser.add_argument("--skip-shopify", action="store_true")
    parser.add_argument("--skip-xlsx", action="store_true")
    args = parser.parse_args()

    cfg = load_drive_review_config(ROOT / "drive_review/config.yaml", base_config_path=ROOT / "config.yaml")
    secret = cfg.drive_credentials_dir / "client_secret.json"
    token = cfg.drive_credentials_dir / "token_write.json"
    if not secret.exists():
        log.error("Missing OAuth client secret at %s", secret)
        return 1

    service = get_drive_service(client_secret_path=secret, token_path=token, write=True)
    sync = DriveOutputsSync(cfg, service)
    review_store = ReviewStore(cfg.review_state_path)

    shopify_client = None
    products_by_sku: dict[str, dict] = {}
    if not args.skip_shopify:
        conn = ensure_shopify_connection(cfg.outputs_dir, env_path=ROOT / ".env")
        if conn.connected and conn.client:
            shopify_client = conn.client
            products_by_sku = shopify_products_by_sku(
                shopify_client,
                active_only=False,
                review_store=review_store,
            )
            log.info("Shopify connected: %s (%d products indexed)", conn.shop_name, len(products_by_sku))
        else:
            log.warning("Shopify not connected — skipping Shopify sync (%s)", conn.error or conn.status_label)

    def _progress(msg: str, cur: int, total: int) -> None:
        if total and (cur % 25 == 0 or cur == total):
            log.info("%s %d/%d", msg, cur, total)

    report = run_prompt_version_sync(
        cfg,
        sync,
        service,
        shopify_client=shopify_client,
        products_by_sku=products_by_sku,
        review_store=review_store,
        skus=args.sku,
        dry_run=args.dry_run,
        fix_drive=not args.skip_drive,
        fix_shopify=not args.skip_shopify and shopify_client is not None,
        fix_xlsx=not args.skip_xlsx,
        progress=_progress,
    )

    s = report.get("summary") or {}
    report_path = write_sync_report(report, cfg.outputs_dir)

    log.info("--- Summary (%s) ---", "dry-run" if args.dry_run else "applied")
    log.info("SKUs audited: %d", report.get("sku_count", 0))
    log.info("Need Drive sync: %d | Synced: %d | Errors: %d", s.get("need_drive_sync"), s.get("drive_synced"), s.get("drive_errors"))
    log.info(
        "Need Shopify sync: %d | Synced: %d | Errors: %d",
        s.get("need_shopify_sync"),
        s.get("shopify_synced"),
        s.get("shopify_errors"),
    )
    log.info("XLSX path mismatches: %d", s.get("xlsx_mismatches"))
    if report.get("xlsx_fix"):
        log.info("XLSX rebuilt and uploaded to Drive sheet")
    log.info("Report: %s", report_path)

    if s.get("drive_errors") or s.get("shopify_errors") or s.get("xlsx_mismatches"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
