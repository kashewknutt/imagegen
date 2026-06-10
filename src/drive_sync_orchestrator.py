"""Coordinate Drive, Shopify, and XLSX sync after review actions."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.drive_outputs_sync import DriveOutputsSync
from src.drive_review_config import DriveReviewConfig
from src.drive_stock_sync import rebuild_and_replace
from src.media_workspace import refresh_manifest
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
) -> SyncResult:
    review_store = review_store or ReviewStore(cfg.review_state_path)
    result = SyncResult(sku=sku, reason="shopify_upload")
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
