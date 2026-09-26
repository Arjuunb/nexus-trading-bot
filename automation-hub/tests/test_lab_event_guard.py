"""News blackout for the research labs (services/lab_event_guard.py).

Each lab keeps its frozen decision path; the gate is a subclass of its paper
account. These tests drive each lab's real entry path and pin that: a lab
whose switch is off trades exactly as before; with it on, a strategy entry
inside the blackout is refused with the blackout as the recorded reason;
manual orders and a lab's own pause reason are untouched; and the Adaptive
lab's bot answers to the lab's one switch.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from services.econ_guard import EconCalendar
from services.instance_event_guard import InstanceEventGuard
from services.lab_event_guard import (EventGuardedPriceActionPaperAccount,
                                      EventGuardedSMCPaperAccount, LabKeyedEventGuard, lab_key)
from services.price_action_lab import PaperExecutionConfig
from services.smc_strategy_lab import SMCPaperConfig
from services.smc_strategy_v1 import evaluate
from tests.test_smc_strategy_ladder import seeded_engine

RULES = {"tick_size": .1, "quantity_step": .001, "min_quantity": .001,
         "max_quantity": 1000, "min_notional": 5}
NOW = datetime(2026, 1, 2, 12, 2, tzinfo=timezone.utc)


def _guard(tmp_path, minutes=10):
    cal = EconCalendar(str(tmp_path / "econ_events.json"))
    at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    cal.set_events([{"name": "CPI m/m", "impact": "high", "time": at}])
    return InstanceEventGuard(str(tmp_path / "lab_event_guard.json"), cal)


# ─────────────────────────── Price Action ───────────────────────────
def _pa(tmp_path, guard):
    account = EventGuardedPriceActionPaperAccount(tmp_path / "pa.db")
    account.event_guard = guard
    account.start(mode="LIVE_PAPER", symbol="BTCUSDT", timeframe="5m",
                  execution_config=PaperExecutionConfig(operating_mode="automatic",
                                                        strategy_id="PA1_SR_REJECTION"))
    return account


def _pa_state(n):
    return {
        "research_id": "PRICE_ACTION_NATIVE_V1_RESEARCH", "strategy_version": "1.1.0",
        "symbol": "BTCUSDT", "timeframe": "5m",
        "setups": [{"id": f"setup-{n}", "strategy_id": "PA1_SR_REJECTION", "direction": "bullish",
                    "phase": "ORDER_PENDING", "zone_id": f"zone-{n}"}],
        "proposals": [{"id": f"proposal-{n}", "setup_id": f"setup-{n}", "strategy_id": "PA1_SR_REJECTION",
                       "direction": "bullish", "entry": 105, "stop": 100, "target": 117.5,
                       "valid_until_index": 20}],
        "metrics": {},
    }


def _pa_sync(account, n):
    candle_time = NOW + timedelta(minutes=5 * (n - 1))
    return account.synchronize_strategy(_pa_state(n), contract_rules=RULES,
                                        candle=Bar(candle_time, 100, 104, 99, 101, 1000),
                                        feed_reliable=True, feed_status={"state": "SYNCHRONIZED"})


def test_price_action_trades_as_before_while_the_switch_is_off(tmp_path):
    account = _pa(tmp_path, _guard(tmp_path))
    assert len(_pa_sync(account, 1)["created"]) == 1


def test_price_action_refuses_a_strategy_entry_in_the_blackout(tmp_path):
    guard = _guard(tmp_path)
    account = _pa(tmp_path, guard)
    guard.set(lab_key("price_action"), True)
    assert _pa_sync(account, 1)["created"] == []
    [row] = account._db.execute("SELECT status,payload FROM pa_candidates").fetchall()
    assert row["status"] == "REJECTED"
    assert json.loads(row["payload"])["reason"].startswith("News blackout: CPI m/m in ")
    assert account.state()["orders"] == []
    # The pause is reported, never written into the lab's own control table.
    stored = account._db.execute("SELECT readiness_recheck_required FROM pa_runtime_controls").fetchall()
    assert all(r[0] == 0 for r in stored)
    assert account._runtime_control()["news_blackout"] is True

    guard.set(lab_key("price_action"), False)
    assert len(_pa_sync(account, 2)["created"]) == 1


def test_price_action_keeps_its_own_pause_reason(tmp_path):
    guard = _guard(tmp_path)
    account = _pa(tmp_path, guard)
    guard.set(lab_key("price_action"), True)
    account._set_readiness_recheck(True, "cleanup needs a readiness recheck")
    control = account._runtime_control()
    assert control["entry_pause_reason"] == "cleanup needs a readiness recheck"
    assert "news_blackout" not in control


def test_outside_the_window_the_switch_changes_nothing(tmp_path):
    guard = _guard(tmp_path, minutes=600)
    account = _pa(tmp_path, guard)
    guard.set(lab_key("price_action"), True)
    assert len(_pa_sync(account, 1)["created"]) == 1


# ─────────────────────────── SMC ───────────────────────────
def _smc(tmp_path, guard):
    account = EventGuardedSMCPaperAccount(tmp_path / "smc.db")
    account.event_guard = guard
    account.configure(config=SMCPaperConfig(operating_mode="automatic"))
    return account


def _smc_sync(account):
    evaluation = evaluate(seeded_engine())
    return account.synchronize_candidate(evaluation, rules=RULES,
                                         reference_price=evaluation["trade_plan"]["entry"],
                                         feed_reliable=True)


def test_smc_trades_as_before_while_the_switch_is_off(tmp_path):
    result = _smc_sync(_smc(tmp_path, _guard(tmp_path)))
    assert result["order"] is not None and result["candidate_status"] == "APPROVED_AUTOMATIC"


def test_smc_refuses_a_strategy_entry_in_the_blackout_but_not_a_manual_order(tmp_path):
    guard = _guard(tmp_path)
    account = _smc(tmp_path, guard)
    guard.set(lab_key("smc"), True)
    result = _smc_sync(account)
    assert result["order"] is None and result["candidate_status"] == "REJECTED"
    assert "News blackout: CPI m/m in" in result["reason"]
    assert account.broker.orders() == []

    manual = account.submit_order(symbol="BTCUSDT", side="long", order_type="market", rules=RULES,
                                  reference_price=100, quantity=0.1, stop_loss=90,
                                  target_1=120, target_2=130, idempotency_key="manual-1")
    assert manual["order"]["id"]


def test_the_switch_is_per_lab(tmp_path):
    guard = _guard(tmp_path)
    guard.set(lab_key("price_action"), True)
    assert _smc_sync(_smc(tmp_path, guard))["order"] is not None


# ─────────────────────────── Adaptive ───────────────────────────
def test_the_adaptive_bot_answers_to_the_labs_one_switch(tmp_path, monkeypatch):
    from data.ledger import SqliteLedger
    from services.auto_engine import AutoStrategyEngine
    from services.trading_instances import TradingInstanceManager
    from strategies.brain_strategy import DecisionBrain

    monkeypatch.setattr(AutoStrategyEngine, "start",
                        lambda self: (setattr(self, "running", True),
                                      setattr(self, "lifecycle_state", "running"), True)[-1])
    guard = _guard(tmp_path)
    manager = TradingInstanceManager(SqliteLedger(":memory:"), strategy_factory=lambda _k, s: DecisionBrain(s),
                                     live=False, live_poll_s=60)
    manager.event_guard = LabKeyedEventGuard(guard, "adaptive")
    inst = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                          strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                          capital_allocation=1_000)
    manager.start(inst.id)
    pipeline = manager._runtime[inst.id][2]
    assert pipeline.econ_events() == [] and pipeline.econ_after_min == 15

    guard.set(lab_key("adaptive"), True)
    assert [e["name"] for e in pipeline.econ_events()] == ["CPI m/m"]
    assert manager.status(inst.id)["event_guard"]["halt_new_entries"] is True
    manager.stop(inst.id)
    manager.delete(inst.id)
    assert guard.enabled(lab_key("adaptive"))           # outlives the bot


# ─────────────────────────── the API ───────────────────────────
def test_the_api_lists_switches_and_needs_the_control_secret(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    import webhook_api
    from routers import lab_event_guard as api

    guard = _guard(tmp_path)
    monkeypatch.setattr(webhook_api, "lab_event_guard", guard)
    listed = api.lab_event_guards()["labs"]
    assert [(row["lab"], row["enabled"]) for row in listed] == [
        ("price_action", False), ("smc", False), ("adaptive", False)]
    assert listed[0]["mode"] == "blackout"             # what switching on would do

    monkeypatch.setattr(webhook_api, "_check_secret", lambda _s: None)
    state = api.update_lab_event_guard("smc", api.LabEventGuardUpdate(enabled=True))
    assert state["enabled"] and state["halt_new_entries"] and guard.enabled(lab_key("smc"))
    with pytest.raises(HTTPException) as unknown:
        api.update_lab_event_guard("nope", api.LabEventGuardUpdate(enabled=True))
    assert unknown.value.status_code == 404

    def refuse(_secret):
        raise HTTPException(401, "bad secret")
    monkeypatch.setattr(webhook_api, "_check_secret", refuse)
    with pytest.raises(HTTPException) as refused:
        api.update_lab_event_guard("price_action", api.LabEventGuardUpdate(enabled=True))
    assert refused.value.status_code == 401 and not guard.enabled(lab_key("price_action"))


def test_the_server_attaches_the_gate_to_all_three_labs():
    pytest.importorskip("fastapi")
    import webhook_api
    from services.smc_agent_runtime import AgentGatedSMCPaperAccount

    assert isinstance(webhook_api.smc_paper, EventGuardedSMCPaperAccount)
    assert isinstance(webhook_api.smc_paper, AgentGatedSMCPaperAccount)
    assert webhook_api.smc_paper.event_guard is webhook_api.lab_event_guard
    assert webhook_api.price_action_paper.event_guard is webhook_api.lab_event_guard
    assert isinstance(webhook_api.adaptive_lab.manager.event_guard, LabKeyedEventGuard)
