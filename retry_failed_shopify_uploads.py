#!/usr/bin/env python3
"""
Retry Shopify product creation for SKUs marked failed in review_state.json.

NOTE: This only retries review_status=failed. SKUs still marked approved but
never uploaded (~200 backlog) are handled by upload_remaining_shopify.py instead.

Typical failures are network timeouts during long upload runs. This script
re-uses the approved recreate path: recover orphan product_ids, create or
resume media sync, then mark uploaded.

Usage:
  python retry_failed_shopify_uploads.py --list
  python retry_failed_shopify_uploads.py --dry-run
  python retry_failed_shopify_uploads.py
  python retry_failed_shopify_uploads.py --limit 10
  python retry_failed_shopify_uploads.py --sku DIAEFHR26005 --sku DIAEFHR26007
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from dotenv import load_dotenv

from dedupe_titles_and_upload import _load_shopify_client
from src.config import load_config
from src.rebuild_executor import list_failed_skus, retry_failed_products
from src.review_store import ReviewStore

log = logging.getLogger("retry_failed")


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Retry failed Shopify uploads from review_state")
    parser.add_argument("--list", action="store_true", help="List failed SKUs and exit")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be retried without uploading")
    parser.add_argument("--sku", action="append", help="Retry specific SKU(s) only")
    parser.add_argument("--limit", type=int, default=0, help="Max SKUs to retry (0 = all failed)")
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds between SKU uploads (default 1.0 for network stability)",
    )
    parser.add_argument("--config", default="config.yaml", help="Config path")
    args = parser.parse_args()

    cfg = load_config(args.config)
    review_store = ReviewStore(cfg.outputs_dir / "review_state.json")
    failed = list_failed_skus(review_store)

    if args.list:
        log.info("%d failed SKU(s)", len(failed))
        error_types = Counter()
        for item in failed:
            err = item["last_error"] or "unknown"
            key = err.split("(")[0].strip()[:80] if err else "unknown"
            error_types[key] += 1
            log.info(
                "%s | %s | product_id=%s | %s",
                item["sku"],
                item["title"][:50],
                item["product_id"] or "-",
                err[:100],
            )
        log.info("Error summary: %s", dict(error_types.most_common(10)))
        return 0

    if not failed and not args.sku:
        log.info("No failed SKUs in review_state.json")
        return 0

    if args.dry_run:
        log.info("DRY RUN — no Shopify uploads")

    client = _load_shopify_client(cfg.outputs_dir)
    report = retry_failed_products(
        cfg,
        client,
        skus=args.sku,
        dry_run=args.dry_run,
        sleep_seconds=args.sleep,
        limit=args.limit,
    )

    log.info(
        "Retry complete: %d ok, %d failed (report: %s/rebuild_executor/retry_failed_products.json)",
        report.get("success_count", 0),
        report.get("failed_count", 0),
        cfg.outputs_dir,
    )
    return 1 if report.get("failed_count") else 0


if __name__ == "__main__":
    raise SystemExit(main())
