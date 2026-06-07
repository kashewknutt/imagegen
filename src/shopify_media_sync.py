"""Build local image upload lists and sync them to Shopify product media."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from src.image_resolve import SUPPORTED_EXTS, find_local_image
from src.name_group import base_key_from_path
from src.sku_aliases import canonical_sku

log = logging.getLogger(__name__)


def _list_pics_raw(images_dir: Path, sku: str) -> list[Path]:
    if not images_dir.exists():
        return []
    return sorted(
        p
        for p in images_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS and base_key_from_path(p) == sku
    )


def images_for_sku(cfg, sku: str) -> list[tuple[Path, str]]:
    """
    Shopify upload order: prompt2 (thumbnail), prompt1, then all pics_raw references.
    """
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(path: Path | None, alt: str) -> None:
        if path is None or not path.is_file():
            return
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        out.append((path, alt))

    for candidate in (sku, canonical_sku(sku)):
        sku_dir = cfg.outputs_dir / candidate
        add(sku_dir / "prompt2_v1.jpg", f"{sku} - Product")
        add(sku_dir / "prompt1_v1.jpg", f"{sku} - Lifestyle")

    for p in _list_pics_raw(cfg.images_dir, sku):
        add(p, f"{sku} - Reference {p.name}")

    if not any("Reference" in alt for _, alt in out):
        add(find_local_image(cfg.images_dir, sku, ""), f"{sku} - Reference")

    return out


def _mime_for(path: Path) -> str:
    return "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"


def sync_product_media(
    client,
    *,
    product_id: str,
    sku: str,
    images: list[tuple[Path, str]],
    replace_existing: bool = True,
    existing_media_ids: list[str] | None = None,
) -> int:
    """Upload full media set with prompt2 first; optionally replace existing media."""
    if not images:
        raise RuntimeError(f"{sku}: no local images to upload")

    if replace_existing and existing_media_ids:
        media_ids = [mid for mid in existing_media_ids if mid]
        if media_ids:
            client.delete_product_media(product_id=product_id, media_ids=media_ids)
            log.info("[%s] Deleted %d existing media item(s)", sku, len(media_ids))
            time.sleep(0.3)

    media_urls: list[tuple[str, str]] = []
    for img_path, alt in images:
        cdn = client.upload_image_bytes(
            file_bytes=img_path.read_bytes(),
            filename=img_path.name,
            mime_type=_mime_for(img_path),
            alt=alt,
        )
        media_urls.append((cdn, alt))
        log.info("[%s] Staged %s", sku, img_path.name)

    attached = client.product_create_media(
        product_id=product_id,
        media=[
            {"mediaContentType": "IMAGE", "originalSource": url, "alt": alt}
            for url, alt in media_urls
        ],
    )
    if not attached:
        raise RuntimeError(f"{sku}: productCreateMedia returned no media")

    featured_id = str(attached[0].get("id") or "")
    if featured_id:
        try:
            client.product_set_featured_media(product_id=product_id, media_id=featured_id)
        except Exception as e:
            log.warning("[%s] Could not set featured media explicitly: %s", sku, e)

    return len(attached)
