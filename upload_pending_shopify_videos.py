#!/usr/bin/env python3
"""
Upload local product videos to Shopify for products that have images but no video yet.

Use after hitting the 250-video plan limit during bulk upload.

Usage:
  python upload_pending_shopify_videos.py --list
  python upload_pending_shopify_videos.py --dry-run
  python upload_pending_shopify_videos.py
  python upload_pending_shopify_videos.py --limit 20
  python upload_pending_shopify_videos.py --sku DIARFHW26074
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from dedupe_titles_and_upload import _load_shopify_client
from src.config import load_config
from src.review_store import ReviewStore
from src.shopify_media_sync import media_paths_for_sku, upload_video_to_product

log = logging.getLogger("upload_videos")


def _fetch_products_with_media(client, *, batch_size: int = 50, max_pages: int = 250) -> list[dict[str, Any]]:
    gql = """
    query ListProductsMedia($first: Int!, $after: String) {
      products(first: $first, after: $after) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            handle
            variants(first: 5) {
              edges { node { sku } }
            }
            media(first: 50) {
              edges {
                node {
                  ... on MediaImage {
                    id
                    mediaContentType
                  }
                  ... on Video {
                    id
                    mediaContentType
                  }
                  ... on ExternalVideo {
                    id
                    mediaContentType
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    products: list[dict[str, Any]] = []
    after = None
    for _ in range(max_pages):
        data = client.graphql(gql, {"first": batch_size, "after": after})
        payload = data.get("products") or {}
        for edge in payload.get("edges") or []:
            node = edge.get("node") or {}
            skus: list[str] = []
            for ve in ((node.get("variants") or {}).get("edges") or []):
                sku = str(((ve.get("node") or {}).get("sku") or "")).strip()
                if sku:
                    skus.append(sku)
            media: list[dict[str, str]] = []
            for me in ((node.get("media") or {}).get("edges") or []):
                mnode = me.get("node") or {}
                mid = str(mnode.get("id") or "")
                if not mid:
                    continue
                media.append(
                    {
                        "id": mid,
                        "content_type": str(mnode.get("mediaContentType") or ""),
                    }
                )
            products.append(
                {
                    "id": str(node.get("id") or ""),
                    "handle": str(node.get("handle") or ""),
                    "sku": skus[0] if skus else "",
                    "skus": skus,
                    "media": media,
                }
            )
        page_info = payload.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
    return products


def find_pending_video_uploads(cfg, review_store: ReviewStore, client) -> list[dict[str, Any]]:
    by_sku: dict[str, dict[str, Any]] = {}
    for prod in _fetch_products_with_media(client):
        sku = str(prod.get("sku") or "").strip()
        if sku and sku not in by_sku:
            by_sku[sku] = prod

    pending: list[dict[str, Any]] = []
    for sku, rec in sorted(review_store.all_records().items()):
        if str(rec.get("review_status") or "") != "uploaded":
            continue
        paths = media_paths_for_sku(cfg, sku, review_store=review_store)
        videos = [v for v in (paths.get("videos") or []) if v.is_file()]
        if not videos:
            continue
        product_id = str(rec.get("product_id") or "").strip()
        prod = by_sku.get(sku)
        if prod and not product_id:
            product_id = str(prod.get("id") or "")
        if not product_id:
            continue
        shop_videos = [
            m
            for m in (prod.get("media") or []) if prod
            if str(m.get("content_type") or "").upper() in {"VIDEO", "EXTERNAL_VIDEO"}
        ]
        if shop_videos:
            continue
        pending.append(
            {
                "sku": sku,
                "product_id": product_id,
                "handle": str(rec.get("handle") or prod.get("handle") if prod else ""),
                "title": str(rec.get("title") or ""),
                "videos": [str(v) for v in videos],
            }
        )
    return pending


def upload_pending_videos(
    cfg,
    client,
    review_store: ReviewStore,
    *,
    skus: list[str] | None = None,
    dry_run: bool = False,
    limit: int = 0,
    sleep_seconds: float = 1.0,
) -> dict[str, Any]:
    pending = find_pending_video_uploads(cfg, review_store, client)
    if skus:
        allow = set(skus)
        pending = [p for p in pending if p["sku"] in allow]

    if limit > 0:
        pending = pending[:limit]

    ok: list[str] = []
    failed: dict[str, str] = {}

    log.info("%d product(s) pending video upload", len(pending))
    for i, item in enumerate(pending, start=1):
        sku = item["sku"]
        product_id = item["product_id"]
        video_path = Path(item["videos"][0])
        log.info("[%d/%d] %s -> %s", i, len(pending), sku, video_path.name)
        if dry_run:
            ok.append(sku)
            continue
        try:
            upload_video_to_product(
                client,
                product_id=product_id,
                sku=sku,
                video_path=video_path,
            )
            ok.append(sku)
            log.info("[%s] Video uploaded", sku)
        except Exception as e:
            failed[sku] = str(e)
            log.error("[%s] Video upload failed: %s", sku, e)
        time.sleep(sleep_seconds)

    report = {
        "dry_run": dry_run,
        "pending_count": len(pending),
        "success_count": len(ok),
        "failed_count": len(failed),
        "success": ok,
        "failed": failed,
    }
    out = cfg.outputs_dir / "rebuild_executor" / "upload_pending_videos.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Upload missing product videos to Shopify")
    parser.add_argument("--list", action="store_true", help="List pending SKUs and exit")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sku", action="append", help="Limit to SKU(s)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    review_store = ReviewStore(cfg.outputs_dir / "review_state.json")
    client = _load_shopify_client(cfg.outputs_dir)

    if args.list:
        pending = find_pending_video_uploads(cfg, review_store, client)
        log.info("%d SKU(s) need video upload", len(pending))
        for item in pending:
            log.info("%s | %s | %s", item["sku"], item["title"][:50], Path(item["videos"][0]).name)
        return 0

    if args.dry_run:
        log.info("DRY RUN — no uploads")

    report = upload_pending_videos(
        cfg,
        client,
        review_store,
        skus=args.sku,
        dry_run=args.dry_run,
        limit=args.limit,
        sleep_seconds=args.sleep,
    )
    log.info(
        "Done: %d uploaded, %d failed (report: %s/rebuild_executor/upload_pending_videos.json)",
        report["success_count"],
        report["failed_count"],
        cfg.outputs_dir,
    )
    return 1 if report["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
