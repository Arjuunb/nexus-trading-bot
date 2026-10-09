"""Optional, bounded producer transport; trading never waits for Guardian I/O."""
from __future__ import annotations

import math
import re
import uuid
from collections import deque
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .events import GuardianEvent, GuardianEventError
from .transport_health import KIND, validate_transport_event


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None  # A producer credential must never follow a Location header.


class GuardianEmitter:
    """Best-effort telemetry. Canonical trading ledgers remain authoritative."""

    def __init__(self, *, source_service: str, endpoint: str, key: str,
                 capacity: int = 1024, timeout_s: float = 1.0,
                 sender: Callable[[bytes], int] | None = None,
                 publish_diagnostics: bool = False, diagnostics_interval_s: float = 30.0):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname \
                or parsed.path != "/v1/events" or parsed.username or parsed.password \
                or parsed.query or parsed.fragment:
            raise ValueError("Guardian endpoint must end in /v1/events")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("unencrypted Guardian ingestion is loopback-only")
        if (not isinstance(source_service, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", source_service) or
                not isinstance(key, str) or not key or type(capacity) is not int or not 1 <= capacity <= 65536 or
                type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 30 or
                type(publish_diagnostics) is not bool or type(diagnostics_interval_s) not in (int, float) or
                not math.isfinite(diagnostics_interval_s) or not 1 <= diagnostics_interval_s <= 300):
            raise ValueError("invalid Guardian producer configuration")
        self.source_service = source_service
        self.endpoint = endpoint
        self.key = key
        self.timeout_s = timeout_s
        self._queue: Queue[tuple[bytes, float]] = Queue(maxsize=capacity)
        self._queued_at: deque[float] = deque()
        self._in_flight_at: float | None = None
        self._stopped = Event()
        self._wake = Event()
        self._lock = Lock()
        self._counts = {"enqueued": 0, "delivered": 0, "invalid": 0,
                        "backpressure_dropped": 0, "delivery_failed": 0}
        self._sender = sender or self._send_http
        self._epoch = uuid.uuid4().hex
        self._started_at = monotonic()
        self._last_delivery_latency_ms: float | None = None
        self._publish_diagnostics = publish_diagnostics
        self._diagnostics_interval_s = diagnostics_interval_s
        self._next_diagnostic = self._started_at
        self._snapshot_sequence = 0
        self._diagnostic_delivery_failed = 0
        self._thread = Thread(target=self._run, name="guardian-emitter", daemon=True)
        self._thread.start()

    def _increment(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def _send_http(self, payload: bytes) -> int:
        request = Request(self.endpoint, data=payload, method="POST", headers={
            "Content-Type": "application/json", "X-Guardian-Key": self.key,
        })
        with build_opener(_NoRedirect()).open(request, timeout=self.timeout_s) as response:
            response.read(1024)
            return response.status

    def emit(self, event: GuardianEvent) -> bool:
        """Never perform network/DB I/O or raise on a telemetry failure."""
        if not isinstance(event, GuardianEvent) \
                or event.source_service != self.source_service:
            self._increment("invalid")
            return False
        try:
            payload = event.canonical_json().encode("utf-8")
        except (GuardianEventError, TypeError, ValueError):
            self._increment("invalid")
            return False
        # Admission, queue accounting and close share one short local lock.
        # Serialization/network/DB work never holds this lock.
        with self._lock:
            if self._stopped.is_set():
                self._counts["invalid"] += 1
                return False
            queued_at = monotonic()
            try:
                self._queue.put_nowait((payload, queued_at))
            except Full:
                self._counts["backpressure_dropped"] += 1
                return False
            self._queued_at.append(queued_at)
            self._counts["enqueued"] += 1
        self._wake.set()
        return True

    def _run(self) -> None:
        while True:
            with self._lock:
                try:
                    item = self._queue.get_nowait()
                    self._in_flight_at = self._queued_at.popleft()
                except Empty:
                    item = None
                drained = self._stopped.is_set() and item is None
            if item is not None:
                payload, queued_at = item
                outcome = "delivery_failed"
                try:
                    if self._sender(payload) in (200, 201):
                        outcome = "delivered"
                except Exception:  # Best-effort telemetry cannot kill the sender.
                    pass
                finally:
                    with self._lock:
                        self._counts[outcome] += 1
                        self._last_delivery_latency_ms = round(max(0, monotonic()-queued_at)*1000, 3)
                        self._in_flight_at = None
                        self._queue.task_done()
            self._publish_if_due(force=drained)
            if drained:
                return
            if item is None:
                self._wake.wait(0.1)
                self._wake.clear()

    def _diagnostics_locked(self) -> dict:
        now = monotonic()
        oldest = self._in_flight_at if self._in_flight_at is not None else self._queued_at[0] if self._queued_at else None
        return {
            "transport_schema_version": 1, "producer_epoch": self._epoch,
            "snapshot_sequence": self._snapshot_sequence, "uptime_seconds": round(max(0, now-self._started_at), 6),
            "queue_capacity": self._queue.maxsize, "queued_events": len(self._queued_at),
            "in_flight_events": int(self._in_flight_at is not None),
            "oldest_pending_age_seconds": None if oldest is None else round(max(0, now-oldest), 6),
            "last_delivery_latency_ms": self._last_delivery_latency_ms,
            "accepting_events": not self._stopped.is_set(), "worker_alive": self._thread.is_alive(),
            "counters": dict(self._counts), "diagnostic_delivery_failed": self._diagnostic_delivery_failed,
        }

    def diagnostics(self) -> dict:
        """One coherent local snapshot, without network, storage or secrets."""
        with self._lock:
            return self._diagnostics_locked()

    def _publish_if_due(self, *, force: bool = False) -> None:
        if not self._publish_diagnostics or (not force and monotonic() < self._next_diagnostic):
            return
        # Reports bypass the event queue/counters, preventing self-amplification.
        with self._lock:
            self._snapshot_sequence += 1
            data = self._diagnostics_locked()
        try:
            event = GuardianEvent(
                source_service=self.source_service, source_component="transport", event_type=KIND,
                event_id=f"transport_{data['producer_epoch']}_{data['snapshot_sequence']}", evidence=data)
            validate_transport_event(event)
            if self._sender(event.canonical_json().encode("utf-8")) not in (200, 201):
                raise RuntimeError("untrusted transport send failure")
        except Exception:  # Optional reporting failure must not kill the event worker.
            with self._lock:
                self._diagnostic_delivery_failed += 1
        self._next_diagnostic = monotonic()+self._diagnostics_interval_s

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def close(self, *, timeout_s: float = 3.0) -> bool:
        """Try to flush queued telemetry; return False if a sender remains stuck."""
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 <= timeout_s <= 30:
            raise ValueError("invalid close timeout")
        with self._lock:
            self._stopped.set()
        self._wake.set()
        self._thread.join(timeout=timeout_s)
        return not self._thread.is_alive()
