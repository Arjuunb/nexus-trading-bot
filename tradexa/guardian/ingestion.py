"""Bounded per-process HTTP admission, never a trading or persistence authority."""
from __future__ import annotations

import math
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, fields
from threading import Lock
from time import monotonic
from typing import Callable, Mapping, Sequence

from .events import _NAME


@dataclass(frozen=True)
class IngestionLimits:
    # Local protective defaults, not workload or production acceptance targets.
    event_rate: int = 60
    event_burst: int = 256
    event_source_rate: int = 20
    event_source_burst: int = 128
    event_inflight: int = 4
    heartbeat_rate: int = 10
    heartbeat_burst: int = 32
    heartbeat_source_rate: int = 2
    heartbeat_source_burst: int = 8
    heartbeat_inflight: int = 1

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            upper = 16 if item.name.endswith("inflight") else 2000 if item.name.endswith("burst") else 1000
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("invalid Guardian ingestion limits")
        for kind in ("event", "heartbeat"):
            if (getattr(self, kind+"_source_rate") > getattr(self, kind+"_rate") or
                    getattr(self, kind+"_source_burst") > getattr(self, kind+"_burst")):
                raise ValueError("source limits cannot exceed route limits")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> IngestionLimits:
        values = {}
        allowed = {"GUARDIAN_INGESTION_"+item.name.upper() for item in fields(cls)}
        if any(name.startswith("GUARDIAN_INGESTION_") and name not in allowed for name in environ):
            raise ValueError("unknown Guardian ingestion environment configuration")
        for item in fields(cls):
            name = "GUARDIAN_INGESTION_"+item.name.upper()
            if name in environ:
                raw = environ[name]
                if not isinstance(raw, str) or re.fullmatch(r"[1-9][0-9]{0,3}", raw) is None:
                    raise ValueError("invalid Guardian ingestion environment configuration")
                values[item.name] = int(raw)
        return cls(**values)


class IngestionOverload(Exception):
    def __init__(self, reasons: tuple[str, ...], retry_after_seconds: int):
        super().__init__("INGESTION_LIMITED")
        self.reasons = reasons
        self.retry_after_seconds = retry_after_seconds


def _clock_value(clock: Callable[[], float]) -> float:
    value = clock()
    if type(value) not in (int, float) or not -2**53 <= value <= 2**53 or not math.isfinite(value):
        raise ValueError("invalid Guardian admission clock")
    return float(value)


def _bucket(rate: int, burst: int, now: float) -> dict:
    return {"rate": rate, "burst": burst, "tokens": float(burst), "refilled_at": now,
            "admitted_requests": 0, "rate_limited_requests": 0,
            "capacity_limited_requests": 0, "in_flight_requests": 0}


class IngestionAdmission:
    """Atomic token/capacity leases for a fixed, authenticated source inventory.

    No database, timer, queue, network, arbitrary-address map or durable log.
    Events and heartbeats have independent budgets. Never hold the local lock
    while reading a body, validating evidence or waiting on persistence.
    """
    def __init__(self, sources: Sequence[str], *, limits: IngestionLimits | None = None,
                 clock: Callable[[], float] = monotonic):
        if (not isinstance(sources, (tuple, list)) or not 1 <= len(sources) <= 128 or
                any(not isinstance(name, str) or _NAME.fullmatch(name) is None for name in sources) or
                len(set(sources)) != len(sources)):
            raise ValueError("invalid Guardian ingestion sources")
        self._limits = limits if limits is not None else IngestionLimits()
        if not isinstance(self._limits, IngestionLimits):
            raise ValueError("invalid Guardian ingestion policy")
        self._clock = clock
        self._started_at = self._last_time = _clock_value(clock)
        self._epoch = uuid.uuid4().hex
        self._lock = Lock()
        self._routes = {}
        for name, kind in (("events", "event"), ("heartbeats", "heartbeat")):
            row = _bucket(getattr(self._limits, kind+"_rate"),
                          getattr(self._limits, kind+"_burst"), self._started_at)
            row["max_in_flight"] = getattr(self._limits, kind+"_inflight")
            row["sources"] = {source: _bucket(getattr(self._limits, kind+"_source_rate"),
                                             getattr(self._limits, kind+"_source_burst"), self._started_at)
                              for source in sources}
            self._routes[name] = row

    def _now_locked(self) -> float:
        # A backwards injected clock cannot grant tokens or rewind refill state.
        self._last_time = max(self._last_time, _clock_value(self._clock))
        return self._last_time

    @staticmethod
    def _refill(row: dict, now: float) -> None:
        row["tokens"] = min(row["burst"], row["tokens"]+(now-row["refilled_at"])*row["rate"])
        row["refilled_at"] = now

    @contextmanager
    def admit(self, kind: str, source: str):
        if kind not in self._routes or source not in self._routes[kind]["sources"]:
            raise ValueError("unconfigured Guardian admission source/route")
        with self._lock:
            route = self._routes[kind]
            producer = route["sources"][source]
            now = self._now_locked()
            self._refill(route, now)
            self._refill(producer, now)
            reasons, wait = [], 1
            for row, reason in ((route, "GLOBAL_RATE_LIMIT"), (producer, "SOURCE_RATE_LIMIT")):
                if row["tokens"] < 1:
                    reasons.append(reason)
                    wait = max(wait, math.ceil((1-row["tokens"])/row["rate"]))
            if reasons:
                route["rate_limited_requests"] += 1
                producer["rate_limited_requests"] += 1
                raise IngestionOverload(tuple(reasons), wait)
            if route["in_flight_requests"] >= route["max_in_flight"]:
                route["capacity_limited_requests"] += 1
                producer["capacity_limited_requests"] += 1
                raise IngestionOverload(("WRITE_CAPACITY_FULL",), 1)
            for row in (route, producer):
                row["tokens"] -= 1
                row["admitted_requests"] += 1
                row["in_flight_requests"] += 1
        try:
            yield
        finally:
            with self._lock:
                route["in_flight_requests"] -= 1
                producer["in_flight_requests"] -= 1

    def snapshot(self) -> dict:
        with self._lock:
            now = self._now_locked()
            rows = {}
            for name, route in self._routes.items():
                self._refill(route, now)
                for producer in route["sources"].values():
                    self._refill(producer, now)
                def project(row):
                    return {"rate_per_second": row["rate"], "burst": row["burst"],
                            "available_tokens": round(row["tokens"], 6),
                            **{key: row[key] for key in ("admitted_requests", "rate_limited_requests",
                                                        "capacity_limited_requests", "in_flight_requests")}}
                rows[name] = {**project(route), "max_in_flight": route["max_in_flight"],
                              "sources": {source: project(row) for source, row in route["sources"].items()}}
            limited = any(row["rate_limited_requests"] or row["capacity_limited_requests"] or
                          row["in_flight_requests"] >= row["max_in_flight"] for row in rows.values())
            return {
                "scope": "CURRENT_GUARDIAN_PROCESS_ADMISSION_ONLY", "process_epoch": self._epoch,
                "uptime_seconds": round(now-self._started_at, 6), "routes": rows,
                "state": "DEGRADED" if limited else "HEALTHY",
                "reason": "ADMISSION_PRESSURE_OBSERVED" if limited else "LOCAL_ADMISSION_WITHIN_LIMITS",
                "history_persistent": False, "persistence_verified": False,
                "producer_coverage_verified": False, "trading_integrity_verified": False,
                "automatic_action_allowed": False,
            }
