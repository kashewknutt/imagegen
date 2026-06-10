from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    csv_path: Path
    images_dir: Path
    input_mode: str
    xlsx_path: Path
    xlsx_sheets: list[str]
    allow_url_fallback: bool
    download_cache_dir: Path
    outputs_dir: Path
    outputsv2_dir: Path
    state_path: Path
    missing_images_report: Path
    cost_log_csv: Path
    model: str
    pricing_usd_per_million_tokens: dict
    max_attempts_per_sku: int
    max_total_generations: int
    min_seconds_between_requests: float
    temp_keep_last_skus: int
    max_parallel_sessions: int
    lease_ttl_seconds: int
    max_inflight_generations: int
    output_format: str
    quality_guard_enabled: bool
    quality_edge_corr_threshold: float
    quality_luma_corr_threshold: float
    page_title: str


def _p(value: str) -> Path:
    return Path(value).expanduser().resolve() if value else Path(value)


def load_config(config_path: str | Path = "config.yaml") -> AppConfig:
    load_dotenv(override=False)
    config_path = Path(config_path)
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    def get(key: str, default: Any) -> Any:
        return raw.get(key, default)

    output_format = str(get("output_format", "png")).lower().strip()
    if output_format not in {"png", "jpg", "jpeg"}:
        raise ValueError(f"Unsupported output_format: {output_format}")
    if output_format == "jpeg":
        output_format = "jpg"

    return AppConfig(
        csv_path=_p(str(get("csv_path", "1.csv"))),
        images_dir=_p(str(get("images_dir", "dslr_shots"))),
        input_mode=str(get("input_mode", "csv")).strip().lower(),
        xlsx_path=_p(str(get("xlsx_path", "Stock.xlsx"))),
        xlsx_sheets=list(get("xlsx_sheets", ["Total"]) or ["Total"]),
        allow_url_fallback=bool(get("allow_url_fallback", True)),
        download_cache_dir=_p(str(get("download_cache_dir", "outputs/_download_cache"))),
        outputs_dir=_p(str(get("outputs_dir", "outputs"))),
        outputsv2_dir=_p(str(get("outputsv2_dir", "outputsv2"))),
        state_path=_p(str(get("state_path", "outputs/state.json"))),
        missing_images_report=_p(str(get("missing_images_report", "outputs/missing_local_images.csv"))),
        cost_log_csv=_p(str(get("cost_log_csv", "outputs/cost_log.csv"))),
        model=str(get("model", "gemini-2.5-flash-image")),
        pricing_usd_per_million_tokens=dict(get("pricing_usd_per_million_tokens", {}) or {}),
        max_attempts_per_sku=int(get("max_attempts_per_sku", 25)),
        max_total_generations=int(get("max_total_generations", 0)),
        min_seconds_between_requests=float(get("min_seconds_between_requests", 1.0)),
        temp_keep_last_skus=int(get("temp_keep_last_skus", 5)),
        max_parallel_sessions=int(get("max_parallel_sessions", 4)),
        lease_ttl_seconds=int(get("lease_ttl_seconds", 3600)),
        max_inflight_generations=int(get("max_inflight_generations", 4)),
        output_format=output_format,
        quality_guard_enabled=bool(get("quality_guard_enabled", True)),
        quality_edge_corr_threshold=float(get("quality_edge_corr_threshold", 0.22)),
        quality_luma_corr_threshold=float(get("quality_luma_corr_threshold", 0.30)),
        page_title=str(get("page_title", "Jewellery Generator Review")),
    )
