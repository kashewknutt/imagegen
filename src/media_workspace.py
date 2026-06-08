"""Local per-SKU media workspace: raw images, generated prompts, videos, manifest."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.image_resolve import SUPPORTED_EXTS, find_local_image
from src.name_group import base_key_from_path
from src.sku_aliases import canonical_sku

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
PROMPT_RE = re.compile(r"^(prompt[12])_v(\d+)\.(jpg|jpeg|png)$", re.IGNORECASE)
MANIFEST_NAME = "media_manifest.json"


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def sku_workspace_dir(outputs_dir: Path, sku: str) -> Path:
    """Resolve outputs/{SKU}/ trying canonical alias when needed."""
    sku = (sku or "").strip()
    for candidate in (sku, canonical_sku(sku)):
        if not candidate:
            continue
        p = outputs_dir / candidate
        if p.is_dir():
            return p
    return outputs_dir / (canonical_sku(sku) or sku)


def raw_dir(sku_dir: Path) -> Path:
    return sku_dir / "raw"


def videos_dir(sku_dir: Path) -> Path:
    return sku_dir / "videos"


def manifest_path(sku_dir: Path) -> Path:
    return sku_dir / MANIFEST_NAME


def _is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_EXTS


def _is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTS


def scan_source_dir(source_dir: Path) -> dict[str, dict[str, list[Path]]]:
    """Group DAIJE (or pics_raw) files by SKU base name."""
    out: dict[str, dict[str, list[Path]]] = {}
    if not source_dir.exists():
        return out
    for p in sorted(source_dir.iterdir()):
        if not p.is_file():
            continue
        sku = base_key_from_path(p)
        if not sku:
            continue
        bucket = out.setdefault(sku, {"images": [], "videos": []})
        if _is_image(p):
            bucket["images"].append(p)
        elif _is_video(p):
            bucket["videos"].append(p)
    return out


def list_raw_images(sku_dir: Path) -> list[Path]:
    """Raw reference images from outputs/{SKU}/raw/ or legacy DAIJE-style names at root."""
    found: list[Path] = []
    if not sku_dir.is_dir():
        return found
    rd = raw_dir(sku_dir)
    if rd.is_dir():
        found.extend(sorted(p for p in rd.iterdir() if _is_image(p)))
    if found:
        return found
    for p in sorted(sku_dir.iterdir()):
        if _is_image(p) and not PROMPT_RE.match(p.name):
            found.append(p)
    return found


def list_prompt_versions(sku_dir: Path, prompt_id: str) -> list[tuple[int, Path]]:
    """Return sorted (version, path) for prompt1 or prompt2."""
    prompt_id = prompt_id.strip().lower()
    versions: list[tuple[int, Path]] = []
    if not sku_dir.is_dir():
        return versions
    for p in sku_dir.iterdir():
        m = PROMPT_RE.match(p.name)
        if not m or m.group(1).lower() != prompt_id:
            continue
        versions.append((int(m.group(2)), p))
    versions.sort(key=lambda x: x[0])
    return versions


def latest_prompt_path(sku_dir: Path, prompt_id: str, *, version: int | None = None) -> Path | None:
    versions = list_prompt_versions(sku_dir, prompt_id)
    if not versions:
        return None
    if version is not None:
        for v, p in versions:
            if v == version:
                return p
        return None
    return versions[-1][1]


def list_videos(sku_dir: Path) -> list[Path]:
    found: list[Path] = []
    if not sku_dir.is_dir():
        return found
    vd = videos_dir(sku_dir)
    if vd.is_dir():
        found.extend(sorted(p for p in vd.iterdir() if _is_video(p)))
    if found:
        return found
    for p in sorted(sku_dir.iterdir()):
        if _is_video(p):
            found.append(p)
    return found


def relative_workspace_path(sku_dir: Path, path: Path, *, outputs_dir: Path) -> str:
    try:
        rel = path.resolve().relative_to(outputs_dir.resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return path.name


@dataclass
class SkuMediaIndex:
    sku: str
    workspace_dir: Path
    raw_images: list[Path] = field(default_factory=list)
    prompt1_versions: list[tuple[int, Path]] = field(default_factory=list)
    prompt2_versions: list[tuple[int, Path]] = field(default_factory=list)
    videos: list[Path] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def latest_prompt1(self) -> Path | None:
        return self.prompt1_versions[-1][1] if self.prompt1_versions else None

    @property
    def latest_prompt2(self) -> Path | None:
        return self.prompt2_versions[-1][1] if self.prompt2_versions else None


def index_sku_media(*, outputs_dir: Path, sku: str) -> SkuMediaIndex:
    sku = (sku or "").strip()
    sku_dir = sku_workspace_dir(outputs_dir, sku)
    manifest = load_manifest(sku_dir)
    return SkuMediaIndex(
        sku=sku,
        workspace_dir=sku_dir,
        raw_images=list_raw_images(sku_dir),
        prompt1_versions=list_prompt_versions(sku_dir, "prompt1"),
        prompt2_versions=list_prompt_versions(sku_dir, "prompt2"),
        videos=list_videos(sku_dir),
        manifest=manifest,
    )


def default_manifest(sku: str) -> dict[str, Any]:
    return {
        "sku": sku,
        "raw": [],
        "generated": {"prompt1": [], "prompt2": []},
        "videos": [],
        "shopify_media_ids": [],
        "review_status": "pending_review",
        "upload_status": "pending",
        "approved": {"prompt1_version": None, "prompt2_version": None},
        "updated_at_utc": _now_utc(),
    }


def load_manifest(sku_dir: Path) -> dict[str, Any]:
    mp = manifest_path(sku_dir)
    if not mp.is_file():
        return {}
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_manifest(sku_dir: Path, manifest: dict[str, Any]) -> Path:
    sku_dir.mkdir(parents=True, exist_ok=True)
    manifest["updated_at_utc"] = _now_utc()
    mp = manifest_path(sku_dir)
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(mp)
    return mp


def refresh_manifest(
    *,
    outputs_dir: Path,
    sku: str,
    patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild manifest from on-disk files, preserving approval/upload fields."""
    sku_dir = sku_workspace_dir(outputs_dir, sku)
    existing = load_manifest(sku_dir)
    manifest = default_manifest(sku)
    manifest.update({k: v for k, v in existing.items() if k not in ("raw", "generated", "videos")})

    prev = {str(i.get("filename") or ""): str(i.get("source") or "") for i in existing.get("raw") or []}
    prev_v = {str(i.get("filename") or ""): str(i.get("source") or "") for i in existing.get("videos") or []}
    manifest["raw"] = [
        {
            "path": relative_workspace_path(sku_dir, p, outputs_dir=outputs_dir),
            "filename": p.name,
            **({"source": prev[p.name]} if prev.get(p.name) else {}),
        }
        for p in list_raw_images(sku_dir)
    ]
    manifest["generated"] = {
        "prompt1": [
            {
                "version": v,
                "path": relative_workspace_path(sku_dir, p, outputs_dir=outputs_dir),
                "filename": p.name,
            }
            for v, p in list_prompt_versions(sku_dir, "prompt1")
        ],
        "prompt2": [
            {
                "version": v,
                "path": relative_workspace_path(sku_dir, p, outputs_dir=outputs_dir),
                "filename": p.name,
            }
            for v, p in list_prompt_versions(sku_dir, "prompt2")
        ],
    }
    manifest["videos"] = [
        {
            "path": relative_workspace_path(sku_dir, p, outputs_dir=outputs_dir),
            "filename": p.name,
            **({"source": prev_v[p.name]} if prev_v.get(p.name) else {}),
        }
        for p in list_videos(sku_dir)
    ]
    if patch:
        manifest.update(patch)
    save_manifest(sku_dir, manifest)
    return manifest


def _file_digest(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_source_names(sku_dir: Path) -> set[str]:
    manifest = load_manifest(sku_dir)
    names: set[str] = set()
    for section in ("raw", "videos"):
        for item in manifest.get(section) or []:
            src = str(item.get("source") or "").strip()
            if src:
                names.add(src)
    return names


def _folder_has_digest(folder: Path, digest: str) -> bool:
    if not folder.is_dir():
        return False
    for p in folder.iterdir():
        if p.is_file() and _file_digest(p) == digest:
            return True
    return False


def dedupe_sku_workspace_media(sku_dir: Path) -> dict[str, int]:
    """
    Remove byte-identical raw/video duplicates and renumber files sequentially.
    Returns counts of removed files per category.
    """
    result = {"raw_removed": 0, "videos_removed": 0}
    if not sku_dir.is_dir():
        return result

    for folder, prefix, is_video in (
        (raw_dir(sku_dir), "raw", False),
        (videos_dir(sku_dir), "video", True),
    ):
        if not folder.is_dir():
            continue
        checker = _is_video if is_video else _is_image
        files = sorted(p for p in folder.iterdir() if checker(p))
        seen: set[str] = set()
        unique: list[Path] = []
        for p in files:
            digest = _file_digest(p)
            if digest in seen:
                p.unlink(missing_ok=True)
                result["raw_removed" if not is_video else "videos_removed"] += 1
                continue
            seen.add(digest)
            unique.append(p)

        for i, p in enumerate(unique, start=1):
            ext = p.suffix.lower()
            new_name = f"{prefix}_{i}{ext}"
            new_path = folder / new_name
            if p.name != new_name:
                if new_path.exists():
                    new_path.unlink()
                p.rename(new_path)

    return result


def dedupe_all_workspaces(outputs_dir: Path) -> list[dict[str, int]]:
    results: list[dict[str, int]] = []
    for p in sorted(outputs_dir.iterdir()):
        if not p.is_dir() or p.name.startswith("_"):
            continue
        counts = dedupe_sku_workspace_media(p)
        if counts["raw_removed"] or counts["videos_removed"]:
            results.append({"sku": p.name, **counts})
        refresh_manifest(outputs_dir=outputs_dir, sku=p.name)
    return results


def _next_raw_name(sku_dir: Path, src: Path) -> Path:
    rd = raw_dir(sku_dir)
    rd.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    n = 1
    while True:
        candidate = rd / f"raw_{n}{ext}"
        if not candidate.exists():
            return candidate
        n += 1


def _next_video_name(sku_dir: Path, src: Path) -> Path:
    vd = videos_dir(sku_dir)
    vd.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    n = 1
    while True:
        candidate = vd / f"video_{n}{ext}"
        if not candidate.exists():
            return candidate
        n += 1


def organize_sku_from_source(
    *,
    source_dir: Path,
    outputs_dir: Path,
    sku: str,
    copy: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Copy DAIJE/pics_raw assets into outputs/{SKU}/raw/ and outputs/{SKU}/videos/.
    Existing prompt1_v*.jpg / prompt2_v*.jpg at SKU root are preserved.
    """
    sku = (sku or "").strip()
    grouped = scan_source_dir(source_dir)
    assets = grouped.get(sku) or grouped.get(canonical_sku(sku)) or {"images": [], "videos": []}
    sku_dir = outputs_dir / (canonical_sku(sku) or sku)
    result: dict[str, Any] = {
        "sku": sku,
        "workspace_dir": str(sku_dir),
        "raw_copied": [],
        "videos_copied": [],
        "skipped": [],
    }
    if dry_run:
        result["raw_copied"] = [p.name for p in assets.get("images") or []]
        result["videos_copied"] = [p.name for p in assets.get("videos") or []]
        return result

    sku_dir.mkdir(parents=True, exist_ok=True)
    op = shutil.copy2 if copy else shutil.move
    known_sources = _manifest_source_names(sku_dir)
    source_map: dict[str, str] = {}

    for src in assets.get("images") or []:
        digest = _file_digest(src)
        if src.name in known_sources or _folder_has_digest(raw_dir(sku_dir), digest):
            result["skipped"].append(src.name)
            continue
        dest = _next_raw_name(sku_dir, src)
        op(str(src), str(dest))
        source_map[dest.name] = src.name
        known_sources.add(src.name)
        result["raw_copied"].append(dest.name)

    for src in assets.get("videos") or []:
        digest = _file_digest(src)
        if src.name in known_sources or _folder_has_digest(videos_dir(sku_dir), digest):
            result["skipped"].append(src.name)
            continue
        dest = _next_video_name(sku_dir, src)
        op(str(src), str(dest))
        source_map[dest.name] = src.name
        known_sources.add(src.name)
        result["videos_copied"].append(dest.name)

    manifest = refresh_manifest(outputs_dir=outputs_dir, sku=sku)
    for section, key in (("raw", "raw"), ("videos", "videos")):
        for item in manifest.get(section) or []:
            fname = str(item.get("filename") or "")
            if fname in source_map:
                item["source"] = source_map[fname]
    save_manifest(sku_dir, manifest)
    return result


def organize_all_from_source(
    *,
    source_dir: Path,
    outputs_dir: Path,
    skus: list[str] | None = None,
    copy: bool = True,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    grouped = scan_source_dir(source_dir)
    targets = sorted(skus) if skus else sorted(grouped.keys())
    results: list[dict[str, Any]] = []
    for sku in targets:
        if sku not in grouped and canonical_sku(sku) not in grouped:
            results.append({"sku": sku, "skipped": True, "reason": "no source files"})
            continue
        results.append(
            organize_sku_from_source(
                source_dir=source_dir,
                outputs_dir=outputs_dir,
                sku=sku,
                copy=copy,
                dry_run=dry_run,
            )
        )
    return results


def prune_prompt_versions_for_sku(
    outputs_dir: Path,
    sku: str,
    *,
    keep_prompt1_version: int | None = None,
    keep_prompt2_version: int | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Keep one prompt1 and one prompt2 version; delete older files."""
    sku_dir = sku_workspace_dir(outputs_dir, sku)
    if not sku_dir.is_dir():
        return {"sku": sku, "deleted": [], "kept": {}}

    deleted: list[str] = []
    kept: dict[str, str] = {}

    for prompt_id, keep_v in (("prompt1", keep_prompt1_version), ("prompt2", keep_prompt2_version)):
        versions = list_prompt_versions(sku_dir, prompt_id)
        if not versions:
            continue
        if keep_v is None:
            keep_v = versions[-1][0]
        keep_path = None
        for v, p in versions:
            if v == keep_v:
                keep_path = p
            else:
                if not dry_run:
                    try:
                        p.unlink()
                    except OSError:
                        pass
                deleted.append(p.name)
        if keep_path is not None:
            kept[prompt_id] = keep_path.name

    if deleted and not dry_run:
        refresh_manifest(outputs_dir=outputs_dir, sku=sku)

    return {"sku": sku, "deleted": deleted, "kept": kept}


def prune_all_prompt_versions(
    outputs_dir: Path,
    *,
    review_store: "ReviewStore | None" = None,
    skus: list[str] | None = None,
    dry_run: bool = False,
) -> list[dict[str, object]]:
    """Prune prompt versions for all workspace SKUs (or a provided list)."""
    if skus is None:
        skus = sorted(
            p.name
            for p in outputs_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
    results: list[dict[str, object]] = []
    for sku in skus:
        p1 = p2 = None
        if review_store is not None:
            rec = review_store.get(sku)
            if rec is not None:
                p1 = rec.approved_prompt1_version
                p2 = rec.approved_prompt2_version
        results.append(
            prune_prompt_versions_for_sku(
                outputs_dir,
                sku,
                keep_prompt1_version=p1,
                keep_prompt2_version=p2,
                dry_run=dry_run,
            )
        )
    return results


def resolve_thumbnail_path(
    *,
    outputs_dir: Path,
    sku: str,
    images_dir: Path | None = None,
    prompt2_version: int | None = None,
) -> Path | None:
    """prompt2 thumbnail, else raw fallback."""
    sku_dir = sku_workspace_dir(outputs_dir, sku)
    p2 = latest_prompt_path(sku_dir, "prompt2", version=prompt2_version)
    if p2 and p2.is_file():
        return p2
    raw = list_raw_images(sku_dir)
    if raw:
        return raw[0]
    if images_dir is not None:
        legacy = find_local_image(images_dir, sku, "")
        if legacy and legacy.is_file():
            return legacy
    return None


def thumbnail_relative_path(
    *,
    outputs_dir: Path,
    sku: str,
    prompt2_version: int | None = None,
) -> str:
    """Relative path like SKU/prompt2_v1.jpg for XLSX column."""
    sku_dir = sku_workspace_dir(outputs_dir, sku)
    p = latest_prompt_path(sku_dir, "prompt2", version=prompt2_version)
    if p is None:
        raw = list_raw_images(sku_dir)
        if raw:
            p = raw[0]
        else:
            return ""
    ws_sku = sku_dir.name
    if p.parent.resolve() == sku_dir.resolve():
        return f"{ws_sku}/{p.name}"
    try:
        rel = p.resolve().relative_to(sku_dir.resolve())
        return f"{ws_sku}/{rel}".replace("\\", "/")
    except ValueError:
        return f"{ws_sku}/{p.name}"
