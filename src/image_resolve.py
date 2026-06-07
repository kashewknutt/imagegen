from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from PIL import Image


SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


@dataclass(frozen=True)
class ResolvedImage:
    source: str  # "local" | "download"
    path: Path


def _iter_image_files(images_dir: Path) -> Iterable[Path]:
    for p in images_dir.iterdir():
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            yield p


def find_local_image_by_sku(images_dir: Path, sku: str) -> Path | None:
    s = (sku or "").strip()
    if not s:
        return None

    # If SKU actually contains a camera filename (e.g. "DSC03042.ARW" or "DSC03042"),
    # match against file stem first.
    sku_stem_norm = _norm(Path(s).stem)
    if sku_stem_norm:
        exact: list[Path] = []
        for p in _iter_image_files(images_dir):
            if _norm(p.stem) == sku_stem_norm:
                exact.append(p)
        if exact:
            exact.sort(key=lambda p: (len(p.name), p.name))
            return exact[0]

    sku_upper = s.upper()
    candidates: list[Path] = []
    for p in _iter_image_files(images_dir):
        stem = p.stem.upper()
        if stem == sku_upper or stem.startswith(f"{sku_upper}_") or stem.startswith(f"{sku_upper} "):
            candidates.append(p)

    if not candidates:
        return None

    def score(p: Path) -> tuple[int, int, str]:
        ext = p.suffix.lower()
        ext_rank = {".png": 0, ".jpg": 1, ".jpeg": 1, ".webp": 2, ".heic": 3, ".heif": 3}.get(ext, 9)
        return (ext_rank, len(p.name), p.name)

    candidates.sort(key=score)
    return candidates[0]


def _norm(s: str) -> str:
    return "".join(ch.lower() for ch in s if ch.isalnum())


def find_local_image_by_title(images_dir: Path, title: str) -> Path | None:
    """
    Some exports store the camera filename in the Title field, e.g. "DSC03042.ARW".
    We match against the file stem in images_dir (typically JPG exports), ignoring extension.
    """
    t = (title or "").strip()
    if not t:
        return None

    # If title looks like a filename, use its stem.
    stem = Path(t).stem
    target = _norm(stem)
    if not target:
        return None

    candidates: list[Path] = []
    for p in _iter_image_files(images_dir):
        if _norm(p.stem) == target:
            candidates.append(p)

    if not candidates:
        # fallback: substring match on normalized names
        for p in _iter_image_files(images_dir):
            if target in _norm(p.stem):
                candidates.append(p)

    if not candidates:
        return None

    def score(p: Path) -> tuple[int, int, str]:
        ext = p.suffix.lower()
        ext_rank = {".png": 0, ".jpg": 1, ".jpeg": 1, ".webp": 2, ".heic": 3, ".heif": 3}.get(ext, 9)
        return (ext_rank, len(p.name), p.name)

    candidates.sort(key=score)
    return candidates[0]


def find_local_image(images_dir: Path, sku: str, title: str) -> Path | None:
    by_sku = find_local_image_by_sku(images_dir, sku)
    if by_sku:
        return by_sku
    return find_local_image_by_title(images_dir, title)

def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def download_first_image_url(urls: list[str], cache_dir: Path, timeout_s: int = 30) -> Path | None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        u = url.strip()
        if not u or not u.startswith("http"):
            continue
        h = hashlib.sha256(u.encode("utf-8")).hexdigest()[:16]
        # Try to preserve extension if present, else default to .jpg
        ext = Path(u.split("?")[0]).suffix.lower()
        if ext not in SUPPORTED_EXTS:
            ext = ".jpg"
        out_path = cache_dir / f"download_{h}{ext}"
        if out_path.exists():
            return out_path
        try:
            resp = requests.get(u, timeout=timeout_s)
            resp.raise_for_status()
            out_path.write_bytes(resp.content)
            # quick sanity open
            Image.open(out_path).verify()
            return out_path
        except Exception:
            if out_path.exists():
                try:
                    out_path.unlink()
                except Exception:
                    pass
            continue
    return None


def parse_media_links(media_links_raw: str) -> list[str]:
    if not media_links_raw:
        return []
    # The CSV field often contains comma-separated urls.
    parts = [p.strip() for p in media_links_raw.split(",")]
    # Prefer image urls (png/jpg) over videos
    image_like = [p for p in parts if any(x in p.lower() for x in [".png", ".jpg", ".jpeg", ".webp", "tr=f-jpg", "tr=f-png"])]
    return image_like or parts


def open_pil(path: Path) -> Image.Image:
    img = Image.open(path)
    return img.convert("RGB")


def guess_mime_for_google(path: Path) -> str:
    mime = _guess_mime(path)
    if mime.startswith("image/"):
        return mime
    # fallback for common cases
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(ext, "image/jpeg")
