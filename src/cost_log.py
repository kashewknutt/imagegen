from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .file_lock import file_lock


@dataclass(frozen=True)
class CostRow:
    ts_utc: str
    key: str
    ref_tag: str
    prompt_id: str
    attempt: int
    model: str
    mode: str
    status: str  # success|error
    action: str  # generate|approve|regenerate_click|skip
    response_id: str
    model_version: str
    total_tokens: int
    prompt_tokens: int
    candidates_tokens: int
    image_prompt_tokens: int
    image_candidates_tokens: int
    estimated_cost_usd: str
    error: str


def _now_utc() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat()


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def extract_image_modality_tokens(usage_metadata: Optional[dict]) -> tuple[int, int]:
    """
    Returns (image_prompt_tokens, image_candidates_tokens)
    """
    if not usage_metadata:
        return 0, 0
    prompt_details = usage_metadata.get("prompt_tokens_details") or []
    cand_details = usage_metadata.get("candidates_tokens_details") or []
    img_prompt = 0
    img_cand = 0
    for d in prompt_details:
        if (d.get("modality") or "").upper() == "IMAGE":
            img_prompt += _safe_int(d.get("token_count"))
    for d in cand_details:
        if (d.get("modality") or "").upper() == "IMAGE":
            img_cand += _safe_int(d.get("token_count"))
    return img_prompt, img_cand


def append_cost_row(csv_path: Path, row: CostRow) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    exists = csv_path.exists()
    lock_path = csv_path.with_suffix(csv_path.suffix + ".lock")
    with file_lock(lock_path):
        exists = csv_path.exists()
        with csv_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow([field.name for field in CostRow.__dataclass_fields__.values()])  # type: ignore[attr-defined]
            w.writerow(
                [
                    row.ts_utc,
                    row.key,
                    row.ref_tag,
                    row.prompt_id,
                    row.attempt,
                    row.model,
                    row.mode,
                    row.status,
                    row.action,
                    row.response_id,
                    row.model_version,
                    row.total_tokens,
                    row.prompt_tokens,
                    row.candidates_tokens,
                    row.image_prompt_tokens,
                    row.image_candidates_tokens,
                    row.estimated_cost_usd,
                    row.error,
                ]
            )


def make_generate_row(
    *,
    key: str,
    ref_tag: str,
    prompt_id: str,
    attempt: int,
    model: str,
    mode: str,
    status: str,
    action: str,
    usage_metadata: Optional[dict],
    response_id: str = "",
    model_version: str = "",
    estimated_cost_usd: str = "",
    error: str = "",
) -> CostRow:
    total_tokens = _safe_int((usage_metadata or {}).get("total_token_count"))
    prompt_tokens = _safe_int((usage_metadata or {}).get("prompt_token_count"))
    candidates_tokens = _safe_int((usage_metadata or {}).get("candidates_token_count"))
    img_prompt, img_cand = extract_image_modality_tokens(usage_metadata)
    return CostRow(
        ts_utc=_now_utc(),
        key=key,
        ref_tag=ref_tag,
        prompt_id=prompt_id,
        attempt=attempt,
        model=model,
        mode=mode,
        status=status,
        action=action,
        response_id=response_id or "",
        model_version=model_version or "",
        total_tokens=total_tokens,
        prompt_tokens=prompt_tokens,
        candidates_tokens=candidates_tokens,
        image_prompt_tokens=img_prompt,
        image_candidates_tokens=img_cand,
        estimated_cost_usd=estimated_cost_usd,
        error=error,
    )


def estimate_cost_usd(
    pricing_usd_per_million_tokens: dict,
    model: str,
    prompt_tokens: int,
    candidates_tokens: int,
    image_prompt_tokens: int = 0,
    image_candidates_tokens: int = 0,
) -> str:
    """
    pricing_usd_per_million_tokens example:
      {
        "models/gemini-2.5-flash-image": {"input": 0.35, "output": 0.53}
      }
    Returns formatted string or "" if pricing not available.
    """
    if not pricing_usd_per_million_tokens:
        return ""
    key = model
    alt = model.replace("publishers/google/models/", "models/")
    entry = pricing_usd_per_million_tokens.get(key) or pricing_usd_per_million_tokens.get(alt)
    if not isinstance(entry, dict):
        return ""
    if "per_image" in entry:
        return ""
    # Prefer modality-aware pricing if available.
    if any(k in entry for k in ("input_text", "input_image", "output_text", "output_image")):
        try:
            in_text = float(entry.get("input_text", 0.0))
            in_img = float(entry.get("input_image", in_text))
            out_text = float(entry.get("output_text", 0.0))
            out_img = float(entry.get("output_image", out_text))
        except Exception:
            return ""
        text_prompt_tokens = max(0, int(prompt_tokens) - int(image_prompt_tokens))
        text_candidates_tokens = max(0, int(candidates_tokens) - int(image_candidates_tokens))
        cost = 0.0
        cost += (text_prompt_tokens * in_text) / 1_000_000.0
        cost += (int(image_prompt_tokens) * in_img) / 1_000_000.0
        cost += (text_candidates_tokens * out_text) / 1_000_000.0
        cost += (int(image_candidates_tokens) * out_img) / 1_000_000.0
        return f"{cost:.6f}"

    # Backward-compatible flat rates.
    in_rate = entry.get("input")
    out_rate = entry.get("output")
    try:
        in_rate_f = float(in_rate)
        out_rate_f = float(out_rate)
    except Exception:
        return ""
    cost = (prompt_tokens * in_rate_f + candidates_tokens * out_rate_f) / 1_000_000.0
    return f"{cost:.6f}"
