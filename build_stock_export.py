#!/usr/bin/env python3
"""
Build an enriched Stock.xlsx export in outputs/ with extra Shopify/product columns.

Usage:
  python build_stock_export.py
  python build_stock_export.py --output outputs/stock_enriched.xlsx
"""
from __future__ import annotations

import argparse
from pathlib import Path

from openpyxl import Workbook

from src.xlsx_ingest import iter_rows


def _norm_sku(value: object) -> str:
    return str(value or "").strip()


def _normalize_category(value: str) -> str:
    v = (value or "").strip()
    if v.lower() == "pandent":
        return "pendant"
    return v


def _load_product_titles(products_path: Path) -> dict[str, str]:
    """Map Variant SKU -> Title from products export."""
    title_by_sku: dict[str, str] = {}
    rows = iter_rows(products_path, ["products_export_1"])
    for row in rows:
        sku = _norm_sku(row.values.get("Variant SKU"))
        if not sku:
            continue
        title = str(row.values.get("Title") or "").strip()
        if title and sku not in title_by_sku:
            title_by_sku[sku] = title
    return title_by_sku


def _thumbnail_image_name(*, category: str, sku: str) -> str:
    cat = _normalize_category(category)
    parts = ["ZOCI"]
    if cat:
        parts.append(cat)
    if sku:
        parts.append(sku)
    return " ".join(parts)


def _thumbnail_image_path(*, outputs_dir: Path, sku: str) -> str:
    """Relative path to prompt2 thumbnail inside outputs/ (xlsx lives in outputs/)."""
    if not sku:
        return ""
    thumb = outputs_dir / sku / "prompt2_v1.jpg"
    if thumb.is_file():
        return f"{sku}/prompt2_v1.jpg"
    return ""


def build_export(
    *,
    stock_path: Path,
    products_path: Path,
    outputs_dir: Path,
    output_path: Path,
    stock_sheets: list[str] | None = None,
    product_names: dict[str, str] | None = None,
) -> int:
    sheets = stock_sheets or ["Total"]
    stock_rows = iter_rows(stock_path, sheets)
    if not stock_rows:
        raise RuntimeError(f"No rows found in {stock_path} sheets={sheets}")

    title_by_sku = _load_product_titles(products_path)
    overrides = product_names or {}

    stock_columns = list(stock_rows[0].values.keys())
    extra_columns = [
        "productName",
        "thumbnailImage",
        "thumbnailImageName",
        "productDescription",
        "hashtag/keyword",
    ]
    headers = stock_columns + extra_columns

    wb = Workbook()
    ws = wb.active
    ws.title = "Total"
    ws.append(headers)

    for row in stock_rows:
        vals = row.values
        sku = _norm_sku(vals.get("SKU"))
        category = str(vals.get("category") or "").strip()

        base = [vals.get(col, "") for col in stock_columns]
        extras = [
            overrides.get(sku) or title_by_sku.get(sku, ""),
            _thumbnail_image_path(outputs_dir=outputs_dir, sku=sku),
            _thumbnail_image_name(category=category, sku=sku),
            "",
            "",
        ]
        ws.append(base + extras)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return len(stock_rows)


def main() -> None:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Build enriched Stock.xlsx in outputs/")
    parser.add_argument("--stock", type=Path, default=root / "Stock.xlsx")
    parser.add_argument("--products", type=Path, default=root / "products_export_1.xlsx")
    parser.add_argument("--outputs-dir", type=Path, default=root / "outputs")
    parser.add_argument("--output", type=Path, default=root / "outputs" / "stock_enriched.xlsx")
    parser.add_argument(
        "--sheet",
        action="append",
        default=None,
        help="Stock sheet to include (default: Total). Repeat for multiple sheets.",
    )
    args = parser.parse_args()

    count = build_export(
        stock_path=args.stock,
        products_path=args.products,
        outputs_dir=args.outputs_dir,
        output_path=args.output,
        stock_sheets=args.sheet,
    )
    print(f"Wrote {count} rows to {args.output}")


if __name__ == "__main__":
    main()
