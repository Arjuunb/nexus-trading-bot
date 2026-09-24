"""Tiny in-process sliding-window rate limiter.

The hub runs as a single process, so an in-memory limiter is sufficient (and has
zero dependencies). Keyed by an arbitrary string (typically ``ip:route``);
thread-safe. Not distributed — when the app is decomposed into multiple replicas
this moves behind Redis (see docs/SAD.md §11). Deterministic: pass ``now`` in
tests.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float, now: float | None = None) -> bool:
        """Record an attempt; return False if it exceeds ``limit`` within ``window_s``."""
        t = time.time() if now is None else now
        with self._lock:
            dq = self._hits.get(key)
            if dq is None:
                dq = deque()
                self._hits[key] = dq
            cutoff = t - window_s
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(t)
            return True

    def retry_after(self, key: str, window_s: float, now: float | None = None) -> int:
        """Seconds until the oldest in-window hit ages out (for a Retry-After header)."""
        t = time.time() if now is None else now
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return 0
            return max(1, int(dq[0] + window_s - t) + 1)

    def used(self, key: str, window_s: float, now: float | None = None) -> int:
        """How many attempts ``key`` has made within the window."""
        t = time.time() if now is None else now
        with self._lock:
            dq = self._hits.get(key)
            return sum(1 for hit in dq if hit > t - window_s) if dq else 0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# process-wide instance
limiter = RateLimiter()
