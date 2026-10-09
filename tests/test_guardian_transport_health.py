"""Producer transport evidence is epoch-scoped, optional and never trading authority."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event
from time import perf_counter

import pytest

from tradexa.guardian import transport_health as diagnostics
from tradexa.guardian.emitter import GuardianEmitter
from tradexa.guardian.events import GuardianEvent, GuardianEventError
from tradexa.guardian.service import GuardianService
from tradexa.guardian.store import GuardianStore
from tests.test_guardian_service import READ_KEY, SOURCE_KEY, _request

NOW = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
EPOCH = "1"*32
FIELDS = ("enqueued", "delivered", "invalid", "backpressure_dropped", "delivery_failed")


def snapshot(**changes):
    result = {
        "transport_schema_version": 1, "producer_epoch": EPOCH, "snapshot_sequence": 1,
        "uptime_seconds": 10.0, "queue_capacity": 10, "queued_events": 0, "in_flight_events": 0,
        "oldest_pending_age_seconds": None, "last_delivery_latency_ms": None,
        "accepting_events": True, "worker_alive": True,
        "counters": {field: 0 for field in FIELDS}, "diagnostic_delivery_failed": 0,
    }
    result.update(changes)
    return result


def report(metrics=None, *, source="smc_lab", timestamp=NOW):
    metrics = snapshot() if metrics is None else metrics
    return GuardianEvent(source_service=source, source_component="transport",
                         event_type="producer_transport_observed", timestamp=timestamp,
                         event_id=f"transport_{metrics['producer_epoch']}_{metrics['snapshot_sequence']}",
                         evidence=metrics)


@pytest.fixture
def store(tmp_path):
    return GuardianStore(tmp_path/"guardian.db")


def _append(store, event, *, receipt=NOW):
    with closing(store._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        store._append_in_transaction(conn, event, receipt.isoformat())
        conn.commit()


def _view(store):
    return diagnostics.transport_health(store.path, ("smc_lab", "pa_lab"), now=NOW)


def _source(view, source="smc_lab"):
    return view["sources"][source]


def _event(number=1):
    return GuardianEvent(source_service="smc_lab", source_component="agent", event_type="decision_observed",
                         event_id=f"decision_fixture_{number:08d}", timestamp=NOW)


def _emitter(sender, **kwargs):
    return GuardianEmitter(source_service="smc_lab", endpoint="http://127.0.0.1:8765/v1/events",
                           key=SOURCE_KEY, sender=sender, **kwargs)


def test_unobserved_producers_are_unknown_not_zero_or_healthy(store):
    view = _view(store)
    assert view["state"] == "UNKNOWN" and view["producer_inventory_verified"] is False
    assert view["trading_integrity_verified"] is view["automatic_action_allowed"] is False
    assert view["source_clock_verified"] is False and view["all_events_delivered_verified"] is False
    for row in view["sources"].values():
        assert row["state"] == "UNKNOWN" and row["metrics"] is None
    assert store.count() == 0


def test_latest_fresh_report_exposes_only_typed_epoch_scoped_metrics(store):
    _append(store, report())
    row = _source(_view(store))
    assert row["state"] == "HEALTHY" and row["report_age_seconds"] == 0
    assert row["metrics"]["producer_epoch"] == EPOCH
    assert row["metrics"]["counters"] == {field: 0 for field in FIELDS}
    assert row["counter_history_verified"] is False
    assert _source(_view(store), "pa_lab")["state"] == "UNKNOWN"


@pytest.mark.parametrize("field", ["invalid", "backpressure_dropped", "delivery_failed", "diagnostic_delivery_failed"])
def test_retained_loss_evidence_is_degraded_not_silently_green(store, field):
    data = snapshot()
    if field == "diagnostic_delivery_failed":
        data[field] = 1
    else:
        data["counters"][field] = 1
        if field == "delivery_failed":
            data["counters"]["enqueued"] = 1
            data["last_delivery_latency_ms"] = 2.0
    _append(store, report(data))
    assert _source(_view(store))["state"] == "DEGRADED"


@pytest.mark.parametrize("age", [91, -6])
@pytest.mark.parametrize("clock", ["receipt", "source"])
def test_old_or_future_reports_never_refresh_producer_readiness(store, age, clock):
    time = NOW-timedelta(seconds=age)
    _append(store, report(timestamp=time if clock == "source" else NOW),
            receipt=time if clock == "receipt" else NOW)
    row = _source(_view(store))
    assert row["state"] == "UNKNOWN" and row["metrics"] is None


@pytest.mark.parametrize("field", ["accepting_events", "worker_alive"])
def test_stopped_reported_producer_is_blocked_even_with_no_pending_events(store, field):
    _append(store, report(snapshot(**{field: False})))
    assert _source(_view(store))["state"] == "BLOCKED"


def test_pending_and_inflight_age_is_not_network_ingestion_delay(store):
    data = snapshot(uptime_seconds=100.0, queued_events=1, in_flight_events=1, oldest_pending_age_seconds=61.0)
    data["counters"]["enqueued"] = 2
    _append(store, report(data))
    row = _source(_view(store))
    assert row["state"] == "DEGRADED" and row["reason"] == "PRODUCER_BACKLOG_PRESSURE"
    assert row["metrics"]["queued_events"] == row["metrics"]["in_flight_events"] == 1
    assert _view(store)["network_ingestion_delay_ms"] is None


def test_restart_epoch_resets_do_not_pool_counters_or_prove_process_inventory(store):
    before = snapshot(last_delivery_latency_ms=5.0)
    before["counters"].update(enqueued=10, delivered=10)
    _append(store, report(before))
    after = snapshot(producer_epoch="2"*32)
    _append(store, report(after))
    row = _source(_view(store))
    assert row["state"] == "HEALTHY" and row["metrics"]["counters"]["delivered"] == 0
    assert row["metrics"]["producer_epoch"] == "2"*32
    assert row["counter_history_verified"] is False
    assert _view(store)["producer_inventory_verified"] is False


@pytest.mark.parametrize("fault", ["counter_regression", "sequence_regression", "uptime_regression", "capacity_change"])
def test_same_epoch_regression_is_unknown_not_reset(store, fault):
    before = snapshot(snapshot_sequence=2, last_delivery_latency_ms=5.0)
    before["counters"].update(enqueued=10, delivered=10)
    _append(store, report(before))
    after = deepcopy(before)
    after["snapshot_sequence"] = 3
    if fault == "counter_regression":
        after["counters"].update(enqueued=9, delivered=9)
    elif fault == "sequence_regression":
        after["snapshot_sequence"] = 1
    elif fault == "uptime_regression":
        after["uptime_seconds"] = 9.0
    else:
        after["queue_capacity"] = 11
    _append(store, report(after))
    row = _source(_view(store))
    assert row["state"] == "UNKNOWN" and row["metrics"] is None


@pytest.mark.parametrize("field,value", [
    ("snapshot_sequence", 0), ("snapshot_sequence", True), ("snapshot_sequence", 2**63),
    ("producer_epoch", "wrong"), ("transport_schema_version", 2), ("uptime_seconds", -1),
    ("uptime_seconds", float("inf")), ("uptime_seconds", True), ("queued_events", -1),
    ("queue_capacity", 0), ("queue_capacity", True), ("in_flight_events", 2),
    ("accepting_events", "true"), ("worker_alive", 1), ("diagnostic_delivery_failed", -1),
    ("oldest_pending_age_seconds", 1.0), ("last_delivery_latency_ms", 0.0),
], ids=("zero-sequence", "bool-sequence", "overflow-sequence", "bad-epoch", "bad-schema",
         "negative-uptime", "infinite-uptime", "bool-uptime", "negative-queue", "zero-capacity",
         "bool-capacity", "two-inflight", "string-accepting", "integer-alive", "negative-report-failure",
         "age-without-pending", "latency-without-completed"))
def test_invalid_typed_snapshot_is_rejected(field, value):
    with pytest.raises(GuardianEventError):
        diagnostics.validate_transport_event(report(snapshot(**{field: value})))


def test_huge_integer_duration_is_rejected_without_overflow_or_http_500(store):
    data = snapshot(uptime_seconds=10**1000)
    with pytest.raises(GuardianEventError):
        diagnostics.validate_transport_event(report(data))
    app = GuardianService(store, source_keys={"smc_lab": SOURCE_KEY}, read_key=READ_KEY,
                          required_components=("guardian",))
    assert _request(app, "POST", "/v1/events", payload=json.loads(report(data).canonical_json()), key=SOURCE_KEY)[0] == 422
    assert store.count() == 0


@pytest.mark.parametrize("fault", ["missing", "extra", "counter-bool", "counter-overflow", "accounting", "queue-over-capacity"])
def test_bad_shape_and_incoherent_accounting_are_rejected(fault):
    data = snapshot()
    if fault == "missing":
        data.pop("worker_alive")
    elif fault == "extra":
        data["debug_endpoint"] = "sensitive-prose"
    elif fault == "counter-bool":
        data["counters"]["enqueued"] = True
    elif fault == "counter-overflow":
        data["counters"]["invalid"] = 2**63
    elif fault == "accounting":
        data["counters"]["enqueued"] = 1
    else:
        data.update(queued_events=11, oldest_pending_age_seconds=1.0)
        data["counters"]["enqueued"] = 11
    with pytest.raises(GuardianEventError):
        diagnostics.validate_transport_event(report(data))


@pytest.mark.parametrize("field", ["oldest_pending_age_seconds", "last_delivery_latency_ms"])
def test_transport_measurements_cannot_predate_the_current_process_epoch(field):
    data = snapshot()
    if field == "oldest_pending_age_seconds":
        data.update(queued_events=1, oldest_pending_age_seconds=11.0)
        data["counters"]["enqueued"] = 1
    else:
        data["counters"].update(enqueued=1, delivered=1)
        data["last_delivery_latency_ms"] = 11000.0
    with pytest.raises(GuardianEventError):
        diagnostics.validate_transport_event(report(data))


def test_default_emitter_never_publishes_extra_reports_and_epochs_differ():
    sent = []
    def sender(payload):
        sent.append(json.loads(payload))
        return 201
    first, second = _emitter(sender), _emitter(sender)
    assert first.emit(_event())
    assert first.close() and second.close()
    assert len(sent) == 1 and sent[0]["event_type"] == "decision_observed"
    one, two = first.diagnostics(), second.diagnostics()
    assert one["producer_epoch"] != two["producer_epoch"]
    assert one["snapshot_sequence"] == two["snapshot_sequence"] == 0
    assert one["worker_alive"] is one["accepting_events"] is False
    assert SOURCE_KEY not in json.dumps(one) and "endpoint" not in one


def test_inflight_queue_drop_snapshot_is_coherent_nonblocking_and_monotonic(monkeypatch):
    entered, release = Event(), Event()
    def sender(_):
        entered.set()
        assert release.wait(3)
        return 201
    emitter = _emitter(sender, capacity=1)
    try:
        assert emitter.emit(_event()) and entered.wait(1)
        start = perf_counter()
        assert emitter.emit(_event(2)) and not emitter.emit(_event(3))
        for _ in range(100):
            data = emitter.diagnostics()
            assert data["queued_events"] == data["in_flight_events"] == 1
            assert data["counters"]["enqueued"] == 2 and data["counters"]["backpressure_dropped"] == 1
            assert data["oldest_pending_age_seconds"] >= 0
        assert perf_counter()-start < 0.5
    finally:
        release.set()
        assert emitter.close()
    data = emitter.diagnostics()
    assert data["queued_events"] == data["in_flight_events"] == 0
    assert data["counters"]["delivered"] == 2
    assert data["oldest_pending_age_seconds"] is None and data["last_delivery_latency_ms"] >= 0


def test_optional_publisher_uses_existing_sender_and_persists_source_scoped_report(store):
    app = GuardianService(store, source_keys={"smc_lab": SOURCE_KEY}, read_key=READ_KEY,
                          required_components=("guardian",))
    published = Event()
    def sender(payload):
        body = json.loads(payload)
        status, _, _ = _request(app, "POST", "/v1/events", payload=body, key=SOURCE_KEY)
        if body["event_type"] == "producer_transport_observed":
            published.set()
        return status
    emitter = _emitter(sender, publish_diagnostics=True)
    try:
        assert published.wait(1)
        view = diagnostics.transport_health(store.path, ("smc_lab",))
        assert view["sources"]["smc_lab"]["state"] == "HEALTHY"
        assert view["sources"]["smc_lab"]["metrics"]["snapshot_sequence"] == 1
    finally:
        assert emitter.close()
    assert emitter.counters()["enqueued"] == emitter.counters()["delivered"] == 0
    assert emitter.diagnostics()["diagnostic_delivery_failed"] == 0


def test_report_send_failure_does_not_kill_worker_or_increment_event_failures():
    attempted = Event()
    def sender(payload):
        if json.loads(payload)["event_type"] == "producer_transport_observed":
            attempted.set()
            raise RuntimeError("untrusted-private-error")
        return 201
    emitter = _emitter(sender, publish_diagnostics=True)
    try:
        assert attempted.wait(1)
        assert emitter.emit(_event())
    finally:
        assert emitter.close()
    data = emitter.diagnostics()
    assert data["diagnostic_delivery_failed"] >= 1
    assert data["counters"]["delivery_failed"] == 0 and data["counters"]["delivered"] == 1
    assert "untrusted-private-error" not in json.dumps(data)


def test_emitting_after_close_does_not_admit_an_unflushable_event():
    emitter = _emitter(lambda _: 201)
    assert emitter.close()
    assert not emitter.emit(_event())
    data = emitter.diagnostics()
    assert data["counters"]["enqueued"] == 0 and data["counters"]["invalid"] == 1


def test_close_timeout_retains_stuck_inflight_truth_until_sender_returns():
    entered, release = Event(), Event()
    def sender(_):
        entered.set()
        assert release.wait(3)
        return 201
    emitter = _emitter(sender)
    try:
        assert emitter.emit(_event()) and entered.wait(1)
        assert emitter.close(timeout_s=0.01) is False
        data = emitter.diagnostics()
        assert data["accepting_events"] is False and data["worker_alive"] is True
        assert data["in_flight_events"] == 1 and data["counters"]["delivered"] == 0
    finally:
        release.set()
        assert emitter.close()
    assert emitter.diagnostics()["counters"]["delivered"] == 1


def test_http_redirects_never_forward_the_producer_credential(monkeypatch):
    import tradexa.guardian.emitter as module
    from urllib.error import HTTPError
    calls = []
    def opener(handler):
        class Client:
            def open(self, request, *, timeout):
                calls.append(request)
                assert request.get_header("X-guardian-key") == SOURCE_KEY
                assert handler.redirect_request(request, None, 302, "Found", {}, "https://evil.invalid/events") is None
                raise HTTPError(request.full_url, 302, "Found", {}, None)
        return Client()
    monkeypatch.setattr(module, "build_opener", opener)
    emitter = GuardianEmitter(source_service="smc_lab", endpoint="http://127.0.0.1:8765/v1/events", key=SOURCE_KEY)
    assert emitter.emit(_event()) and emitter.close()
    assert len(calls) == 1 and emitter.counters()["delivery_failed"] == 1


@pytest.mark.parametrize("kwargs", [
    {"capacity": True}, {"capacity": 0}, {"capacity": 65537},
    {"timeout_s": float("inf")}, {"timeout_s": True}, {"timeout_s": 31},
    {"publish_diagnostics": "true"}, {"diagnostics_interval_s": 0.5},
    {"diagnostics_interval_s": float("nan")}, {"diagnostics_interval_s": 301},
])
def test_transport_configuration_is_bounded_before_thread_start(kwargs):
    with pytest.raises(ValueError):
        _emitter(lambda _: 201, **kwargs)


def test_concurrent_emitters_and_snapshots_preserve_accounting_without_waiting_for_io():
    emitter = _emitter(lambda _: 201, capacity=64)
    def produce(worker):
        for number in range(200):
            emitter.emit(_event(worker*1000+number))
    def observe():
        for _ in range(500):
            data = emitter.diagnostics()
            counters = data["counters"]
            assert counters["enqueued"] == (counters["delivered"] + counters["delivery_failed"] +
                                              data["queued_events"] + data["in_flight_events"])
            assert 0 <= data["queued_events"] <= data["queue_capacity"]
            assert data["in_flight_events"] in (0, 1)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(produce, 0), pool.submit(produce, 1), pool.submit(observe)]
            for future in futures:
                future.result()
    finally:
        assert emitter.close()
    data = emitter.diagnostics()
    assert data["counters"]["enqueued"] + data["counters"]["backpressure_dropped"] == 400
    assert data["counters"]["delivered"] == data["counters"]["enqueued"]
    assert data["queued_events"] == data["in_flight_events"] == 0


def test_blocked_report_sender_does_not_block_event_admission():
    entered, release = Event(), Event()
    def sender(payload):
        if json.loads(payload)["event_type"] == "producer_transport_observed":
            entered.set()
            assert release.wait(3)
        return 201
    emitter = _emitter(sender, publish_diagnostics=True, capacity=1)
    try:
        assert entered.wait(1)
        start = perf_counter()
        assert emitter.emit(_event())
        assert not emitter.emit(_event(2))
        data = emitter.diagnostics()
        assert data["queued_events"] == 1 and data["in_flight_events"] == 0
        assert perf_counter()-start < 0.5
    finally:
        release.set()
        assert emitter.close()
    assert emitter.counters()["delivered"] == 1


def test_missing_corrupt_and_unsafe_database_are_unknown_without_creation(store, tmp_path):
    path = tmp_path/"not-created"/"guardian.db"
    assert diagnostics.transport_health(path, ("smc_lab",), now=NOW)["state"] == "UNKNOWN"
    assert not path.parent.exists()
    corrupt = tmp_path/"corrupt.db"
    corrupt.write_bytes(b"untrusted-secret-text-not-a-database")
    corrupt.chmod(0o600)
    view = diagnostics.transport_health(corrupt, ("smc_lab",), now=NOW)
    assert view["reason"] == "GUARDIAN_DB_READ_FAILED"
    assert "untrusted-secret-text" not in json.dumps(view)
    link = tmp_path/"link.db"
    link.symlink_to(store.path)
    assert diagnostics.transport_health(link, ("smc_lab",), now=NOW)["reason"] == "UNSAFE_STORAGE_PATH"


def test_sql_read_deadline_is_unknown_not_zero_metrics(store, monkeypatch):
    ticks = iter((0.0, 1.0))
    monkeypatch.setattr(diagnostics, "monotonic", lambda: next(ticks, 1.0))
    view = _view(store)
    assert view["state"] == "UNKNOWN" and view["reason"] == "GUARDIAN_DB_READ_BLOCKED"
    assert view["database_snapshot_atomic"] is False
    assert all(row["metrics"] is None for row in view["sources"].values())


def test_read_uses_partial_index_and_does_not_relabel_other_event_types(store):
    _append(store, _event())
    assert _source(_view(store))["state"] == "UNKNOWN"
    with closing(store._connect()) as conn:
        plans = conn.execute(
            "EXPLAIN QUERY PLAN SELECT payload_json FROM events WHERE source_service=? "
            "AND event_type='producer_transport_observed' ORDER BY sequence DESC LIMIT 2", ("smc_lab",)).fetchall()
    assert any("events_producer_transport" in row["detail"] for row in plans)


def test_uncommitted_wal_report_does_not_mix_read_snapshots(store):
    _append(store, report())
    writer = store._connect()
    try:
        writer.execute("BEGIN IMMEDIATE")
        store._append_in_transaction(writer, report(snapshot(snapshot_sequence=2)), NOW.isoformat())
        for _ in range(50):
            view = _view(store)
            assert view["database_snapshot_atomic"] is True
            assert _source(view)["metrics"]["snapshot_sequence"] == 1
        writer.commit()
        row = _source(_view(store))
        assert row["metrics"]["snapshot_sequence"] == 2 and row["counter_history_verified"] is True
    finally:
        writer.close()


def test_api_auth_method_query_ingestion_validation_and_replay_do_not_refresh(store):
    app = GuardianService(store, source_keys={"smc_lab": SOURCE_KEY}, read_key=READ_KEY,
                          required_components=("guardian",))
    path = "/v1/producer-health"
    assert _request(app, "GET", path, key=SOURCE_KEY)[0] == 401
    assert _request(app, "GET", path)[0] == 401
    assert _request(app, "GET", path, key=READ_KEY)[0] == 200
    assert _request(app, "GET", path, key=READ_KEY, query="source=other")[0] == 400
    assert _request(app, "POST", path, key=READ_KEY, payload={})[0] == 405
    payload = json.loads(report(timestamp=datetime.now(timezone.utc)).canonical_json())
    assert _request(app, "POST", "/v1/events", key=READ_KEY, payload=payload)[0] == 401
    assert _request(app, "POST", "/v1/events", key=SOURCE_KEY, payload={**payload, "source_service": "pa_lab"})[0] == 403
    assert _request(app, "POST", "/v1/events", key=SOURCE_KEY, payload=payload)[0] == 201
    before = store.recent()[0]["received_at"]
    for _ in range(100):
        assert _request(app, "POST", "/v1/events", key=SOURCE_KEY, payload=payload)[0] == 200
        status, body, headers = _request(app, "GET", path, key=READ_KEY)
        assert status == 200 and headers["Cache-Control"] == "no-store"
        assert body["sources"]["smc_lab"]["state"] == "HEALTHY"
    assert store.count() == 1 and store.recent()[0]["received_at"] == before
    bad = deepcopy(payload)
    bad["evidence"]["counters"]["enqueued"] = 5
    assert _request(app, "POST", "/v1/events", key=SOURCE_KEY, payload=bad)[0] == 422
    assert store.count() == 1
    assert app.incidents.scan() == 1 and app.incidents.list() == []


def test_storage_lock_and_release_are_unknown_then_recover(store):
    _append(store, report())
    with closing(store._connect()) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    writer = store._connect()
    try:
        writer.execute("BEGIN EXCLUSIVE")
        view = _view(store)
        assert view["state"] == "UNKNOWN" and view["reason"] == "GUARDIAN_DB_READ_BLOCKED"
        assert _source(view)["metrics"] is None
        writer.rollback()
        assert _source(_view(store))["state"] == "HEALTHY"
    finally:
        writer.close()


def test_read_is_bounded_query_only_and_no_writer_or_source_io(store, monkeypatch):
    _append(store, report())
    original = sqlite3.connect
    traces = []
    def connect(*args, **kwargs):
        assert "mode=ro" in args[0] and kwargs["uri"] is True
        conn = original(*args, **kwargs)
        conn.set_trace_callback(traces.append)
        return conn
    def forbidden(*args, **kwargs):
        raise AssertionError("reader used writer")
    monkeypatch.setattr(diagnostics.sqlite3, "connect", connect)
    monkeypatch.setattr(store, "_connect", forbidden)
    for _ in range(100):
        assert _source(_view(store))["state"] == "HEALTHY"
    assert "PRAGMA query_only=ON" in traces and "PRAGMA busy_timeout=250" in traces
    assert traces.count("BEGIN") == traces.count("ROLLBACK") == 100
    assert not any("COUNT(" in sql.upper() or "CHECKPOINT" in sql.upper() for sql in traces)
