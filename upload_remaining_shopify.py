#!/usr/bin/env python3
"""
Upload approved SKUs that are not yet on Shopify.

Unlike retry_failed_shopify_uploads.py (which only handles review_status=failed),
this script uploads the main backlog: eligible SKUs still marked approved with
no successful upload yet. That is typically ~200+ SKUs when a long run stops
partway through.

Usage:
  python upload_remaining_shopify.py --status
  python upload_remaining_shopify.py --list
  python upload_remaining_shopify.py --dry-run
  python upload_remaining_shopify.py
  python upload_remaining_shopify.py --limit 50
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from dotenv import load_dotenv

from dedupe_titles_and_upload import _load_shopify_client
from src.config import load_config
from src.rebuild_executor import (
    list_failed_skus,
    list_remaining_upload_skus,
    upload_remaining_approved_products,
)
from src.review_store import ReviewStore

log = logging.getLogger("upload_remaining")


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Upload approved SKUs not yet marked uploaded on Shopify"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show upload progress summary (uploaded / remaining / failed)",
    )
    parser.add_argument("--list", action="store_true", help="List remaining SKUs and exit")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument("--sku", action="append", help="Upload specific SKU(s) only")
    parser.add_argument("--limit", type=int, default=0, help="Max SKUs to upload (0 = all remaining)")
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds between SKU uploads (default 1.0)",
    )
    parser.add_argument(
        "--network-stop",
        type=int,
        default=3,
        help="Stop after N consecutive network errors (default 3)",
    )
    parser.add_argument("--config", default="config.yaml", help="Config path")
    args = parser.parse_args()

    cfg = load_config(args.config)
    review_store = ReviewStore(cfg.outputs_dir / "review_state.json")
    remaining = list_remaining_upload_skus(cfg, review_store)
    failed = list_failed_skus(review_store)

    if args.status:
        all_recs = review_store.all_records()
        review_counts = Counter(str(r.get("review_status") or "") for r in all_recs.values())
        uploaded = review_counts.get("uploaded", 0)
        log.info("Review status: %s", dict(review_counts))
        log.info("Uploaded on Shopify (local): %d", uploaded)
        log.info("Remaining approved backlog (eligible, not uploaded): %d", len(remaining))
        log.info("Failed (use retry_failed_shopify_uploads.py): %d", len(failed))
        log.info("Total eligible target: 447 | done + remaining + failed ≈ %d", uploaded + len(remaining) + len(failed))
        return 0

    if args.list:
        log.info("%d remaining SKU(s) to upload", len(remaining))
        for item in remaining:
            log.info(
                "%s | %s | status=%s | product_id=%s",
                item["sku"],
                item["title"][:50],
                item["review_status"],
                item["product_id"] or "-",
            )
        if failed:
            log.info("Also %d failed SKU(s) — run retry_failed_shopify_uploads.py after this", len(failed))
        return 0

    if not remaining and not args.sku:
        log.info("No remaining SKUs to upload")
        if failed:
            log.info("There are %d failed SKU(s) — run: python retry_failed_shopify_uploads.py", len(failed))
        return 0

    if args.dry_run:
        log.info("DRY RUN — no Shopify uploads")

    client = _load_shopify_client(cfg.outputs_dir)
    report = upload_remaining_approved_products(
        cfg,
        client,
        skus=args.sku,
        dry_run=args.dry_run,
        sleep_seconds=args.sleep,
        limit=args.limit,
        stop_after_network_errors=args.network_stop,
    )

    log.info(
        "Upload complete: %d ok, %d failed (report: %s/rebuild_executor/upload_remaining_approved.json)",
        report.get("success_count", 0),
        report.get("failed_count", 0),
        cfg.outputs_dir,
    )
    if report.get("stopped_early"):
        log.warning("Stopped early due to network errors — re-run when connection is stable")
    if list_failed_skus(review_store):
        log.info("Some SKUs failed — run: python retry_failed_shopify_uploads.py")
    return 1 if report.get("failed_count") else 0


if __name__ == "__main__":
    raise SystemExit(main())
