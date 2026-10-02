"""A poor historical record does not make a Trading Instance read BLOCKED.

The instance card showed STRATEGY BLOCKED for SOLUSDT whenever its paper record
graded Unhealthy (15 trades, profit factor 0.35), while the line under it said
"Blocker: GATE_REJECTED: WARMUP" and a healthy instance in the same state read
WARMING_UP. Nothing blocks on that grade: the engine's health guard only
shrinks the risk of the next entry, and entries go on. The label claimed a
block that did not exist and hid what the strategy was really doing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.decision_store import DecisionStore
from data.ledger import SqliteLedger
from services import instance_status
from services.strategy_health import StrategyHealthMonitor, entry_risk_factor
from services.trading_instances import TradingInstanceManager
from tests.test_trading_instances import _factory


def _losing_record(n: int = 15) -> list[dict]:
    trades = []
    for i in range(n):
        pnl = 10.0 if i % 5 == 0 else -8.0          # 20% win rate, profit factor well under 1
        trades.append({"pnl": pnl, "r": pnl / 8, "closed_at": f"2026-09-{i + 1:02d}T00:00:00+00:00"})
    return trades


def test_an_unhealthy_record_only_shrinks_the_next_entry():
    # The premise: if Unhealthy ever starts refusing entries, BLOCKED becomes
    # true again and this file should be revisited, not silently pass.
    health = StrategyHealthMonitor().evaluate(_losing_record())
    assert health.status == "Unhealthy"
    assert 0 < entry_risk_factor(health) < 1


def test_a_warming_instance_reads_warming_up_whatever_its_record():
    status, reason = instance_status.strategy_status(
        market=instance_status.LIVE, worker_state="running", warmup_bars=400,
        warmup_required=400, blocker="GATE_REJECTED: WARMUP", htf_ready=True)
    assert (status, reason) == (instance_status.WARMING_UP, "GATE_REJECTED: WARMUP")


def test_the_solusdt_card_reads_warming_up_not_blocked(monkeypatch):
    """The card from the report: running, live feed, warm-up blocker, Unhealthy record."""
    manager = TradingInstanceManager(SqliteLedger(":memory:"), strategy_factory=_factory,
                                     live=False, live_poll_s=60, decision_store=DecisionStore(":memory:"))
    instance = manager.create(symbol="SOLUSDT", strategy_key="three_candle_rejection",
                              strategy_label="3-candle rejection", strategy_version="v1", timeframe="5m",
                              risk_per_trade_pct=0.005, capital_allocation=1_000)
    manager._instances[instance.id].state = "running"
    metrics = manager.metrics
    unhealthy = StrategyHealthMonitor().evaluate(_losing_record()).to_dict()
    monkeypatch.setattr(manager, "metrics", lambda *a, **k: {**metrics(*a, **k), "strategy_health": unhealthy})
    live_feed = {"market_data_status": "healthy", "last_blocker": "GATE_REJECTED: WARMUP",
                 "last_market_data_timestamp": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}

    status = manager.status(instance.id, market_snapshot=live_feed)
    assert status["strategy_health"]["status"] == "Unhealthy"      # still reported, as history
    assert status["market_status"] == instance_status.LIVE
    assert status["strategy_status"] == instance_status.WARMING_UP
    assert status["current_blocker"] == "GATE_REJECTED: WARMUP"
    assert status["ui_status"] == "RUNNING_UNARMED"

    live_feed["last_blocker"] = None                                 # warmed up, no setup on the candle
    status = manager.status(instance.id, market_snapshot=live_feed)
    assert status["strategy_status"] == instance_status.WAITING_FOR_SETUP
