from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .file_lock import file_lock


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _default_record(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "sku": "",
        "product_id": "",
        "generated_title": "",
        "new_title": "",
        "cost_usd": "",
        "status": "",
        "model": "",
        "updated_at_utc": "",
    }


class TitleStore:
    """Persist generated/edited titles locally, keyed by SKU or product_id."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"records": {}})

    def _read(self) -> dict[str, Any]:
        with file_lock(self.lock_path):
            return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        with file_lock(self.lock_path):
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)

    @staticmethod
    def row_key(*, sku: str = "", product_id: str = "") -> str:
        s = (sku or "").strip()
        if s:
            return s
        return (product_id or "").strip()

    def get(self, key: str) -> dict[str, Any]:
        data = self._read()
        rec = (data.get("records") or {}).get(key) or _default_record(key)
        return dict(rec)

    def update(self, key: str, **patch: Any) -> dict[str, Any]:
        data = self._read()
        db = data.setdefault("records", {})
        rec = db.get(key) or _default_record(key)
        rec.update(patch)
        rec["key"] = key
        rec["updated_at_utc"] = _now_utc()
        db[key] = rec
        self._write(data)
        return dict(rec)

    def all_records(self) -> dict[str, dict[str, Any]]:
        data = self._read()
        return dict(data.get("records") or {})
