#!/usr/bin/env python3
"""Audit typo-derived SKU folders and write outputs/typo_sku_audit.json + .md."""
from __future__ import annotations

import argparse
from pathlib import Path

from src.config import load_config
from src.typo_sku_cleanup import audit_typo_folders, write_audit_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit typo SKU folders in outputs/")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    review_store_path = cfg.outputs_dir / "review_state.json"
    audit = audit_typo_folders(
        outputs_dir=cfg.outputs_dir,
        xlsx_path=cfg.xlsx_path,
        review_store_path=review_store_path if review_store_path.is_file() else None,
    )
    json_path, md_path = write_audit_report(audit, cfg.outputs_dir)
    summary = audit.get("summary") or {}
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(
        "Summary: "
        f"delete_safe={summary.get('delete_safe', 0)}, "
        f"migrate={summary.get('migrate', 0)}, "
        f"unresolved={summary.get('unresolved', 0)}"
    )


if __name__ == "__main__":
    main()
