#!/usr/bin/env python3
"""
Sync Shopify product media: pics_raw + prompt1 + prompt2, with prompt2 as thumbnail.

Usage:
  python sync_shopify_media.py --audit
  python sync_shopify_media.py --sku DIAESTR26057
  python sync_shopify_media.py --fix-all
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

from dedupe_titles_and_upload import _load_shopify_client
from src.config import load_config
from src.shopify_media_sync import images_for_sku, sync_product_media
from src.shopify_product_dedup import is_active_shopify_product, prefer_canonical_product, primary_sku_from_product
from src.title_store import TitleStore

log = logging.getLogger("sync_shopify_media")


def _featured_is_raw_only(*, sku: str, featured_url: str) -> bool:
    """True when thumbnail looks like an original pics_raw upload, not prompt2."""
    feat = featured_url.lower()
    if "prompt2" in feat:
        return False
    sku_l = sku.lower()
    raw_markers = (f"{sku_l}_1", f"{sku_l}__1", f"{sku_l}-1")
    return any(m in feat for m in raw_markers)


def _needs_sync(*, cfg, sku: str, product: dict) -> tuple[bool, int, int]:
    from src.shopify_media_sync import media_paths_for_sku

    expected_images = images_for_sku(cfg, sku)
    paths = media_paths_for_sku(cfg, sku)
    expected_count = len(expected_images) + len(paths.get("videos") or [])
    actual = list(product.get("media") or [])
    featured = str(product.get("featured_image_url") or "")
    ok_count = len(actual) >= expected_count
    ok_thumb = not _featured_is_raw_only(sku=sku, featured_url=featured)
    return (not ok_count or not ok_thumb), len(actual), expected_count


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Sync Shopify media for products")
    parser.add_argument("--sku", action="append", help="Limit to SKU(s)")
    parser.add_argument("--audit", action="store_true", help="Report products needing media sync")
    parser.add_argument("--fix-all", action="store_true", help="Sync every product that needs it")
    args = parser.parse_args()

    cfg = load_config()
    store = TitleStore(cfg.outputs_dir / "title_gen_state.json")
    client = _load_shopify_client(cfg.outputs_dir)

    by_sku: dict[str, dict] = {}
    after = None
    for _ in range(200):
        page = client.list_products(first=50, after=after, query=None)
        for p in page.get("products") or []:
            if not is_active_shopify_product(p):
                continue
            sku = primary_sku_from_product(p)
            if not sku:
                continue
            if sku not in by_sku:
                by_sku[sku] = p
            else:
                by_sku[sku] = prefer_canonical_product(by_sku[sku], p, sku=sku)
        pi = page.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")

    targets: list[tuple[str, str]] = []
    for key, rec in sorted(store.all_records().items()):
        sku = str(rec.get("sku") or key).strip()
        product_id = str(rec.get("product_id") or "").strip()
        if not sku or not product_id:
            continue
        if args.sku and sku not in set(args.sku):
            continue
        product = by_sku.get(sku)
        if not product:
            log.warning("%s — not found on Shopify", sku)
            continue
        needs, actual, expected = _needs_sync(cfg=cfg, sku=sku, product=product)
        if args.audit or (args.fix_all and needs) or (args.sku and sku in set(args.sku)):
            status = "NEEDS SYNC" if needs else "OK"
            feat = str(product.get("featured_image_url") or "")
            thumb_ok = not _featured_is_raw_only(sku=sku, featured_url=feat)
            log.info("%s — %s (%d/%d images, thumbnail ok=%s)", sku, status, actual, expected, thumb_ok)
        if needs and (args.fix_all or (args.sku and sku in set(args.sku))):
            targets.append((sku, product_id))

    if args.audit and not args.fix_all and not args.sku:
        return 0

    if not targets:
        log.info("No products selected for sync.")
        return 0

    ok = 0
    failed = 0
    for i, (sku, product_id) in enumerate(targets, start=1):
        from src.shopify_media_sync import media_paths_for_sku

        images = images_for_sku(cfg, sku)
        paths = media_paths_for_sku(cfg, sku)
        if not images:
            log.error("[%d/%d] %s — no local images", i, len(targets), sku)
            failed += 1
            continue
        log.info(
            "[%d/%d] Syncing %s (%d images, %d videos)...",
            i,
            len(targets),
            sku,
            len(images),
            len(paths.get("videos") or []),
        )
        try:
            existing_ids = [str(m.get("id") or "") for m in (by_sku.get(sku) or {}).get("media") or []]
            result = sync_product_media(
                client,
                product_id=product_id,
                sku=sku,
                images=images,
                videos=paths.get("videos"),
                replace_existing=True,
                existing_media_ids=existing_ids,
            )
            log.info(
                "[%s] Synced %d image(s), %d video(s); prompt2 is thumbnail",
                sku,
                result.get("image_count", 0),
                result.get("video_count", 0),
            )
            ok += 1
        except Exception as e:
            log.error("[%s] Sync failed: %s", sku, e)
            failed += 1

    log.info("Done: %d synced, %d failed", ok, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
