#!/usr/bin/env python3
"""
Deduplicate generated product titles, upload to Shopify, refresh stock_enriched.xlsx.

Usage:
  python dedupe_titles_and_upload.py
  python dedupe_titles_and_upload.py --max-attempts 12 --skip-upload
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

from build_stock_export import build_export
from src.config import load_config
from src.shopify_client import ShopifyClient, ShopifyConfig
from src.shopify_settings import load_shopify_settings
from src.shopify_token_cache import CachedToken, load_cached_token, save_cached_token
from src.title_generator import generate_title_from_image
from src.title_prompts import normalize_product_category
from src.title_store import TitleStore
from src.xlsx_ingest import iter_rows

log = logging.getLogger("dedupe_titles")


def _norm_title(title: str) -> str:
    return " ".join((title or "").strip().lower().split())


def _load_cached_token_raw(cache_path: Path, cache_key: str) -> str:
    """Load token from cache file even if past expiry (best-effort fallback)."""
    try:
        import json

        data = json.loads(cache_path.read_text(encoding="utf-8"))
        rec = (data.get("tokens") or {}).get(cache_key) or {}
        return str(rec.get("access_token") or "").strip()
    except Exception:
        return ""


def _load_shopify_client(outputs_dir: Path) -> ShopifyClient:
    settings_path = outputs_dir / ".shopify_settings.json"
    settings = load_shopify_settings(settings_path)
    if not settings:
        raise RuntimeError(f"Missing Shopify settings: {settings_path}")
    cache_path = outputs_dir / ".shopify_token_cache.json"
    cache_key = f"{settings.shop_domain}|{settings.client_id}"
    token = ""
    cached = load_cached_token(cache_path, cache_key)
    if cached and cached.access_token:
        token = cached.access_token
    else:
        client_secret = (os.getenv("SHOPIFY_CLIENT_SECRET") or os.getenv("shopify_client_secret") or "").strip()
        if client_secret:
            log.info("Cached Shopify token missing/expired — refreshing via client credentials...")
            data = ShopifyClient.oauth_token_client_credentials(
                shop_domain=settings.shop_domain,
                client_id=settings.client_id,
                client_secret=client_secret,
            )
            token = str(data.get("access_token") or "")
            expires_in = int(data.get("expires_in") or 0)
            if not token:
                raise RuntimeError(f"Token refresh failed: {data}")
            save_cached_token(
                cache_path,
                cache_key,
                CachedToken(
                    access_token=token,
                    expires_at_epoch=time.time() + float(expires_in or 0),
                    scope=str(data.get("scope") or ""),
                ),
            )
            log.info("Shopify token refreshed and cached.")
    if not token:
        token = _load_cached_token_raw(cache_path, cache_key)
        if token:
            log.warning("Using cached Shopify token past expiry — refresh in app if uploads fail.")
    if not token:
        raise RuntimeError(
            "Missing Shopify token. Connect via the app sidebar or set SHOPIFY_CLIENT_SECRET in .env"
        )
    return ShopifyClient(
        ShopifyConfig(
            shop_domain=settings.shop_domain,
            admin_access_token=token,
            api_version=settings.api_version,
        )
    )


def _sku_category_map(stock_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in iter_rows(stock_path, ["Total"]):
        sku = str(row.values.get("SKU") or "").strip()
        if sku:
            out[sku] = str(row.values.get("category") or "").strip()
    return out


def _find_duplicates(records: dict[str, dict]) -> dict[str, list[str]]:
    by_title: dict[str, list[str]] = defaultdict(list)
    for key, rec in records.items():
        title = str(rec.get("new_title") or rec.get("generated_title") or "").strip()
        if not title:
            continue
        by_title[_norm_title(title)].append(key)
    return {t: keys for t, keys in by_title.items() if len(keys) > 1}


def _image_path_for_sku(outputs_dir: Path, sku: str) -> Path | None:
    p = outputs_dir / sku / "prompt2_v1.jpg"
    return p if p.is_file() else None


def dedupe_titles(
    *,
    cfg,
    store: TitleStore,
    outputs_dir: Path,
    stock_path: Path,
    max_attempts: int,
    model: str,
) -> tuple[int, int]:
    records = store.all_records()
    sku_categories = _sku_category_map(stock_path)
    used_titles = {
        _norm_title(str(r.get("new_title") or r.get("generated_title") or ""))
        for r in records.values()
        if str(r.get("new_title") or r.get("generated_title") or "").strip()
    }

    regen_count = 0
    attempt_total = 0

    while True:
        dups = _find_duplicates(records)
        if not dups:
            log.info("All titles are unique.")
            break

        log.info("Found %d duplicate title group(s).", len(dups))
        progressed = False

        for norm_title, keys in dups.items():
            keys_sorted = sorted(keys)
            keeper = keys_sorted[0]
            log.warning("Duplicate '%s' (%d) — keeping %s, regenerating others", norm_title, len(keys), keeper)

            for key in keys_sorted[1:]:
                rec = records[key]
                sku = str(rec.get("sku") or key).strip()
                category_key = normalize_product_category(
                    category=sku_categories.get(sku, ""),
                    product_type="",
                    title="",
                )
                image_path = _image_path_for_sku(outputs_dir, sku)
                if image_path is None:
                    log.error("[%s] No prompt2_v1.jpg — cannot regenerate", sku)
                    continue

                avoid = sorted(
                    {
                        str(r.get("new_title") or r.get("generated_title") or "").strip()
                        for r in records.values()
                        if str(r.get("new_title") or r.get("generated_title") or "").strip()
                    }
                )

                new_title = ""
                cost = 0.0
                for attempt in range(1, max_attempts + 1):
                    attempt_total += 1
                    log.info("[%s] Regenerate attempt %d/%d", sku, attempt, max_attempts)
                    title, run_cost, err, _meta = generate_title_from_image(
                        cfg,
                        image_path=image_path,
                        category_key=category_key,
                        cache_dir=cfg.download_cache_dir,
                        sku=sku,
                        model=model,
                        avoid_titles=avoid,
                    )
                    if err:
                        log.error("[%s] Generation error: %s", sku, err)
                        time.sleep(1.0)
                        continue
                    norm = _norm_title(title)
                    if norm and norm not in used_titles:
                        new_title = title
                        cost = run_cost
                        break
                    log.warning("[%s] Still duplicate or empty: '%s'", sku, title)
                    avoid.append(title)
                    time.sleep(0.5)

                if not new_title:
                    log.error("[%s] Failed to get unique title after %d attempts", sku, max_attempts)
                    continue

                used_titles.add(_norm_title(new_title))
                prev_total = 0.0
                try:
                    prev_total = float(rec.get("total_cost_usd") or rec.get("cost_usd") or 0.0)
                except Exception:
                    pass
                updated = store.update(
                    key,
                    sku=sku,
                    product_id=rec.get("product_id", ""),
                    generated_title=new_title,
                    new_title=new_title,
                    cost_usd=f"{cost:.6f}",
                    total_cost_usd=f"{prev_total + cost:.6f}",
                    status="deduped",
                    model=model,
                )
                records[key] = updated
                regen_count += 1
                progressed = True
                log.info("[%s] New unique title: '%s'", sku, new_title)

        if not progressed:
            log.error("No progress in dedupe pass — stopping to avoid infinite loop.")
            break

        records = store.all_records()

    remaining = _find_duplicates(records)
    if remaining:
        log.error("%d duplicate group(s) remain.", len(remaining))
    return regen_count, len(remaining)


def upload_titles(*, store: TitleStore, client: ShopifyClient) -> tuple[int, int, int]:
    records = store.all_records()
    ok = 0
    skipped = 0
    failed = 0

    items = sorted(records.items(), key=lambda x: str(x[0]))
    total = len(items)
    for i, (key, rec) in enumerate(items, start=1):
        product_id = str(rec.get("product_id") or "").strip()
        new_title = str(rec.get("new_title") or rec.get("generated_title") or "").strip()
        sku = str(rec.get("sku") or key)
        if not product_id or not new_title:
            log.warning("[%d/%d] %s — skip (missing product_id or title)", i, total, sku)
            skipped += 1
            continue

        log.info("[%d/%d] Uploading title for %s", i, total, sku)
        try:
            result = client.product_update_title(product_id=product_id, title=new_title)
            store.update(key, status="uploaded", new_title=result.get("title") or new_title)
            ok += 1
            log.info("[%s] Updated -> '%s'", sku, result.get("title") or new_title)
        except Exception as e:
            failed += 1
            store.update(key, status=f"upload_error: {e}")
            log.error("[%s] Upload failed: %s", sku, e)
        time.sleep(0.2)

    return ok, skipped, failed


def product_names_from_store(store: TitleStore) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, rec in store.all_records().items():
        sku = str(rec.get("sku") or key).strip()
        title = str(rec.get("new_title") or rec.get("generated_title") or "").strip()
        if sku and title:
            out[sku] = title
    return out


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Deduplicate titles, upload to Shopify, refresh xlsx")
    parser.add_argument("--max-attempts", type=int, default=12, help="Max regen attempts per duplicate SKU")
    parser.add_argument("--skip-dedupe", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--skip-xlsx", action="store_true")
    parser.add_argument("--model", default="models/gemini-2.5-flash")
    args = parser.parse_args()

    cfg = load_config()
    outputs_dir = cfg.outputs_dir
    store = TitleStore(outputs_dir / "title_gen_state.json")
    stock_path = root / "Stock.xlsx"
    products_path = root / "products_export_1.xlsx"
    xlsx_out = outputs_dir / "stock_enriched.xlsx"

    records = store.all_records()
    with_titles = sum(1 for r in records.values() if str(r.get("new_title") or r.get("generated_title") or "").strip())
    log.info("Loaded %d title records (%d with titles)", len(records), with_titles)

    if not args.skip_dedupe:
        log.info("=== Phase 1: Deduplicate titles ===")
        regen_n, remaining = dedupe_titles(
            cfg=cfg,
            store=store,
            outputs_dir=outputs_dir,
            stock_path=stock_path,
            max_attempts=args.max_attempts,
            model=args.model,
        )
        log.info("Regenerated %d title(s). Remaining duplicate groups: %d", regen_n, remaining)
        if remaining > 0:
            log.warning("Duplicates remain — review logs before upload.")

    if not args.skip_upload:
        log.info("=== Phase 2: Upload titles to Shopify ===")
        client = _load_shopify_client(outputs_dir)
        ok, skipped, failed = upload_titles(store=store, client=client)
        log.info("Upload done: %d updated, %d skipped, %d failed", ok, skipped, failed)
        if failed > 0:
            return 1

    if not args.skip_xlsx:
        log.info("=== Phase 3: Update stock_enriched.xlsx ===")
        names = product_names_from_store(store)
        count = build_export(
            stock_path=stock_path,
            products_path=products_path,
            outputs_dir=outputs_dir,
            output_path=xlsx_out,
            product_names=names,
        )
        log.info("Wrote %d rows to %s with %d productName overrides", count, xlsx_out, len(names))

    log.info("All done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
