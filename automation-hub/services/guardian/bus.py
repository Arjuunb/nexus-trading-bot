"""The Guardian event bus: publish is O(1), never blocks and never raises.

Trading code may publish from its hot path. Everything that can be slow or
fail -- writing to disk -- happens on the bus's own thread (PRD §42). When the
queue is full an event is dropped and counted, never waited for; a store that
fails keeps its batch and retries it, so a transient disk error loses nothing
while the queue has room. Every number here is exposed through ``stats()`` so
Guardian's self-monitoring can report drops and delay instead of assuming
them away (PRD §17).
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Optional

from services.guardian.schema import GuardianEvent, utcnow


class EventBus:
    def __init__(self, store, *, capacity: int = 10_000, batch: int = 500,
                 flush_interval_s: float = 0.5):
        self.store = store
        self.capacity = int(capacity)
        self.batch = int(batch)
        self.flush_interval_s = float(flush_interval_s)
        self._q: "queue.Queue[tuple[GuardianEvent, float]]" = queue.Queue(maxsize=self.capacity)
        self._pending: list[tuple[GuardianEvent, float]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.published = self.stored = self.dropped = self.rejected = 0
        self.store_failures = 0
        self.last_store_error: Optional[str] = None
        self.last_stored_at: Optional[str] = None
        self.max_delay_ms = 0.0          # worst publish-to-stored delay since start
        self.last_delay_ms = 0.0

    # ------------------------------------------------------------- publish
    def publish(self, event: GuardianEvent) -> bool:
        """Queue one event. False when it was dropped. Never raises."""
        try:
            self._q.put_nowait((event, time.monotonic()))
            with self._lock:
                self.published += 1
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False
        except Exception:  # noqa: BLE001 -- publishing must never reach the caller
            with self._lock:
                self.dropped += 1
            return False

    def reject(self) -> None:
        """Count an event that failed validation before it reached the bus."""
        with self._lock:
            self.rejected += 1

    # --------------------------------------------------------------- drain
    def flush(self) -> int:
        """Write everything queued so far. Returns the number stored."""
        while len(self._pending) < self.capacity:
            try:
                self._pending.append(self._q.get_nowait())
            except queue.Empty:
                break
        stored = 0
        while self._pending:
            chunk = self._pending[:self.batch]
            try:
                self.store.append_events([e for e, _ in chunk], received_at=utcnow())
            except Exception as exc:  # noqa: BLE001 -- kept, counted, retried next flush
                with self._lock:
                    self.store_failures += 1
                    self.last_store_error = f"{type(exc).__name__}: {exc}"[:300]
                return stored
            now = time.monotonic()
            delay = max((now - t) * 1000 for _, t in chunk)
            del self._pending[:len(chunk)]
            stored += len(chunk)
            with self._lock:
                self.stored += len(chunk)
                self.last_delay_ms = round(delay, 1)
                self.max_delay_ms = max(self.max_delay_ms, self.last_delay_ms)
                self.last_stored_at = utcnow()
                self.last_store_error = None
        return stored

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.flush()
            except Exception as exc:  # noqa: BLE001 -- the drain loop itself never dies
                with self._lock:
                    self.last_store_error = f"{type(exc).__name__}: {exc}"[:300]
            self._stop.wait(self.flush_interval_s)
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="guardian-bus", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stats(self) -> dict:
        with self._lock:
            return {
                "running": self.running, "capacity": self.capacity,
                "backlog": self._q.qsize() + len(self._pending),
                "published": self.published, "stored": self.stored,
                "dropped": self.dropped, "rejected": self.rejected,
                "store_failures": self.store_failures, "last_store_error": self.last_store_error,
                "last_stored_at": self.last_stored_at,
                "last_delay_ms": self.last_delay_ms, "max_delay_ms": self.max_delay_ms,
            }
