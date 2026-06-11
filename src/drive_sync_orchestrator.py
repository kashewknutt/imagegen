"""Coordinate Drive, Shopify, and XLSX sync after review actions."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.drive_outputs_sync import DriveOutputsSync
from src.drive_review_config import DriveReviewConfig
from src.drive_stock_sync import rebuild_and_replace
from src.media_workspace import index_sku_media, refresh_manifest
from src.review_store import ReviewStore
from src.shopify_media_sync import (
    images_for_sku,
    media_paths_for_sku,
    replace_prompt_images_on_product,
    update_shopify_product_from_review,
)
from src.text_format import title_case_category

OPEN_SAVE_STATUSES = frozenset({"pending_review", "approved", "failed"})


@dataclass
class SyncResult:
    sku: str
    reason: str
    drive_push: dict[str, Any] = field(default_factory=dict)
    review_state_pushed: bool = False
    xlsx_replaced: dict[str, Any] = field(default_factory=dict)
    shopify: dict[str, Any] = field(default_factory=dict)
    media_readiness: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def sync_sku_workspace(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    sku: str,
    *,
    reason: str,
    review_store: ReviewStore | None = None,
    push_xlsx: bool = False,
) -> SyncResult:
    review_store = review_store or ReviewStore(cfg.review_state_path)
    result = SyncResult(sku=sku, reason=reason)
    try:
        result.drive_push = sync.push_sku(sku)
    except Exception as e:
        result.errors.append(f"drive_push: {e}")
    try:
        sync.sync_review_state_push()
        result.review_state_pushed = True
    except Exception as e:
        result.errors.append(f"review_state_push: {e}")
    if push_xlsx:
        try:
            result.xlsx_replaced = rebuild_and_replace(cfg, service, review_store=review_store)
        except Exception as e:
            result.errors.append(f"xlsx_replace: {e}")
    return result


def sync_after_regenerate(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    sku: str,
    *,
    prompt_slot: str,
    review_store: ReviewStore | None = None,
) -> SyncResult:
    review_store = review_store or ReviewStore(cfg.review_state_path)
    refresh_manifest(outputs_dir=cfg.outputs_dir, sku=sku)
    return sync_sku_workspace(
        cfg,
        sync,
        service,
        sku,
        reason=f"regenerate_{prompt_slot}",
        review_store=review_store,
        push_xlsx=prompt_slot == "prompt2",
    )


def sync_regenerate_local_only(
    cfg: DriveReviewConfig,
    sku: str,
    *,
    prompt_slot: str,
) -> dict[str, Any]:
    """Refresh manifest after a local-only gallery regen; no Drive/Shopify/Sheet sync."""
    refresh_manifest(outputs_dir=cfg.outputs_dir, sku=sku)
    p1, p2 = _latest_prompt_versions(cfg, sku)
    ver = p1 if prompt_slot == "prompt1" else p2
    return {
        "sku": sku,
        "prompt_slot": prompt_slot,
        "version": ver,
        "local_only": True,
    }


def sync_gallery_batch_save(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    *,
    edited: dict[str, dict[str, int | None]],
    shopify_client=None,
    products_by_sku: dict[str, dict] | None = None,
    review_store: ReviewStore | None = None,
) -> list[SyncResult]:
    """
    Push only edited prompt images to Drive and replace matching slots on Shopify.
    Skips raw images and videos entirely.
    """
    review_store = review_store or ReviewStore(cfg.review_state_path)
    products_by_sku = products_by_sku or {}
    results: list[SyncResult] = []

    for sku, slots_ver in sorted(edited.items()):
        slots = [s for s in ("prompt1", "prompt2") if slots_ver.get(s) is not None]
        if not slots:
            continue

        result = SyncResult(sku=sku, reason="gallery_batch_save")
        p1_ver, p2_ver = _latest_prompt_versions(cfg, sku)
        rec = review_store.get_record(sku)
        product_id = str(rec.get("product_id") or (products_by_sku.get(sku) or {}).get("id") or "").strip()

        try:
            result.drive_push = sync.push_prompt_files(sku, slots=slots, prune_old=True)
        except Exception as e:
            result.errors.append(f"drive_push: {e}")

        store_patch: dict[str, Any] = {}
        manifest_patch: dict[str, Any] = {"review_status": rec.get("review_status") or "uploaded"}
        if p1_ver is not None:
            store_patch["approved_prompt1_version"] = p1_ver
            manifest_patch["approved_prompt1_version"] = p1_ver
        if p2_ver is not None:
            store_patch["approved_prompt2_version"] = p2_ver
            manifest_patch["approved_prompt2_version"] = p2_ver
        if store_patch:
            review_store.update(sku, **store_patch)
        refresh_manifest(outputs_dir=cfg.outputs_dir, sku=sku, patch=manifest_patch)

        if shopify_client and product_id:
            shop_prod = products_by_sku.get(sku)
            try:
                result.shopify = replace_prompt_images_on_product(
                    shopify_client,
                    product_id=product_id,
                    sku=sku,
                    slots=slots,
                    shop_media=(shop_prod or {}).get("media"),
                    review_store=review_store,
                    cfg=cfg.base,
                )
                if result.shopify.get("errors"):
                    result.errors.extend(result.shopify["errors"])
            except Exception as e:
                result.errors.append(f"shopify_replace: {e}")
        elif shopify_client and not product_id:
            result.warnings.append("shopify_skipped: no product_id")

        results.append(result)

    return results


def sync_after_approve(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    sku: str,
    *,
    review_store: ReviewStore | None = None,
) -> SyncResult:
    review_store = review_store or ReviewStore(cfg.review_state_path)
    refresh_manifest(
        outputs_dir=cfg.outputs_dir,
        sku=sku,
        patch={"review_status": "approved"},
    )
    return sync_sku_workspace(
        cfg,
        sync,
        service,
        sku,
        reason="approve",
        review_store=review_store,
        push_xlsx=True,
    )


def _latest_prompt_versions(cfg: DriveReviewConfig, sku: str) -> tuple[int | None, int | None]:
    media_idx = index_sku_media(outputs_dir=cfg.outputs_dir, sku=sku)
    p1 = media_idx.prompt1_versions[-1][0] if media_idx.prompt1_versions else None
    p2 = media_idx.prompt2_versions[-1][0] if media_idx.prompt2_versions else None
    return p1, p2


def _shopify_media_snapshot(shop_prod: dict | None, *, expected_images: int, expected_videos: int) -> dict[str, Any]:
    if not shop_prod:
        return {"connected": False}
    media = shop_prod.get("media") or []
    images = [m for m in media if str(m.get("content_type") or "IMAGE").upper() != "VIDEO"]
    videos = [m for m in media if str(m.get("content_type") or "").upper() in {"VIDEO", "EXTERNAL_VIDEO"}]
    return {
        "connected": True,
        "image_count": len(images),
        "video_count": len(videos),
        "expected_images": expected_images,
        "expected_videos": expected_videos,
        "images_ok": len(images) >= expected_images,
        "videos_ok": expected_videos == 0 or len(videos) >= expected_videos,
    }


def check_save_everything_media(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    sku: str,
    *,
    review_store: ReviewStore | None = None,
    shop_prod: dict | None = None,
) -> dict[str, Any]:
    """Pre-flight: local generated/raw/video assets and Drive/Shopify gaps."""
    review_store = review_store or ReviewStore(cfg.review_state_path)
    idx = index_sku_media(outputs_dir=cfg.outputs_dir, sku=sku)
    paths = media_paths_for_sku(cfg.base, sku, review_store=review_store)
    images = images_for_sku(cfg.base, sku, review_store=review_store, generated_only=False)

    local = {
        "prompt1_versions": len(idx.prompt1_versions),
        "prompt2_versions": len(idx.prompt2_versions),
        "raw_count": len(paths["raw"]),
        "video_count": len(paths["videos"]),
        "upload_image_count": len(images),
        "has_prompt1": bool(idx.prompt1_versions),
        "has_prompt2": bool(idx.prompt2_versions),
        "has_raw": bool(paths["raw"]),
        "has_videos": bool(paths["videos"]),
    }

    drive_check = sync.check_drive_raw_videos(sku)
    drive_meta = sync.scan_sku_metadata(sku)
    drive = {
        **drive_check,
        "has_prompt1": drive_meta.get("has_prompt1"),
        "has_prompt2": drive_meta.get("has_prompt2"),
    }

    shopify = _shopify_media_snapshot(
        shop_prod,
        expected_images=len(images),
        expected_videos=len(paths["videos"]),
    )

    blocking: list[str] = []
    warnings: list[str] = []
    if not local["has_prompt1"] or not local["has_prompt2"]:
        blocking.append("missing_prompts")
    if not local["has_raw"]:
        blocking.append("missing_raw")
    if not local["has_videos"]:
        warnings.append("no_local_videos")

    return {
        "sku": sku,
        "local": local,
        "drive": drive,
        "shopify_before": shopify,
        "blocking": blocking,
        "warnings": warnings,
        "ready": not blocking,
    }


def sku_ready_to_save(
    cfg: DriveReviewConfig,
    sku: str,
    *,
    review_store: ReviewStore | None = None,
) -> bool:
    """SKU has both prompts and is not already uploaded/verified."""
    review_store = review_store or ReviewStore(cfg.review_state_path)
    idx = index_sku_media(outputs_dir=cfg.outputs_dir, sku=sku)
    if not idx.prompt1_versions or not idx.prompt2_versions:
        return False
    status = str(review_store.get_record(sku).get("review_status") or "pending_review")
    return status in OPEN_SAVE_STATUSES


def _shopify_existing_media_ids(
    *,
    sku: str,
    review_store: ReviewStore,
    shop_prod: dict | None,
) -> list[str]:
    if shop_prod:
        ids = [str(m.get("id") or "") for m in shop_prod.get("media") or [] if m.get("id")]
        if ids:
            return ids
    rec = review_store.get_record(sku)
    return [str(x) for x in rec.get("shopify_media_ids") or [] if str(x).strip()]


def sync_save_everything(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    *,
    sku: str,
    title: str,
    category: str,
    description: str = "",
    tags: str = "",
    prompt1_text: str = "",
    prompt2_text: str = "",
    product_id: str = "",
    handle: str = "",
    shopify_client=None,
    shop_prod: dict | None = None,
    review_store: ReviewStore | None = None,
) -> SyncResult:
    """
    One-shot save: persist metadata, push latest prompt1/prompt2 (+ workspace) to Drive,
    rebuild/upload Google Sheet, and replace Shopify product media when connected.
    """
    review_store = review_store or ReviewStore(cfg.review_state_path)
    result = SyncResult(sku=sku, reason="save_everything")

    readiness = check_save_everything_media(
        cfg, sync, sku, review_store=review_store, shop_prod=shop_prod,
    )
    result.media_readiness = readiness
    result.warnings.extend(readiness.get("warnings") or [])
    for code in readiness.get("blocking") or []:
        if code == "missing_prompts":
            result.errors.append("missing_prompts: both prompt1 and prompt2 must exist before save")
        elif code == "missing_raw":
            result.errors.append("missing_raw: no raw/reference images found locally or in DAIJE")
        else:
            result.errors.append(code)
    if not readiness.get("ready"):
        return result

    p1_ver, p2_ver = _latest_prompt_versions(cfg, sku)
    if p1_ver is None or p2_ver is None:
        result.errors.append("missing_prompts: both prompt1 and prompt2 must exist before save")
        return result

    product_id = str(product_id or review_store.get_record(sku).get("product_id") or (shop_prod or {}).get("id") or "").strip()
    handle = str(handle or review_store.get_record(sku).get("handle") or (shop_prod or {}).get("handle") or "").strip()
    category = title_case_category(category)

    review_store.approve(
        sku,
        title=title,
        category=category,
        product_type=category,
        description=description,
        tags=tags,
        prompt1_version=p1_ver,
        prompt2_version=p2_ver,
        product_id=product_id,
        handle=handle,
    )
    review_store.update(sku, prompt1_text=prompt1_text, prompt2_text=prompt2_text)

    refresh_manifest(
        outputs_dir=cfg.outputs_dir,
        sku=sku,
        patch={
            "review_status": "approved",
            "approved_prompt1_version": p1_ver,
            "approved_prompt2_version": p2_ver,
        },
    )

    try:
        media_check = sync.check_drive_raw_videos(sku)
        result.drive_push = {
            "media_check": media_check,
            "pre_push": readiness.get("drive"),
        }
        pruned = sync.prune_remote_prompt_versions(sku, keep_p1=p1_ver, keep_p2=p2_ver)
        push_result = sync.push_sku(sku, skip_existing_raw_videos=True)
        result.drive_push.update(push_result)
        if pruned:
            result.drive_push["pruned_old_prompts"] = pruned
        post_meta = sync.scan_sku_metadata(sku)
        post_check = sync.check_drive_raw_videos(sku)
        result.drive_push["post_push"] = {
            "has_prompt1": post_meta.get("has_prompt1"),
            "has_prompt2": post_meta.get("has_prompt2"),
            "raw_on_drive": post_check.get("raw_on_drive"),
            "videos_on_drive": post_check.get("videos_on_drive"),
            "raw_missing_on_drive": post_check.get("raw_missing_on_drive"),
            "videos_missing_on_drive": post_check.get("videos_missing_on_drive"),
        }
        if not post_meta.get("has_prompt1"):
            result.errors.append("drive_missing_prompt1_after_push")
        if not post_meta.get("has_prompt2"):
            result.errors.append("drive_missing_prompt2_after_push")
        if post_check.get("raw_missing_on_drive"):
            result.errors.append(f"raw_missing_on_drive: {post_check['raw_missing_on_drive']}")
        if post_check.get("videos_missing_on_drive"):
            result.errors.append(f"videos_missing_on_drive: {post_check['videos_missing_on_drive']}")
    except Exception as e:
        result.errors.append(f"drive_push: {e}")

    try:
        sync.sync_review_state_push()
        result.review_state_pushed = True
    except Exception as e:
        result.errors.append(f"review_state_push: {e}")

    try:
        result.xlsx_replaced = rebuild_and_replace(cfg, service, review_store=review_store)
    except Exception as e:
        result.errors.append(f"xlsx_replace: {e}")

    if shopify_client and product_id:
        try:
            tags_list = [t.strip() for t in tags.split(",") if t.strip()]
            existing_ids = _shopify_existing_media_ids(
                sku=sku,
                review_store=review_store,
                shop_prod=shop_prod,
            )
            result.shopify = update_shopify_product_from_review(
                shopify_client,
                cfg.base,
                sku=sku,
                product_id=product_id,
                title=title,
                product_type=category,
                description_html=description.replace("\n", "<br>") if description and "<" not in description else description,
                tags=tags_list or None,
                review_store=review_store,
                existing_media_ids=existing_ids or None,
                replace_media=True,
                generated_only=False,
            )
            expected_images = readiness["local"]["upload_image_count"]
            expected_videos = readiness["local"]["video_count"]
            uploaded_images = int(result.shopify.get("image_count") or 0)
            uploaded_videos = int(result.shopify.get("video_count") or 0)
            result.shopify["expected_images"] = expected_images
            result.shopify["expected_videos"] = expected_videos
            if uploaded_images < expected_images:
                result.errors.append(
                    f"shopify_images_incomplete: uploaded {uploaded_images}/{expected_images}"
                )
            if expected_videos and uploaded_videos < expected_videos:
                result.errors.append(
                    f"shopify_videos_incomplete: uploaded {uploaded_videos}/{expected_videos}"
                )
            review_store.mark_uploaded(sku, shopify_media_ids=result.shopify.get("media_ids") or [])
            refresh_manifest(
                outputs_dir=cfg.outputs_dir,
                sku=sku,
                patch={"review_status": "uploaded", "upload_status": "uploaded"},
            )
            try:
                from src.title_store import TitleStore

                TitleStore(cfg.outputs_dir / "title_gen_state.json").update(
                    sku,
                    sku=sku,
                    product_id=product_id,
                    new_title=title,
                )
            except Exception:
                pass
        except Exception as e:
            result.errors.append(f"shopify_upload: {e}")
    elif shopify_client and not product_id:
        result.errors.append("shopify_skipped: no product_id (product not on Shopify yet)")

    post = (result.drive_push or {}).get("post_push") or {}
    if post.get("has_prompt1") and post.get("has_prompt2"):
        rec = review_store.get_record(sku)
        if str(rec.get("review_status") or "") not in {"uploaded", "verified"}:
            media_ids = (result.shopify.get("media_ids") or []) if result.shopify else None
            review_store.mark_uploaded(sku, shopify_media_ids=media_ids)
            refresh_manifest(
                outputs_dir=cfg.outputs_dir,
                sku=sku,
                patch={"review_status": "uploaded", "upload_status": "uploaded"},
            )

    return result


def sync_after_shopify_upload(
    cfg: DriveReviewConfig,
    sync: DriveOutputsSync,
    service,
    *,
    sku: str,
    shopify_client,
    product_id: str,
    title: str = "",
    product_type: str = "",
    description_html: str = "",
    tags: list[str] | None = None,
    review_store: ReviewStore | None = None,
    shop_prod: dict | None = None,
) -> SyncResult:
    review_store = review_store or ReviewStore(cfg.review_state_path)
    result = SyncResult(sku=sku, reason="shopify_upload")
    existing_ids = _shopify_existing_media_ids(sku=sku, review_store=review_store, shop_prod=shop_prod)
    try:
        result.shopify = update_shopify_product_from_review(
            shopify_client,
            cfg.base,
            sku=sku,
            product_id=product_id,
            title=title,
            product_type=product_type,
            description_html=description_html,
            tags=tags,
            review_store=review_store,
            existing_media_ids=existing_ids or None,
            replace_media=True,
        )
        review_store.mark_uploaded(sku, shopify_media_ids=result.shopify.get("media_ids") or [])
    except Exception as e:
        result.errors.append(f"shopify_upload: {e}")
        return result

    workspace = sync_sku_workspace(
        cfg,
        sync,
        service,
        sku,
        reason="shopify_upload",
        review_store=review_store,
        push_xlsx=True,
    )
    result.drive_push = workspace.drive_push
    result.review_state_pushed = workspace.review_state_pushed
    result.xlsx_replaced = workspace.xlsx_replaced
    result.errors.extend(workspace.errors)
    return result


def validate_local_workspace(cfg: DriveReviewConfig, sku: str) -> dict[str, Any]:
    from src.media_workspace import index_sku_media

    idx = index_sku_media(outputs_dir=cfg.outputs_dir, sku=sku)
    return {
        "sku": sku,
        "has_raw": bool(idx.raw_images),
        "prompt1_versions": len(idx.prompt1_versions),
        "prompt2_versions": len(idx.prompt2_versions),
        "videos": len(idx.videos),
        "workspace_dir": str(idx.workspace_dir),
        "manifest_exists": (idx.workspace_dir / "media_manifest.json").is_file(),
    }
