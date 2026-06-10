"""Audit and remediate typo-derived SKU workspace folders under outputs/."""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from src.media_workspace import (
    index_sku_media,
    list_raw_images,
    list_videos,
    raw_dir,
    refresh_manifest,
    videos_dir,
)
from src.outputsv2 import mirror_sku_snapshot
from src.review_store import ReviewStore
from src.sku_aliases import SKU_ALIASES
from src.xlsx_ingest import index_by_sku, iter_rows

# Confirmed typo folder -> real Stock SKU mappings from prior workspace audit.
KNOWN_TYPO_TO_REAL: dict[str, str] = {
    "DIABBLR26064": "DIABBLR26024",
    "DIABBLR26932": "DIABBLR26032",
    "DIABFHR260065": "DIABFHR26005",
    "DIAEFFHW26021": "DIAEFHW26021",
    "DIAEFHR2610": "DIAEFHR26010",
    "DIAEFR26028": "DIAEFHR26028",
    "DIAESSTR26038": "DIAESTR26038",
    "DIAESTW2037": "DIAESTW26037",
    "DIAFHW26008": "DIANFHW26008",
    "DIPFHW26019": "DIANFHW26019",
    # DAIJE used DIAESTR prefix; Stock uses DIAESTW for same numeric suffix.
    "DIAESTR26083": "DIAESTW26083",
    "DIAESTR26085": "DIAESTW26085",
    "DIAESTR26087": "DIAESTW26087",
    "DIAESTR26089": "DIAESTW26089",
    # Legacy 1.csv SKU; real product is adjacent Stock row with prompts.
    "DIARBNR26015": "DIARBNR26014",
}

_DIAESTR_PREFIX_RE = re.compile(r"^DIAESTR(\d+)$", re.IGNORECASE)

SKIP_OUTPUT_DIRS = {
    "_download_cache",
    "_leases",
    "_temp",
    "_semaphore",
    "_semaphores",
    "rebuild_executor",
    "Stock",
}

SKU_DIR_RE = re.compile(r"^[A-Z]{2,}[A-Z0-9-]{3,}$", re.IGNORECASE)
AUDIT_JSON_NAME = "typo_sku_audit.json"
AUDIT_MD_NAME = "typo_sku_audit.md"


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def load_stock_skus(xlsx_path: Path) -> set[str]:
    rows = index_by_sku(iter_rows(xlsx_path, ["Total"]), sku_column="SKU")
    return {sku.strip() for sku in rows if sku.strip()}


def list_output_sku_dirs(outputs_dir: Path) -> list[str]:
    if not outputs_dir.is_dir():
        return []
    out: list[str] = []
    for p in sorted(outputs_dir.iterdir()):
        if not p.is_dir():
            continue
        name = p.name
        if name in SKIP_OUTPUT_DIRS:
            continue
        if name.startswith(".") or name.startswith("_"):
            continue
        if not SKU_DIR_RE.match(name):
            continue
        out.append(name)
    return out


def stock_equivalent_folders(stock_skus: set[str]) -> set[str]:
    """Stock SKUs plus canonical output folders from sku_aliases (e.g. DDIANFHW26007)."""
    return set(stock_skus) | set(SKU_ALIASES.values())


def sku_has_both_prompts(outputs_dir: Path, sku: str) -> bool:
    idx = index_sku_media(outputs_dir=outputs_dir, sku=sku)
    return bool(idx.prompt1_versions and idx.prompt2_versions)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.upper(), b.upper()).ratio()


def suggest_real_sku(typo_sku: str, stock_skus: set[str]) -> str | None:
    if typo_sku in KNOWN_TYPO_TO_REAL:
        real = KNOWN_TYPO_TO_REAL[typo_sku]
        return real if real in stock_skus else None

    m = _DIAESTR_PREFIX_RE.match(typo_sku)
    if m:
        candidate = f"DIAESTW{m.group(1)}"
        if candidate in stock_skus:
            return candidate

    # High-confidence fuzzy fallback: same length, single-character edit, unique match.
    typo_u = typo_sku.upper()
    candidates: list[str] = []
    for stock in stock_skus:
        stock_u = stock.upper()
        if typo_u == stock_u or len(typo_u) != len(stock_u):
            continue
        if _similarity(typo_u, stock_u) >= 0.96:
            candidates.append(stock)
    if len(candidates) == 1:
        return candidates[0]
    return None


@dataclass
class TypoAuditEntry:
    typo_sku: str
    action: str
    real_sku: str | None
    evidence: dict[str, Any]
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "typo_sku": self.typo_sku,
            "action": self.action,
            "real_sku": self.real_sku,
            "evidence": self.evidence,
            "notes": self.notes,
        }


def audit_typo_folders(
    *,
    outputs_dir: Path,
    xlsx_path: Path,
    review_store_path: Path | None = None,
) -> dict[str, Any]:
    stock_skus = load_stock_skus(xlsx_path)
    equivalent = stock_equivalent_folders(stock_skus)
    review_store = ReviewStore(review_store_path) if review_store_path else None
    entries: list[TypoAuditEntry] = []

    for folder_sku in list_output_sku_dirs(outputs_dir):
        if folder_sku in equivalent:
            continue

        real_sku = suggest_real_sku(folder_sku, stock_skus)
        typo_idx = index_sku_media(outputs_dir=outputs_dir, sku=folder_sku)
        evidence: dict[str, Any] = {
            "in_stock": False,
            "typo_has_prompt1": bool(typo_idx.prompt1_versions),
            "typo_has_prompt2": bool(typo_idx.prompt2_versions),
            "typo_raw_count": len(typo_idx.raw_images),
            "typo_video_count": len(typo_idx.videos),
            "mapping_source": "known" if folder_sku in KNOWN_TYPO_TO_REAL else ("fuzzy" if real_sku else "none"),
        }
        if review_store is not None:
            rec = review_store.get_record(folder_sku)
            evidence["typo_review_status"] = str(rec.get("review_status") or "")

        if not real_sku:
            has_prompts = evidence["typo_has_prompt1"] or evidence["typo_has_prompt2"]
            review_status = str(evidence.get("typo_review_status") or "")
            uploaded_like = review_status in {"uploaded", "verified", "approved"}
            if not has_prompts and not uploaded_like:
                entries.append(
                    TypoAuditEntry(
                        typo_sku=folder_sku,
                        action="delete_orphan",
                        real_sku=None,
                        evidence=evidence,
                        notes="Not in Stock.xlsx, no mapping, no generated prompts — safe orphan cleanup.",
                    )
                )
            else:
                entries.append(
                    TypoAuditEntry(
                        typo_sku=folder_sku,
                        action="unresolved",
                        real_sku=None,
                        evidence=evidence,
                        notes="No confident real Stock SKU mapping.",
                    )
                )
            continue

        evidence["real_sku_in_stock"] = real_sku in stock_skus
        real_dir = outputs_dir / real_sku
        evidence["real_workspace_exists"] = real_dir.is_dir()
        evidence["real_has_prompt1"] = False
        evidence["real_has_prompt2"] = False
        if real_dir.is_dir():
            real_idx = index_sku_media(outputs_dir=outputs_dir, sku=real_sku)
            evidence["real_has_prompt1"] = bool(real_idx.prompt1_versions)
            evidence["real_has_prompt2"] = bool(real_idx.prompt2_versions)
            evidence["real_raw_count"] = len(real_idx.raw_images)
            evidence["real_video_count"] = len(real_idx.videos)

        if (
            real_dir.is_dir()
            and evidence["real_has_prompt1"]
            and evidence["real_has_prompt2"]
        ):
            action = "delete_safe"
            notes = "Real workspace exists with prompt1 and prompt2; typo folder is redundant."
        else:
            action = "migrate"
            notes = "Move typo raw/video assets into real SKU workspace and mark pending_review."

        entries.append(
            TypoAuditEntry(
                typo_sku=folder_sku,
                action=action,
                real_sku=real_sku,
                evidence=evidence,
                notes=notes,
            )
        )

    summary = {
        "delete_safe": sum(1 for e in entries if e.action == "delete_safe"),
        "delete_orphan": sum(1 for e in entries if e.action == "delete_orphan"),
        "migrate": sum(1 for e in entries if e.action == "migrate"),
        "unresolved": sum(1 for e in entries if e.action == "unresolved"),
    }
    return {
        "generated_at_utc": _now_utc(),
        "outputs_dir": str(outputs_dir),
        "xlsx_path": str(xlsx_path),
        "summary": summary,
        "entries": [e.to_dict() for e in entries],
    }


def write_audit_report(audit: dict[str, Any], outputs_dir: Path) -> tuple[Path, Path]:
    json_path = outputs_dir / AUDIT_JSON_NAME
    md_path = outputs_dir / AUDIT_MD_NAME
    json_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")

    lines = [
        "# Typo SKU Audit",
        "",
        f"Generated: {audit.get('generated_at_utc', '')}",
        "",
        "## Summary",
        "",
        f"- delete_safe: {audit.get('summary', {}).get('delete_safe', 0)}",
        f"- delete_orphan: {audit.get('summary', {}).get('delete_orphan', 0)}",
        f"- migrate: {audit.get('summary', {}).get('migrate', 0)}",
        f"- unresolved: {audit.get('summary', {}).get('unresolved', 0)}",
        "",
        "## Entries",
        "",
    ]
    for entry in audit.get("entries") or []:
        lines.append(f"### {entry.get('typo_sku')} -> {entry.get('action')}")
        if entry.get("real_sku"):
            lines.append(f"- real SKU: `{entry['real_sku']}`")
        if entry.get("notes"):
            lines.append(f"- notes: {entry['notes']}")
        evidence = entry.get("evidence") or {}
        for key in sorted(evidence):
            lines.append(f"- {key}: {evidence[key]}")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def _copy_tree_files(src_dir: Path, dest_dir: Path, *, move: bool) -> list[str]:
    copied: list[str] = []
    if not src_dir.is_dir():
        return copied
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(src_dir.iterdir()):
        if not src.is_file():
            continue
        dest = dest_dir / src.name
        if dest.exists():
            continue
        if move:
            shutil.move(str(src), str(dest))
        else:
            shutil.copy2(src, dest)
        copied.append(dest.name)
    return copied


def _migrate_raw_and_videos(
    *,
    typo_dir: Path,
    real_dir: Path,
    move: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {"raw_moved": [], "videos_moved": [], "skipped": []}

    typo_raw = raw_dir(typo_dir)
    if typo_raw.is_dir():
        result["raw_moved"].extend(_copy_tree_files(typo_raw, raw_dir(real_dir), move=move))
    else:
        real_raw = raw_dir(real_dir)
        real_raw.mkdir(parents=True, exist_ok=True)
        for src in list_raw_images(typo_dir):
            dest = real_raw / src.name
            if dest.exists():
                result["skipped"].append(src.name)
                continue
            if move:
                shutil.move(str(src), str(dest))
            else:
                shutil.copy2(src, dest)
            result["raw_moved"].append(dest.name)

    typo_videos = videos_dir(typo_dir)
    if typo_videos.is_dir():
        result["videos_moved"].extend(_copy_tree_files(typo_videos, videos_dir(real_dir), move=move))
    else:
        real_videos = videos_dir(real_dir)
        real_videos.mkdir(parents=True, exist_ok=True)
        for src in list_videos(typo_dir):
            dest = real_videos / src.name
            if dest.exists():
                result["skipped"].append(src.name)
                continue
            if move:
                shutil.move(str(src), str(dest))
            else:
                shutil.copy2(src, dest)
            result["videos_moved"].append(dest.name)

    return result


def delete_orphan_folder(
    *,
    outputs_dir: Path,
    typo_sku: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    typo_dir = outputs_dir / typo_sku
    result: dict[str, Any] = {
        "typo_sku": typo_sku,
        "action": "delete_orphan",
        "dry_run": dry_run,
        "deleted": False,
        "timestamp_utc": _now_utc(),
    }
    if not typo_dir.is_dir():
        result["skipped"] = True
        result["reason"] = "folder missing"
        return result
    if dry_run:
        result["would_delete"] = str(typo_dir)
        return result
    shutil.rmtree(typo_dir)
    result["deleted"] = True
    result["deleted_path"] = str(typo_dir)
    return result


def delete_typo_folder(
    *,
    outputs_dir: Path,
    typo_sku: str,
    real_sku: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    typo_dir = outputs_dir / typo_sku
    result: dict[str, Any] = {
        "typo_sku": typo_sku,
        "real_sku": real_sku,
        "action": "delete_safe",
        "dry_run": dry_run,
        "deleted": False,
        "timestamp_utc": _now_utc(),
    }
    if not typo_dir.is_dir():
        result["skipped"] = True
        result["reason"] = "typo folder missing"
        return result
    if not sku_has_both_prompts(outputs_dir, real_sku):
        result["skipped"] = True
        result["reason"] = "real workspace missing prompt1/prompt2"
        return result
    if dry_run:
        result["would_delete"] = str(typo_dir)
        return result
    shutil.rmtree(typo_dir)
    result["deleted"] = True
    result["deleted_path"] = str(typo_dir)
    return result


def migrate_typo_folder(
    *,
    outputs_dir: Path,
    outputsv2_dir: Path,
    typo_sku: str,
    real_sku: str,
    review_store_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    typo_dir = outputs_dir / typo_sku
    real_dir = outputs_dir / real_sku
    result: dict[str, Any] = {
        "typo_sku": typo_sku,
        "real_sku": real_sku,
        "action": "migrate",
        "dry_run": dry_run,
        "timestamp_utc": _now_utc(),
    }
    if not typo_dir.is_dir():
        result["skipped"] = True
        result["reason"] = "typo folder missing"
        return result
    if sku_has_both_prompts(outputs_dir, real_sku):
        result["skipped"] = True
        result["reason"] = "real workspace already has prompt1/prompt2; use delete_safe instead"
        return result

    if dry_run:
        typo_idx = index_sku_media(outputs_dir=outputs_dir, sku=typo_sku)
        result["would_move_raw"] = len(typo_idx.raw_images)
        result["would_move_videos"] = len(typo_idx.videos)
        result["would_create_review"] = real_sku
        return result

    review_store = ReviewStore(review_store_path)
    real_dir.mkdir(parents=True, exist_ok=True)
    move_result = _migrate_raw_and_videos(typo_dir=typo_dir, real_dir=real_dir, move=True)
    result.update(move_result)

    refresh_manifest(outputs_dir=outputs_dir, sku=real_sku, patch={"review_status": "pending_review"})
    review_store.update(
        real_sku,
        review_status="pending_review",
        upload_status="pending",
        last_error="",
    )

    mirror_sku_snapshot(
        outputs_dir=outputs_dir,
        outputsv2_dir=outputsv2_dir,
        sku=real_sku,
        reason="typo_migration",
        review_record=review_store.get_record(real_sku),
        extra_metadata={"typo_sku": typo_sku},
    )

    if typo_dir.is_dir():
        shutil.rmtree(typo_dir)
        result["typo_folder_removed"] = True

    return result


def apply_typo_cleanup(
    *,
    outputs_dir: Path,
    outputsv2_dir: Path,
    xlsx_path: Path,
    review_store_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    audit = audit_typo_folders(
        outputs_dir=outputs_dir,
        xlsx_path=xlsx_path,
        review_store_path=review_store_path,
    )
    write_audit_report(audit, outputs_dir)

    results: dict[str, Any] = {
        "dry_run": dry_run,
        "applied_at_utc": _now_utc(),
        "deletions": [],
        "orphan_deletions": [],
        "migrations": [],
        "skipped": [],
    }

    for entry in audit.get("entries") or []:
        action = str(entry.get("action") or "")
        typo_sku = str(entry.get("typo_sku") or "")
        real_sku = str(entry.get("real_sku") or "")
        if action == "delete_orphan":
            results["orphan_deletions"].append(
                delete_orphan_folder(
                    outputs_dir=outputs_dir,
                    typo_sku=typo_sku,
                    dry_run=dry_run,
                )
            )
        elif action == "delete_safe" and real_sku:
            results["deletions"].append(
                delete_typo_folder(
                    outputs_dir=outputs_dir,
                    typo_sku=typo_sku,
                    real_sku=real_sku,
                    dry_run=dry_run,
                )
            )
        elif action == "migrate" and real_sku:
            mig = migrate_typo_folder(
                outputs_dir=outputs_dir,
                outputsv2_dir=outputsv2_dir,
                typo_sku=typo_sku,
                real_sku=real_sku,
                review_store_path=review_store_path,
                dry_run=dry_run,
            )
            if mig.get("skipped") and mig.get("reason", "").startswith("real workspace already"):
                mig = delete_typo_folder(
                    outputs_dir=outputs_dir,
                    typo_sku=typo_sku,
                    real_sku=real_sku,
                    dry_run=dry_run,
                )
                mig["fallback_from"] = "migrate"
                results["deletions"].append(mig)
            else:
                results["migrations"].append(mig)
        else:
            results["skipped"].append(
                {
                    "typo_sku": typo_sku,
                    "action": action,
                    "reason": entry.get("notes") or "unresolved",
                }
            )

    results_path = outputs_dir / "typo_sku_cleanup_results.json"
    if not dry_run:
        results_path.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    results["results_path"] = str(results_path)
    return results
