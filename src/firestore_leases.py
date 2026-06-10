"""Firestore-backed SKU leases for cross-machine Streamlit tabs."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _now_epoch() -> float:
    return time.time()


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class RemoteLease:
    key: str
    holder_id: str
    machine_id: str
    tab_id: str
    acquired_at_utc: str
    heartbeat_at_epoch: float


class FirestoreLeaseManager:
    def __init__(
        self,
        *,
        project_id: str,
        collection: str,
        ttl_seconds: int = 600,
        max_concurrent: int = 40,
    ) -> None:
        if not project_id:
            raise ValueError("firestore_project_id is required")
        from google.cloud import firestore

        self._db = firestore.Client(project=project_id)
        self._collection = collection
        self._ttl_seconds = int(ttl_seconds)
        self._max_concurrent = int(max_concurrent)

    def _col(self):
        return self._db.collection(self._collection)

    def _is_stale(self, data: dict[str, Any]) -> bool:
        hb = float(data.get("heartbeat_at_epoch") or data.get("acquired_at_epoch") or 0)
        return (_now_epoch() - hb) > float(self._ttl_seconds)

    def _to_lease(self, key: str, data: dict[str, Any]) -> RemoteLease:
        return RemoteLease(
            key=key,
            holder_id=str(data.get("holder_id") or ""),
            machine_id=str(data.get("machine_id") or ""),
            tab_id=str(data.get("tab_id") or ""),
            acquired_at_utc=str(data.get("acquired_at_utc") or ""),
            heartbeat_at_epoch=float(data.get("heartbeat_at_epoch") or 0),
        )

    def list_active(self) -> list[RemoteLease]:
        out: list[RemoteLease] = []
        for doc in self._col().stream():
            data = doc.to_dict() or {}
            if self._is_stale(data):
                doc.reference.delete()
                continue
            out.append(self._to_lease(doc.id, data))
        return out

    def get(self, key: str) -> RemoteLease | None:
        doc = self._col().document(key).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        if self._is_stale(data):
            doc.reference.delete()
            return None
        return self._to_lease(doc.id, data)

    def refresh(self, key: str, holder_id: str) -> bool:
        ref = self._col().document(key)
        doc = ref.get()
        if not doc.exists:
            return False
        data = doc.to_dict() or {}
        if str(data.get("holder_id") or "") != holder_id:
            return False
        ref.update({"heartbeat_at_epoch": _now_epoch(), "heartbeat_at_utc": _now_utc()})
        return True

    def release(self, key: str, holder_id: str) -> bool:
        ref = self._col().document(key)
        doc = ref.get()
        if not doc.exists:
            return False
        data = doc.to_dict() or {}
        if str(data.get("holder_id") or "") != holder_id:
            return False
        ref.delete()
        return True

    def force_release(self, key: str) -> None:
        self._col().document(key).delete()

    def try_acquire(
        self,
        key: str,
        *,
        holder_id: str,
        machine_id: str,
        tab_id: str,
    ) -> RemoteLease | None:
        ref = self._col().document(key)
        now = _now_epoch()
        doc = ref.get()
        if doc.exists:
            data = doc.to_dict() or {}
            if not self._is_stale(data):
                if str(data.get("holder_id") or "") == holder_id:
                    ref.update({"heartbeat_at_epoch": now, "heartbeat_at_utc": _now_utc()})
                    return self._to_lease(key, ref.get().to_dict() or {})
                return None
            ref.delete()

        active = self.list_active()
        if self._max_concurrent > 0 and len(active) >= self._max_concurrent:
            return None

        payload = {
            "holder_id": holder_id,
            "machine_id": machine_id,
            "tab_id": tab_id,
            "acquired_at_utc": _now_utc(),
            "acquired_at_epoch": now,
            "heartbeat_at_epoch": now,
            "heartbeat_at_utc": _now_utc(),
            "state": "active",
        }
        ref.set(payload)
        return self._to_lease(key, payload)

    def cleanup_stale(self) -> int:
        removed = 0
        for doc in self._col().stream():
            data = doc.to_dict() or {}
            if self._is_stale(data):
                doc.reference.delete()
                removed += 1
        return removed
