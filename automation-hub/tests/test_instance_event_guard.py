"""News blackout for Trading Instances (services/instance_event_guard.py).

Instances never consulted the economic calendar. The guard is opt-in per
instance and off by default; these tests pin that an instance which has not
opted in behaves exactly as before, that switching it on blocks new entries
around a release without rebuilding the worker, and that the API refuses
another owner's instance.
"""
from datetime import datetime, timedelta, timezone

import pytest

from data.ledger import SqliteLedger
from services.econ_guard import EconCalendar, evaluate
from services.instance_event_guard import AFTER_MIN, InstanceEventGuard
from services.tenancy import OWNER_TENANT
from services.trading_instances import TradingInstanceManager


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


@pytest.fixture
def fast_worker(monkeypatch):
    """A worker whose thread never runs, so a test owns all the timing."""
    from services.auto_engine import AutoStrategyEngine
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1])


def _at(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _calendar(tmp_path, *events):
    cal = EconCalendar(str(tmp_path / "econ_events.json"))
    cal.set_events(list(events))
    return cal


def _manager(guard=None, owner_id=OWNER_TENANT):
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    manager.event_guard = guard
    inst = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                          strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                          capital_allocation=1_000, owner_id=owner_id)
    return manager, inst


def _entry(pipeline, n=1):
    return pipeline.process({"alert_id": f"guard-{n}", "symbol": "BTCUSDT", "side": "BUY",
                             "entry": 100.0, "stop": 95.0, "confidence": 1.0})


# ─────────────────────────── the window ───────────────────────────
def test_the_blackout_can_extend_past_the_release():
    released = [{"name": "CPI", "impact": "high", "time": _at(-5)}]
    # The global engine's long-standing behaviour is unchanged: a release in
    # the past no longer counts.
    assert evaluate(released)["mode"] == "normal"
    ev = evaluate(released, after_min=15)
    assert ev["halt_new_entries"] and ev["minutes_to_event"] < 0
    assert "released" in ev["actions"][0]
    assert evaluate([{"name": "CPI", "impact": "high", "time": _at(-20)}], after_min=15)["mode"] == "normal"


# ─────────────────────────── the switch ───────────────────────────
def test_off_by_default_and_persisted_per_instance(tmp_path):
    cal = _calendar(tmp_path, {"name": "FOMC", "time": _at(10)})
    path = str(tmp_path / "instance_event_guard.json")
    guard = InstanceEventGuard(path, cal)
    assert guard.enabled("a") is False and guard.events_for("a")() == []
    state = guard.set("a", True, by="tester")
    assert state["enabled"] and state["halt_new_entries"] and state["mode"] == "blackout"
    assert [e["name"] for e in guard.events_for("a")()] == ["FOMC"]
    assert guard.enabled("b") is False                      # per instance
    assert InstanceEventGuard(path, cal).enabled("a") is True   # survives a restart
    guard.forget("a")
    assert InstanceEventGuard(path, cal).enabled("a") is False


def test_state_shows_the_calendar_even_while_off(tmp_path):
    guard = InstanceEventGuard(None, _calendar(tmp_path, {"name": "CPI", "time": _at(90)}))
    state = guard.state("a")
    # The owner can see what switching on would do, but nothing is enforced.
    assert state["enabled"] is False and state["mode"] == "caution"
    assert state["halt_new_entries"] is False and state["risk_multiplier"] == 1.0
    assert state["window"] == {"blackout_before_min": 30, "blackout_after_min": AFTER_MIN,
                               "caution_before_min": 120}


# ─────────────────────────── the worker ───────────────────────────
def test_a_running_instance_follows_the_switch_without_a_restart(tmp_path, fast_worker):
    guard = InstanceEventGuard(None, _calendar(tmp_path, {"name": "Non-Farm Employment Change",
                                                          "time": _at(10)}))
    manager, inst = _manager(guard)
    manager.start(inst.id)
    pipeline = manager._runtime[inst.id][2]
    assert pipeline.econ_after_min == AFTER_MIN

    off = _entry(pipeline, 1)
    assert off.stage != "event_risk"

    guard.set(inst.id, True)
    on = _entry(pipeline, 2)
    assert not on.accepted and on.stage == "event_risk"
    assert "Non-Farm Employment Change in" in on.reason and "no new entries" in on.reason
    assert manager.status(inst.id)["event_guard"]["halt_new_entries"] is True

    guard.set(inst.id, False)
    assert _entry(pipeline, 3).stage != "event_risk"


def test_without_a_guard_attached_instances_have_no_calendar(fast_worker):
    manager, inst = _manager(None)
    manager.start(inst.id)
    assert manager._runtime[inst.id][2].econ_events is None
    assert manager.status(inst.id)["event_guard"] is None


def test_deleting_an_instance_clears_its_switch(tmp_path, fast_worker):
    guard = InstanceEventGuard(None, _calendar(tmp_path))
    manager, inst = _manager(guard)
    guard.set(inst.id, True)
    manager.delete(inst.id)
    assert guard.enabled(inst.id) is False


# ─────────────────────────── the API ───────────────────────────
def test_the_api_switches_it_and_refuses_another_owner(tmp_path, monkeypatch, fast_worker):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from routers import instances as instance_api

    guard = InstanceEventGuard(None, _calendar(tmp_path, {"name": "CPI", "time": _at(20)}))
    manager, mine = _manager(guard)
    theirs = manager.create(symbol="ETHUSDT", strategy_key="brain", strategy_label="Decision Brain",
                            strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                            capital_allocation=1_000, owner_id="someone-else")
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)
    monkeypatch.setattr(instance_api._wa, "_check_secret", lambda _s: None)
    monkeypatch.setattr(instance_api, "_owner", lambda _request: OWNER_TENANT)
    monkeypatch.setattr(instance_api, "_initiated_by", lambda _request: "tester")

    assert instance_api.instance_event_guard(mine.id)["enabled"] is False
    body = instance_api.EventGuardUpdate(enabled=True)
    state = instance_api.update_instance_event_guard(mine.id, body)
    assert state["enabled"] and state["halt_new_entries"]
    assert any("News blackout on" in row["message"] for row in manager.store.engine_logs(mine.id))

    for call in (lambda: instance_api.instance_event_guard(theirs.id),
                 lambda: instance_api.update_instance_event_guard(theirs.id, body)):
        with pytest.raises(HTTPException) as refused:
            call()
        assert refused.value.status_code == 404
    assert guard.enabled(theirs.id) is False


def test_the_api_requires_the_control_secret(tmp_path, monkeypatch, fast_worker):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from routers import instances as instance_api

    guard = InstanceEventGuard(None, _calendar(tmp_path))
    manager, mine = _manager(guard)
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)

    def refuse(_secret):
        raise HTTPException(401, "bad secret")
    monkeypatch.setattr(instance_api._wa, "_check_secret", refuse)
    with pytest.raises(HTTPException) as refused:
        instance_api.update_instance_event_guard(mine.id, instance_api.EventGuardUpdate(enabled=True))
    assert refused.value.status_code == 401 and guard.enabled(mine.id) is False
