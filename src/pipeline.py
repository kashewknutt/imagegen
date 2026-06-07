from __future__ import annotations

import csv
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .config import AppConfig
from .csv_ingest import CsvEntry, iter_entries
from .genai_client import GenAiImageClient
from .image_resolve import (
    download_first_image_url,
    find_local_image,
    open_pil,
    parse_media_links,
)
from .quality_guard import METAL_GUARDRAIL_SUFFIX, check_similarity
from .state_store import StateStore


PROMPT_1 = """Create a realistic luxury model try-on image (body part only ) using the uploaded jewelduct as the exact reference. The jewellery design must remain identical to the original product image. Show the jewellery naturally worn by a professional fashion model in a premium luxury setting. Focus on elegance, realistic skin texture, premium lighting, luxury fashion photography style, soft cinematic lighting, natural posing, and realistic reflections on the jewellery. The jewellery should remain the main focus of the image. High-end jewellery advertisement aesthetic, ultra realistic, premium editorial fashion photography, shallow depth of field, luxury brand campaign look, photorealistic, 8k."""

PROMPT_2 = """Create a realistic premium jewellery product photograph using the uploaded jewellery image as the exact reference. The jewellery design, proportions, craftsmanship, chain structure, stones, metal texture, and detailing must remain identical to the original product image. Use Zoci’s luxury brand identity subtly and naturally: - Deep emerald/teal luxury tones - Warm champagne gold accents - Elegant pmium surfaces like dark marble, matte stone, soft velvet, brushed metal, or silk fabric - Minimal and refined luxury composition The lighting should feel realistic and natural like a professional jewellery photoshoot: - Soft studio lighting - Controlled reflections - Slight imperfections in reflections and shadows for realism - Balanced highlights without excessive glow - Natural depth of field Keep the image grounded and believable instead of hyper-stylized or CGI-looking. The jewellery should remain the hero of the frame with authentic material texture and realistic gold reflections. Visual style should resemble: high-end commercial jewellery photography shot in a professional studio for premium Indian jewellery brands. Avoid: overly cinematic lighting, excessive glow, unreal reflections, extreme sharpness, fantasy styling, floating objects, exaggerated luxury effects, artificial bokeh, or CGI appearance. Photorealistic, premium studio photography, elegant, realistic, refined luxury aesthetic, soft contrast, high detail."""


@dataclass(frozen=True)
class WorkItem:
    key: str
    reference_paths: list[Path]
    reference_rgbs: list[Image.Image]


def load_entries_and_state(cfg: AppConfig) -> tuple[list[CsvEntry], StateStore]:
    entries = iter_entries(cfg.csv_path)
    store = StateStore(cfg.state_path)
    store.ensure_skus([e.sku for e in entries])
    return entries, store


def resolve_reference(cfg: AppConfig, entry: CsvEntry) -> Path | None:
    # 0) If user has manually assigned a reference image before, reuse it.
    # (Stored as an absolute path string.)
    # NOTE: this is read from state in prepare_work_item and passed in as entry override via store.
    local = find_local_image(cfg.images_dir, entry.sku, entry.title)
    if local:
        return local
    if cfg.allow_url_fallback:
        urls = parse_media_links(entry.media_links_raw)
        downloaded = download_first_image_url(urls, cfg.download_cache_dir)
        if downloaded:
            return downloaded
    return None


def write_missing_report(cfg: AppConfig, missing_skus: list[str]) -> None:
    cfg.missing_images_report.parent.mkdir(parents=True, exist_ok=True)
    with cfg.missing_images_report.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["SKU"])
        for sku in sorted(set(missing_skus)):
            w.writerow([sku])


def prepare_work_item_for_path(cfg: AppConfig, key: str, reference_path: Path) -> WorkItem:
    return WorkItem(key=key, reference_paths=[reference_path], reference_rgbs=[open_pil(reference_path)])


def prepare_work_item_for_paths(cfg: AppConfig, key: str, reference_paths: list[Path]) -> WorkItem:
    paths = [Path(p) for p in reference_paths if Path(p).exists()]
    if not paths:
        raise FileNotFoundError("No valid reference images selected.")
    rgbs = [open_pil(p) for p in paths]
    return WorkItem(key=key, reference_paths=paths, reference_rgbs=rgbs)


def _temp_dir(cfg: AppConfig, key: str) -> Path:
    return cfg.outputs_dir / "_temp" / key


def _final_dir(cfg: AppConfig, key: str) -> Path:
    return cfg.outputs_dir / key


def _cleanup_temp_dirs_keep_last_skus(cfg: AppConfig, active_key: str) -> None:
    """
    Keep temp outputs only for the most recently touched N SKU dirs.
    When generating for the (N+1)th distinct SKU, older SKU temp dirs are deleted entirely.
    """
    keep_n = int(getattr(cfg, "temp_keep_last_skus", 5) or 5)
    if keep_n <= 0:
        return

    base = cfg.outputs_dir / "_temp"
    base.mkdir(parents=True, exist_ok=True)

    active_dir = base / active_key
    active_dir.mkdir(parents=True, exist_ok=True)
    try:
        active_dir.touch(exist_ok=True)
    except Exception:
        pass

    sku_dirs = [d for d in base.iterdir() if d.is_dir()]
    sku_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    protected: set[str] = {active_key}
    # Also protect any SKUs currently leased by another session so parallel windows
    # don't delete each other's in-review temp outputs.
    leases_dir = cfg.outputs_dir / "_leases"
    if leases_dir.exists():
        try:
            import json
            import time

            ttl = int(getattr(cfg, "lease_ttl_seconds", 0) or 0)
            now = time.time()
            for p in leases_dir.glob("*.json"):
                try:
                    if ttl > 0 and (now - p.stat().st_mtime) > float(ttl):
                        continue
                    data = json.loads(p.read_text(encoding="utf-8"))
                    k = str(data.get("key") or p.stem).strip()
                    if k:
                        protected.add(k)
                except Exception:
                    continue
        except Exception:
            pass
    for d in sku_dirs[keep_n:]:
        if d.name in protected:
            continue
        try:
            shutil.rmtree(d)
        except Exception:
            pass


def generate_pair(
    cfg: AppConfig,
    client: GenAiImageClient,
    work: WorkItem,
    attempt: int,
    ref_tag: str = "",
    extra_context: str = "",
    prompt1_override: str | None = None,
    prompt2_override: str | None = None,
) -> tuple[Path, Path, dict]:
    # Temp outputs are stored directly under the SKU key. We only allow selecting one reference image per SKU.
    temp_dir = _temp_dir(cfg, work.key)
    temp_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_temp_dirs_keep_last_skus(cfg, work.key)
    p1_path = temp_dir / f"prompt1_attempt{attempt}.{cfg.output_format}"
    p2_path = temp_dir / f"prompt2_attempt{attempt}.{cfg.output_format}"

    ctx = ""
    if extra_context.strip():
        ctx = f"PRODUCT CONTEXT (must be consistent):\n{extra_context.strip()}\n\n"
    p1_body = (prompt1_override if (prompt1_override and prompt1_override.strip()) else PROMPT_1).strip()
    p2_body = (prompt2_override if (prompt2_override and prompt2_override.strip()) else PROMPT_2).strip()
    prompt1 = ctx + p1_body + METAL_GUARDRAIL_SUFFIX
    prompt2 = ctx + p2_body + METAL_GUARDRAIL_SUFFIX

    img1, meta1 = client.generate_image_with_meta(work.reference_rgbs, prompt1)
    img2, meta2 = client.generate_image_with_meta(work.reference_rgbs, prompt2)

    meta: dict = {"attempt": attempt, "auto_rejected": False, "quality": {}, "prompt_meta": {"p1": meta1, "p2": meta2}}

    if cfg.quality_guard_enabled:
        # Compare against the first (primary) reference image.
        # Only prompt2 (product shot) is checked — prompt1 is a lifestyle try-on and
        # is expected to differ strongly from the flat product reference.
        ref0 = work.reference_rgbs[0]
        q1 = check_similarity(ref0, img1, cfg.quality_luma_corr_threshold, cfg.quality_edge_corr_threshold)
        q2 = check_similarity(ref0, img2, cfg.quality_luma_corr_threshold, cfg.quality_edge_corr_threshold)
        meta["quality"] = {"prompt1": q1.__dict__, "prompt2": q2.__dict__}
        if not q2.ok:
            meta["auto_rejected"] = True
            # Still save for inspection; caller decides whether to re-roll.

    img1.save(p1_path)
    img2.save(p2_path)
    return p1_path, p2_path, meta


def approve(cfg: AppConfig, store: StateStore, sku: str, temp_p1: Path, temp_p2: Path) -> tuple[Path, Path]:
    st = store.get(sku)
    version = int(st.approved_version) + 1
    out_dir = _final_dir(cfg, sku)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_p1 = out_dir / f"prompt1_v{version}.{cfg.output_format}"
    final_p2 = out_dir / f"prompt2_v{version}.{cfg.output_format}"
    final_p1.write_bytes(temp_p1.read_bytes())
    final_p2.write_bytes(temp_p2.read_bytes())
    store.update(sku, status="approved", approved_version=version)
    return final_p1, final_p2


def approve_many(
    cfg: AppConfig,
    store: StateStore,
    key: str,
    ref_to_temp: dict[str, tuple[Path, Path]],
) -> dict[str, tuple[Path, Path]]:
    st = store.get(key)
    version = int(st.approved_version) + 1
    results: dict[str, tuple[Path, Path]] = {}
    # We store approved outputs directly under outputs/{SKU}/prompt*_vN.ext.
    # If multiple refs are passed (legacy), we keep the first one.
    for _ref_tag, (temp_p1, temp_p2) in list(ref_to_temp.items())[:1]:
        out_dir = _final_dir(cfg, key)
        out_dir.mkdir(parents=True, exist_ok=True)
        final_p1 = out_dir / f"prompt1_v{version}.{cfg.output_format}"
        final_p2 = out_dir / f"prompt2_v{version}.{cfg.output_format}"
        final_p1.write_bytes(temp_p1.read_bytes())
        final_p2.write_bytes(temp_p2.read_bytes())
        results[key] = (final_p1, final_p2)

    store.update(key, status="approved", approved_version=version)
    return results


def skip(store: StateStore, sku: str, reason: str) -> None:
    store.update(sku, status="skipped", skip_reason=reason)


def prepare_work_item_from_url(cfg: AppConfig, key: str, url: str) -> WorkItem:
    """Download a remote image URL and build a WorkItem for inline generation."""
    path = download_first_image_url([url], cfg.download_cache_dir)
    if not path:
        raise FileNotFoundError(f"Could not download reference image from {url}")
    return prepare_work_item_for_path(cfg, key, path)


def generate_single_replacement(
    cfg: AppConfig,
    client: GenAiImageClient,
    work: WorkItem,
    *,
    attempt: int = 1,
    prompt_style: str = "product",
    extra_context: str = "",
    prompt_override: str | None = None,
    output_suffix: str = "replacement",
) -> tuple[Path, dict]:
    """
    Generate a single replacement image (product or lifestyle style) for Shopify review.
    Saves to outputs/_temp/{key}/{output_suffix}_attempt{N}.{ext}
    """
    temp_dir = _temp_dir(cfg, work.key)
    temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = temp_dir / f"{output_suffix}_attempt{attempt}.{cfg.output_format}"

    ctx = ""
    if extra_context.strip():
        ctx = f"PRODUCT CONTEXT (must be consistent):\n{extra_context.strip()}\n\n"
    if prompt_override and prompt_override.strip():
        body = prompt_override.strip()
    else:
        body = PROMPT_2 if prompt_style == "product" else PROMPT_1
    prompt = ctx + body.strip() + METAL_GUARDRAIL_SUFFIX

    img, meta = client.generate_image_with_meta(work.reference_rgbs, prompt)

    result_meta: dict = {
        "attempt": attempt,
        "prompt_style": prompt_style,
        "prompt_body": body,
        "auto_rejected": False,
        "quality": {},
        "prompt_meta": meta,
    }

    if cfg.quality_guard_enabled:
        ref0 = work.reference_rgbs[0]
        q = check_similarity(ref0, img, cfg.quality_luma_corr_threshold, cfg.quality_edge_corr_threshold)
        result_meta["quality"] = q.__dict__
        if not q.ok:
            result_meta["auto_rejected"] = True

    img.save(out_path)
    return out_path, result_meta
