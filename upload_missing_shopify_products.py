#!/usr/bin/env python3
"""
Create Shopify products for titled SKUs that are not on Shopify yet.
Uploads primary product image (prompt2 or raw photo) plus optional extras.
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from dotenv import load_dotenv

from build_stock_export import build_export
from dedupe_titles_and_upload import _load_shopify_client, product_names_from_store
from src.config import load_config
from src.shopify_media_sync import images_for_sku, sync_product_media
from src.title_prompts import normalize_product_category
from src.title_store import TitleStore
from src.xlsx_ingest import index_by_sku, iter_rows

log = logging.getLogger("upload_missing")


def _gid_to_int(gid: str) -> int | None:
    try:
        s = str(gid or "").strip()
        return int(s.rsplit("/", 1)[-1]) if s else None
    except Exception:
        return None


def _parse_float(value: object) -> float | None:
    try:
        s = str(value).strip().replace(",", "")
        return float(s) if s else None
    except Exception:
        return None


def _normalize_category(value: str) -> str:
    v = (value or "").strip()
    return "pendant" if v.lower() == "pandent" else v


_SHOPIFY_PRODUCT_TYPES = {
    "anklets": "Anklets",
    "bracelets": "Bracelets",
    "bracelet": "Bracelets",
    "earrings": "Earrings",
    "earring": "Earrings",
    "necklaces": "Necklaces",
    "necklace": "Necklaces",
    "pendants": "Charms & Pendants",
    "pendant": "Charms & Pendants",
    "rings": "Rings",
    "ring": "Rings",
    "sets": "Jewelry Sets",
}


def _shopify_product_type(category: str, title: str = "") -> str:
    bucket = normalize_product_category(category=category, title=title)
    mapped = _SHOPIFY_PRODUCT_TYPES.get(bucket)
    if mapped:
        return mapped
    c = _normalize_category(category).lower()
    return _SHOPIFY_PRODUCT_TYPES.get(c, category.strip().title() or "Jewelry Sets")


def _stock_row(stock_path: Path, sku: str) -> dict:
    rows = index_by_sku(iter_rows(stock_path, ["Total"]), sku_column="SKU")
    row = rows.get(sku)
    return dict(getattr(row, "values", {}) or {}) if row else {}


def _price_fields(row: dict) -> tuple[str, str, float | None, int]:
    price_sell = str(row.get("price_2") or row.get("price") or "").strip()
    labour = _parse_float(row.get("Labour"))
    rate = _parse_float(row.get("rate"))
    weight_g = _parse_float(row.get("weight"))
    qty = int(_parse_float(row.get("quantity")) or 0)
    cost = None
    if labour is not None and rate is not None:
        cost = labour + (rate * weight_g if weight_g is not None else rate)
    price_cost = f"{cost:.2f}" if cost is not None else ""
    return price_sell, price_cost, weight_g, qty


def _pending_skus(store: TitleStore) -> list[str]:
    out: list[str] = []
    for key, rec in sorted(store.all_records().items()):
        title = str(rec.get("new_title") or rec.get("generated_title") or "").strip()
        product_id = str(rec.get("product_id") or "").strip()
        sku = str(rec.get("sku") or key).strip()
        if title and not product_id and sku:
            out.append(sku)
    return out


def create_missing_products(
    *,
    cfg,
    store: TitleStore,
    client,
    stock_path: Path,
    skus: list[str] | None = None,
) -> tuple[int, int]:
    targets = skus or _pending_skus(store)
    if not targets:
        log.info("No SKUs pending Shopify product creation.")
        return 0, 0

    ok = 0
    failed = 0
    for i, sku in enumerate(targets, start=1):
        rec = store.get(sku)
        title = str(rec.get("new_title") or rec.get("generated_title") or "").strip()
        if not title:
            log.warning("[%d/%d] %s — skip (no title)", i, len(targets), sku)
            failed += 1
            continue
        if str(rec.get("product_id") or "").strip():
            log.info("[%d/%d] %s — already on Shopify", i, len(targets), sku)
            continue

        images = images_for_sku(cfg, sku)
        if not images:
            log.error("[%d/%d] %s — skip (no image)", i, len(targets), sku)
            store.update(sku, status="error: no image for Shopify upload")
            failed += 1
            continue

        row = _stock_row(stock_path, sku)
        category = _normalize_category(str(row.get("category") or ""))
        subcategory = str(row.get("subCategory") or "").strip()
        product_type = _shopify_product_type(category, title=title)
        price_sell, price_cost, weight_g, qty = _price_fields(row)
        desc = f"{title}."
        if subcategory:
            desc = f"{title}. {subcategory}."

        log.info("[%d/%d] Creating Shopify product for %s: '%s'", i, len(targets), sku, title)
        try:
            prod = client.product_create(
                title=title,
                description_html=desc,
                vendor="ZOCI",
                product_type=product_type,
                tags=sorted({t for t in [subcategory.title()] if t}),
            )
            product_id = str(prod.get("id") or "")
            variant_id = _gid_to_int(prod.get("variant_id") or "")
            inventory_item_id = _gid_to_int(prod.get("inventory_item_id") or "")

            if variant_id:
                client.rest_variant_update(variant_id=variant_id, sku=sku, price=price_sell or None)
                if weight_g is not None:
                    client.rest_variant_weight(variant_id=variant_id, weight_kg=float(weight_g) / 1000.0)
            if inventory_item_id:
                cost_f = _parse_float(price_cost)
                if cost_f is not None:
                    client.rest_inventory_item_cost(inventory_item_id=inventory_item_id, cost=cost_f)
                if qty:
                    locs = client.rest_locations()
                    if locs:
                        location_id = int((locs[0] or {}).get("id") or 0)
                        if location_id:
                            client.rest_inventory_set(
                                location_id=location_id,
                                inventory_item_id=inventory_item_id,
                                available=int(qty),
                            )

            n_images = sync_product_media(client, product_id=product_id, sku=sku, images=images, replace_existing=False)
            store.update(
                sku,
                product_id=product_id,
                status="uploaded",
                handle=str(prod.get("handle") or ""),
            )
            ok += 1
            log.info("[%s] Created (%d image(s)) -> %s", sku, n_images, prod.get("handle") or product_id)
        except Exception as e:
            failed += 1
            store.update(sku, status=f"create_error: {e}")
            log.error("[%s] Failed: %s", sku, e)
        time.sleep(0.5)

    return ok, failed


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Create Shopify products for SKUs missing from the store")
    parser.add_argument("--sku", action="append", help="Limit to specific SKU(s)")
    parser.add_argument("--skip-xlsx", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    cfg = load_config()
    store = TitleStore(cfg.outputs_dir / "title_gen_state.json")
    client = _load_shopify_client(cfg.outputs_dir)

    pending = _pending_skus(store)
    log.info("Found %d SKU(s) with title but no Shopify product_id", len(pending))
    if args.sku:
        pending = [s for s in pending if s in set(args.sku)]
        log.info("Filtered to %d SKU(s): %s", len(pending), ", ".join(pending))

    ok, failed = create_missing_products(
        cfg=cfg,
        store=store,
        client=client,
        stock_path=root / "Stock.xlsx",
        skus=pending,
    )
    log.info("Created %d product(s), %d failed", ok, failed)

    if not args.skip_xlsx and ok > 0:
        names = product_names_from_store(store)
        count = build_export(
            stock_path=root / "Stock.xlsx",
            products_path=root / "products_export_1.xlsx",
            outputs_dir=cfg.outputs_dir,
            output_path=cfg.outputs_dir / "stock_enriched.xlsx",
            product_names=names,
        )
        log.info("Refreshed stock_enriched.xlsx (%d rows)", count)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
