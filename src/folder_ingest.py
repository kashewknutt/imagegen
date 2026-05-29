from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .image_resolve import SUPPORTED_EXTS
from .name_group import base_key_from_path


@dataclass(frozen=True)
class FolderGroup:
    key: str
    images: list[Path]


def iter_groups(images_dir: Path) -> list[FolderGroup]:
    groups: dict[str, list[Path]] = {}
    for p in sorted(images_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        k = base_key_from_path(p)
        groups.setdefault(k, []).append(p)
    return [FolderGroup(key=k, images=v) for k, v in sorted(groups.items(), key=lambda kv: kv[0])]

