"""Independent, bounded native HTF loads. Readers never perform provider I/O."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from services.mtf_policy import native_timeframes


@dataclass
class _Load:
    rows: list = field(default_factory=list)
    started: float = 0
    finished: float = 0
    running: bool = False
    error: str = "loading"


class NativeContextLoader:
    """At most one daemon task per symbol/HTF; a stuck task cannot queue retries.

    A deadline makes that timeframe unavailable without waiting for the socket
    timeout. Late results are discarded. Primary and secondary loads never
    depend on each other, and neither task holds the runtime or account lock.
    """

    def __init__(self, fetch: Callable, *, timeout: float = 5, refresh: float = 2):
        self.fetch, self.timeout, self.refresh = fetch, timeout, refresh
        self._loads: dict[tuple[str, str], _Load] = {}
        self._lock = threading.Lock()
        self._closed = False

    def context(self, symbol: str, timeframe: str) -> dict:
        tasks = []
        with self._lock:
            now = time.monotonic()
            result = {}
            for tf in native_timeframes(timeframe):
                key = (symbol, tf)
                load = self._loads.setdefault(key, _Load())
                if load.running and now - load.started >= self.timeout:
                    load.rows, load.error = [], "HTF_LOAD_TIMEOUT"
                due = load.finished == 0 or now - load.finished >= self.refresh
                if not self._closed and not load.running and due:
                    load.running, load.started = True, now
                    tasks.append((key, load))
                result[tf] = list(load.rows)
        for key, load in tasks:
            threading.Thread(target=self._fetch, args=(key, load),
                             name=f"native-htf-{key[0]}-{key[1]}", daemon=True).start()
        return result

    def _fetch(self, key, load):
        try:
            result = self.fetch(*key, 500)
            rows = list(result[0] if isinstance(result, tuple) else result)
            error = "" if rows else "HTF_CANDLES_UNAVAILABLE"
        except Exception as exc:
            rows, error = [], f"{type(exc).__name__}: {exc}"
        with self._lock:
            now = time.monotonic()
            if now - load.started >= self.timeout:
                rows, error = [], "HTF_LOAD_TIMEOUT"
            load.rows, load.error = rows, error
            load.running, load.finished = False, now

    def errors(self, symbol: str, timeframe: str) -> dict:
        with self._lock:
            return {tf: self._loads[(symbol, tf)].error
                    for tf in native_timeframes(timeframe)
                    if (symbol, tf) in self._loads and self._loads[(symbol, tf)].error}

    def stop(self):
        with self._lock:
            self._closed = True
