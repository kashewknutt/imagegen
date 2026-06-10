#!/usr/bin/env python3
"""Apply typo cleanup on local outputs/, push affected SKUs + XLSX to Drive."""
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
from src.drive_typo_cleanup import apply_drive_typo_cleanup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="drive_review/config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_drive_review_config(args.config)
    secret = cfg.drive_credentials_dir / "client_secret.json"
    token = cfg.drive_credentials_dir / "token_write.json"
    service = get_drive_service(client_secret_path=secret, token_path=token, write=True)
    sync = DriveOutputsSync(cfg, service)
    results = apply_drive_typo_cleanup(cfg, sync, service, dry_run=args.dry_run)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
