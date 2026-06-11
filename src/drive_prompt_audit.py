"""Audit Drive SKU folders for prompt1 + prompt2 generated images."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.drive_outputs_sync import DriveOutputsSync
from src.drive_review_config import DriveReviewConfig


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def audit_drive_prompt_images(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    *,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """
    Metadata-only scan: every Drive SKU folder must have prompt1 and prompt2 files.
    """
    t0 = time.monotonic()
    folders = sync.list_sku_folders(refresh=True)
    skus = sorted(folders.keys())
    total = len(skus)

    rows: list[dict[str, Any]] = []
    missing_p1: list[str] = []
    missing_p2: list[str] = []
    missing_both: list[str] = []
    complete: list[str] = []

    for i, sku in enumerate(skus, start=1):
        if progress:
            progress("Scanning Drive SKU folders", i, total)
        folder_id = folders[sku]
        try:
            meta = sync.scan_sku_metadata(sku, folder_id=folder_id)
        except Exception as e:
            meta = {
                "sku": sku,
                "error": str(e),
                "has_prompt1": False,
                "has_prompt2": False,
            }
        has_p1 = bool(meta.get("has_prompt1"))
        has_p2 = bool(meta.get("has_prompt2"))
        issues: list[str] = []
        if not has_p1:
            issues.append("missing_prompt1")
            missing_p1.append(sku)
        if not has_p2:
            issues.append("missing_prompt2")
            missing_p2.append(sku)
        if not has_p1 and not has_p2:
            missing_both.append(sku)
        if has_p1 and has_p2:
            complete.append(sku)

        rows.append(
            {
                "sku": sku,
                "folder_id": folder_id,
                "has_prompt1": has_p1,
                "has_prompt2": has_p2,
                "raw_count": meta.get("raw_count", 0),
                "video_count": meta.get("video_count", 0),
                "issues": issues,
                "error": meta.get("error"),
            }
        )

    elapsed = round(time.monotonic() - t0, 1)
    summary = {
        "drive_sku_folders": total,
        "complete_both_prompts": len(complete),
        "missing_prompt1": len(missing_p1),
        "missing_prompt2": len(missing_p2),
        "missing_both": len(missing_both),
        "elapsed_seconds": elapsed,
    }
    return {
        "generated_at": _now_utc(),
        "drive_outputs_folder_id": cfg.drive_outputs_folder_id,
        "summary": summary,
        "complete": complete,
        "missing_prompt1": missing_p1,
        "missing_prompt2": missing_p2,
        "missing_both": missing_both,
        "rows": rows,
    }


def write_prompt_audit_report(audit: dict[str, Any], outputs_dir: Path) -> tuple[Path, Path]:
    outputs_dir.mkdir(parents=True, exist_ok=True)
    json_path = outputs_dir / "drive_prompt_audit.json"
    md_path = outputs_dir / "drive_prompt_audit.md"
    json_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")

    s = audit.get("summary") or {}
    lines = [
        "# Drive generated-image audit (prompt1 + prompt2)",
        "",
        f"Generated: {audit.get('generated_at', '')}",
        "",
        "## Summary",
        "",
        f"- Drive SKU folders scanned: **{s.get('drive_sku_folders', 0)}**",
        f"- Both prompt1 and prompt2 present: **{s.get('complete_both_prompts', 0)}**",
        f"- Missing prompt1: **{s.get('missing_prompt1', 0)}**",
        f"- Missing prompt2: **{s.get('missing_prompt2', 0)}**",
        f"- Missing both: **{s.get('missing_both', 0)}**",
        "",
        f"Elapsed: {s.get('elapsed_seconds', '?')}s",
        "",
    ]

    def _section(title: str, items: list[str], limit: int = 80) -> None:
        lines.append(f"## {title} ({len(items)})")
        lines.append("")
        if not items:
            lines.append("(none)")
        else:
            for sku in items[:limit]:
                lines.append(f"- `{sku}`")
            if len(items) > limit:
                lines.append(f"- … and {len(items) - limit} more")
        lines.append("")

    _section("Missing prompt1", audit.get("missing_prompt1") or [])
    _section("Missing prompt2", audit.get("missing_prompt2") or [])
    _section("Missing both", audit.get("missing_both") or [])

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
