#!/usr/bin/env python3
"""Audit typo SKU folders using local outputs/ (push report to Drive only)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.drive_client import get_drive_service
from src.drive_outputs_sync import DriveOutputsSync
from src.drive_review_config import load_drive_review_config
from src.drive_typo_cleanup import audit_drive_typo_folders
from src.typo_sku_cleanup import write_audit_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="drive_review/config.yaml")
    parser.add_argument("--no-upload", action="store_true", help="Skip uploading audit report to Drive")
    args = parser.parse_args()

    cfg = load_drive_review_config(
        ROOT / args.config if not Path(args.config).is_absolute() else args.config,
        base_config_path=ROOT / "config.yaml",
    )
    secret = cfg.drive_credentials_dir / "client_secret.json"
    token = cfg.drive_credentials_dir / "token_write.json"
    service = get_drive_service(client_secret_path=secret, token_path=token, write=True)
    sync = DriveOutputsSync(cfg, service)
    audit = audit_drive_typo_folders(cfg, sync, service)
    json_path, md_path = write_audit_report(audit, cfg.outputs_dir)
    print(json.dumps(audit.get("summary") or {}, indent=2))
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")

    if not args.no_upload:
        sync.push_file_to_outputs_root(json_path)
        sync.push_file_to_outputs_root(md_path)
        print("Uploaded audit reports to Drive.")


if __name__ == "__main__":
    main()
