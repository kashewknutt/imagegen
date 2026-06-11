"""Sync Drive SKU workspaces with a local working cache."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.drive_client import (
    DriveFile,
    create_folder,
    delete_file,
    download_file_to_cache,
    list_children,
    upload_or_update_file,
)
from src.drive_review_config import DriveReviewConfig
from src.drive_review_log import get_logger
from src.media_workspace import MANIFEST_NAME, PROMPT_RE, list_raw_images, list_videos

PROMPT_VERSION_FILE_RE = re.compile(r"^prompt([12])_v(\d+)\.", re.IGNORECASE)
from src.typo_sku_cleanup import list_output_sku_dirs

FOLDER_MIME = "application/vnd.google-apps.folder"
SKU_DIR_RE = re.compile(r"^[A-Z]{2,}[A-Z0-9-]{3,}$", re.IGNORECASE)
SKIP_NAMES = {"_download_cache", "_leases", "_temp", "_semaphore", "_semaphores", "rebuild_executor", "Stock"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}


@dataclass
class DriveSkuIndex:
    sku: str
    folder_id: str
    files: list[DriveFile] = field(default_factory=list)


def _is_sku_name(name: str) -> bool:
    if name in SKIP_NAMES or name.startswith(".") or name.startswith("_"):
        return False
    return bool(SKU_DIR_RE.match(name))


class DriveOutputsSync:
    def __init__(self, cfg: DriveReviewConfig, service) -> None:
        self.cfg = cfg
        self.service = service
        self._folder_map_path = cfg.drive_cache_dir / "drive_folder_map.json"
        self._file_map_path = cfg.drive_cache_dir / "drive_file_map.json"

    def _load_map(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_map(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def list_sku_folders(self, *, refresh: bool = False) -> dict[str, str]:
        cached = self._load_map(self._folder_map_path)
        if cached.get("parent_id") == self.cfg.drive_outputs_folder_id and cached.get("folders") and not refresh:
            return dict(cached["folders"])

        folders = list_children(
            service=self.service,
            parent_id=self.cfg.drive_outputs_folder_id,
            mime_type=FOLDER_MIME,
        )
        out: dict[str, str] = {}
        for f in folders:
            if _is_sku_name(f.name):
                out[f.name] = f.id
        self._save_map(
            self._folder_map_path,
            {
                "parent_id": self.cfg.drive_outputs_folder_id,
                "folders": out,
                "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            },
        )
        return out

    def ensure_sku_folder(self, sku: str) -> str:
        folders = self.list_sku_folders()
        if sku in folders:
            return folders[sku]
        created = create_folder(
            service=self.service,
            parent_id=self.cfg.drive_outputs_folder_id,
            name=sku,
        )
        folders[sku] = created.id
        self._save_map(
            self._folder_map_path,
            {
                "parent_id": self.cfg.drive_outputs_folder_id,
                "folders": folders,
            },
        )
        return created.id

    def local_sku_dir(self, sku: str) -> Path:
        return self.cfg.outputs_dir / sku

    def list_sku_filenames(self, folder_id: str) -> list[str]:
        """List all file names in a SKU folder (metadata only, no download)."""
        names: list[str] = []
        for item in list_children(service=self.service, parent_id=folder_id):
            if item.mime_type == FOLDER_MIME:
                for sub in list_children(service=self.service, parent_id=item.id):
                    if sub.mime_type != FOLDER_MIME:
                        names.append(f"{item.name}/{sub.name}")
            else:
                names.append(item.name)
        return names

    def scan_sku_metadata(self, sku: str, *, folder_id: str | None = None) -> dict[str, Any]:
        """Fast remote scan: file names + prompt/raw/video counts without downloading."""
        folder_id = folder_id or self.ensure_sku_folder(sku)
        filenames = self.list_sku_filenames(folder_id)
        has_p1 = has_p2 = False
        raw_count = video_count = 0
        for name in filenames:
            base = name.split("/")[-1]
            m = PROMPT_RE.match(base)
            if m:
                if m.group(1).lower() == "prompt1":
                    has_p1 = True
                else:
                    has_p2 = True
                continue
            ext = Path(base).suffix.lower()
            if ext in {".jpg", ".jpeg", ".png", ".webp"}:
                raw_count += 1
            elif ext in {".mp4", ".mov", ".webm", ".m4v"}:
                video_count += 1
        return {
            "sku": sku,
            "folder_id": folder_id,
            "filenames": filenames,
            "has_prompt1": has_p1,
            "has_prompt2": has_p2,
            "raw_count": raw_count,
            "video_count": video_count,
        }

    def list_local_sku_dirs(self) -> list[str]:
        """SKU folders from local outputs/ (no Drive API)."""
        return list_output_sku_dirs(self.cfg.outputs_dir)

    def ensure_local_sku(self, sku: str) -> Path:
        """Return local SKU workspace path; never downloads from Drive."""
        sku_dir = self.local_sku_dir(sku)
        if not sku_dir.is_dir():
            raise FileNotFoundError(f"Local workspace missing: {sku_dir}")
        return sku_dir

    def pull_sku_from_drive(
        self,
        sku: str,
        *,
        folder_id: str | None = None,
        include_raw: bool = True,
        include_videos: bool = False,
        skip_prompt_slots: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """
        Download Drive workspace files into local outputs/{sku}/.
        Skips prompt slots listed in skip_prompt_slots (e.g. locally edited, unsaved).
        """
        skip_prompt_slots = skip_prompt_slots or frozenset()
        folder_id = folder_id or self.list_sku_folders().get(sku)
        if not folder_id:
            return {"sku": sku, "downloaded": [], "skipped": ["no_drive_folder"]}

        sku_dir = self.local_sku_dir(sku)
        sku_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[str] = []
        skipped: list[str] = []

        def _dest_for(name: str, parent_name: str | None = None) -> Path:
            if parent_name:
                return sku_dir / parent_name / name
            return sku_dir / name

        def _pull_file(item: DriveFile, dest: Path) -> None:
            if dest.exists() and dest.stat().st_size > 0:
                skipped.append(str(dest.relative_to(self.cfg.outputs_dir)))
                return
            download_file_to_cache(service=self.service, file_id=item.id, cache_path=dest)
            downloaded.append(str(dest.relative_to(self.cfg.outputs_dir)))

        for item in list_children(service=self.service, parent_id=folder_id):
            if item.mime_type == FOLDER_MIME:
                if item.name == "raw" and include_raw:
                    raw_dir = sku_dir / "raw"
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    for sub in list_children(service=self.service, parent_id=item.id):
                        if sub.mime_type != FOLDER_MIME:
                            _pull_file(sub, raw_dir / sub.name)
                elif item.name == "videos" and include_videos:
                    vid_dir = sku_dir / "videos"
                    vid_dir.mkdir(parents=True, exist_ok=True)
                    for sub in list_children(service=self.service, parent_id=item.id):
                        if sub.mime_type != FOLDER_MIME:
                            _pull_file(sub, vid_dir / sub.name)
                continue
            m = PROMPT_VERSION_FILE_RE.match(item.name)
            if m:
                slot = "prompt1" if m.group(1) == "1" else "prompt2"
                if slot in skip_prompt_slots:
                    skipped.append(f"{sku}/{item.name}")
                    continue
            _pull_file(item, _dest_for(item.name))

        return {"sku": sku, "downloaded": downloaded, "skipped": skipped}

    def push_prompt_files(
        self,
        sku: str,
        *,
        folder_id: str | None = None,
        slots: list[str] | None = None,
        prune_old: bool = True,
    ) -> dict[str, Any]:
        """Upload only prompt1/prompt2 files for a SKU; never touches raw/ or videos/."""
        log = get_logger()
        sku_dir = self.local_sku_dir(sku)
        if not sku_dir.is_dir():
            raise FileNotFoundError(sku_dir)
        folder_id = folder_id or self.ensure_sku_folder(sku)
        remote_names = set(self.list_sku_filenames(folder_id))
        file_map = self._load_map(self._file_map_path).setdefault("files", {})
        want_slots = {s.strip().lower() for s in (slots or ["prompt1", "prompt2"])}
        uploaded: list[str] = []

        def _push_file(local_path: Path) -> None:
            rel_key = f"{sku}/{local_path.name}"
            name = local_path.name
            existing_id = file_map.get(rel_key)
            if existing_id:
                try:
                    upload_or_update_file(
                        service=self.service,
                        local_path=local_path,
                        parent_id=folder_id,
                        name=name,
                        file_id=existing_id,
                    )
                    uploaded.append(rel_key)
                    return
                except Exception:
                    existing_id = None
            for rf in list_children(service=self.service, parent_id=folder_id):
                if rf.name == name and rf.mime_type != FOLDER_MIME:
                    existing_id = rf.id
                    break
            result = upload_or_update_file(
                service=self.service,
                local_path=local_path,
                parent_id=folder_id,
                name=name,
                file_id=existing_id,
            )
            file_map[rel_key] = result.id
            uploaded.append(rel_key)

        latest_paths: dict[str, Path] = {}
        latest_versions: dict[str, int] = {}
        for path in sorted(sku_dir.iterdir()):
            if not path.is_file():
                continue
            m = PROMPT_VERSION_FILE_RE.match(path.name)
            if not m:
                continue
            slot = "prompt1" if m.group(1) == "1" else "prompt2"
            if slot not in want_slots:
                continue
            ver = int(m.group(2))
            if ver >= latest_versions.get(slot, 0):
                latest_versions[slot] = ver
                latest_paths[slot] = path
        for path in latest_paths.values():
            _push_file(path)

        pruned: list[str] = []
        if prune_old and latest_versions:
            pruned = self.prune_remote_prompt_versions(
                sku,
                keep_p1=latest_versions.get("prompt1"),
                keep_p2=latest_versions.get("prompt2"),
            )

        self._save_map(self._file_map_path, {"files": file_map})
        log.info("Pushed prompt files for %s: %s", sku, uploaded)
        return {
            "sku": sku,
            "uploaded": uploaded,
            "pruned_old_prompts": pruned,
            "latest_versions": latest_versions,
            "folder_id": folder_id,
        }

    def check_drive_raw_videos(self, sku: str) -> dict[str, Any]:
        """Compare local raw images and videos against Drive folder (metadata only)."""
        sku_dir = self.local_sku_dir(sku)
        local_raw = [p.name for p in list_raw_images(sku_dir)]
        local_videos = [p.name for p in list_videos(sku_dir)]
        meta = self.scan_sku_metadata(sku)
        remote_set = set(meta.get("filenames") or [])

        def _split(names: list[str], subdir: str) -> tuple[list[str], list[str]]:
            on_drive: list[str] = []
            missing: list[str] = []
            for name in names:
                keys = {name, f"{subdir}/{name}"}
                if keys & remote_set:
                    on_drive.append(name)
                else:
                    missing.append(name)
            return on_drive, missing

        raw_on, raw_missing = _split(local_raw, "raw")
        videos_on, videos_missing = _split(local_videos, "videos")
        return {
            "sku": sku,
            "local_raw": local_raw,
            "local_videos": local_videos,
            "drive_raw_count": meta.get("raw_count", 0),
            "drive_video_count": meta.get("video_count", 0),
            "raw_on_drive": raw_on,
            "raw_missing_on_drive": raw_missing,
            "videos_on_drive": videos_on,
            "videos_missing_on_drive": videos_missing,
            "all_raw_on_drive": len(local_raw) == 0 or not raw_missing,
            "all_videos_on_drive": len(local_videos) == 0 or not videos_missing,
        }

    def prune_remote_prompt_versions(
        self,
        sku: str,
        *,
        keep_p1: int | None,
        keep_p2: int | None,
    ) -> list[str]:
        """Delete older prompt1/prompt2 files on Drive; keep only the approved latest versions."""
        log = get_logger()
        folder_id = self.ensure_sku_folder(sku)
        deleted: list[str] = []
        file_map = self._load_map(self._file_map_path).setdefault("files", {})
        keys_to_drop: list[str] = []

        for item in list_children(service=self.service, parent_id=folder_id):
            if item.mime_type == FOLDER_MIME:
                continue
            m = PROMPT_VERSION_FILE_RE.match(item.name)
            if not m:
                continue
            slot, ver = m.group(1), int(m.group(2))
            keep = keep_p1 if slot == "1" else keep_p2
            if keep is None or ver == keep:
                continue
            log.info("Deleting old Drive prompt file %s/%s", sku, item.name)
            delete_file(service=self.service, file_id=item.id)
            deleted.append(item.name)
            rel_key = f"{sku}/{item.name}"
            keys_to_drop.append(rel_key)

        if keys_to_drop:
            for key in keys_to_drop:
                file_map.pop(key, None)
            self._save_map(self._file_map_path, {"files": file_map})
        return deleted

    def push_sku(
        self,
        sku: str,
        *,
        folder_id: str | None = None,
        skip_existing_raw_videos: bool = True,
    ) -> dict[str, Any]:
        log = get_logger()
        log.info("Pushing SKU %s to Drive...", sku)
        sku_dir = self.local_sku_dir(sku)
        if not sku_dir.is_dir():
            raise FileNotFoundError(sku_dir)
        folder_id = folder_id or self.ensure_sku_folder(sku)
        remote_children = {f.name: f for f in list_children(service=self.service, parent_id=folder_id)}
        remote_names = set(self.list_sku_filenames(folder_id))
        file_map = self._load_map(self._file_map_path).setdefault("files", {})
        uploaded: list[str] = []
        skipped_existing: list[str] = []

        def _remote_has(rel_key: str, filename: str) -> bool:
            return rel_key in remote_names or filename in remote_names

        def _push_file(local_path: Path, parent_id: str, rel_key: str) -> None:
            nonlocal uploaded
            name = local_path.name
            existing_id = file_map.get(rel_key)
            if existing_id:
                try:
                    upload_or_update_file(
                        service=self.service,
                        local_path=local_path,
                        parent_id=parent_id,
                        name=name,
                        file_id=existing_id,
                    )
                    uploaded.append(rel_key)
                    return
                except Exception:
                    existing_id = None
            for rf in list_children(service=self.service, parent_id=parent_id):
                if rf.name == name and rf.mime_type != FOLDER_MIME:
                    existing_id = rf.id
                    break
            result = upload_or_update_file(
                service=self.service,
                local_path=local_path,
                parent_id=parent_id,
                name=name,
                file_id=existing_id,
            )
            file_map[rel_key] = result.id
            uploaded.append(rel_key)

        for path in sorted(sku_dir.iterdir()):
            if path.is_dir():
                if path.name in {"raw", "videos"} and skip_existing_raw_videos:
                    for sub_file in sorted(path.iterdir()):
                        if not sub_file.is_file():
                            continue
                        rel_key = f"{sku}/{path.name}/{sub_file.name}"
                        if _remote_has(rel_key, sub_file.name):
                            skipped_existing.append(rel_key)
                            continue
                        sub_folder = remote_children.get(path.name)
                        if sub_folder and sub_folder.mime_type == FOLDER_MIME:
                            sub_parent = sub_folder.id
                        else:
                            created = create_folder(service=self.service, parent_id=folder_id, name=path.name)
                            sub_parent = created.id
                        _push_file(sub_file, sub_parent, rel_key)
                    continue
                sub_folder = remote_children.get(path.name)
                if sub_folder and sub_folder.mime_type == FOLDER_MIME:
                    sub_parent = sub_folder.id
                else:
                    created = create_folder(service=self.service, parent_id=folder_id, name=path.name)
                    sub_parent = created.id
                for sub_file in sorted(path.iterdir()):
                    if sub_file.is_file():
                        _push_file(sub_file, sub_parent, f"{sku}/{path.name}/{sub_file.name}")
                continue
            if path.is_file():
                rel_key = f"{sku}/{path.name}"
                if skip_existing_raw_videos:
                    ext = path.suffix.lower()
                    is_raw_img = ext in IMAGE_EXTS and not PROMPT_RE.match(path.name)
                    is_vid = ext in {".mp4", ".mov", ".webm", ".m4v"}
                    if (is_raw_img or is_vid) and _remote_has(rel_key, path.name):
                        skipped_existing.append(rel_key)
                        continue
                _push_file(path, folder_id, rel_key)

        self._save_map(self._file_map_path, {"files": file_map})
        log.info(
            "Pushed SKU %s (%d file(s), skipped %d existing raw/video)",
            sku,
            len(uploaded),
            len(skipped_existing),
        )
        return {
            "sku": sku,
            "uploaded": uploaded,
            "skipped_existing_raw_videos": skipped_existing,
            "folder_id": folder_id,
        }

    def delete_sku_folder_remote(self, sku: str) -> None:
        """Delete SKU folder on Drive only; local outputs/ is unchanged."""
        log = get_logger()
        folders = self.list_sku_folders(refresh=True)
        folder_id = folders.get(sku)
        if folder_id:
            log.info("Deleting Drive folder for %s (id=%s)", sku, folder_id)
            delete_file(service=self.service, file_id=folder_id)
            folders.pop(sku, None)
            self._save_map(
                self._folder_map_path,
                {"parent_id": self.cfg.drive_outputs_folder_id, "folders": folders},
            )

    def pull_all_metadata(self) -> list[DriveSkuIndex]:
        folders = self.list_sku_folders(refresh=True)
        out: list[DriveSkuIndex] = []
        for sku, folder_id in sorted(folders.items()):
            files = list_children(service=self.service, parent_id=folder_id)
            out.append(DriveSkuIndex(sku=sku, folder_id=folder_id, files=files))
        return out

    def sync_review_state_local(self) -> Path:
        """Use local review_state.json; no Drive download."""
        local = self.cfg.review_state_path
        if not local.is_file():
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text('{"skus": {}}', encoding="utf-8")
            get_logger().info("Created empty local review_state.json at %s", local)
        return local

    def sync_review_state_push(self) -> None:
        local = self.cfg.review_state_path
        if not local.is_file():
            return
        children = list_children(service=self.service, parent_id=self.cfg.drive_outputs_folder_id)
        existing_id = None
        for f in children:
            if f.name == "review_state.json":
                existing_id = f.id
                break
        upload_or_update_file(
            service=self.service,
            local_path=local,
            parent_id=self.cfg.drive_outputs_folder_id,
            name="review_state.json",
            file_id=existing_id,
            mime_type="application/json",
        )

    def push_file_to_outputs_root(self, local_path: Path, name: str | None = None) -> DriveFile:
        local_path = Path(local_path)
        upload_name = name or local_path.name
        children = list_children(service=self.service, parent_id=self.cfg.drive_outputs_folder_id)
        existing_id = None
        for f in children:
            if f.name == upload_name and f.mime_type != FOLDER_MIME:
                existing_id = f.id
                break
        return upload_or_update_file(
            service=self.service,
            local_path=local_path,
            parent_id=self.cfg.drive_outputs_folder_id,
            name=upload_name,
            file_id=existing_id,
        )
