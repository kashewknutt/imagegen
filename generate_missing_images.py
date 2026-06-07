#!/usr/bin/env python3
"""Generate prompt1/prompt2 for SKUs that have raw photos but no outputs yet."""
from __future__ import annotations

import argparse
import logging
import time
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from src.config import load_config
from src.genai_client import GenAiImageClient
from src.image_resolve import find_local_image
from src.pipeline import approve, generate_pair, prepare_work_item_for_path
from src.state_store import StateStore

log = logging.getLogger("gen_missing_images")

DEFAULT_SKUS = [
    "DIAESTR26057",
    "DIARCTR26019",
    "DIARFHR26075",
    "DIARFHW26033",
    "DIARFHW26059",
    "DIARFHW26060",
    "DIARFHW26061",
]

DEFAULT_MAX_ATTEMPTS = 3


def generate_for_sku(
    *,
    cfg,
    client: GenAiImageClient,
    store: StateStore,
    sku: str,
    max_attempts: int,
    quality_guard_enabled: bool,
) -> bool:
    out_p2 = cfg.outputs_dir / sku / "prompt2_v1.jpg"
    if out_p2.is_file():
        log.info("%s — already has prompt2, skipping", sku)
        return True

    raw = find_local_image(cfg.images_dir, sku, "")
    if not raw or not raw.is_file():
        log.error("%s — no raw reference image in %s", sku, cfg.images_dir)
        return False

    store.ensure_skus([sku])
    work = prepare_work_item_for_path(cfg, sku, raw)
    run_cfg = replace(cfg, quality_guard_enabled=quality_guard_enabled)

    p1_path = p2_path = None
    for attempt in range(1, max_attempts + 1):
        log.info("%s — generation attempt %d/%d", sku, attempt, max_attempts)
        p1_path, p2_path, meta = generate_pair(run_cfg, client, work, attempt)
        if not meta.get("auto_rejected"):
            break
        q = (meta.get("quality") or {}).get("prompt2") or {}
        log.warning(
            "%s — prompt2 quality guard rejected attempt %d (luma=%.3f edge=%.3f)",
            sku,
            attempt,
            float(q.get("luma_corr") or 0.0),
            float(q.get("edge_corr") or 0.0),
        )
        time.sleep(cfg.min_seconds_between_requests)

    if not p1_path or not p2_path:
        log.error("%s — generation failed", sku)
        store.update(sku, status="failed", last_error="generation_failed")
        return False

    approve(cfg, store, sku, p1_path, p2_path)
    log.info("%s — approved prompt1/prompt2 v1", sku)
    return True


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Generate missing prompt2 images for SKUs")
    parser.add_argument("--sku", action="append", help="SKU to generate (repeatable)")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument(
        "--quality-guard",
        action="store_true",
        help="Enable prompt2 similarity checks (off by default for backfill)",
    )
    args = parser.parse_args()

    skus = args.sku or DEFAULT_SKUS
    cfg = load_config()
    store = StateStore(cfg.state_path)
    client = GenAiImageClient(
        cfg.model,
        cfg.min_seconds_between_requests,
        semaphore_dir=str(cfg.outputs_dir / "_semaphores"),
        max_inflight_generations=cfg.max_inflight_generations,
    )

    ok = 0
    failed = 0
    for sku in skus:
        if generate_for_sku(
            cfg=cfg,
            client=client,
            store=store,
            sku=sku,
            max_attempts=max(1, int(args.max_attempts)),
            quality_guard_enabled=bool(args.quality_guard),
        ):
            ok += 1
        else:
            failed += 1
        time.sleep(cfg.min_seconds_between_requests)

    log.info("Done: %d ok, %d failed", ok, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
