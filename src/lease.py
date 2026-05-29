from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Lease:
    key: str
    session_id: str
    path: Path


def _now() -> float:
    return time.time()


def _is_stale(p: Path, ttl_seconds: int) -> bool:
    try:
        return (_now() - p.stat().st_mtime) > float(ttl_seconds)
    except Exception:
        return True


def list_active_leases(leases_dir: Path, ttl_seconds: int) -> list[Lease]:
    leases_dir.mkdir(parents=True, exist_ok=True)
    out: list[Lease] = []
    for p in leases_dir.glob("*.json"):
        if _is_stale(p, ttl_seconds):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            key = str(data.get("key") or p.stem)
            session_id = str(data.get("session_id") or "")
            out.append(Lease(key=key, session_id=session_id, path=p))
        except Exception:
            continue
    return out


def try_acquire_lease(
    leases_dir: Path,
    key: str,
    session_id: str,
    *,
    ttl_seconds: int,
    max_concurrent: int,
) -> Lease | None:
    leases_dir.mkdir(parents=True, exist_ok=True)

    # Respect global max concurrent by counting non-stale leases.
    active = list_active_leases(leases_dir, ttl_seconds)
    if max_concurrent > 0 and len(active) >= max_concurrent:
        # Allow re-acquiring an existing lease held by this session.
        for l in active:
            if l.key == key and l.session_id == session_id:
                refresh_lease(l)
                return l
        return None

    lease_path = leases_dir / f"{key}.json"
    if lease_path.exists() and not _is_stale(lease_path, ttl_seconds):
        try:
            cur = json.loads(lease_path.read_text(encoding="utf-8"))
            if str(cur.get("session_id") or "") == session_id:
                lease = Lease(key=key, session_id=session_id, path=lease_path)
                refresh_lease(lease)
                return lease
        except Exception:
            pass
        return None

    payload = {"key": key, "session_id": session_id, "ts": _now()}
    try:
        fd = os.open(str(lease_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return None
    except Exception:
        return None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2, sort_keys=True))
    except Exception:
        try:
            lease_path.unlink(missing_ok=True)  # type: ignore[arg-type]
        except Exception:
            pass
        return None

    lease = Lease(key=key, session_id=session_id, path=lease_path)
    refresh_lease(lease)
    return lease


def refresh_lease(lease: Lease) -> None:
    try:
        lease.path.touch(exist_ok=True)
    except Exception:
        pass


def release_lease(lease: Lease) -> None:
    try:
        lease.path.unlink(missing_ok=True)  # type: ignore[arg-type]
    except Exception:
        pass
