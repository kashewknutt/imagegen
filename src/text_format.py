"""Small text formatting helpers for review and generation."""
from __future__ import annotations


def title_case_category(category: str) -> str:
    """Normalize category labels: 'EARRINGS' -> 'Earrings', 'stud earrings' -> 'Stud Earrings'."""
    s = " ".join((category or "").split())
    if not s:
        return ""
    return s.title()


def product_generation_context(*, title: str = "", category: str = "") -> str:
    """Context block prepended to prompt1/prompt2 image generation."""
    lines: list[str] = []
    cat = title_case_category(category)
    if cat:
        lines.append(f"It is a {cat}.")
    t = (title or "").strip()
    if t:
        lines.append(f"Title: {t}")
    return "\n".join(lines)
