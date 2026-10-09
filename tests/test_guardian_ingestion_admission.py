"""Bounded Guardian admission is not an acknowledgement of persistence/trading."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event

import pytest

from tradexa.guardian.ingestion import IngestionAdmission, IngestionLimits, IngestionOverload
from tradexa.guardian.service import GuardianService
from tradexa.guardian.store import GuardianStore
from tests.test_guardian_service import READ_KEY, SOURCE_KEY, _event, _request

PA_KEY = "pa-independent-source-key-123456789"


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def limits(**changes):
    base = IngestionLimits(event_rate=2, event_burst=4, event_source_rate=1,
                           event_source_burst=2, event_inflight=2,
                           heartbeat_rate=2, heartbeat_burst=4,
                           heartbeat_source_rate=1, heartbeat_source_burst=2,
                           heartbeat_inflight=1)
    return replace(base, **changes)


@pytest.fixture
def clock():
    return Clock()


def admission(clock, policy=None):
    return IngestionAdmission(("smc_lab", "pa_lab"), limits=policy or limits(), clock=clock)


def make_app(tmp_path, clock, policy=None):
    store = GuardianStore(tmp_path / "guardian.db")
    app = GuardianService(store, source_keys={"smc_lab": SOURCE_KEY, "pa_lab": PA_KEY},
                          read_key=READ_KEY, required_components=("guardian", "smc_lab", "pa_lab"),
                          ingestion_limits=policy or limits(), ingestion_clock=clock)
    return app


def payload(number=0, source="smc_lab"):
    return json.loads(replace(_event(), event_id=f"admission_event_{number:08d}",
                              source_service=source).canonical_json())


def test_limiter_is_bounded_to_configured_sources_and_has_no_persistence_claim(clock):
    limiter = admission(clock)
    view = limiter.snapshot()
    assert view["scope"] == "CURRENT_GUARDIAN_PROCESS_ADMISSION_ONLY"
    assert view["persistence_verified"] is view["producer_coverage_verified"] is False
    assert view["trading_integrity_verified"] is view["automatic_action_allowed"] is False
    assert set(view["routes"]) == {"events", "heartbeats"}
    for row in view["routes"].values():
        assert set(row["sources"]) == {"smc_lab", "pa_lab"}
        assert row["admitted_requests"] == row["in_flight_requests"] == 0
    with pytest.raises(ValueError):
        with limiter.admit("events", "unknown"):
            pass
    with pytest.raises(ValueError):
        with limiter.admit("orders", "smc_lab"):
            pass
    assert limiter.snapshot() == view


@pytest.mark.parametrize("sources", [(), ("bad-name",), ("smc_lab", "smc_lab"),
                                     (123,), tuple(f"s{i}" for i in range(129))])
def test_invalid_source_inventory_is_rejected(sources):
    with pytest.raises(ValueError):
        IngestionAdmission(sources)


@pytest.mark.parametrize("field", tuple(IngestionLimits.__dataclass_fields__))
@pytest.mark.parametrize("value", [0, True, 10001, 1.5, "2"])
def test_limits_are_strict_bounded_positive_integers(field, value):
    with pytest.raises(ValueError):
        replace(IngestionLimits(), **{field: value})


@pytest.mark.parametrize("changes", [
    {"event_rate": 1, "event_source_rate": 2},
    {"event_burst": 1, "event_source_burst": 2},
    {"heartbeat_rate": 1, "heartbeat_source_rate": 2},
    {"heartbeat_burst": 1, "heartbeat_source_burst": 2},
])
def test_inconsistent_global_and_source_limits_fail_closed(changes):
    with pytest.raises(ValueError):
        replace(IngestionLimits(), **changes)


def test_environment_config_is_allowlisted_strict_and_does_not_disable_limits():
    original = IngestionLimits()
    assert IngestionLimits.from_environment({}) == original
    result = IngestionLimits.from_environment({"GUARDIAN_INGESTION_EVENT_RATE": "75",
                                               "UNRELATED": "ignored"})
    assert result.event_rate == 75
    for raw in ("", "0", "-1", "false", "nan", "2.5", "1e2", "999999999999999999999"):
        with pytest.raises(ValueError):
            IngestionLimits.from_environment({"GUARDIAN_INGESTION_EVENT_RATE": raw})
    with pytest.raises(ValueError):
        IngestionLimits.from_environment({"GUARDIAN_INGESTION_EVENT_RTAE": "100"})


def test_source_limit_does_not_spend_another_sources_budget(clock):
    limiter = admission(clock)
    for _ in range(2):
        with limiter.admit("events", "smc_lab"):
            pass
    with pytest.raises(IngestionOverload) as caught:
        with limiter.admit("events", "smc_lab"):
            pytest.fail("rate denied request ran")
    assert caught.value.reasons == ("SOURCE_RATE_LIMIT",)
    assert caught.value.retry_after_seconds == 1
    with limiter.admit("events", "pa_lab"):
        pass
    view = limiter.snapshot()["routes"]["events"]
    assert view["admitted_requests"] == 3
    assert view["rate_limited_requests"] == 1
    assert view["sources"]["pa_lab"]["rate_limited_requests"] == 0


def test_global_limit_cannot_be_bypassed_by_switching_sources(clock):
    limiter = admission(clock, limits(event_source_burst=4))
    for _ in range(4):
        with limiter.admit("events", "smc_lab"):
            pass
    with pytest.raises(IngestionOverload) as caught:
        with limiter.admit("events", "pa_lab"):
            pass
    assert caught.value.reasons == ("GLOBAL_RATE_LIMIT",)
    assert limiter.snapshot()["routes"]["events"]["admitted_requests"] == 4


def test_monotonic_refill_is_capped_and_backwards_clock_grants_nothing(clock):
    limiter = admission(clock)
    for _ in range(2):
        with limiter.admit("events", "smc_lab"):
            pass
    clock.value = -50
    with pytest.raises(IngestionOverload):
        with limiter.admit("events", "smc_lab"):
            pass
    clock.value = 0.5
    with pytest.raises(IngestionOverload):
        with limiter.admit("events", "smc_lab"):
            pass
    clock.value = 1
    with limiter.admit("events", "smc_lab"):
        pass
    clock.value = 1000
    assert limiter.snapshot()["routes"]["events"]["sources"]["smc_lab"]["available_tokens"] == 2


def test_capacity_denial_is_nonblocking_and_does_not_consume_tokens(clock):
    limiter = admission(clock, limits(event_inflight=1))
    with limiter.admit("events", "smc_lab"):
        before = limiter.snapshot()["routes"]["events"]["sources"]["pa_lab"]["available_tokens"]
        with pytest.raises(IngestionOverload) as caught:
            with limiter.admit("events", "pa_lab"):
                pass
        assert caught.value.reasons == ("WRITE_CAPACITY_FULL",)
        assert limiter.snapshot()["routes"]["events"]["sources"]["pa_lab"]["available_tokens"] == before
        with limiter.admit("heartbeats", "pa_lab"):
            assert limiter.snapshot()["routes"]["heartbeats"]["in_flight_requests"] == 1
    with limiter.admit("events", "pa_lab"):
        pass
    assert limiter.snapshot()["routes"]["events"]["in_flight_requests"] == 0


def test_failed_body_and_persistence_attempts_release_capacity_without_refunding_rate(clock):
    limiter = admission(clock, limits(event_inflight=1))
    for _ in range(2):
        with pytest.raises(sqlite3.OperationalError):
            with limiter.admit("events", "smc_lab"):
                raise sqlite3.OperationalError("fixture private failure")
    assert limiter.snapshot()["routes"]["events"]["in_flight_requests"] == 0
    with pytest.raises(IngestionOverload):
        with limiter.admit("events", "smc_lab"):
            pass


def test_event_flood_does_not_consume_heartbeat_tokens_or_capacity(clock):
    limiter = admission(clock)
    for _ in range(2):
        with limiter.admit("events", "smc_lab"):
            pass
    with pytest.raises(IngestionOverload):
        with limiter.admit("events", "smc_lab"):
            pass
    with limiter.admit("heartbeats", "smc_lab"):
        pass
    assert limiter.snapshot()["routes"]["heartbeats"]["admitted_requests"] == 1


def test_concurrent_admission_cannot_exceed_global_write_capacity(clock):
    limiter = admission(clock, limits(event_inflight=1))
    started, release = Event(), Event()
    def holding():
        with limiter.admit("events", "smc_lab"):
            started.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=12) as pool:
        future = pool.submit(holding)
        assert started.wait(5)
        def attempt(_):
            with pytest.raises(IngestionOverload):
                with limiter.admit("events", "pa_lab"):
                    pytest.fail("over-capacity request ran")
        list(pool.map(attempt, range(50)))
        assert limiter.snapshot()["routes"]["events"]["in_flight_requests"] == 1
        release.set()
        future.result()
    row = limiter.snapshot()["routes"]["events"]
    assert row["capacity_limited_requests"] == 50 and row["admitted_requests"] == 1
    assert row["in_flight_requests"] == 0


def test_concurrent_source_rate_admission_stays_exact(clock):
    limiter = admission(clock, limits(event_inflight=16))
    barrier = Barrier(20)
    def attempt(_):
        barrier.wait(timeout=5)
        try:
            with limiter.admit("events", "smc_lab"):
                return True
        except IngestionOverload:
            return False
    with ThreadPoolExecutor(max_workers=20) as pool:
        assert sum(pool.map(attempt, range(20))) == 2
    row = limiter.snapshot()["routes"]["events"]
    assert row["admitted_requests"] == 2 and row["rate_limited_requests"] == 18
    assert row["in_flight_requests"] == 0


def test_process_restart_resets_volatile_counters_not_persistent_evidence(clock):
    original = admission(clock)
    with original.admit("events", "smc_lab"):
        pass
    restarted = admission(clock)
    assert original.snapshot()["process_epoch"] != restarted.snapshot()["process_epoch"]
    assert restarted.snapshot()["routes"]["events"]["admitted_requests"] == 0
    assert restarted.snapshot()["history_persistent"] is False


def test_http_429_is_truthful_and_retry_preserves_immutable_idempotency(tmp_path, clock):
    app = make_app(tmp_path, clock)
    first = payload()
    assert _request(app, "POST", "/v1/events", payload=first, key=SOURCE_KEY)[0] == 201
    receipt = app.store.recent(1)[0]["received_at"]
    assert _request(app, "POST", "/v1/events", payload=first, key=SOURCE_KEY)[0] == 200
    status, body, headers = _request(app, "POST", "/v1/events", payload=first, key=SOURCE_KEY)
    assert status == 429 and body["error"] == "INGESTION_LIMITED"
    assert body["request_persisted"] is False and body["retry_after_seconds"] == 1
    assert headers["Retry-After"] == "1" and headers["Cache-Control"] == "no-store"
    assert app.store.count() == 1 and app.store.recent(1)[0]["received_at"] == receipt
    clock.value = 1
    assert _request(app, "POST", "/v1/events", payload=first, key=SOURCE_KEY)[0] == 200
    assert app.store.count() == 1 and app.store.recent(1)[0]["received_at"] == receipt


def test_unauthorized_requests_cannot_spend_a_source_budget_or_read_a_body(tmp_path, clock):
    app = make_app(tmp_path, clock)
    class Unreadable:
        def read(self, length):
            pytest.fail("unauthorized request body was read")
    for key in ("", "bad", READ_KEY):
        result = {}
        body = b"".join(app({"REQUEST_METHOD": "POST", "PATH_INFO": "/v1/events",
                             "HTTP_X_GUARDIAN_KEY": key, "wsgi.input": Unreadable()},
                            lambda status, headers: result.update(status=status)))
        assert result["status"].startswith("401") and json.loads(body)["error"] == "UNAUTHORIZED"
    assert app.ingestion.snapshot()["routes"]["events"]["admitted_requests"] == 0


def test_overloaded_authenticated_request_is_rejected_before_body_or_db(tmp_path, clock, monkeypatch):
    app = make_app(tmp_path, clock)
    for i in range(2):
        assert _request(app, "POST", "/v1/events", payload=payload(i), key=SOURCE_KEY)[0] == 201
    def forbidden(*args, **kwargs):
        pytest.fail("denied admission reached body/store")
    monkeypatch.setattr(app, "_body", forbidden)
    monkeypatch.setattr(app.store, "append", forbidden)
    assert _request(app, "POST", "/v1/events", payload=payload(3), key=SOURCE_KEY)[0] == 429
    assert _request(app, "GET", "/v1/ingestion-health", key=READ_KEY)[0] == 200
    assert _request(app, "GET", "/healthz")[0] == 200


def test_persistence_and_validation_failure_dont_leak_or_acknowledge(tmp_path, clock, monkeypatch):
    app = make_app(tmp_path, clock)
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("private database and secret=do-not-show")
    monkeypatch.setattr(app.store, "append", fail)
    status, body, _ = _request(app, "POST", "/v1/events", payload=payload(), key=SOURCE_KEY)
    assert status == 503 and body == {"error": "PERSISTENCE_UNAVAILABLE"}
    assert app.ingestion.snapshot()["routes"]["events"]["in_flight_requests"] == 0
    assert _request(app, "POST", "/v1/events", payload={}, key=SOURCE_KEY)[0] == 422
    assert app.ingestion.snapshot()["routes"]["events"]["in_flight_requests"] == 0
    assert app.store.count() == 0


def test_admission_health_is_read_only_scoped_secret_free_and_not_trading_health(tmp_path, clock, monkeypatch):
    app = make_app(tmp_path, clock)
    route = "/v1/ingestion-health"
    for key in ("", SOURCE_KEY, PA_KEY, "wrong", "owner-not-a-read-key"):
        assert _request(app, "GET", route, key=key)[0] == 401
    assert _request(app, "POST", route, key=READ_KEY)[0] == 405
    assert _request(app, "GET", route, key=READ_KEY, query="reset=1")[0] == 400
    def forbidden(*args, **kwargs):
        pytest.fail("admission health touched SQLite")
    monkeypatch.setattr(app.store, "_connect", forbidden)
    for _ in range(100):
        status, body, headers = _request(app, "GET", route, key=READ_KEY)
        assert status == 200 and headers["Cache-Control"] == "no-store"
        assert body["trading_integrity_verified"] is False
        encoded = json.dumps(body)
        assert SOURCE_KEY not in encoded and PA_KEY not in encoded and READ_KEY not in encoded
        assert "guardian.db" not in encoded
    assert app.ingestion.snapshot()["routes"]["events"]["admitted_requests"] == 0


def test_event_rate_limit_does_not_block_heartbeat_route(tmp_path, clock):
    app = make_app(tmp_path, clock)
    for i in range(2):
        assert _request(app, "POST", "/v1/events", payload=payload(i), key=SOURCE_KEY)[0] == 201
    assert _request(app, "POST", "/v1/events", payload=payload(3), key=SOURCE_KEY)[0] == 429
    heartbeat = {"component": "smc_lab", "state": "HEALTHY",
                 "observed_at": _event().timestamp.isoformat()}
    assert _request(app, "POST", "/v1/heartbeats", payload=heartbeat, key=SOURCE_KEY)[0] == 200
    assert app.store.heartbeats()["smc_lab"]["state"] == "HEALTHY"


def test_startup_invalid_admission_config_fails_before_creating_db_or_starting_threads(tmp_path, monkeypatch):
    import tradexa.guardian.service as service
    monkeypatch.setenv("GUARDIAN_DB_PATH", str(tmp_path / "never-created.db"))
    monkeypatch.setenv("GUARDIAN_READ_KEY", READ_KEY)
    monkeypatch.setenv("GUARDIAN_SOURCE_KEYS_JSON", json.dumps({"smc_lab": SOURCE_KEY}))
    monkeypatch.setenv("GUARDIAN_INGESTION_EVENT_RATE", "0")
    def forbidden(*args, **kwargs):
        pytest.fail("invalid admission config started a thread/store/server")
    monkeypatch.setattr(service, "Thread", forbidden)
    monkeypatch.setattr(service, "GuardianStore", forbidden)
    monkeypatch.setattr(service, "make_server", forbidden)
    with pytest.raises(ValueError):
        service.main()
    assert not (tmp_path / "never-created.db").exists()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, 10**1000, "0"])
def test_invalid_clock_is_rejected_before_admission(value):
    with pytest.raises(ValueError):
        IngestionAdmission(("smc_lab",), clock=lambda: value)


def test_lost_http_ack_after_commit_retries_without_duplicate_event(tmp_path, clock):
    app = make_app(tmp_path, clock)
    encoded = json.dumps(payload()).encode()
    import io
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/v1/events",
           "HTTP_X_GUARDIAN_KEY": SOURCE_KEY, "CONTENT_TYPE": "application/json",
           "CONTENT_LENGTH": str(len(encoded)), "wsgi.input": io.BytesIO(encoded)}
    def lose_ack(*args):
        raise ConnectionError("fixture client disappeared after committed append")
    with pytest.raises(ConnectionError):
        app(env, lose_ack)
    assert app.store.count() == 1
    receipt = app.store.recent(1)[0]["received_at"]
    assert app.ingestion.snapshot()["routes"]["events"]["in_flight_requests"] == 0
    assert _request(app, "POST", "/v1/events", payload=payload(), key=SOURCE_KEY)[0] == 200
    assert app.store.count() == 1 and app.store.recent(1)[0]["received_at"] == receipt


def test_capacity_rejection_keeps_heartbeat_and_read_route_available(tmp_path, clock, monkeypatch):
    app = make_app(tmp_path, clock, limits(event_inflight=1))
    append = app.store.append
    entered, release = Event(), Event()
    def held(event):
        entered.set()
        assert release.wait(5)
        return append(event)
    monkeypatch.setattr(app.store, "append", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_request, app, "POST", "/v1/events", payload=payload(), key=SOURCE_KEY)
        assert entered.wait(5)
        try:
            status, body, headers = _request(app, "POST", "/v1/events", payload=payload(1), key=PA_KEY)
            assert status == 429 and body["reasons"] == ["WRITE_CAPACITY_FULL"]
            assert body["request_persisted"] is False and headers["Retry-After"] == "1"
            status, view, _ = _request(app, "GET", "/v1/ingestion-health", key=READ_KEY)
            assert status == 200 and view["state"] == "DEGRADED"
            assert view["routes"]["events"]["in_flight_requests"] == 1
            heartbeat = {"component": "smc_lab", "state": "UNKNOWN",
                         "observed_at": _event().timestamp.isoformat()}
            assert _request(app, "POST", "/v1/heartbeats", payload=heartbeat, key=SOURCE_KEY)[0] == 200
        finally:
            release.set()
        assert first.result()[0] == 201
    assert app.store.count() == 1
    assert app.ingestion.snapshot()["routes"]["events"]["in_flight_requests"] == 0


def test_restarting_service_resets_only_process_counters_not_evidence(tmp_path, clock):
    first = make_app(tmp_path, clock)
    assert _request(first, "POST", "/v1/events", payload=payload(), key=SOURCE_KEY)[0] == 201
    before = first.ingestion.snapshot()
    restarted = make_app(tmp_path, clock)
    after = restarted.ingestion.snapshot()
    assert after["process_epoch"] != before["process_epoch"] and after["history_persistent"] is False
    assert after["routes"]["events"]["admitted_requests"] == 0
    assert restarted.store.count() == 1
    assert _request(restarted, "POST", "/v1/events", payload=payload(), key=SOURCE_KEY)[0] == 200
    assert restarted.store.count() == 1
