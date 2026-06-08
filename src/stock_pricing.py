"""Derive sell/cost prices from Stock.xlsx row fields (including formula fallback)."""
from __future__ import annotations


def _parse_float(value: object) -> float | None:
    try:
        s = str(value).strip().replace(",", "")
        return float(s) if s else None
    except Exception:
        return None


def sell_price_from_row(row: dict) -> str:
    """
    Selling price from Stock row.
    Uses price_2/price when present; otherwise matches Excel formula (Labour+rate)*weight.
    """
    for key in ("price_2", "price"):
        raw = str(row.get(key) or "").strip()
        if raw:
            try:
                return f"{float(raw.replace(',', '')):.2f}"
            except ValueError:
                return raw

    labour = _parse_float(row.get("Labour"))
    rate = _parse_float(row.get("rate"))
    weight_g = _parse_float(row.get("weight"))
    if labour is not None and rate is not None and weight_g is not None:
        return f"{(labour + rate) * weight_g:.2f}"
    return ""


def cost_price_from_row(row: dict) -> str:
    """Cost = Labour + (rate * weight in grams)."""
    labour = _parse_float(row.get("Labour"))
    rate = _parse_float(row.get("rate"))
    weight_g = _parse_float(row.get("weight"))
    cost = None
    if labour is not None and rate is not None:
        cost = labour + (rate * weight_g if weight_g is not None else rate)
    return f"{cost:.2f}" if cost is not None else ""


def price_fields_from_row(row: dict) -> tuple[str, str, float | None, int]:
    """Return (sell_price, cost_price, weight_g, quantity)."""
    weight_g = _parse_float(row.get("weight"))
    qty = int(_parse_float(row.get("quantity")) or 0)
    return sell_price_from_row(row), cost_price_from_row(row), weight_g, qty
