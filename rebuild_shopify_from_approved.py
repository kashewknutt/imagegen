#!/usr/bin/env python3
"""
Destructive Shopify wipe and reupload from approved local review state.

Sequence:
  1. Verify cached Shopify token
  2. Hard-delete every Shopify product (--confirm-delete-all required)
  3. Reset local product_id/handle/upload fields
  4. Prune old prompt1/prompt2 versions per SKU
  5. Validate SKU/title uniqueness and rebuild stock_enriched.xlsx
  6. Recreate products for approved SKUs with Stock.xlsx rows
  7. Final audit

Usage:
  python rebuild_shopify_from_approved.py --dry-run
  python rebuild_shopify_from_approved.py --confirm-delete-all
  python rebuild_shopify_from_approved.py --confirm-delete-all --skip-upload
  python rebuild_shopify_from_approved.py --skip-delete --skip-reset --skip-prune
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

from dedupe_titles_and_upload import _load_shopify_client
from src.config import load_config
from src.rebuild_executor import run_full_executor

log = logging.getLogger("rebuild_shopify")


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Wipe Shopify catalog and reupload from approved local workspaces"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run local prune/validation without delete, state reset, XLSX write, or upload",
    )
    parser.add_argument(
        "--confirm-delete-all",
        action="store_true",
        help="Required to hard-delete every product in the Shopify store",
    )
    parser.add_argument(
        "--skip-delete",
        action="store_true",
        help="Skip Shopify delete phase (for testing downstream steps)",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip product recreate/upload after validation",
    )
    parser.add_argument(
        "--skip-reset",
        action="store_true",
        help="Skip clearing local product_id/handle (use when resuming upload)",
    )
    parser.add_argument(
        "--skip-prune",
        action="store_true",
        help="Skip pruning old prompt1/prompt2 versions",
    )
    parser.add_argument(
        "--products-path",
        default="products_export_1.xlsx",
        help="Products export used for stock_enriched fallback titles",
    )
    parser.add_argument("--config", default="config.yaml", help="Config path")
    args = parser.parse_args()

    if args.dry_run:
        log.info("DRY RUN — no destructive Shopify or upload actions will be committed")
    elif not args.skip_delete and not args.confirm_delete_all:
        log.error("Refusing to run: pass --confirm-delete-all to wipe the entire Shopify catalog")
        log.error("Use --dry-run to preview local phases only")
        return 2

    cfg = load_config(args.config)
    client = _load_shopify_client(cfg.outputs_dir)
    products_path = Path(args.products_path)
    if not products_path.is_absolute():
        products_path = Path.cwd() / products_path

    try:
        summary = run_full_executor(
            cfg,
            client,
            confirm_delete_all=args.confirm_delete_all,
            dry_run=args.dry_run,
            skip_delete=args.skip_delete,
            skip_reset=args.skip_reset,
            skip_prune=args.skip_prune,
            skip_upload=args.skip_upload,
            products_path=products_path,
        )
    except Exception as e:
        log.error("Executor failed: %s", e)
        return 1

    if summary.success:
        log.info("Rebuild executor completed successfully")
        log.info("Phase reports: %s/rebuild_executor/", cfg.outputs_dir)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
