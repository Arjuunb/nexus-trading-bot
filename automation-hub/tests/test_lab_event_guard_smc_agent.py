"""SMC lab news blackout under the crash-safe agent execution path.

The lab blackout (services/lab_event_guard.py) refuses a strategy-owned SMC
entry by raising from ``submit_order``. The agent path records a durable
intent before it submits (services/smc_agent.py, smc_agent_runtime.py). A
refusal must end as a failed attempt with no order: never a taken trade,
never an order the journal cannot account for, and never a permanent
PERSISTENCE_BLOCKED that stops the lab after the release has passed.
"""
from datetime import datetime, timedelta, timezone

from services.econ_guard import EconCalendar
from services.instance_event_guard import InstanceEventGuard
from services.lab_event_guard import EventGuardedSMCPaperAccount, lab_key
from services.smc_agent import SMCAgent
from services.smc_agent_journal import SMCAgentJournal, TAKEN
from services.smc_agent_runtime import AGENT_APPROVAL_MODE, AgentSMCStrategyLabRuntime
from services.smc_strategy_lab import SMCPaperConfig
from tests.test_smc_agent_live_wiring import ORDINARY_EQUITY, Market, entry_ready

PENDING_STATES = ("DECISION_APPROVED", "EXECUTION_PENDING", "EXECUTED", "EXECUTION_UNCERTAIN")


def _guard(tmp_path, *, on: bool):
    cal = EconCalendar(str(tmp_path / "econ_events.json"))
    cal.set_events([{"name": "CPI m/m", "impact": "high",
                     "time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}])
    guard = InstanceEventGuard(str(tmp_path / "lab_event_guard.json"), cal)
    guard.set(lab_key("smc"), on)
    return guard


def _lab(tmp_path, monkeypatch, guard):
    evaluation = entry_ready()
    account = EventGuardedSMCPaperAccount(tmp_path / "smc.db", starting_balance=ORDINARY_EQUITY)
    account.event_guard = guard
    account.configure(config=SMCPaperConfig(operating_mode=AGENT_APPROVAL_MODE))
    price = float(evaluation["trade_plan"]["entry"])
    candle_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    candle = {"timestamp": candle_time.isoformat(), "open": price, "high": price + 1,
              "low": price - 1, "close": price, "volume": 1_000}
    monkeypatch.setattr(
        "services.native_smc_live_visual.live_visual_state",
        lambda *args, **kwargs: {
            "candles": [candle], "source_strategy": evaluation,
            "live_display": {"last_price": price},
            "data_provenance": {"last_closed_candle": candle_time.isoformat()}})
    journal = SMCAgentJournal(str(tmp_path / "agent.db"))
    runtime = AgentSMCStrategyLabRuntime(Market(price), account,
                                         agent=SMCAgent(journal, equity=ORDINARY_EQUITY),
                                         autostart=False)
    return account, journal, runtime


def _strategy_orders(account):
    return [row for row in account.state()["order_metadata"] if row["ownership"] == "strategy"]


def test_with_the_blackout_off_the_agent_still_takes_the_trade(tmp_path, monkeypatch):
    account, journal, runtime = _lab(tmp_path, monkeypatch, _guard(tmp_path, on=False))
    result = runtime.tick()
    assert result["agent"]["outcome"] == TAKEN and len(_strategy_orders(account)) == 1


def test_a_blackout_refusal_places_nothing_and_does_not_lock_the_lab(tmp_path, monkeypatch):
    account, journal, runtime = _lab(tmp_path, monkeypatch, _guard(tmp_path, on=True))

    first = runtime.tick()["agent"]
    assert (first["outcome"], first["executed"]) == ("EXECUTION_FAILED", False)
    [decision] = journal.decisions(limit=5)
    assert decision["outcome"] == "EXECUTION_FAILED"
    assert "News blackout: CPI m/m in" in decision["reason"]
    assert _strategy_orders(account) == [] and account.broker.orders() == []
    assert account.broker.positions() == []
    assert journal.trades() == []

    # The next tick reconciles against the broker first: it finds no order,
    # resolves the attempt, and clears any entry block. Nothing is left
    # pending that could stop the lab once the release has passed.
    runtime.tick()
    assert journal.execution_intents(states=PENDING_STATES) == []
    assert runtime.persistence_blocker == ""
    assert account.entry_persistence_blocker == ""
    assert runtime.reconciliation["state"] in ("COMPLETE", "NOT_REQUIRED")
    assert account.broker.orders() == []
