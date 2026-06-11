"""Build local image/video upload lists and sync them to Shopify product media."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.image_resolve import SUPPORTED_EXTS, find_local_image
from src.media_workspace import (
    index_sku_media,
    latest_prompt_path,
    list_raw_images,
    list_videos,
    refresh_manifest,
    sku_workspace_dir,
)
from src.name_group import base_key_from_path
from src.sku_aliases import canonical_sku

if TYPE_CHECKING:
    from src.review_store import ReviewStore

log = logging.getLogger(__name__)


def _list_source_images(images_dir: Path, sku: str) -> list[Path]:
    if not images_dir.exists():
        return []
    return sorted(
        p
        for p in images_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS and base_key_from_path(p) == sku
    )


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    return "application/octet-stream"


def _video_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".mov":
        return "video/quicktime"
    if ext == ".webm":
        return "video/webm"
    return "video/mp4"


def media_paths_for_sku(
    cfg,
    sku: str,
    *,
    review_store: "ReviewStore | None" = None,
) -> dict[str, Any]:
    """
    Resolve local media paths for a SKU.
    Upload order: prompt2, prompt1, raw images, videos.
    """
    sku = (sku or "").strip()
    sku_dir = sku_workspace_dir(cfg.outputs_dir, sku)
    rec = review_store.get_record(sku) if review_store else {}
    p2_ver = rec.get("approved_prompt2_version")
    p1_ver = rec.get("approved_prompt1_version")

    prompt2 = latest_prompt_path(sku_dir, "prompt2", version=int(p2_ver) if p2_ver else None)
    prompt1 = latest_prompt_path(sku_dir, "prompt1", version=int(p1_ver) if p1_ver else None)
    raw = list_raw_images(sku_dir)
    if not raw:
        for candidate in (sku, canonical_sku(sku)):
            raw = _list_source_images(cfg.images_dir, candidate)
            if raw:
                break
    if not raw:
        legacy = find_local_image(cfg.images_dir, sku, "")
        if legacy:
            raw = [legacy]
    videos = list_videos(sku_dir)
    if not videos:
        for candidate in (sku, canonical_sku(sku)):
            src_vids = sorted(
                p
                for p in cfg.images_dir.iterdir()
                if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".webm", ".m4v"}
                and base_key_from_path(p) == candidate
            ) if cfg.images_dir.exists() else []
            if src_vids:
                videos = src_vids
                break

    return {
        "sku_dir": sku_dir,
        "prompt2": prompt2,
        "prompt1": prompt1,
        "raw": raw,
        "videos": videos,
        "approved_prompt2_version": p2_ver,
        "approved_prompt1_version": p1_ver,
    }


def images_for_sku(
    cfg,
    sku: str,
    *,
    review_store: "ReviewStore | None" = None,
    generated_only: bool = False,
) -> list[tuple[Path, str]]:
    """
    Shopify image upload order: prompt2 (thumbnail), prompt1, then raw references (unless generated_only).
    """
    paths = media_paths_for_sku(cfg, sku, review_store=review_store)
    sku = (sku or "").strip()
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

    add(paths["prompt2"], f"{sku} - Product")
    add(paths["prompt1"], f"{sku} - Lifestyle")
    if not generated_only:
        for i, p in enumerate(paths["raw"], start=1):
            add(p, f"{sku} - Reference {p.name}" if len(paths["raw"]) > 1 else f"{sku} - Reference")

    return out


def upload_video_to_product(
    client,
    *,
    product_id: str,
    sku: str,
    video_path: Path,
) -> str:
    """Stage-upload a local video and attach to product. Returns resource URL used."""
    file_bytes = video_path.read_bytes()
    mime = _video_mime(video_path)
    target = client.staged_upload_create(
        filename=video_path.name,
        mime_type=mime,
        resource="VIDEO",
        file_size=len(file_bytes),
        http_method="POST",
    )
    client.upload_to_staged_target(
        target=target,
        filename=video_path.name,
        mime_type=mime,
        file_bytes=file_bytes,
    )
    resource_url = str(target.get("resourceUrl") or target.get("url") or "").strip()
    if not resource_url:
        raise RuntimeError(f"Staged target missing resourceUrl for video: {target}")
    client.product_create_media(
        product_id=product_id,
        media=[{"mediaContentType": "VIDEO", "originalSource": resource_url, "alt": f"{sku} - Video"}],
    )
    return resource_url


def sync_product_media(
    client,
    *,
    product_id: str,
    sku: str,
    images: list[tuple[Path, str]],
    videos: list[Path] | None = None,
    replace_existing: bool = True,
    existing_media_ids: list[str] | None = None,
) -> dict[str, Any]:
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

    video_count = 0
    for vp in videos or []:
        if not vp.is_file():
            continue
        try:
            upload_video_to_product(client, product_id=product_id, sku=sku, video_path=vp)
            log.info("[%s] Uploaded video %s", sku, vp.name)
            video_count += 1
        except Exception as e:
            log.warning("[%s] Video upload failed for %s (images kept): %s", sku, vp.name, e)
        time.sleep(0.5)

    media_ids = [str(m.get("id") or "") for m in attached if m.get("id")]
    return {
        "image_count": len(attached),
        "video_count": video_count,
        "media_ids": media_ids,
        "featured_media_id": featured_id,
    }


PROMPT_SLOT_IMAGE_INDEX = {"prompt2": 0, "prompt1": 1}


def _shopify_image_media(shop_media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        m
        for m in shop_media
        if str(m.get("content_type") or "IMAGE").upper() not in {"VIDEO", "EXTERNAL_VIDEO"}
    ]


def replace_prompt_images_on_product(
    client,
    *,
    product_id: str,
    sku: str,
    slots: list[str],
    shop_media: list[dict[str, Any]] | None = None,
    review_store: "ReviewStore | None" = None,
    cfg=None,
) -> dict[str, Any]:
    """
    Replace only prompt1 and/or prompt2 on Shopify (position 1=lifestyle, 0=product thumbnail).
    Does not touch raw images or videos.
    """
    if not slots:
        return {"replaced": [], "skipped": ["no_slots"]}

    paths = media_paths_for_sku(cfg, sku, review_store=review_store)
    sku_dir = paths["sku_dir"]
    images = _shopify_image_media(shop_media or [])
    replaced: list[dict[str, Any]] = []
    errors: list[str] = []

    for slot in slots:
        slot = slot.strip().lower()
        idx = PROMPT_SLOT_IMAGE_INDEX.get(slot)
        img_path = latest_prompt_path(sku_dir, slot)
        if idx is None:
            errors.append(f"unknown_slot:{slot}")
            continue
        if img_path is None or not img_path.is_file():
            errors.append(f"missing_local:{slot}")
            continue
        if idx >= len(images):
            errors.append(f"shopify_missing_image_at_index:{slot}:{idx}")
            continue
        old_id = str(images[idx].get("id") or "")
        if not old_id:
            errors.append(f"shopify_missing_media_id:{slot}")
            continue
        try:
            result = client.replace_product_image(
                product_id=product_id,
                old_media_id=old_id,
                file_bytes=img_path.read_bytes(),
                filename=img_path.name,
                mime_type=_mime_for(img_path),
                alt=f"{sku} - {'Product' if slot == 'prompt2' else 'Lifestyle'}",
            )
            new_attached = result.get("attached") or []
            new_id = str((new_attached[0] or {}).get("id") or "") if new_attached else ""
            replaced.append({"slot": slot, "old_media_id": old_id, "new_media_id": new_id})
            if slot == "prompt2" and new_id:
                try:
                    client.product_set_featured_media(product_id=product_id, media_id=new_id)
                except Exception as e:
                    log.warning("[%s] Could not set featured media after prompt2 replace: %s", sku, e)
        except Exception as e:
            errors.append(f"{slot}:{e}")

    return {"replaced": replaced, "errors": errors}


def update_shopify_product_from_review(
    client,
    cfg,
    *,
    sku: str,
    product_id: str,
    title: str = "",
    product_type: str = "",
    description_html: str = "",
    tags: list[str] | None = None,
    review_store: "ReviewStore | None" = None,
    existing_media_ids: list[str] | None = None,
    replace_media: bool = True,
    generated_only: bool = False,
) -> dict[str, Any]:
    """Update product fields and sync local media set to Shopify."""
    if title or product_type or description_html or tags:
        client.product_update_fields(
            product_id=product_id,
            title=title or None,
            product_type=product_type or None,
            description_html=description_html or None,
            tags=tags,
        )

    images = images_for_sku(cfg, sku, review_store=review_store, generated_only=generated_only)
    paths = media_paths_for_sku(cfg, sku, review_store=review_store)
    result = sync_product_media(
        client,
        product_id=product_id,
        sku=sku,
        images=images,
        videos=[] if generated_only else paths["videos"],
        replace_existing=replace_media,
        existing_media_ids=existing_media_ids,
    )
    refresh_manifest(
        outputs_dir=cfg.outputs_dir,
        sku=sku,
        patch={
            "shopify_media_ids": result.get("media_ids") or [],
            "upload_status": "uploaded",
            "review_status": "uploaded",
        },
    )
    try:
        from src.outputsv2 import mirror_sku_from_config

        mirror_sku_from_config(
            cfg,
            sku,
            reason="shopify_upload",
            review_store=review_store,
        )
    except Exception:
        pass
    return result
