"""Optional, bounded producer transport; trading never waits for Guardian I/O."""
from __future__ import annotations

from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .events import GuardianEvent, GuardianEventError


class GuardianEmitter:
    """Best-effort telemetry. Canonical trading ledgers remain authoritative."""

    def __init__(self, *, source_service: str, endpoint: str, key: str,
                 capacity: int = 1024, timeout_s: float = 1.0,
                 sender: Callable[[bytes], int] | None = None):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname \
                or parsed.path != "/v1/events" or parsed.username or parsed.password \
                or parsed.query or parsed.fragment:
            raise ValueError("Guardian endpoint must end in /v1/events")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("unencrypted Guardian ingestion is loopback-only")
        if not source_service or not key or capacity < 1 or timeout_s <= 0:
            raise ValueError("invalid Guardian producer configuration")
        self.source_service = source_service
        self.endpoint = endpoint
        self.key = key
        self.timeout_s = timeout_s
        self._queue: Queue[bytes] = Queue(maxsize=capacity)
        self._stopped = Event()
        self._lock = Lock()
        self._counts = {"enqueued": 0, "delivered": 0, "invalid": 0,
                        "backpressure_dropped": 0, "delivery_failed": 0}
        self._sender = sender or self._send_http
        self._thread = Thread(target=self._run, name="guardian-emitter", daemon=True)
        self._thread.start()

    def _increment(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def _send_http(self, payload: bytes) -> int:
        request = Request(self.endpoint, data=payload, method="POST", headers={
            "Content-Type": "application/json", "X-Guardian-Key": self.key,
        })
        with urlopen(request, timeout=self.timeout_s) as response:
            response.read(1024)
            return response.status

    def emit(self, event: GuardianEvent) -> bool:
        """Never perform network/DB I/O or raise on a telemetry failure."""
        if self._stopped.is_set() or not isinstance(event, GuardianEvent) \
                or event.source_service != self.source_service:
            self._increment("invalid")
            return False
        try:
            payload = event.canonical_json().encode("utf-8")
        except (GuardianEventError, TypeError, ValueError):
            self._increment("invalid")
            return False
        try:
            self._queue.put_nowait(payload)
        except Full:
            self._increment("backpressure_dropped")
            return False
        self._increment("enqueued")
        return True

    def _run(self) -> None:
        while not self._stopped.is_set() or not self._queue.empty():
            try:
                payload = self._queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                if self._sender(payload) in (200, 201):
                    self._increment("delivered")
                else:
                    self._increment("delivery_failed")
            except Exception:  # Optional telemetry must not kill its worker thread.
                self._increment("delivery_failed")
            finally:
                self._queue.task_done()

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def close(self, *, timeout_s: float = 3.0) -> bool:
        """Try to flush queued telemetry; return False if a sender remains stuck."""
        self._stopped.set()
        self._thread.join(timeout=timeout_s)
        return not self._thread.is_alive()
