"""Standalone, read-only Guardian HTTP surface; never imports trading workers."""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
from importlib import resources
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any, Mapping
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from .events import GuardianEvent, GuardianEventError, MAX_EVENT_BYTES
from .decision_traces import decision_traces
from .health import component_health
from .incidents import GuardianIncidentEngine
from .lab_backfill import GuardianLabBackfill
from .lab_feed_observer import GuardianLabFeedObserver
from .lab_observer import GuardianLabObserver
from .public_status import GuardianPublicStatusCollector
from .smc_execution_observer import GuardianSMCExecutionObserver
from .store import GuardianStore

_STATUSES = {
    200: "200 OK", 201: "201 Created", 400: "400 Bad Request",
    401: "401 Unauthorized", 403: "403 Forbidden", 404: "404 Not Found",
    405: "405 Method Not Allowed", 411: "411 Length Required",
    413: "413 Content Too Large", 422: "422 Unprocessable Content",
    503: "503 Service Unavailable",
}
_MAX_HTTP_BYTES = MAX_EVENT_BYTES + 2048
_ASSETS = {
    "/": ("command_center.html", "text/html; charset=utf-8"),
    "/assets/command-center.css": ("command_center.css", "text/css; charset=utf-8"),
    "/assets/command-center.js": ("command_center.js", "text/javascript; charset=utf-8"),
}
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class GuardianService:
    """WSGI app with per-source ingestion keys and a separate read key."""

    def __init__(self, store: GuardianStore, *, source_keys: Mapping[str, str],
                 read_key: str, required_components: tuple[str, ...]):
        if not source_keys or any(not isinstance(key, str) or len(key) < 24
                                  for key in source_keys.values()):
            raise ValueError("each Guardian source requires a private key of at least 24 characters")
        if len(set(source_keys.values())) != len(source_keys) or not isinstance(read_key, str) \
                or len(read_key) < 24 or read_key in source_keys.values():
            raise ValueError("Guardian source and read keys must be distinct")
        if not required_components or "guardian" not in required_components:
            raise ValueError("Guardian health must include its own component")
        self.store = store
        self.incidents = GuardianIncidentEngine(store)
        self.source_keys = dict(source_keys)
        self.read_key = read_key
        self.required_components = tuple(required_components)

    def _source(self, presented: str) -> str | None:
        for source, key in self.source_keys.items():
            if hmac.compare_digest(presented, key):
                return source
        return None

    def _read(self, presented: str) -> bool:
        return hmac.compare_digest(presented, self.read_key)

    @staticmethod
    def _body(environ: Mapping[str, Any]) -> dict:
        raw_length = environ.get("CONTENT_LENGTH", "")
        if not raw_length:
            raise _HTTPError(411, "CONTENT_LENGTH_REQUIRED")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise _HTTPError(400, "INVALID_CONTENT_LENGTH") from exc
        if length < 1 or length > _MAX_HTTP_BYTES:
            raise _HTTPError(413, "EVENT_TOO_LARGE")
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
            raise _HTTPError(400, "JSON_REQUIRED")
        body = environ["wsgi.input"].read(length)
        if len(body) != length:
            raise _HTTPError(400, "INCOMPLETE_BODY")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _HTTPError(400, "INVALID_JSON") from exc
        if not isinstance(payload, dict):
            raise _HTTPError(422, "JSON_OBJECT_REQUIRED")
        return payload

    @staticmethod
    def _respond(start_response, status: int, payload: dict):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        start_response(_STATUSES[status], [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(encoded))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ])
        return [encoded]

    @staticmethod
    def _static(start_response, path: str):
        filename, mime = _ASSETS[path]
        encoded = resources.files("tradexa.guardian").joinpath("assets", filename).read_bytes()
        start_response(_STATUSES[200], [
            ("Content-Type", mime), ("Content-Length", str(len(encoded))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", _CSP),
        ])
        return [encoded]

    def __call__(self, environ: Mapping[str, Any], start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "")
        presented = str(environ.get("HTTP_X_GUARDIAN_KEY", ""))
        try:
            if path in _ASSETS:
                if method != "GET":
                    raise _HTTPError(405, "METHOD_NOT_ALLOWED")
                return self._static(start_response, path)
            if path == "/healthz":
                if method != "GET":
                    raise _HTTPError(405, "METHOD_NOT_ALLOWED")
                self.store.record_heartbeat("guardian", "HEALTHY")
                return self._respond(start_response, 200, {
                    "service": "guardian", "self_state": "HEALTHY",
                    "platform_state": "UNKNOWN_UNTIL_EVIDENCE_CHECKED",
                })
            if path in ("/v1/events", "/v1/heartbeats") and method == "POST":
                source = self._source(presented)
                if source is None:
                    raise _HTTPError(401, "UNAUTHORIZED")
                payload = self._body(environ)
                if path == "/v1/events":
                    event = GuardianEvent.from_payload(payload)
                    if event.source_service != source:
                        raise _HTTPError(403, "SOURCE_MISMATCH")
                    inserted = self.store.append(event)
                    return self._respond(start_response, 201 if inserted else 200, {
                        "event_id": event.event_id,
                        "result": "APPENDED" if inserted else "ALREADY_PRESENT",
                    })
                component = payload.get("component")
                if not isinstance(component, str) or not (
                    component == source or component.startswith(source + "_")):
                    raise _HTTPError(403, "SOURCE_MISMATCH")
                if set(payload) - {"component", "state", "reason", "observed_at"}:
                    raise _HTTPError(422, "INVALID_HEARTBEAT")
                try:
                    observed_at = datetime.fromisoformat(
                        payload["observed_at"].replace("Z", "+00:00"))
                except (KeyError, AttributeError, ValueError) as exc:
                    raise _HTTPError(422, "INVALID_HEARTBEAT") from exc
                self.store.record_heartbeat(component, payload.get("state"),
                                            reason=payload.get("reason", ""),
                                            observed_at=observed_at)
                return self._respond(start_response, 200, {"result": "RECORDED"})
            if (path in ("/v1/events", "/v1/health", "/v1/incidents", "/v1/decision-traces") or
                    path.startswith("/v1/incidents/")) and method == "GET":
                if not self._read(presented):
                    raise _HTTPError(401, "UNAUTHORIZED")
                if path == "/v1/health":
                    health = component_health(
                        self.store.heartbeats(), self.required_components)
                    incidents = self.incidents.active_summary()
                    health["active_incidents"] = incidents
                    if health["state"] == "HEALTHY" and incidents["warning_or_higher"]:
                        # A heartbeat proves the components answered recently,
                        # not that an unresolved execution or journal finding
                        # disappeared. This is an observation only; no trading
                        # gate is changed by Guardian.
                        health["state"] = "DEGRADED"
                        health["state_reason"] = "ACTIVE_INCIDENTS"
                    return self._respond(start_response, 200, health)
                query = parse_qs(environ.get("QUERY_STRING", ""))
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError as exc:
                    raise _HTTPError(400, "INVALID_LIMIT") from exc
                if not 1 <= limit <= 500:
                    raise _HTTPError(400, "INVALID_LIMIT")
                if path == "/v1/decision-traces":
                    if limit > 100:
                        raise _HTTPError(400, "INVALID_LIMIT")
                    lab = query.get("lab", [None])[0]
                    if lab not in (None, "PRICE_ACTION", "SMC"):
                        raise _HTTPError(400, "INVALID_LAB")
                    return self._respond(start_response, 200,
                                         decision_traces(self.store, limit=limit, lab=lab))
                if path == "/v1/incidents":
                    state = query.get("state", [None])[0]
                    if state not in (None, "OPEN", "RECOVERING", "RECOVERED"):
                        raise _HTTPError(400, "INVALID_STATE")
                    return self._respond(start_response, 200, {
                        "incidents": self.incidents.list(limit=limit, state=state)})
                if path.startswith("/v1/incidents/"):
                    match = re.fullmatch(r"/v1/incidents/([0-9a-f]{32})/timeline", path)
                    if match is None:
                        raise _HTTPError(404, "NOT_FOUND")
                    incident = self.incidents.get(match.group(1))
                    if incident is None:
                        raise _HTTPError(404, "NOT_FOUND")
                    return self._respond(start_response, 200, {
                        "incident": incident,
                        "updates": self.incidents.timeline(match.group(1))})
                source = query.get("source_service", [None])[0]
                return self._respond(start_response, 200, {
                    "events": self.store.recent(limit, source_service=source)})
            if path in ("/v1/events", "/v1/health", "/v1/heartbeats", "/v1/incidents",
                        "/v1/decision-traces"):
                raise _HTTPError(405, "METHOD_NOT_ALLOWED")
            raise _HTTPError(404, "NOT_FOUND")
        except _HTTPError as exc:
            return self._respond(start_response, exc.status, {"error": exc.code})
        except GuardianEventError:
            return self._respond(start_response, 422, {"error": "INVALID_EVIDENCE"})
        except (ValueError, TypeError):
            return self._respond(start_response, 422, {"error": "INVALID_EVIDENCE"})
        except sqlite3.Error:
            return self._respond(start_response, 503, {"error": "PERSISTENCE_UNAVAILABLE"})


class _HTTPError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _self_heartbeat(store: GuardianStore, stopped: Event) -> None:
    while not stopped.is_set():
        try:
            store.record_heartbeat("guardian", "HEALTHY")
        except sqlite3.Error:
            pass  # A missing/stale heartbeat is reported as UNKNOWN by /v1/health.
        stopped.wait(15)


def _incident_monitor(store: GuardianStore, engine: GuardianIncidentEngine,
                      stopped: Event) -> None:
    while not stopped.is_set():
        try:
            processed = engine.scan(limit=500)
            store.record_heartbeat("guardian_incident_engine", "HEALTHY")
        except Exception:
            processed = 0
            try:
                store.record_heartbeat("guardian_incident_engine", "FAILED",
                                       reason="INCIDENT_ANALYSIS_FAILED")
            except sqlite3.Error:
                pass
        stopped.wait(0.25 if processed == 500 else 5)


def _public_status_monitor(store: GuardianStore, collector: GuardianPublicStatusCollector,
                           stopped: Event) -> None:
    while not stopped.is_set():
        try:
            collector.poll()
        except Exception:
            # A failed probe cannot declare trading unhealthy or healthy.
            # Its own heartbeat explains why downstream evidence may go stale.
            try:
                store.record_heartbeat("guardian_public_probe", "FAILED",
                                       reason="PUBLIC_STATUS_PROBE_FAILED")
            except sqlite3.Error:
                pass
        stopped.wait(30)


def _lab_observation_monitor(store: GuardianStore, collector: GuardianLabObserver,
                             stopped: Event) -> None:
    while not stopped.is_set():
        try:
            collector.poll()
        except Exception:
            try:
                store.record_heartbeat("guardian_lab_probe", "FAILED",
                                       reason="LAB_OBSERVATION_FAILED")
            except sqlite3.Error:
                pass
        stopped.wait(30)


def _lab_feed_monitor(store: GuardianStore, collector: GuardianLabFeedObserver,
                      stopped: Event) -> None:
    while not stopped.is_set():
        try:
            collector.poll()
        except Exception:
            try:
                store.record_heartbeat("guardian_lab_feed_probe", "FAILED",
                                       reason="LAB_FEED_OBSERVATION_FAILED")
            except sqlite3.Error:
                pass
        stopped.wait(15)


def _lab_backfill_monitor(store: GuardianStore, collector: GuardianLabBackfill,
                          stopped: Event) -> None:
    while not stopped.is_set():
        try:
            collector.poll()
        except Exception:
            try:
                store.record_heartbeat("guardian_lab_backfill", "FAILED",
                                       reason="LAB_BACKFILL_FAILED")
            except sqlite3.Error:
                pass
        stopped.wait(30)


def _smc_execution_monitor(store: GuardianStore,
                           collector: GuardianSMCExecutionObserver,
                           stopped: Event) -> None:
    while not stopped.is_set():
        try:
            collector.poll()
        except Exception:
            try:
                store.record_heartbeat("guardian_smc_execution_probe", "FAILED",
                                       reason="SMC_EXECUTION_OBSERVATION_FAILED")
            except sqlite3.Error:
                pass
        stopped.wait(30)


def main() -> None:
    """Run separately: python -m tradexa.guardian.service (loopback by default)."""
    source_keys = json.loads(os.environ["GUARDIAN_SOURCE_KEYS_JSON"])
    if not isinstance(source_keys, dict):
        raise ValueError("GUARDIAN_SOURCE_KEYS_JSON must be an object")
    read_key = os.environ["GUARDIAN_READ_KEY"]
    lab_observer_key = os.environ.get("GUARDIAN_LAB_OBSERVER_KEY", "")
    hub_key = os.environ.get("HUB_CONTROL_KEY")
    if hub_key and hub_key in (read_key, lab_observer_key, *source_keys.values()):
        raise ValueError("Guardian credentials must not reuse HUB_CONTROL_KEY")
    if lab_observer_key and lab_observer_key in (read_key, *source_keys.values()):
        raise ValueError("Guardian lab observation credential must be independent")
    required = tuple(part.strip() for part in os.environ.get(
        "GUARDIAN_REQUIRED_COMPONENTS",
        "guardian,guardian_incident_engine,api,instance_ledger,instance_market_data,"
        "trading_instances,pa_lab,smc_lab"
    ).split(",") if part.strip())
    public_url = os.environ.get("GUARDIAN_PUBLIC_STATUS_URL", "").strip()
    if public_url and "guardian_public_probe" not in required:
        required += ("guardian_public_probe",)
    lab_url = os.environ.get("GUARDIAN_LAB_OBSERVER_URL", "").strip()
    if lab_url and "guardian_lab_probe" not in required:
        required += ("guardian_lab_probe",)
    lab_feed_url = os.environ.get("GUARDIAN_LAB_FEED_URL", "").strip()
    if lab_feed_url:
        required += tuple(name for name in
                          ("guardian_lab_feed_probe", "pa_feed", "smc_feed")
                          if name not in required)
    backfill_url = os.environ.get("GUARDIAN_LAB_BACKFILL_URL", "").strip()
    if backfill_url and "guardian_lab_backfill" not in required:
        required += ("guardian_lab_backfill",)
    smc_execution_url = os.environ.get("GUARDIAN_SMC_EXECUTION_URL", "").strip()
    if smc_execution_url and "guardian_smc_execution_probe" not in required:
        required += ("guardian_smc_execution_probe",)
    store = GuardianStore(Path(os.environ["GUARDIAN_DB_PATH"]))
    app = GuardianService(store, source_keys=source_keys, read_key=read_key,
                          required_components=required)
    stopped = Event()
    monitor = Thread(target=_self_heartbeat, args=(store, stopped), daemon=True)
    incident_monitor = Thread(target=_incident_monitor,
                              args=(store, app.incidents, stopped), daemon=True)
    public_collector = (GuardianPublicStatusCollector(store, public_url)
                        if public_url else None)
    public_monitor = (Thread(target=_public_status_monitor,
                             args=(store, public_collector, stopped), daemon=True)
                      if public_collector else None)
    lab_collector = (GuardianLabObserver(store, lab_url, lab_observer_key)
                     if lab_url else None)
    lab_monitor = (Thread(target=_lab_observation_monitor,
                          args=(store, lab_collector, stopped), daemon=True)
                   if lab_collector else None)
    lab_feed_collector = (GuardianLabFeedObserver(store, lab_feed_url, lab_observer_key)
                          if lab_feed_url else None)
    lab_feed_monitor = (Thread(target=_lab_feed_monitor,
                               args=(store, lab_feed_collector, stopped), daemon=True)
                        if lab_feed_collector else None)
    backfill_collector = (GuardianLabBackfill(store, backfill_url, lab_observer_key)
                          if backfill_url else None)
    backfill_monitor = (Thread(target=_lab_backfill_monitor,
                               args=(store, backfill_collector, stopped), daemon=True)
                        if backfill_collector else None)
    smc_execution_collector = (
        GuardianSMCExecutionObserver(store, smc_execution_url, lab_observer_key)
        if smc_execution_url else None)
    smc_execution_monitor = (
        Thread(target=_smc_execution_monitor,
               args=(store, smc_execution_collector, stopped), daemon=True)
        if smc_execution_collector else None)
    monitor.start()
    incident_monitor.start()
    if public_monitor:
        public_monitor.start()
    if lab_monitor:
        lab_monitor.start()
    if lab_feed_monitor:
        lab_feed_monitor.start()
    if backfill_monitor:
        backfill_monitor.start()
    if smc_execution_monitor:
        smc_execution_monitor.start()
    try:
        with make_server(os.environ.get("GUARDIAN_BIND_HOST", "127.0.0.1"),
                         int(os.environ.get("GUARDIAN_PORT", "8765")), app) as server:
            server.serve_forever()
    finally:
        stopped.set()
        monitor.join(timeout=2)
        incident_monitor.join(timeout=2)
        if public_monitor:
            public_monitor.join(timeout=2)
        if lab_monitor:
            lab_monitor.join(timeout=2)
        if lab_feed_monitor:
            lab_feed_monitor.join(timeout=2)
        if backfill_monitor:
            backfill_monitor.join(timeout=2)
        if smc_execution_monitor:
            smc_execution_monitor.join(timeout=2)


if __name__ == "__main__":
    main()
