from __future__ import annotations

import re

TITLE_CATEGORIES: list[str] = [
    "rings",
    "earrings",
    "pendants",
    "bracelets",
    "necklaces",
    "anklets",
    "sets",
    "other",
]

_SHARED_RULES = """
Rules:
- Study the product image carefully and name what is visibly there: shape, motif, pattern, stone layout, metal tone, silhouette.
- Write a short, brandable, customer-facing product title (typically 2-6 words).
- The title must feel specific to THIS piece, not generic catalogue filler.
- Do NOT include SKU, price, brand prefix, or marketing fluff.
- Do NOT invent gemstones, materials, or details that are not clearly visible.
- Do NOT use a standalone letter "X" as a separator between words (bad: "Rose Gold X Tennis Bracelet"). Write natural phrases instead (good: "Rose Gold Tennis Bracelet").
- Words that naturally contain the letter x are fine (e.g. "Classic", "Mixed").
- Use natural title case. No quotes. No punctuation at the end.
- Output format EXACTLY one line:
TITLE: <your title>
""".strip()

_CATEGORY_PROMPTS: dict[str, str] = {
    "rings": """
You are naming fine jewellery rings for an e-commerce catalogue.
Focus on the ring's visible design: solitaire, halo, band style, motif, stone arrangement, silhouette.
Good examples: Classic Solitaire Ring, Floral Cluster Ring, Pavé Band Ring, Oval Halo Ring.
""".strip(),
    "earrings": """
You are naming fine jewellery earrings for an e-commerce catalogue.
Focus on hoop shape, drop style, stud motif, floral details, metal finish, and earring silhouette.
Good examples: Ionic Round Hoops, Petite Floral Earrings, Rose Gold Clover Earrings, Classic Solitaire Earrings.
""".strip(),
    "pendants": """
You are naming fine jewellery pendants for an e-commerce catalogue.
Focus on pendant motif, charm shape, symbolic design, stone placement, and necklace drop style.
Good examples: Balloon Heart Pendant, Starburst Pendant, Evil Eye Pendant, Clover Charm Pendant.
""".strip(),
    "bracelets": """
You are naming fine jewellery bracelets for an e-commerce catalogue.
Focus on bracelet structure: tennis, cuff, chain, bangle, charm, mosaic pattern, and visible motif.
Good examples: Signature Bracelet, Circle of White Bracelet, Radiant Tennis Bracelet, Evil Eye Tennis Bracelet, Blue Mosaic Cuff Bracelet.
""".strip(),
    "necklaces": """
You are naming fine jewellery necklaces for an e-commerce catalogue.
Focus on chain style, pendant integration, layering look, motif, and neckline presence.
Good examples: Layered Chain Necklace, Teardrop Pendant Necklace, Station Bead Necklace.
""".strip(),
    "anklets": """
You are naming fine jewellery anklets for an e-commerce catalogue.
Focus on chain delicacy, charm motif, bead pattern, and ankle jewellery silhouette.
Good examples: Delicate Chain Anklet, Charm Drop Anklet, Beaded Summer Anklet.
""".strip(),
    "sets": """
You are naming fine jewellery sets for an e-commerce catalogue.
Focus on the coordinated pieces visible in the image and the shared motif across the set.
Good examples: Floral Pendant Set, Tennis Bracelet Earring Set, Bridal Jewellery Set.
""".strip(),
    "other": """
You are naming fine jewellery products for an e-commerce catalogue.
Infer the most appropriate jewellery type from the image and name the visible design clearly.
Good examples: Signature Bracelet, Ionic Round Hoops, Balloon Heart Pendant.
""".strip(),
}

_ALIAS_TO_CATEGORY: list[tuple[str, str]] = [
    (r"\bring(s)?\b", "rings"),
    (r"\bearring(s)?\b", "earrings"),
    (r"\bpendant(s)?\b", "pendants"),
    (r"\bpendent(s)?\b", "pendants"),
    (r"\bcharm(s)?\b", "pendants"),
    (r"\bbracelet(s)?\b", "bracelets"),
    (r"\bbangle(s)?\b", "bracelets"),
    (r"\bcuff(s)?\b", "bracelets"),
    (r"\bnecklace(s)?\b", "necklaces"),
    (r"\bchain(s)?\b", "necklaces"),
    (r"\banklet(s)?\b", "anklets"),
    (r"\bset(s)?\b", "sets"),
    (r"\bjewel(l)?ery set(s)?\b", "sets"),
]


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def normalize_product_category(*, category: str = "", product_type: str = "", title: str = "") -> str:
    """
    Map Shopify category/product type/title text to a canonical jewellery bucket.
    """
    haystack = _norm_text(" ".join([category, product_type, title]))
    if not haystack:
        return "other"

    for pattern, bucket in _ALIAS_TO_CATEGORY:
        if re.search(pattern, haystack):
            return bucket

    # Direct label matches from taxonomy/product type strings.
    for bucket in TITLE_CATEGORIES:
        if bucket in haystack:
            return bucket

    return "other"


def build_title_prompt(
    *,
    category_key: str,
    current_title: str = "",
    product_type: str = "",
    sku: str = "",
    avoid_titles: list[str] | None = None,
) -> str:
    key = category_key if category_key in _CATEGORY_PROMPTS else "other"
    system = _CATEGORY_PROMPTS[key]
    context_lines = []
    if current_title.strip():
        context_lines.append(f"Current Shopify title (for context only, do not copy blindly): {current_title.strip()}")
    if product_type.strip():
        context_lines.append(f"Product type: {product_type.strip()}")
    if sku.strip():
        context_lines.append(f"SKU (do not include in title): {sku.strip()}")
    avoid = [str(t).strip() for t in (avoid_titles or []) if str(t).strip()]
    if avoid:
        shown = "\n".join(f"- {t}" for t in avoid[:40])
        context_lines.append(
            "These titles are already used and must NOT be repeated. Create a clearly different name:\n"
            f"{shown}"
        )
    context = "\n".join(context_lines)
    parts = [system, _SHARED_RULES]
    if context:
        parts.append(context)
    return "\n\n".join(parts)


def sanitize_generated_title(title: str) -> str:
    """Remove standalone ' X ' separators; keep words that contain x naturally."""
    cleaned = re.sub(r"\s+[xX]\s+", " ", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def parse_generated_title(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    title = ""
    if "TITLE:" in raw.upper():
        for line in raw.splitlines():
            if line.strip().upper().startswith("TITLE:"):
                title = line.split(":", 1)[1].strip().strip('"').strip("'")
                break
    if not title:
        for line in raw.splitlines():
            line = line.strip().strip('"').strip("'")
            if line:
                title = line
                break
    if not title:
        title = raw.strip().strip('"').strip("'")
    return sanitize_generated_title(title)
