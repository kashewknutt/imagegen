from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .file_lock import file_lock


@dataclass
class UploadState:
    sku: str
    status: str  # pending|uploaded|failed|skipped
    product_id: str
    handle: str
    last_error: str
    updated_at_utc: str


def _default_record(sku: str) -> dict[str, Any]:
    return {
        "sku": sku,
        "status": "pending",
        "product_id": "",
        "handle": "",
        "last_error": "",
        "updated_at_utc": "",
    }


class UploadStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"skus": {}})

    def _read(self) -> dict[str, Any]:
        with file_lock(self.lock_path):
            return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        with file_lock(self.lock_path):
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)

    def ensure_skus(self, skus: list[str]) -> None:
        data = self._read()
        db = data.setdefault("skus", {})
        changed = False
        for sku in skus:
            if sku not in db:
                db[sku] = _default_record(sku)
                changed = True
        if changed:
            self._write(data)

    def get(self, sku: str) -> UploadState:
        data = self._read()
        rec = (data.get("skus") or {}).get(sku) or _default_record(sku)
        return UploadState(**{k: rec.get(k, "") for k in _default_record(sku).keys()})

    def get_record(self, sku: str) -> dict[str, Any]:
        """
        Returns the full raw record (including any extended fields we may add over time).
        """
        data = self._read()
        rec = (data.get("skus") or {}).get(sku) or _default_record(sku)
        return dict(rec)

    def update(self, sku: str, **patch: Any) -> UploadState:
        data = self._read()
        db = data.setdefault("skus", {})
        rec = db.get(sku) or _default_record(sku)
        rec.update(patch)
        db[sku] = rec
        self._write(data)
        return self.get(sku)
