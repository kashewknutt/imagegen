from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SemaphoreToken:
    path: Path


def acquire_dir_semaphore(
    base_dir: Path,
    *,
    name: str,
    slots: int,
    timeout_seconds: int,
    poll_seconds: float = 0.25,
) -> SemaphoreToken:
    """
    Cross-process semaphore using slot files created with O_EXCL.
    Each acquired token is a file: {base_dir}/{name}.{i}.lock

    - Safe across multiple Streamlit sessions/processes.
    - If a process crashes, stale slot files remain; the TTL here is based on mtime.
      We treat a slot file older than timeout_seconds as stale and reclaim it.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    slots = max(1, int(slots))
    timeout_seconds = max(1, int(timeout_seconds))

    def is_stale(p: Path) -> bool:
        try:
            return (time.time() - p.stat().st_mtime) > float(timeout_seconds)
        except Exception:
            return True

    while True:
        # First, reclaim stale slots.
        for i in range(slots):
            p = base_dir / f"{name}.{i}.lock"
            if p.exists() and is_stale(p):
                try:
                    p.unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass

        # Try acquire any free slot.
        for i in range(slots):
            p = base_dir / f"{name}.{i}.lock"
            try:
                fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            except Exception:
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(f"pid={os.getpid()}\n")
                    f.write(f"ts={time.time()}\n")
                p.touch(exist_ok=True)
                return SemaphoreToken(path=p)
            except Exception:
                try:
                    p.unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                continue

        if (time.time() - started) >= float(timeout_seconds):
            raise TimeoutError(f"Timed out waiting for generation slot ({name}), slots={slots}.")
        time.sleep(poll_seconds)


def refresh_token(token: SemaphoreToken) -> None:
    try:
        token.path.touch(exist_ok=True)
    except Exception:
        pass


def release_token(token: SemaphoreToken) -> None:
    try:
        token.path.unlink(missing_ok=True)  # type: ignore[arg-type]
    except Exception:
        pass

