"""Configuration for the Drive-backed review app."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from src.config import AppConfig, load_config


@dataclass(frozen=True)
class DriveReviewConfig:
    base: AppConfig
    drive_outputs_folder_id: str
    drive_xlsx_file_id: str
    local_outputs_dir: Path
    drive_cache_dir: Path
    drive_credentials_dir: Path
    firestore_project_id: str
    firestore_collection: str
    lease_ttl_seconds: int
    max_parallel_leases: int
    machine_id: str

    @property
    def outputs_dir(self) -> Path:
        """Canonical local workspace (same layout as Drive outputs/)."""
        return self.local_outputs_dir

    @property
    def review_state_path(self) -> Path:
        return self.outputs_dir / "review_state.json"

    @property
    def stock_spreadsheet_id(self) -> str:
        """Google Sheets / Drive spreadsheet ID for canonical stock entries."""
        return self.drive_xlsx_file_id

    @property
    def local_stock_path(self) -> Path:
        """Cached export of the Drive stock sheet (not repo Stock.xlsx)."""
        return self.drive_cache_dir / "stock_sheet.xlsx"

    @property
    def enriched_xlsx_path(self) -> Path:
        return self.outputs_dir / "stock_enriched.xlsx"


def _p(value: str) -> Path:
    return Path(value).expanduser().resolve() if value else Path(value)


def load_drive_review_config(
    config_path: str | Path = "drive_review/config.yaml",
    *,
    base_config_path: str | Path = "config.yaml",
) -> DriveReviewConfig:
    load_dotenv(override=False)
    config_path = Path(config_path)
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    base = load_config(base_config_path)

    def get(key: str, default: Any) -> Any:
        return raw.get(key, default)

    cache_dir = _p(str(get("drive_cache_dir", "drive_review/cache")))
    creds_dir = _p(str(get("drive_credentials_dir", "drive_review/credentials")))

    local_outputs = _p(str(get("local_outputs_dir", get("outputs_dir", "outputs"))))

    def env_or(key: str, yaml_key: str, default: Any = "") -> str:
        return str(os.getenv(key) or get(yaml_key, default) or "").strip()

    return DriveReviewConfig(
        base=base,
        drive_outputs_folder_id=env_or("DRIVE_OUTPUTS_FOLDER_ID", "drive_outputs_folder_id"),
        drive_xlsx_file_id=env_or("DRIVE_XLSX_FILE_ID", "drive_xlsx_file_id"),
        local_outputs_dir=local_outputs,
        drive_cache_dir=cache_dir,
        drive_credentials_dir=creds_dir,
        firestore_project_id=env_or("FIRESTORE_PROJECT_ID", "firestore_project_id"),
        firestore_collection=str(get("firestore_collection", "drive_review_leases")).strip(),
        lease_ttl_seconds=int(get("lease_ttl_seconds", base.lease_ttl_seconds)),
        max_parallel_leases=int(get("max_parallel_leases", base.max_parallel_sessions)),
        machine_id=str(get("machine_id", "local")).strip() or "local",
    )
