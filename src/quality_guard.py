from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter


@dataclass(frozen=True)
class QualityResult:
    ok: bool
    luma_corr: float
    edge_corr: float
    reason: str


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float32).ravel()
    b = b.astype(np.float32).ravel()
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-8:
        return 0.0
    return float(np.dot(a, b) / denom)


def _resize_gray(img: Image.Image, size: int = 256) -> np.ndarray:
    g = img.convert("L").resize((size, size))
    return np.asarray(g)


def _edge_map(img: Image.Image, size: int = 256) -> np.ndarray:
    e = img.convert("L").filter(ImageFilter.FIND_EDGES).resize((size, size))
    return np.asarray(e)


def check_similarity(reference_rgb: Image.Image, generated_rgb: Image.Image, luma_thresh: float, edge_thresh: float) -> QualityResult:
    ref_l = _resize_gray(reference_rgb)
    gen_l = _resize_gray(generated_rgb)
    luma_corr = _corr(ref_l, gen_l)

    ref_e = _edge_map(reference_rgb)
    gen_e = _edge_map(generated_rgb)
    edge_corr = _corr(ref_e, gen_e)

    ok = (luma_corr >= luma_thresh) and (edge_corr >= edge_thresh)
    reason = "ok" if ok else "low similarity (possible geometry/metal corruption)"
    return QualityResult(ok=ok, luma_corr=luma_corr, edge_corr=edge_corr, reason=reason)


METAL_GUARDRAIL_SUFFIX = (
    "\n\nSTRICT ACCURACY REQUIREMENTS:\n"
    "- The jewellery design must remain IDENTICAL to the reference image.\n"
    "- Do NOT change metal type/color/finish, do NOT warp/melt/blur, do NOT remove stones/prongs.\n"
    "- If any written description conflicts with the reference image, FOLLOW THE REFERENCE IMAGE.\n"
    "- Maintain accurate gold/metal reflections and realistic material texture.\n"
)
