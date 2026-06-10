#!/usr/bin/env python3
"""Apply typo SKU cleanup: delete safe folders and migrate fixable ones."""
from __future__ import annotations

import argparse
import json

from src.config import load_config
from src.typo_sku_cleanup import apply_typo_cleanup


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply typo SKU cleanup from audit")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Report actions without changing files")
    args = parser.parse_args()

    cfg = load_config(args.config)
    review_store_path = cfg.outputs_dir / "review_state.json"
    results = apply_typo_cleanup(
        outputs_dir=cfg.outputs_dir,
        outputsv2_dir=cfg.outputsv2_dir,
        xlsx_path=cfg.xlsx_path,
        review_store_path=review_store_path,
        dry_run=args.dry_run,
    )
    print(json.dumps(results, indent=2))
    if args.dry_run:
        print("(dry-run: no files changed)")


if __name__ == "__main__":
    main()
