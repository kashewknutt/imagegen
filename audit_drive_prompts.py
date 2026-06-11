#!/usr/bin/env python3
"""CLI: audit every Drive SKU folder for prompt1 + prompt2 generated images."""
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
from src.drive_prompt_audit import audit_drive_prompt_images, write_prompt_audit_report
from src.drive_review_config import load_drive_review_config

log = logging.getLogger("audit_drive_prompts")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Audit Drive folders for prompt1+prompt2 images")
    parser.add_argument("--push-report", action="store_true", help="Upload report JSON/MD to Drive outputs root")
    args = parser.parse_args()

    cfg = load_drive_review_config(ROOT / "drive_review/config.yaml", base_config_path=ROOT / "config.yaml")
    secret = cfg.drive_credentials_dir / "client_secret.json"
    token = cfg.drive_credentials_dir / "token_write.json"
    if not secret.exists():
        log.error("Missing OAuth client secret at %s", secret)
        return 1

    service = get_drive_service(client_secret_path=secret, token_path=token, write=True)
    sync = DriveOutputsSync(cfg, service)

    def _progress(msg: str, cur: int, total: int) -> None:
        if total and cur % 25 == 0 or cur == total:
            log.info("%s %d/%d", msg, cur, total)

    audit = audit_drive_prompt_images(cfg, sync, progress=_progress)
    json_path, md_path = write_prompt_audit_report(audit, cfg.outputs_dir)
    s = audit.get("summary") or {}
    log.info(
        "Done in %ss — folders=%d complete=%d missing_p1=%d missing_p2=%d missing_both=%d",
        s.get("elapsed_seconds"),
        s.get("drive_sku_folders"),
        s.get("complete_both_prompts"),
        s.get("missing_prompt1"),
        s.get("missing_prompt2"),
        s.get("missing_both"),
    )
    log.info("Reports: %s , %s", json_path, md_path)

    if args.push_report:
        sync.push_file_to_outputs_root(json_path)
        sync.push_file_to_outputs_root(md_path)
        log.info("Uploaded reports to Drive outputs root")

    missing = int(s.get("missing_prompt1") or 0) + int(s.get("missing_prompt2") or 0)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
