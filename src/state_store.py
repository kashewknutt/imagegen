from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .file_lock import file_lock


@dataclass
class SkuState:
    sku: str
    status: str  # pending|approved|skipped
    attempts: int
    approved_version: int
    last_temp_p1: str
    last_temp_p2: str
    reference_path: str = ""
    selected_reference_paths: list[str] | None = None
    skip_reason: str = ""
    last_error: str = ""


def _default_record(sku: str) -> dict[str, Any]:
    return {
        "sku": sku,
        "status": "pending",
        "attempts": 0,
        "approved_version": 0,
        "last_temp_p1": "",
        "last_temp_p2": "",
        "reference_path": "",
        "selected_reference_paths": None,
        "skip_reason": "",
        "last_error": "",
    }


class StateStore:
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

    def get(self, sku: str) -> SkuState:
        data = self._read()
        rec = (data.get("skus") or {}).get(sku) or _default_record(sku)
        return _coerce_state(rec)

    def update(self, sku: str, **patch: Any) -> SkuState:
        data = self._read()
        db = data.setdefault("skus", {})
        rec = db.get(sku) or _default_record(sku)
        rec.update(patch)
        db[sku] = rec
        self._write(data)
        return _coerce_state(rec)

    def next_pending(self, ordered_skus: list[str]) -> str | None:
        data = self._read()
        db = data.get("skus") or {}
        for sku in ordered_skus:
            rec = db.get(sku) or _default_record(sku)
            if rec.get("status") == "pending":
                return sku
        return None

    def next_actionable(self, ordered_skus: list[str]) -> str | None:
        """
        Prefer resuming an in-progress pending SKU (attempts > 0) so a user doesn't
        lose their place if they generated but didn't approve before restarting.
        Falls back to the first pending SKU in order.
        """
        data = self._read()
        db = data.get("skus") or {}
        for sku in ordered_skus:
            rec = db.get(sku) or _default_record(sku)
            if rec.get("status") == "pending" and int(rec.get("attempts") or 0) > 0:
                return sku
        return self.next_pending(ordered_skus)


def _coerce_state(rec: dict[str, Any]) -> SkuState:
    allowed = {
        "sku",
        "status",
        "attempts",
        "approved_version",
        "last_temp_p1",
        "last_temp_p2",
        "reference_path",
        "selected_reference_paths",
        "skip_reason",
        "last_error",
    }
    filtered = {k: v for k, v in rec.items() if k in allowed}
    return SkuState(**filtered)
