#!/usr/bin/env python3
"""
Find products with a local image but no generated title, generate titles,
upload all titled records to Shopify, and refresh stock_enriched.xlsx.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from dotenv import load_dotenv

from build_stock_export import build_export
from dedupe_titles_and_upload import _load_shopify_client, product_names_from_store, upload_titles
from src.config import load_config
from src.image_resolve import find_local_image
from src.title_generator import fetch_all_products, generate_title_from_image
from src.title_prompts import normalize_product_category
from src.title_store import TitleStore
from src.xlsx_ingest import iter_rows

log = logging.getLogger("missing_titles")


def _title_for_record(rec: dict) -> str:
    return str(rec.get("new_title") or rec.get("generated_title") or "").strip()


def _image_path_for_sku(cfg, sku: str) -> Path | None:
    generated = cfg.outputs_dir / sku / "prompt2_v1.jpg"
    if generated.is_file():
        return generated
    dslr = find_local_image(cfg.images_dir, sku, "")
    return dslr if dslr and dslr.is_file() else None


def _sku_category_map(stock_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in iter_rows(stock_path, ["Total"]):
        sku = str(row.values.get("SKU") or "").strip()
        if sku:
            out[sku] = str(row.values.get("category") or "").strip()
    return out


def find_missing_skus(*, cfg, store: TitleStore, stock_path: Path) -> list[str]:
    records = store.all_records()
    stock_skus = {
        str(row.values.get("SKU") or "").strip()
        for row in iter_rows(stock_path, ["Total"])
        if str(row.values.get("SKU") or "").strip()
    }

    missing: set[str] = set()

    for sku in sorted(stock_skus):
        rec = records.get(sku) or {}
        if _title_for_record(rec):
            continue
        if _image_path_for_sku(cfg, sku):
            missing.add(sku)

    for path in sorted(cfg.outputs_dir.iterdir()):
        if not path.is_dir():
            continue
        sku = path.name
        if not (path / "prompt2_v1.jpg").is_file():
            continue
        rec = records.get(sku) or {}
        if not _title_for_record(rec):
            missing.add(sku)

    return sorted(missing)


def generate_missing_titles(
    *,
    cfg,
    store: TitleStore,
    stock_path: Path,
    model: str,
) -> tuple[int, int]:
    sku_categories = _sku_category_map(stock_path)
    records = store.all_records()
    used_titles = {
        " ".join(_title_for_record(r).lower().split())
        for r in records.values()
        if _title_for_record(r)
    }

    skus = find_missing_skus(cfg=cfg, store=store, stock_path=stock_path)
    if not skus:
        log.info("No SKUs with images missing titles.")
        return 0, 0

    log.info("Found %d SKU(s) with image but no title: %s", len(skus), ", ".join(skus))
    ok = 0
    failed = 0

    for i, sku in enumerate(skus, start=1):
        image_path = _image_path_for_sku(cfg, sku)
        if image_path is None:
            log.warning("[%d/%d] %s — skip (no image)", i, len(skus), sku)
            failed += 1
            continue

        category_key = normalize_product_category(
            category=sku_categories.get(sku, ""),
            product_type="",
            title="",
        )
        avoid = sorted({t for t in used_titles if t})

        log.info("[%d/%d] Generating title for %s (%s)", i, len(skus), sku, image_path.name)
        title, cost, err, _meta = generate_title_from_image(
            cfg,
            image_path=image_path,
            category_key=category_key,
            cache_dir=cfg.download_cache_dir,
            sku=sku,
            model=model,
            avoid_titles=avoid,
        )
        if err or not title:
            log.error("[%d/%d] %s — generation failed: %s", i, len(skus), sku, err or "empty title")
            store.update(sku, sku=sku, status=f"error: {err or 'empty title'}", model=model)
            failed += 1
            time.sleep(1.0)
            continue

        norm = " ".join(title.lower().split())
        used_titles.add(norm)
        store.update(
            sku,
            sku=sku,
            generated_title=title,
            new_title=title,
            cost_usd=f"{cost:.6f}",
            total_cost_usd=f"{cost:.6f}",
            status="generated",
            model=model,
        )
        ok += 1
        log.info("[%d/%d] %s -> '%s'", i, len(skus), sku, title)
        time.sleep(0.5)

    return ok, failed


def attach_shopify_product_ids(*, store: TitleStore, client) -> int:
    products = fetch_all_products(client, query=None, page_size=50, max_pages=200)
    by_sku = {str(p.get("sku") or "").strip(): str(p.get("id") or "").strip() for p in products if p.get("sku")}
    updated = 0
    for key, rec in store.all_records().items():
        sku = str(rec.get("sku") or key).strip()
        product_id = str(rec.get("product_id") or "").strip()
        if product_id or not sku:
            continue
        match = by_sku.get(sku)
        if not match:
            continue
        store.update(key, product_id=match)
        updated += 1
        log.info("Linked %s -> Shopify product %s", sku, match)
    return updated


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    root = Path(__file__).resolve().parent
    cfg = load_config()
    store = TitleStore(cfg.outputs_dir / "title_gen_state.json")
    stock_path = root / "Stock.xlsx"
    products_path = root / "products_export_1.xlsx"
    xlsx_out = cfg.outputs_dir / "stock_enriched.xlsx"
    model = "models/gemini-2.5-flash"

    log.info("=== Phase 1: Generate missing titles ===")
    ok, failed = generate_missing_titles(cfg=cfg, store=store, stock_path=stock_path, model=model)
    log.info("Generated %d title(s), %d failed", ok, failed)

    log.info("=== Phase 2: Upload titles to Shopify ===")
    client = _load_shopify_client(cfg.outputs_dir)
    linked = attach_shopify_product_ids(store=store, client=client)
    if linked:
        log.info("Attached %d Shopify product id(s) for new records", linked)
    upload_ok, upload_skipped, upload_failed = upload_titles(store=store, client=client)
    log.info("Upload done: %d updated, %d skipped, %d failed", upload_ok, upload_skipped, upload_failed)

    log.info("=== Phase 3: Update stock_enriched.xlsx ===")
    names = product_names_from_store(store)
    count = build_export(
        stock_path=stock_path,
        products_path=products_path,
        outputs_dir=cfg.outputs_dir,
        output_path=xlsx_out,
        product_names=names,
    )
    log.info("Wrote %d rows to %s with %d productName overrides", count, xlsx_out, len(names))

    if failed > 0 or upload_failed > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
