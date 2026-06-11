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
from src.shopify_media_sync import update_shopify_product_from_review


@dataclass
class SyncResult:
    sku: str
    reason: str
    drive_push: dict[str, Any] = field(default_factory=dict)
    review_state_pushed: bool = False
    xlsx_replaced: dict[str, Any] = field(default_factory=dict)
    shopify: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


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

    p1_ver, p2_ver = _latest_prompt_versions(cfg, sku)
    if p1_ver is None or p2_ver is None:
        result.errors.append("missing_prompts: both prompt1 and prompt2 must exist before save")
        return result

    product_id = str(product_id or review_store.get_record(sku).get("product_id") or (shop_prod or {}).get("id") or "").strip()
    handle = str(handle or review_store.get_record(sku).get("handle") or (shop_prod or {}).get("handle") or "").strip()

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
        result.drive_push = {"media_check": media_check}
        if media_check.get("videos_missing_on_drive"):
            result.errors.append(
                f"videos_missing_on_drive: {media_check['videos_missing_on_drive']}"
            )
        if media_check.get("raw_missing_on_drive"):
            result.errors.append(f"raw_missing_on_drive: {media_check['raw_missing_on_drive']}")
        pruned = sync.prune_remote_prompt_versions(sku, keep_p1=p1_ver, keep_p2=p2_ver)
        push_result = sync.push_sku(sku, skip_existing_raw_videos=True)
        result.drive_push.update(push_result)
        if pruned:
            result.drive_push["pruned_old_prompts"] = pruned
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
                generated_only=True,
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
