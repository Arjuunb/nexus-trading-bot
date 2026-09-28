"""Guardian Phase 1 acceptance (PRD "Nexus Guardian" §44, the Phase 1 subset).

The platform side is real wherever it can be: a real TradingInstanceManager
with the shared market hub and the engine's own freshness checks and recovery
loop (only the Binance transport is a double), a real worker crash inside the
worker thread, the real 3-Candle Rejection strategy trading through the real
pipeline while Guardian is broken, a real hub subscription for the labs, and a
real journal recorder facing a ledger it cannot read.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import services.guardian as guardian
from data.ledger import SqliteLedger
from services.guardian import health as h
from services.guardian import sources
from services.guardian.bus import EventBus
from services.guardian.schema import make_event
from services.guardian.service import GuardianService
from services.guardian.store import GuardianStore
from tests.test_journal_integrity import _StaleThenFresh, _wait_for

GUARDIAN_DIR = Path(__file__).resolve().parents[1] / "services" / "guardian"


# --------------------------------------------------------------- helpers
@pytest.fixture
def g():
    """A Guardian store and bus, installed so the platform's events reach it."""
    store = GuardianStore()
    bus = EventBus(store, flush_interval_s=0.05)
    bus.start()
    guardian.install(bus)
    yield store, bus
    guardian.uninstall()
    bus.stop()


def _manager(tmp_path, strategy_factory, *, fresh: bool):
    from services.forward_paper_hub import ForwardPaperMarketDataHub
    from services.trading_instances import TradingInstanceManager
    _StaleThenFresh.fresh = fresh
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_StaleThenFresh)
    hub.synchronous_delivery = True
    manager = TradingInstanceManager(ledger, strategy_factory=strategy_factory, live=True, live_poll_s=1.0)
    manager.market_hub = hub
    manager.symbol_rules_provider = lambda _s: {"symbol": "X", "tick_size": 0.01, "step_size": 0.001,
                                                "min_qty": 0.001, "min_notional": 5.0}
    manager.configure(paper_account_capital=100_000)
    inst = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                          strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                          capital_allocation=1_000)
    return manager, inst


def _lifecycle(manager, inst) -> list[str]:
    return [json.loads(r["message"][len("instance_event "):])["event"]
            for r in manager.store.engine_logs(inst.id, limit=500)
            if r["message"].startswith("instance_event ")]


def _service(store, bus, manager, **kw) -> GuardianService:
    return GuardianService(store, bus, instances={"main": lambda: sources.instance_rows(manager)},
                           interval_s=kw.pop("interval_s", 1.0), **kw)


def _brain(_k, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


# ------------------------------------------------ 1, 3, 4: feed failures
def test_stale_candles_block_the_instance_and_are_recorded_as_evidence(tmp_path, g):
    """The engine's own freshness check stands the worker down; Guardian marks
    the feed FAILED, the instance BLOCKED by it (not broken), records the
    stale and disconnect events, and sees the recovery."""
    store, bus = g
    manager, inst = _manager(tmp_path, _brain, fresh=False)
    svc = _service(store, bus, manager)
    try:
        manager.start(inst.id)
        assert _wait_for(lambda: _lifecycle(manager, inst).count("MARKET_DISCONNECTED") >= 1)
        nodes = svc.cycle()
        feed, node = nodes[f"feed:instance:{inst.id}"], nodes[f"instance:{inst.id}"]
        assert feed.effective == h.FAILED and "candles are late" in feed.detail
        assert node.raw == h.HEALTHY                    # the worker itself is fine
        assert node.effective == h.BLOCKED and node.blocked_by == [feed.id]

        _StaleThenFresh.fresh = True                    # the feed recovers
        assert _wait_for(lambda: "MARKET_CONNECTED" in _lifecycle(manager, inst))
        assert _wait_for(lambda: svc.cycle()[f"instance:{inst.id}"].effective == h.HEALTHY)
    finally:
        manager.shutdown()
        _StaleThenFresh.fresh = False
    bus.flush()
    stale = store.events(event_type="stale_candle", instance_id=inst.id)
    lost = store.events(event_type="websocket_disconnected", instance_id=inst.id)
    assert stale and stale[0]["severity"] == "WARNING" and stale[0]["symbol"] == "BTCUSDT"
    assert lost and lost[0]["source_component"] == f"instance:{inst.id}"
    changes = [(e["state_before"], e["state_after"]) for e in reversed(
        store.events(event_type="health_changed", source_component=f"instance:{inst.id}"))]
    assert changes[0] == (None, h.BLOCKED) and (h.BLOCKED, h.HEALTHY) in changes


def test_a_quiet_healthy_instance_is_not_an_alert(tmp_path, g):
    """No trade is not a problem: a running worker on a fresh feed with no
    setup is HEALTHY, and nothing at WARNING or worse is recorded."""
    store, bus = g
    manager, inst = _manager(tmp_path, _brain, fresh=True)
    svc = _service(store, bus, manager)
    try:
        manager.start(inst.id)
        assert _wait_for(lambda: "MARKET_CONNECTED" in _lifecycle(manager, inst))
        nodes = svc.cycle()
    finally:
        manager.shutdown()
        _StaleThenFresh.fresh = False
    bus.flush()
    assert nodes[f"instance:{inst.id}"].effective == h.HEALTHY
    assert nodes[f"feed:instance:{inst.id}"].effective == h.HEALTHY
    during_run = [e for e in store.events(min_severity="WARNING") if e["event_type"] != "worker_stopped"]
    assert during_run == []


# -------------------------------------------- 7, 11: worker crash
def test_a_worker_crash_is_detected_and_guardian_keeps_running(tmp_path, g):
    """The worker restarts after missing two candles and its strategy fails on
    the first one -- a real crash inside the worker thread. Guardian records
    it as HIGH, marks the instance FAILED, and its own loop carries on."""
    from strategies.brain_strategy import DecisionBrain

    class Failing(DecisionBrain):
        def on_bar(self, bar):
            raise RuntimeError("strategy fault on a live candle")

    store, bus = g
    manager, inst = _manager(tmp_path, lambda _k, s: Failing(s), fresh=True)
    step = 300
    anchor = int(datetime.now(timezone.utc).timestamp()) // step * step
    manager.store.save_market_state(inst.id, last_processed_candle_timestamp=datetime.fromtimestamp(
        anchor - 3 * step, tz=timezone.utc).isoformat())
    svc = _service(store, bus, manager, interval_s=0.2)
    svc.start()
    try:
        manager.start(inst.id)
        assert _wait_for(lambda: "INSTANCE_ERROR" in _lifecycle(manager, inst))
        assert _wait_for(lambda: (store.components().get(f"instance:{inst.id}") or {})
                         .get("effective") == h.FAILED)
        cycles = svc.cycles
        assert _wait_for(lambda: svc.cycles > cycles + 2)      # still observing after the crash
        assert svc.running
    finally:
        svc.stop()
        manager.shutdown()
        _StaleThenFresh.fresh = False
    crash = store.events(event_type="worker_crashed", instance_id=inst.id)
    assert crash and crash[0]["severity"] == "HIGH"
    assert store.components()[f"instance:{inst.id}"]["detail"] in (
        "the worker is not running though the instance should be", "the worker stopped on an error")


# -------------------------------------------- 12: trading survives Guardian
class _BrokenStore:
    def append_events(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")


def test_trading_is_unaffected_when_guardian_is_broken(tmp_path):
    """Guardian's evidence store fails on every write and its queue holds one
    event. The real strategy still trades through the real pipeline, the real
    manager still starts its worker, and nothing raised."""
    from tests.test_loss_streak_order import _attempt, _engine
    bus = EventBus(_BrokenStore(), capacity=1)
    bus.start()
    guardian.install(bus)
    try:
        assert guardian.emit("no_such_event", source_service="x", source_component="y") is False
        _, engine, paper = _engine(tmp_path)
        assert _attempt(engine, paper, 0, "win")                 # a full trade, opened and closed
        manager, inst = _manager(tmp_path / "m", _brain, fresh=True)
        try:
            manager.start(inst.id)
            assert _wait_for(lambda: "MARKET_CONNECTED" in _lifecycle(manager, inst))
            assert manager.worker_alive(inst.id)
        finally:
            manager.shutdown()
            _StaleThenFresh.fresh = False
    finally:
        guardian.uninstall()
        bus.stop()
    stats = bus.stats()
    assert stats["rejected"] == 1
    assert stats["dropped"] + stats["store_failures"] > 0        # counted, never hidden
    assert stats["stored"] == 0


def test_emit_never_blocks_and_a_failed_write_is_retried():
    store = GuardianStore()
    bus = EventBus(store, capacity=2)
    guardian.install(bus)
    try:
        started = time.monotonic()
        results = [guardian.emit("candle_closed", source_service="t", source_component="c")
                   for _ in range(200)]
        assert time.monotonic() - started < 1.0
        assert results.count(True) == 2 and bus.stats()["dropped"] == 198
        real = store.append_events
        store.append_events = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
        assert bus.flush() == 0 and bus.stats()["store_failures"] == 1
        store.append_events = real                               # the disk recovers
        assert bus.flush() == 2 and len(store.events()) == 2     # nothing was lost
    finally:
        guardian.uninstall()


# ----------------------------------------- 13, 14, 15: no write authority
_ALLOWED_IMPORTS = {"__future__", "json", "queue", "sqlite3", "threading", "time", "uuid",
                    "dataclasses", "datetime", "pathlib", "typing", "services.redaction"}


def test_guardian_code_imports_nothing_that_can_trade():
    for path in GUARDIAN_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                     [node.module] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert name in _ALLOWED_IMPORTS or name.startswith("services.guardian"), \
                    f"{path.name} imports {name}"


def test_guardian_only_reads_the_objects_it_is_shown():
    """sources.py is the only code that touches platform objects. Every
    method it calls on them is a read."""
    reads = {"worker_alive", "is_alive", "status", "values", "get", "session", "items", "pop",
             "append", "startswith"}
    tree = ast.parse((GUARDIAN_DIR / "sources.py").read_text())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert called <= reads, called - reads


def test_the_guardian_api_is_read_only_and_live_trading_stays_locked():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    import routers.guardian
    routes = routers.guardian.router.routes
    assert {r.path for r in routes} == {"/guardian/status", "/guardian/events",
                                         "/guardian/actions", "/guardian/catalogue"}
    assert all(r.methods == {"GET"} for r in routes)
    app = FastAPI()
    app.include_router(webhook_api.router)
    client = TestClient(app)
    status = client.get("/guardian/status").json()
    assert status["boundary"]["mode"] == "read-only" and status["boundary"]["may_change"] == []
    assert client.get("/guardian/events", params={"min_severity": "LOUD"}).status_code == 400
    assert client.post("/guardian/status").status_code == 405
    assert webhook_api.broker_registry.live_locked() is True
    assert os.environ.get("HUB_ENABLE_EXTERNAL_LIVE", "0") in ("0", "", "false")


# ------------------------------------------- 18: every action audited
def test_every_guardian_action_is_audited_and_evidence_cannot_be_edited():
    store = GuardianStore()
    svc = GuardianService(store, EventBus(store), interval_s=5)
    svc.start()
    svc.stop()
    assert [a["action"] for a in reversed(store.actions())] == ["GUARDIAN_STARTED", "GUARDIAN_STOPPED"]
    assert {e["event_type"] for e in store.events()} >= {"guardian_started", "guardian_stopped"}
    for table in ("guardian_events", "guardian_actions"):
        for sql in (f"UPDATE {table} SET seq=seq", f"DELETE FROM {table}"):
            with pytest.raises(sqlite3.DatabaseError):
                store._c.execute(sql)


# ------------------------------------------- 19, 20: honest reporting
def _feed_rows(*statuses):
    return [{"id": f"i{n}", "symbol": "BTCUSDT", "timeframe": "5m", "live_feed": True, "alive": True,
             "lifecycle_state": "data_stale" if s == "stale" else "running", "market_data_status": s}
            for n, s in enumerate(statuses)]


def test_an_unchanged_state_is_reported_once():
    store = GuardianStore()
    bus = EventBus(store)
    rows = _feed_rows("stale")
    svc = GuardianService(store, bus, instances={"main": lambda: rows})
    for _ in range(3):
        svc.cycle()
    bus.flush()
    assert len(store.events(event_type="health_changed", source_component="instance:i0")) == 1


def test_one_dead_socket_is_not_a_binance_outage_but_two_are():
    store = GuardianStore()
    single = GuardianService(store, EventBus(store), instances={"m": lambda: _feed_rows("stale")}).cycle()
    assert single["binance_usdm"].effective == h.DEGRADED
    assert "not enough evidence" in single["binance_usdm"].detail
    pair = GuardianService(store, EventBus(store),
                           instances={"m": lambda: _feed_rows("stale", "stale")}).cycle()
    assert pair["binance_usdm"].effective == h.FAILED


def test_a_blind_collector_reports_unknown_rather_than_guessing():
    store = GuardianStore()
    bus = EventBus(store)
    state = {"broken": False}

    def rows():
        if state["broken"]:
            raise ConnectionError("manager unreachable")
        return _feed_rows("healthy")
    svc = GuardianService(store, bus, instances={"main": rows})
    assert svc.cycle()["instance:i0"].effective == h.HEALTHY
    state["broken"] = True
    nodes = svc.cycle()
    assert nodes["instance:i0"].effective == h.UNKNOWN
    assert "collector failed" in nodes["instance:i0"].detail
    assert nodes["guardian"].effective == h.DEGRADED
    state["broken"] = False
    svc.cycle()
    bus.flush()
    assert store.events(event_type="collector_failed") and store.events(event_type="collector_recovered")


# ------------------------------------------------- §17: self-monitoring
def test_a_stopped_guardian_reports_itself_failed():
    store = GuardianStore()
    svc = GuardianService(store, EventBus(store), interval_s=5)
    svc.cycle()
    snap = svc.snapshot()                                 # cycled once, but not running
    [me] = [c for c in snap["components"] if c["id"] == "guardian"]
    assert me["effective"] == h.FAILED and me["detail"] == "Guardian is not running"
    assert snap["summary"]["state"] == h.FAILED


def test_secrets_never_reach_the_evidence(monkeypatch):
    from services import redaction
    monkeypatch.setenv("BINANCE_API_SECRET", "live-secret-value-123456789")
    redaction.refresh_known_secrets()
    try:
        event = make_event("api_error", source_service="t", source_component="c",
                           reason="auth failed: Bearer abcdefghijklmnopqrstuvwxyz123456 "
                                  "with live-secret-value-123456789",
                           evidence={"api_key": "plain-key-value", "detail": "live-secret-value-123456789"})
        store = GuardianStore()
        store.append_events([event])
        [row] = store.events()
        text = json.dumps(row)
        assert "live-secret-value-123456789" not in text and "abcdefghijklmnopqrstuvwxyz123456" not in text
        assert "plain-key-value" not in text
    finally:
        monkeypatch.delenv("BINANCE_API_SECRET")
        redaction.refresh_known_secrets()


# --------------------------------------------------- labs and journal
def test_a_lab_whose_feed_is_down_is_blocked_and_an_idle_lab_is_healthy():
    """Real hub subscriptions: one never started (its status says so), one
    running on a synchronised channel."""
    from services.forward_paper_hub import ForwardPaperMarketDataHub
    _StaleThenFresh.fresh = True
    hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_StaleThenFresh)

    class Runtime:
        def __init__(self, stream):
            self.stream, self._thread = stream, type("T", (), {"is_alive": lambda self: True})()
    down = Runtime(hub.subscription("smc-lab"))            # never started
    store = GuardianStore()
    svc = GuardianService(store, EventBus(store), labs={
        "smc": lambda: sources.lab_row("smc", "SMC Lab", down, session=lambda: {"id": "s1"}),
        "pa": lambda: sources.lab_row("pa", "Price Action Lab", down, session=lambda: {})})
    nodes = svc.cycle()
    _StaleThenFresh.fresh = False
    assert nodes["feed:lab:smc"].effective == h.FAILED
    assert nodes["lab:smc"].raw == h.HEALTHY and nodes["lab:smc"].effective == h.BLOCKED
    assert nodes["lab:smc"].blocked_by == ["feed:lab:smc"]
    assert nodes["lab:pa"].effective == h.HEALTHY and "idle" in nodes["lab:pa"].detail
    assert "feed:lab:pa" not in nodes                    # an idle lab's feed is not its problem


def test_a_journal_that_cannot_read_its_ledger_says_so(tmp_path):
    """Production's ledger is Supabase, which the journal recorder cannot read
    and skips. Guardian reports the journal DEGRADED with that reason instead
    of calling it healthy. The ledger is the real SupabaseLedger class, built
    without its network client -- the recorder only looks for a local
    connection, which a Supabase ledger never has."""
    from data.ledger import SupabaseLedger
    from data.trade_record_store import TradeRecordStore
    from services.journal_recorder import JournalRecorder, LedgerSource
    recorder = JournalRecorder(TradeRecordStore(str(tmp_path / "tr.db")))
    recorder.add_ledger(LedgerSource("MAIN", SupabaseLedger.__new__(SupabaseLedger)))
    recorder.start()                                      # its loop, as in the app
    try:
        assert _wait_for(lambda: recorder.passes >= 1)
        store = GuardianStore()
        nodes = GuardianService(store, EventBus(store),
                                journal=lambda: sources.journal_row(recorder)).cycle()
    finally:
        recorder.stop()
    assert nodes["journal"].effective == h.DEGRADED
    assert "not journalling: MAIN" in nodes["journal"].detail
