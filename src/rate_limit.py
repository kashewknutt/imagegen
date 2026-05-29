from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, min_seconds_between_calls: float) -> None:
        self._min_s = float(min_seconds_between_calls)
        self._lock = threading.Lock()
        self._last_call_ts = 0.0

    def wait(self) -> None:
        if self._min_s <= 0:
            return
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call_ts
            if elapsed < self._min_s:
                time.sleep(self._min_s - elapsed)
            self._last_call_ts = time.time()
