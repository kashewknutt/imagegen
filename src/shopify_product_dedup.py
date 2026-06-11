"""Deduplicate Shopify products that share the same variant SKU."""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def primary_sku_from_product(prod: dict[str, Any]) -> str:
    skus = prod.get("skus") or []
    return str(skus[0] if skus else prod.get("sku") or "").strip()


def prefer_canonical_product(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    review_store=None,
    sku: str = "",
) -> dict[str, Any]:
    """Pick the product to keep when two Shopify products share a SKU."""
    if review_store is not None and sku:
        rec = review_store.get_record(sku)
        pid = str(rec.get("product_id") or "").strip()
        if pid:
            if str(a.get("id") or "") == pid:
                return a
            if str(b.get("id") or "") == pid:
                return b
    a_media = len(a.get("media") or [])
    b_media = len(b.get("media") or [])
    if a_media != b_media:
        return a if a_media > b_media else b
    a_title = str(a.get("title") or "")
    b_title = str(b.get("title") or "")
    if a_title and not b_title:
        return a
    if b_title and not a_title:
        return b
    return a


def dedupe_products_by_sku(
    products: list[dict[str, Any]],
    *,
    review_store=None,
) -> list[dict[str, Any]]:
    """Return one product per SKU, preserving first-seen order."""
    by_sku: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for prod in products:
        sku = primary_sku_from_product(prod)
        if not sku:
            continue
        if sku not in by_sku:
            by_sku[sku] = prod
            order.append(sku)
        else:
            by_sku[sku] = prefer_canonical_product(
                by_sku[sku],
                prod,
                review_store=review_store,
                sku=sku,
            )
    return [by_sku[sku] for sku in order]


def group_shopify_products_by_sku(products: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for prod in products:
        sku = primary_sku_from_product(prod)
        if sku:
            groups[sku].append(prod)
    return dict(groups)


def split_canonical_and_duplicates(
    groups: dict[str, list[dict[str, Any]]],
    *,
    review_store=None,
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, dict[str, Any], dict[str, Any]]]]:
    """
    Returns canonical product per SKU and list of (sku, canonical, duplicate) triples.
    """
    canonical: dict[str, dict[str, Any]] = {}
    dupes: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for sku, prods in groups.items():
        if len(prods) <= 1:
            if prods:
                canonical[sku] = prods[0]
            continue
        keep = prods[0]
        for other in prods[1:]:
            keep = prefer_canonical_product(keep, other, review_store=review_store, sku=sku)
        canonical[sku] = keep
        keep_id = str(keep.get("id") or "")
        for p in prods:
            if str(p.get("id") or "") != keep_id:
                dupes.append((sku, keep, p))
    return canonical, dupes


def is_active_shopify_product(prod: dict[str, Any]) -> bool:
    return str(prod.get("status") or "ACTIVE").upper() not in {"ARCHIVED", "DRAFT"}


def shopify_products_by_sku(
    client,
    *,
    active_only: bool = False,
    review_store=None,
) -> dict[str, dict[str, Any]]:
    """Deduplicated SKU -> product map for reliable lookups (not search-query based)."""
    products = fetch_all_shopify_products(client, active_only=active_only)
    deduped = dedupe_products_by_sku(products, review_store=review_store)
    out: dict[str, dict[str, Any]] = {}
    for prod in deduped:
        sku = primary_sku_from_product(prod)
        if sku:
            out[sku] = prod
    return out


def lookup_shopify_product(
    products_by_sku: dict[str, dict[str, Any]],
    sku: str,
    *,
    review_store=None,
) -> tuple[dict[str, Any] | None, str]:
    """
    Resolve a product for a workspace SKU.
    Returns (product, message). message is empty when product is found.
    """
    sku = (sku or "").strip()
    if not sku:
        return None, "SKU is empty."

    prod = products_by_sku.get(sku)
    if prod:
        return prod, ""

    if review_store is not None:
        rec = review_store.get_record(sku)
        pid = str(rec.get("product_id") or "").strip()
        if pid:
            for candidate in products_by_sku.values():
                if str(candidate.get("id") or "") == pid:
                    return candidate, ""

    return None, f"No Shopify product with variant SKU `{sku}`."


def fetch_all_shopify_products(
    client,
    *,
    batch_size: int = 50,
    max_pages: int = 250,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    after = None
    for _ in range(max_pages):
        page = client.list_products(first=batch_size, after=after, query=None)
        batch = page.get("products") or []
        if active_only:
            batch = [p for p in batch if is_active_shopify_product(p)]
        products.extend(batch)
        pi = page.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        after = pi.get("endCursor")
    return products
