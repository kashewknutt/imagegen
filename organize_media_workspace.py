#!/usr/bin/env python3
"""
Organize DAIJE (or pics_raw) media into per-SKU outputs/{SKU}/ workspaces.

Usage:
  python organize_media_workspace.py --dry-run
  python organize_media_workspace.py
  python organize_media_workspace.py --sku DIAESTR26057
"""
from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

from src.config import load_config
from src.media_workspace import organize_all_from_source, scan_source_dir

log = logging.getLogger("organize_media_workspace")


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Organize raw media into outputs/{SKU}/ workspaces")
    parser.add_argument("--source", type=str, default="", help="Override source dir (default: config images_dir)")
    parser.add_argument("--sku", action="append", help="Limit to SKU(s)")
    parser.add_argument("--dry-run", action="store_true", help="Report without copying files")
    parser.add_argument("--move", action="store_true", help="Move instead of copy")
    parser.add_argument("--dedupe", action="store_true", help="Remove duplicate raw/video files after organizing")
    parser.add_argument("--dedupe-only", action="store_true", help="Only dedupe existing workspaces, do not copy")
    args = parser.parse_args()

    cfg = load_config()
    source = args.source or str(cfg.images_dir)
    from pathlib import Path

    source_dir = Path(source).expanduser().resolve()
    if not source_dir.is_dir():
        log.error("Source dir not found: %s", source_dir)
        return 1

    if args.dedupe_only:
        from src.media_workspace import dedupe_all_workspaces

        removed = dedupe_all_workspaces(cfg.outputs_dir)
        total_raw = sum(r.get("raw_removed", 0) for r in removed)
        total_vid = sum(r.get("videos_removed", 0) for r in removed)
        log.info("Deduped %d SKU(s): removed %d raw, %d video duplicate(s)", len(removed), total_raw, total_vid)
        return 0

    grouped = scan_source_dir(source_dir)
    log.info("Source %s — %d SKU(s) with media", source_dir, len(grouped))

    results = organize_all_from_source(
        source_dir=source_dir,
        outputs_dir=cfg.outputs_dir,
        skus=args.sku,
        copy=not args.move,
        dry_run=args.dry_run,
    )

    copied_raw = 0
    copied_vid = 0
    for r in results:
        if r.get("skipped") is True:
            log.info("%s — skipped (%s)", r.get("sku"), r.get("reason"))
            continue
        raw_n = len(r.get("raw_copied") or [])
        vid_n = len(r.get("videos_copied") or [])
        copied_raw += raw_n
        copied_vid += vid_n
        if raw_n or vid_n or args.dry_run:
            log.info(
                "%s — raw=%d video=%d skipped=%d%s",
                r.get("sku"),
                raw_n,
                vid_n,
                len(r.get("skipped") or []),
                " (dry-run)" if args.dry_run else "",
            )

    log.info("Done — %d raw, %d video across %d SKU(s)", copied_raw, copied_vid, len(results))

    if args.dedupe:
        from src.media_workspace import dedupe_all_workspaces

        removed = dedupe_all_workspaces(cfg.outputs_dir)
        total_raw = sum(r.get("raw_removed", 0) for r in removed)
        total_vid = sum(r.get("videos_removed", 0) for r in removed)
        log.info("Deduped %d SKU(s): removed %d raw, %d video duplicate(s)", len(removed), total_raw, total_vid)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
