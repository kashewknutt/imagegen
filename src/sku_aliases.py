"""Map stock/export SKU typos to canonical SKUs for title and image lookup."""

from __future__ import annotations

# Stock.xlsx typo -> canonical SKU with generated assets in outputs/
SKU_ALIASES: dict[str, str] = {
    "DIANFHW26007": "DDIANFHW26007",
}


def canonical_sku(sku: str) -> str:
    s = (sku or "").strip()
    return SKU_ALIASES.get(s, s)
