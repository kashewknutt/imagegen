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
    list_children,
    upload_or_update_file,
)
from src.drive_review_config import DriveReviewConfig
from src.drive_review_log import get_logger
from src.media_workspace import MANIFEST_NAME, PROMPT_RE
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

    def push_sku(self, sku: str, *, folder_id: str | None = None) -> dict[str, Any]:
        log = get_logger()
        log.info("Pushing SKU %s to Drive...", sku)
        sku_dir = self.local_sku_dir(sku)
        if not sku_dir.is_dir():
            raise FileNotFoundError(sku_dir)
        folder_id = folder_id or self.ensure_sku_folder(sku)
        remote_children = {f.name: f for f in list_children(service=self.service, parent_id=folder_id)}
        file_map = self._load_map(self._file_map_path).setdefault("files", {})
        uploaded: list[str] = []

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
                _push_file(path, folder_id, f"{sku}/{path.name}")

        self._save_map(self._file_map_path, {"files": file_map})
        log.info("Pushed SKU %s (%d file(s))", sku, len(uploaded))
        return {"sku": sku, "uploaded": uploaded, "folder_id": folder_id}

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
