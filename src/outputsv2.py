"""Full per-SKU workspace snapshots under outputsv2/ for changed SKUs."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.media_workspace import MANIFEST_NAME, sku_workspace_dir

SKIP_WORKSPACE_NAMES = {
    "_download_cache",
    "_leases",
    "_temp",
    "review_state.json",
    "state.json",
    "stock_enriched.xlsx",
    "typo_sku_audit.json",
    "typo_sku_audit.md",
}


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _revision_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_reason(reason: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in (reason or "change").strip())
    return cleaned[:80] or "change"


def mirror_sku_snapshot(
    *,
    outputs_dir: Path,
    outputsv2_dir: Path,
    sku: str,
    reason: str,
    review_record: dict[str, Any] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> Path | None:
    """
    Copy the full active SKU workspace into outputsv2/{SKU}/revisions/{timestamp}_{reason}/.
    Append-only: never overwrites prior revision folders.
    """
    sku = (sku or "").strip()
    if not sku:
        return None

    src = sku_workspace_dir(outputs_dir, sku)
    if not src.is_dir():
        return None

    stamp = _revision_stamp()
    safe_reason = _safe_reason(reason)
    rev_root = outputsv2_dir / sku / "revisions" / f"{stamp}_{safe_reason}"
    if rev_root.exists():
        rev_root = outputsv2_dir / sku / "revisions" / f"{stamp}_{safe_reason}_dup"
    workspace_dest = rev_root / "workspace"
    workspace_dest.mkdir(parents=True, exist_ok=True)

    for item in sorted(src.iterdir()):
        dest = workspace_dest / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    meta: dict[str, Any] = {
        "sku": sku,
        "reason": reason,
        "mirrored_at_utc": _now_utc(),
        "source_workspace": str(src),
        "revision_dir": str(rev_root),
    }
    if extra_metadata:
        meta.update(extra_metadata)
    if review_record is not None:
        meta["review_record"] = review_record

    (rev_root / "mirror_metadata.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    latest_path = outputsv2_dir / sku / "latest_revision.json"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(
        json.dumps(
            {
                "sku": sku,
                "latest_revision": rev_root.name,
                "latest_revision_path": str(rev_root),
                "reason": reason,
                "mirrored_at_utc": meta["mirrored_at_utc"],
                "manifest_present": (workspace_dest / MANIFEST_NAME).is_file(),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return rev_root


def mirror_sku_from_config(
    cfg,
    sku: str,
    *,
    reason: str,
    review_store=None,
    extra_metadata: dict[str, Any] | None = None,
) -> Path | None:
    """Convenience wrapper using AppConfig paths."""
    outputsv2_dir = getattr(cfg, "outputsv2_dir", None)
    if outputsv2_dir is None:
        outputsv2_dir = cfg.outputs_dir.parent / "outputsv2"
    review_record = None
    if review_store is not None:
        try:
            review_record = review_store.get_record(sku)
        except Exception:
            review_record = None
    return mirror_sku_snapshot(
        outputs_dir=cfg.outputs_dir,
        outputsv2_dir=Path(outputsv2_dir),
        sku=sku,
        reason=reason,
        review_record=review_record,
        extra_metadata=extra_metadata,
    )
