from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .file_lock import file_lock


@dataclass(frozen=True)
class CachedToken:
    access_token: str
    expires_at_epoch: float
    scope: str


def _now() -> float:
    return time.time()


def load_cached_token(cache_path: Path, cache_key: str) -> CachedToken | None:
    if not cache_path.exists():
        return None
    lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
    with file_lock(lock_path):
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None
    rec = (data.get("tokens") or {}).get(cache_key) or {}
    token = str(rec.get("access_token") or "")
    expires_at = float(rec.get("expires_at_epoch") or 0.0)
    scope = str(rec.get("scope") or "")
    if not token or not expires_at:
        return None
    # Consider token expired if within 60s of expiry.
    if _now() >= (expires_at - 60.0):
        return None
    return CachedToken(access_token=token, expires_at_epoch=expires_at, scope=scope)


def save_cached_token(cache_path: Path, cache_key: str, token: CachedToken) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
    with file_lock(lock_path):
        try:
            data: dict[str, Any] = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        except Exception:
            data = {}
        tokens = data.setdefault("tokens", {})
        tokens[cache_key] = {
            "access_token": token.access_token,
            "expires_at_epoch": token.expires_at_epoch,
            "scope": token.scope,
        }
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(cache_path)

