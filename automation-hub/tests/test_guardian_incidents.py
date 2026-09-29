"""Guardian Phase 3: one outage is one incident, its root cause carries an
honest confidence, its timeline is reconstructed from evidence, it is never
erased, and deviations from a stream's own normal are reported as anomalies
-- never as failures.

The instance scenarios use the real TradingInstanceManager and engine (the
Phase 1 harness) and the real 3-Candle Rejection engine; the multi-consumer
outage uses the read-only row shape Guardian's collectors produce, because a
real Binance outage cannot be staged here.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

import services.guardian as guardian
from services.guardian.anomalies import NOT_A_FAILURE, AnomalyDetector
from services.guardian.bus import EventBus
from services.guardian.incidents import CLOSED, OPEN, RECOVERED
from services.guardian.service import GuardianService
from services.guardian.store import GuardianStore
from tests.test_guardian import _brain, _lifecycle, _manager, _service
from tests.test_journal_integrity import _StaleThenFresh, _wait_for


def _rows(*statuses, prefix="i"):
    return [{"id": f"{prefix}{n}", "symbol": "BTCUSDT", "timeframe": "5m", "live_feed": True, "alive": True,
             "lifecycle_state": "data_stale" if s == "stale" else "running", "market_data_status": s,
             "strategy_id": "three_candle_rejection"}
            for n, s in enumerate(statuses)]


def _lab(state):
    return {"id": "smc", "label": "SMC Lab", "thread_alive": True, "session_active": True,
            "stream": {"state": state, "reliable": state == "SYNCHRONIZED", "failing_dependency": None,
                       "symbol": "BTCUSDT", "timeframe": "5m"}}


def _svc(store, rows_ref, lab_ref=None, **kw):
    bus = EventBus(store)
    labs = {"smc": lambda: lab_ref["row"]} if lab_ref is not None else {}
    return GuardianService(store, bus, instances={"main": lambda: rows_ref["rows"]}, labs=labs,
                           interval_s=1, **kw), bus


def _incidents(svc, state=None):
    return svc.incidents.list(state=state)


# ------------------------------------------- 10, 19: one outage, one incident
def test_a_binance_outage_is_one_incident_however_many_components_it_stops():
    store = GuardianStore()
    rows = {"rows": _rows("healthy", "healthy", "healthy")}
    lab = {"row": _lab("SYNCHRONIZED")}
    svc, bus = _svc(store, rows, lab, incident_verify_s=0)
    svc.cycle()
    assert _incidents(svc) == []

    rows["rows"] = _rows("stale", "stale", "stale")
    lab["row"] = _lab("DISCONNECTED")
    guardian.install(bus)
    try:                                     # what three engines report between cycles
        for n in range(3):
            guardian.observe_instance_event({"event": "MARKET_STALE", "instance_id": f"i{n}",
                                             "symbol": "BTCUSDT", "timeframe": "5m", "status": "stale",
                                             "detail": "candles are late"})
    finally:
        guardian.uninstall()
    for _ in range(3):
        svc.cycle()
        bus.flush()
    [incident] = _incidents(svc)
    assert incident["key"] == "binance_usdm" and incident["state"] == OPEN
    assert incident["diagnosis"]["confidence"] == "HIGH CONFIDENCE"
    assert "not CONFIRMED" in incident["diagnosis"]["why_this_confidence"]
    affected = set(incident["affected"])
    assert {f"instance:i{n}" for n in range(3)} <= affected and "lab:smc" in affected
    assert {f"feed:instance:i{n}" for n in range(3)} | {"feed:lab:smc"} <= affected
    assert set(incident["signals"]) == {f"instance:i{n}" for n in range(3)}   # attached, not separate

    rows["rows"] = _rows("healthy", "healthy", "healthy")
    lab["row"] = _lab("SYNCHRONIZED")
    guardian.install(bus)
    try:
        for n in range(3):
            guardian.observe_instance_event({"event": "MARKET_CONNECTED", "instance_id": f"i{n}",
                                             "symbol": "BTCUSDT", "timeframe": "5m", "status": "connected"})
    finally:
        guardian.uninstall()
    bus.flush()
    svc.cycle()
    assert _incidents(svc)[0]["state"] == RECOVERED
    svc.cycle()
    [closed] = _incidents(svc)
    assert closed["state"] == CLOSED and closed["verified_at"] and closed["closed_at"]
    full = svc.incidents.get(closed["id"])
    assert [e["entry"] for e in full["log"]][:1] == ["opened"]
    assert [e["entry"] for e in full["log"]][-3:] == ["recovered", "verified", "closed"]
    bus.flush()
    kinds = [e["event_type"] for e in store.events(category="guardian", limit=500)]
    assert kinds.count("incident_opened") == 1                 # grouped: one alert, not eight


def test_incidents_are_never_erased_and_their_log_cannot_be_rewritten():
    store = GuardianStore()
    rows = {"rows": _rows("stale", "stale")}
    svc, _ = _svc(store, rows)
    svc.cycle()
    [incident] = _incidents(svc)
    for sql in ("DELETE FROM guardian_incident_log", "UPDATE guardian_incident_log SET detail='x'",
                "DELETE FROM guardian_incidents"):
        with pytest.raises(sqlite3.DatabaseError):
            store._c.execute(sql)
    store._c.rollback()
    assert svc.incidents.get(incident["id"])["log"]


def test_failing_again_before_recovery_is_verified_reopens_the_same_incident():
    store = GuardianStore()
    rows = {"rows": _rows("stale", "healthy")}
    svc, _ = _svc(store, rows, incident_verify_s=3600)
    svc.cycle()
    rows["rows"] = _rows("healthy", "healthy")
    svc.cycle()
    assert _incidents(svc)[0]["state"] == RECOVERED
    rows["rows"] = _rows("stale", "healthy")
    svc.cycle()
    [incident] = _incidents(svc)                          # still one incident
    assert incident["state"] == OPEN and incident["updates"] >= 2
    assert [e["entry"] for e in svc.incidents.get(incident["id"])["log"]] == ["opened", "recovered", "reopened"]


# ------------------------------------------------ §11: honest confidence
def test_one_consumer_down_while_others_receive_data_is_local_to_that_consumer():
    store = GuardianStore()
    svc, _ = _svc(store, {"rows": _rows("stale", "healthy", "healthy")})
    svc.cycle()
    [incident] = _incidents(svc)
    assert incident["key"] == "feed:instance:i0"
    assert incident["diagnosis"]["confidence"] == "HIGH CONFIDENCE"
    assert "2 other live consumer(s) receive data" in incident["diagnosis"]["why_this_confidence"]
    assert set(incident["affected"]) == {"feed:instance:i0", "instance:i0"}


def test_the_only_consumer_down_cannot_be_told_apart_from_binance():
    store = GuardianStore()
    svc, _ = _svc(store, {"rows": _rows("stale")})
    svc.cycle()
    [incident] = _incidents(svc)
    assert incident["diagnosis"]["confidence"] == "POSSIBLE"
    assert "cannot tell them apart" in incident["diagnosis"]["root_cause"]


def test_starting_up_is_not_an_incident_but_staying_degraded_is():
    store = GuardianStore()
    rows = {"rows": _rows("warming_up")}
    clock = {"t": 1_000_000.0}
    svc, _ = _svc(store, rows, degraded_grace_s=300)
    svc.clock = lambda: clock["t"]
    svc.cycle()
    assert _incidents(svc) == []
    clock["t"] += 301
    svc.cycle()
    by_key = {i["key"]: i for i in _incidents(svc)}
    assert by_key["feed:instance:i0"]["severity"] == "WATCH"
    # (The test's bus is not running, so Guardian itself has stayed degraded
    # too -- and says so with an incident of its own.)
    assert by_key["guardian"]["diagnosis"]["root_cause"] == "the event bus is not draining"


# ------------------------------------------- real engine: stale, recovered
def test_a_real_instance_going_stale_opens_an_incident_with_its_own_timeline(tmp_path):
    store = GuardianStore()
    bus = EventBus(store, flush_interval_s=0.05)
    bus.start()
    guardian.install(bus)
    manager, inst = _manager(tmp_path, _brain, fresh=False)
    svc = _service(store, bus, manager, incident_verify_s=0)
    try:
        manager.start(inst.id)
        assert _wait_for(lambda: _lifecycle(manager, inst).count("MARKET_DISCONNECTED") >= 1)
        bus.flush()
        svc.cycle()
        [incident] = svc.incidents.list()
        assert incident["key"] == f"feed:instance:{inst.id}" and incident["state"] == OPEN
        assert f"instance:{inst.id}" in incident["affected"]
        _StaleThenFresh.fresh = True
        assert _wait_for(lambda: "MARKET_CONNECTED" in _lifecycle(manager, inst))
        bus.flush()
        assert _wait_for(lambda: (svc.cycle(), bus.flush(), svc.incidents.list()[0]["state"])[2] == CLOSED)
    finally:
        manager.shutdown()
        _StaleThenFresh.fresh = False
        guardian.uninstall()
        bus.stop()
    full = svc.incidents.get(incident["id"])
    whats = [item["what"] for item in full["timeline"]]
    assert "stale_candle" in whats and "health_changed" in whats and "incident opened" in whats
    assert whats.index("incident opened") < whats.index("incident closed")
    assert [item["at"] for item in full["timeline"]] == sorted(item["at"] for item in full["timeline"])
    # The event and the health state never opened two incidents for one fault.
    assert len(svc.incidents.list()) == 1


def test_a_missing_higher_timeframe_is_its_own_confirmed_incident(tmp_path):
    from services.auto_engine import EngineFeedError
    from tests.test_guardian_strategy import _bars, _instance
    from tests.test_three_candle_rejection import _history, _long_pattern
    store = GuardianStore()
    bus = EventBus(store)
    guardian.install(bus)
    try:
        _, engine, _ = _instance(tmp_path)
        engine._forward_fetch_for_timeframe = lambda *a: (_ for _ in ()).throw(EngineFeedError("timeout"))
        rows, i = _history(trend="down")
        bars = _bars(rows + _long_pattern(i, open_=101.6))
        strategy = engine.strategy_factory("BTCUSDT")
        with pytest.raises(EngineFeedError):
            engine._refresh_multi_timeframe_context("BTCUSDT", strategy, entry_bars=bars)
        bus.flush()
        svc = GuardianService(store, bus, incident_verify_s=0)
        svc.cycle()
        [incident] = svc.incidents.list()
        assert incident["key"] == "htf:instance:inst-s"
        assert incident["diagnosis"]["confidence"] == "CONFIRMED"
        strategy.bars.extend(bars[:-1])
        engine._process_bar("BTCUSDT", bars[-1], strategy)     # the next candle evaluates normally
        bus.flush()
        svc.cycle()
        svc.cycle()
        assert svc.incidents.list()[0]["state"] == CLOSED
    finally:
        guardian.uninstall()


def test_overlapping_incidents_on_the_same_instance_are_related():
    store = GuardianStore()
    bus = EventBus(store)
    svc = GuardianService(store, bus, instances={"m": lambda: _rows("stale", "healthy")})
    guardian.install(bus)
    try:
        guardian.emit("missing_htf_candle", source_service="trading_instances",
                      source_component="instance:i0", severity="WARNING", reason="1h not loaded")
    finally:
        guardian.uninstall()
    bus.flush()
    svc.cycle()
    by_key = {i["key"]: i for i in svc.incidents.list()}
    htf, feed = by_key["htf:instance:i0"], by_key["feed:instance:i0"]
    assert htf["related"] == [{"incident_id": feed["id"], "title": feed["title"], "confidence": "PROBABLE",
                               "why": "both involve i0"}]


# ------------------------------------------------------ §21: anomalies
def _roll(store, *, day, scope, decision, count, code="", last_at=None, timeframe="5m"):
    """A rollup row: the per-day counts Guardian derives from traces."""
    store._c.execute(
        "INSERT INTO guardian_strategy_rollup(day,source_component,strategy_id,symbol,timeframe,decision,"
        "blocker_code,lab_id,instance_id,strategy_version,count,last_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (day, scope, "three_candle_rejection", "BTCUSDT", timeframe, decision, code, None,
         scope.split(":", 1)[1], "v1", count, last_at or f"{day}T12:00:00+00:00"))
    store._c.commit()


def _healthy_nodes(scope="instance:i0"):
    store = GuardianStore()
    svc = GuardianService(store, EventBus(store), instances={"m": lambda: _rows("healthy")})
    return svc.observe()


def test_a_healthy_instance_that_stopped_evaluating_is_an_anomaly_not_a_failure(tmp_path):
    from tests.test_guardian_strategy import _instance, _run
    from tests.test_three_candle_rejection import _history, _long_pattern
    store = GuardianStore()
    bus = EventBus(store)
    guardian.install(bus)
    try:
        _, engine, _ = _instance(tmp_path)
        engine.instance_id = "i0"
        rows, i = _history()
        _run(engine, rows + _long_pattern(i))               # real traces
        bus.flush()
    finally:
        guardian.uninstall()
    detector = AnomalyDetector(store)
    nodes = _healthy_nodes()
    published: list = []
    record = lambda kind, **f: published.append((kind, f))  # noqa: E731
    assert detector.cycle(nodes, now=time.time(), publish=record) == []
    later = time.time() + 3 * 300 + 120
    [anomaly] = detector.cycle(nodes, now=later, publish=record)
    assert anomaly["detector"] == "evaluations_stopped" and anomaly["note"] == NOT_A_FAILURE
    assert published[-1][0] == "anomaly_detected" and published[-1][1]["severity"] == "WATCH"
    detector.cycle(nodes, now=later + 1, publish=record)
    assert [k for k, _ in published].count("anomaly_detected") == 1       # once, not every cycle
    # With the feed down it is not the strategy's silence to explain.
    stale = GuardianService(GuardianStore(), EventBus(GuardianStore()),
                            instances={"m": lambda: _rows("stale")}).observe()
    assert detector.cycle(stale, now=later + 2, publish=record) == []
    assert published[-1][0] == "anomaly_cleared"


def test_no_setups_today_is_flagged_only_when_the_streams_own_history_makes_it_improbable():
    store = GuardianStore()
    now = datetime(2026, 9, 20, 18, tzinfo=timezone.utc).timestamp()
    day = lambda k: (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=k)).date().isoformat()  # noqa: E731
    for k in range(1, 8):
        _roll(store, day=day(k), scope="instance:i0", decision="NO_SETUP", count=280, code="NO_ELIGIBLE_ZONE")
        _roll(store, day=day(k), scope="instance:i0", decision="ENTERED", count=8)
    _roll(store, day=day(0), scope="instance:i0", decision="NO_SETUP", count=200, code="NO_ELIGIBLE_ZONE",
          last_at=datetime.fromtimestamp(now - 60, timezone.utc).isoformat())
    detector = AnomalyDetector(store)
    found = detector.setup_drought(_healthy_nodes(), now)
    assert [a["detector"] for a in found] == ["setup_drought"]
    assert "chance of none" in found[0]["detail"]
    # Three days of history is not a baseline: nothing is claimed.
    thin = GuardianStore()
    for k in range(1, 4):
        _roll(thin, day=day(k), scope="instance:i0", decision="ENTERED", count=8)
    _roll(thin, day=day(0), scope="instance:i0", decision="NO_SETUP", count=200)
    assert AnomalyDetector(thin).setup_drought(_healthy_nodes(), now) == []


def test_a_shift_in_rejection_reasons_needs_enough_samples_and_a_real_difference():
    store = GuardianStore()
    now = datetime(2026, 9, 20, 18, tzinfo=timezone.utc).timestamp()
    day = lambda k: (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=k)).date().isoformat()  # noqa: E731
    for k in range(2, 8):
        _roll(store, day=day(k), scope="instance:i0", decision="NO_SETUP", count=90, code="NO_ELIGIBLE_ZONE")
        _roll(store, day=day(k), scope="instance:i0", decision="NO_SETUP", count=10, code="EMA_TREND_NOT_ALIGNED")
    _roll(store, day=day(0), scope="instance:i0", decision="NO_SETUP", count=40, code="NO_ELIGIBLE_ZONE")
    _roll(store, day=day(0), scope="instance:i0", decision="NO_SETUP", count=60, code="EMA_TREND_NOT_ALIGNED")
    codes = {a["key"].rsplit(":", 1)[1] for a in AnomalyDetector(store).rejection_mix({}, now)}
    assert codes == {"NO_ELIGIBLE_ZONE", "EMA_TREND_NOT_ALIGNED"}
    few = GuardianStore()
    for k in range(2, 8):
        _roll(few, day=day(k), scope="instance:i0", decision="NO_SETUP", count=90, code="NO_ELIGIBLE_ZONE")
    _roll(few, day=day(0), scope="instance:i0", decision="NO_SETUP", count=10, code="EMA_TREND_NOT_ALIGNED")
    assert AnomalyDetector(few).rejection_mix({}, now) == []     # 10 samples: no claim


# ------------------------------------------------------------------ API
def test_the_incident_api_is_read_only():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    import routers.guardian
    assert all(r.methods == {"GET"} for r in routers.guardian.router.routes)
    app = FastAPI()
    app.include_router(webhook_api.router)
    client = TestClient(app)
    listing = client.get("/guardian/incidents").json()
    assert "incidents" in listing and "counts" in listing
    assert client.get("/guardian/incidents/999999").status_code == 404
    assert "active" in client.get("/guardian/anomalies").json()
    assert client.get("/guardian/integrity").status_code == 200
    assert client.post("/guardian/incidents").status_code == 405
