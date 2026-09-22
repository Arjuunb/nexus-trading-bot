"""The agent wired into the live SMC Lab tick, proven end to end.

Every test here drives the real runtime: the real lab account, the real
closed-candle tick, the real strategy evaluation, the real candidate staging
and the real paper-order path. The only substitution is the market feed, which
has to be deterministic.

What the suite is here to hold down:

  1. the complete live path reaches the agent;
  2. ENTRY_READY with a valid >=3R plan creates a paper order;
  3. below 3R is declined;
  4. below 0.01 BTC is declined and never rounded up;
  5. above 0.9 BTC is capped, placed at the cap, and journalled as capped;
  6. no non-ENTRY_READY state can create an order;
  7. duplicate ticks cannot duplicate an order;
  8. a restart or replay cannot re-take an executed decision;
  9. a broken agent or journal fails closed instead of being bypassed;
 10. both SMC strategy locks still hold after a complete lifecycle.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from services.smc_agent import (BTC_MAX_SIZE, BTC_MIN_SIZE, SMCAgent,
                                SizingInputs)
from services.smc_agent_journal import (MISSED, NOT_READY, REJECTED, TAKEN,
                                        SMCAgentJournal)
from services.smc_agent_runtime import (AGENT_APPROVAL_MODE,
                                        NOT_AUTOMATIC_BLOCKER,
                                        AgentGatedSMCPaperAccount,
                                        AgentSMCStrategyLabRuntime)
from services.smc_strategy_lab import (SMCPaperAccount, SMCPaperConfig,
                                       SMCStrategyLabRuntime)
from services.smc_strategy_v1 import evaluate
from tests.test_smc_strategy_ladder import seeded_engine

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "tick_size": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
    "max_quantity": 100.0, "min_notional": 5.0,
}
#: Equity that puts the risk-based size inside the BTC bounds on this plan.
ORDINARY_EQUITY = 100.0
#: Equity that drives the risk-based size above the 0.9 BTC cap.
CAPPED_EQUITY = 10_000.0
#: Equity that drives the risk-based size below the 0.01 BTC floor.
TINY_EQUITY = 10.0


def entry_ready() -> dict:
    """A real ENTRY_READY evaluation from the real, untouched strategy."""
    return evaluate(seeded_engine())


class Market:
    def __init__(self, price: float):
        self.price = price

    def usdm_contract_rules(self, symbol):
        return dict(RULES)

    def public_usdm_quote(self, symbol):
        now = datetime.now(timezone.utc).isoformat()
        return {"bid": self.price, "ask": self.price + 0.01, "mark": self.price,
                "provider_time": now, "funding_rate": 0.0001,
                "last_funding_time": "2026-08-24T08:00:00+00:00",
                "next_funding_time": "2026-08-24T16:00:00+00:00"}


class Lab:
    """One wired-up lab: account, journal, agent and runtime over one feed."""

    def __init__(self, account, journal, runtime, candle_time):
        self.account, self.journal = account, journal
        self.runtime, self.candle_time = runtime, candle_time

    def strategy_orders(self):
        return [row for row in self.account.state()["order_metadata"]
                if row["ownership"] == "strategy"]

    def candidate_status(self):
        rows = self.account.state()["candidates"]
        return rows[0]["status"] if rows else ""

    def decisions(self):
        return self.journal.decisions(limit=100)


def build(tmp_path, monkeypatch, *, evaluation=None, mode=AGENT_APPROVAL_MODE,
          equity=ORDINARY_EQUITY, agent=True, journal=None, db="smc.db",
          gated=True, journal_path=None, candle_age_minutes=5):
    evaluation = entry_ready() if evaluation is None else evaluation
    account_class = AgentGatedSMCPaperAccount if gated else SMCPaperAccount
    account = account_class(tmp_path / db, starting_balance=equity)
    account.configure(config=SMCPaperConfig(operating_mode=mode))
    price = float((evaluation.get("trade_plan") or {}).get("entry") or 100.9)
    candle_time = datetime.now(timezone.utc) - timedelta(minutes=candle_age_minutes)
    candle = {"timestamp": candle_time.isoformat(), "open": price,
              "high": price + 1, "low": price - 1, "close": price, "volume": 1_000}
    monkeypatch.setattr(
        "services.native_smc_live_visual.live_visual_state",
        lambda *args, **kwargs: {
            "candles": [candle], "source_strategy": evaluation,
            "live_display": {"last_price": price},
            "data_provenance": {"last_closed_candle": candle_time.isoformat()}})
    if journal is None:
        journal = SMCAgentJournal(journal_path or ":memory:")
    runtime = AgentSMCStrategyLabRuntime(
        Market(price), account,
        agent=SMCAgent(journal, equity=equity) if agent else None,
        autostart=False)
    return Lab(account, journal, runtime, candle_time)


def plan_with(evaluation: dict, **plan_changes) -> dict:
    """The same strategy decision with the plan's own numbers moved.

    Used to reach agent-side outcomes the current strategy never produces by
    itself. It edits a copy of one evaluation dictionary; the strategy and
    every module behind it are untouched, which test 10 re-proves.
    """
    changed = deepcopy(evaluation)
    changed["trade_plan"] = {**changed["trade_plan"], **plan_changes}
    return changed


# ------------------------------------------------------------------ 1. reach
def test_the_complete_live_lab_tick_reaches_the_agent(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch)

    result = lab.runtime.tick()

    assert result["agent"]["enabled"] is True
    assert result["agent"]["outcome"] == TAKEN
    # The lab journalled the decision and the agent journalled its own, and
    # both are about the same closed candle. If either side ever changes how
    # it names the candle, this is what catches it.
    decision = lab.decisions()[0]
    lab_evaluation = lab.account.state()["evaluations"][0]
    assert decision["candle_time"] == lab_evaluation["candle_time"]
    assert decision["strategy_fingerprint"]


# ----------------------------------------------------------------- 2. entry
def test_entry_ready_with_a_valid_plan_creates_one_paper_order(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch)

    result = lab.runtime.tick()

    agent = result["agent"]
    assert (agent["outcome"], agent["executed"]) == (TAKEN, True)
    assert agent["order_id"]
    orders = lab.strategy_orders()
    assert len(orders) == 1 and orders[0]["order_id"] == agent["order_id"]
    assert lab.candidate_status() == "ORDER_CREATED"
    trade = lab.journal.trades()[0]
    assert trade["planned_rr"] >= 3.0
    assert trade["order_id"] == agent["order_id"]
    assert trade["size_capped"] == 0
    # The size the journal claims is the size the book actually holds.
    assert lab.account.broker.order(agent["order_id"])["quantity"] == pytest.approx(trade["size"])
    assert BTC_MIN_SIZE <= trade["size"] <= BTC_MAX_SIZE
    assert lab.account.state()["real_execution_allowed"] is False


# -------------------------------------------------------------------- 3. RR
def test_a_plan_below_three_r_is_declined_and_no_order_exists(tmp_path, monkeypatch):
    # The strategy's own runner floor is 3R, so this is the agent's floor
    # holding independently of it rather than duplicating it.
    evaluation = entry_ready()
    entry, stop = evaluation["trade_plan"]["entry"], evaluation["trade_plan"]["stop"]
    short_runner = entry + (entry - stop) * 2.5
    lab = build(tmp_path, monkeypatch,
                evaluation=plan_with(evaluation, target_2=short_runner))

    result = lab.runtime.tick()

    assert result["agent"]["outcome"] == REJECTED
    assert lab.strategy_orders() == []
    assert lab.candidate_status() == "PENDING_APPROVAL"
    decision = lab.decisions()[0]
    assert decision["reason_code"] == "MINIMUM_REWARD_TO_RISK"
    gate = next(g for g in decision["gates"] if g["name"] == "minimum_reward_to_risk")
    assert gate["passed"] is False and gate["value"] == pytest.approx(2.5)


# ------------------------------------------------------------------ 4. floor
def test_a_size_below_the_floor_is_declined_and_never_rounded_up(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch, equity=TINY_EQUITY)

    result = lab.runtime.tick()

    assert result["agent"]["outcome"] == REJECTED
    assert lab.strategy_orders() == []
    assert lab.account.broker.positions() == []
    assert lab.journal.trades() == []
    decision = lab.decisions()[0]
    assert decision["reason_code"] == "POSITION_SIZE_WITHIN_BOUNDS"
    gate = next(g for g in decision["gates"] if g["name"] == "position_size_within_bounds")
    # Declined at the size risk asked for. Lifting it to the floor would have
    # risked more than the plan allowed, which is why this is a veto.
    assert 0 < gate["value"] < BTC_MIN_SIZE
    assert gate["limit"] == BTC_MIN_SIZE


# --------------------------------------------------------------------- 5. cap
def test_a_size_above_the_cap_is_placed_at_the_cap_and_journalled_as_capped(
        tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch, equity=CAPPED_EQUITY)

    result = lab.runtime.tick()

    agent = result["agent"]
    assert (agent["outcome"], agent["executed"]) == (TAKEN, True)
    trade = lab.journal.trades()[0]
    assert trade["size"] == pytest.approx(BTC_MAX_SIZE)
    assert trade["requested_size"] > BTC_MAX_SIZE
    assert trade["size_capped"] == 1
    assert trade["risk_amount"] == pytest.approx(
        abs(trade["entry"] - trade["stop"]) * BTC_MAX_SIZE)
    # Capped in the book too, not only in the journal.
    assert lab.account.broker.order(agent["order_id"])["quantity"] == pytest.approx(BTC_MAX_SIZE)
    decision = lab.decisions()[0]
    assert any(g["name"] == "position_size_capped" for g in decision["gates"])
    assert "capped down from" in decision["reason"]


def test_the_cap_only_applies_to_the_agents_own_placement(tmp_path, monkeypatch):
    """A human approval in the same session is sized by the lab, as before."""
    lab = build(tmp_path, monkeypatch, equity=CAPPED_EQUITY, agent=False)

    lab.runtime.tick()
    assert lab.candidate_status() == "PENDING_APPROVAL"
    proposal_id = lab.account.state()["candidates"][0]["proposal_id"]
    placed = lab.account.approve_candidate(proposal_id)

    assert placed["order"]["quantity"] > BTC_MAX_SIZE


# ------------------------------------------------------------- 6. not ready
@pytest.mark.parametrize("state", ["WATCHING", "PARKED", "", "SOMETHING_ELSE"])
def test_no_non_entry_ready_state_can_create_an_order(tmp_path, monkeypatch, state):
    evaluation = {**entry_ready(), "state": state}
    lab = build(tmp_path, monkeypatch, evaluation=evaluation)

    result = lab.runtime.tick()

    assert result["agent"]["outcome"] == NOT_READY
    assert lab.strategy_orders() == []
    assert lab.account.state()["candidates"] == []
    assert lab.journal.trades() == []


def test_entry_ready_without_a_plan_cannot_create_an_order(tmp_path, monkeypatch):
    evaluation = {**entry_ready(), "trade_plan": None}
    lab = build(tmp_path, monkeypatch, evaluation=evaluation)

    result = lab.runtime.tick()

    assert result["agent"]["outcome"] == NOT_READY
    assert lab.strategy_orders() == []
    assert lab.journal.trades() == []


def test_an_evaluation_from_another_strategy_is_not_traded(tmp_path, monkeypatch):
    # The lab refuses it at its own attestation check, before the agent; the
    # agent refuses it again on its own. Neither produces an order.
    evaluation = {**entry_ready(), "strategy_id": "SOMEONE_ELSES_STRATEGY"}
    lab = build(tmp_path, monkeypatch, evaluation=evaluation)

    with pytest.raises(ValueError):
        lab.runtime.tick()
    assert lab.strategy_orders() == []

    outcome = SMCAgent(lab.journal, equity=ORDINARY_EQUITY).observe(evaluation)
    assert outcome["outcome"] == NOT_READY
    assert lab.journal.decisions()[0]["reason_code"] == "NOT_AN_SMC_SIGNAL"


def test_a_stale_feed_records_a_miss_and_places_nothing(tmp_path, monkeypatch):
    # A 5m candle two hours old: the feed is not synchronized, so the lab
    # stages nothing and the agent records that it could not act.
    lab = build(tmp_path, monkeypatch, candle_age_minutes=120)

    result = lab.runtime.tick()

    assert result["agent"]["outcome"] == MISSED
    assert lab.strategy_orders() == []
    assert lab.decisions()[0]["reason_code"] == "AGENT_COULD_NOT_ACT"


# ------------------------------------------------------------- 7. duplicates
def test_repeated_ticks_on_one_candle_cannot_create_a_second_order(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch)

    first = lab.runtime.tick()
    repeats = [lab.runtime.tick() for _ in range(4)]

    assert first["agent"]["outcome"] == TAKEN
    assert [r["agent"]["outcome"] for r in repeats] == ["ALREADY_DECIDED"] * 4
    assert [r["agent"]["previous_outcome"] for r in repeats] == [TAKEN] * 4
    assert len(lab.strategy_orders()) == 1
    assert len(lab.journal.trades()) == 1
    # And the journal did not fill up with one row per poll either.
    assert len(lab.decisions()) == 1


def test_repeated_ticks_on_a_watching_candle_record_one_row(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch,
                evaluation={**entry_ready(), "state": "WATCHING"})

    for _ in range(5):
        lab.runtime.tick()

    assert len(lab.decisions()) == 1
    assert lab.decisions()[0]["outcome"] == NOT_READY


# ---------------------------------------------------------------- 8. restart
def test_a_restart_cannot_re_take_a_decision_already_executed(tmp_path, monkeypatch):
    journal_path = tmp_path / "agent-journal.db"
    lab = build(tmp_path, monkeypatch, journal_path=journal_path)
    assert lab.runtime.tick()["agent"]["outcome"] == TAKEN
    lab.journal.close()

    # A restart: new runtime, new agent, new journal handle on the same file,
    # and a lab database that has forgotten the candidate entirely — so this
    # is the agent's own protection, not the lab's, doing the work.
    restarted = build(tmp_path, monkeypatch, db="smc-after-restart.db",
                      journal_path=journal_path)
    result = restarted.runtime.tick()

    assert result["agent"]["outcome"] == "ALREADY_DECIDED"
    assert result["agent"]["previous_outcome"] == TAKEN
    assert restarted.strategy_orders() == []
    assert len(restarted.journal.trades()) == 1


def test_a_replay_of_the_same_proposal_cannot_double_the_position(tmp_path, monkeypatch):
    journal_path = tmp_path / "agent-journal.db"
    lab = build(tmp_path, monkeypatch, journal_path=journal_path)
    lab.runtime.tick()
    lab.journal.close()

    # Same proposal, different closed candle: the candle key does not match,
    # so the proposal key is what has to stop it.
    replay = build(tmp_path, monkeypatch, db="replay.db",
                   journal_path=journal_path, candle_age_minutes=10)
    result = replay.runtime.tick()

    assert result["agent"]["outcome"] == "ALREADY_DECIDED"
    assert replay.strategy_orders() == []
    assert len(replay.journal.trades()) == 1


# ------------------------------------------------------------ 9. fail closed
def test_a_runtime_without_an_agent_places_nothing_in_approval_mode(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch, agent=False)

    result = lab.runtime.tick()

    assert result["agent"]["enabled"] is False
    assert result["agent"]["executed"] is False
    assert lab.strategy_orders() == []
    assert lab.candidate_status() == "PENDING_APPROVAL"


def test_an_agent_that_raises_places_nothing(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch)

    class Broken:
        def observe(self, *args, **kwargs):
            raise RuntimeError("the agent is down")

    lab.runtime.agent = Broken()
    result = lab.runtime.tick()

    assert result["agent"]["failed"] is True
    assert result["agent"]["executed"] is False
    assert "the agent is down" in result["agent"]["reason"]
    assert lab.strategy_orders() == []
    assert lab.account.broker.positions() == []


def test_an_unwritable_journal_places_nothing(tmp_path, monkeypatch):
    class Unwritable(SMCAgentJournal):
        def record_decision(self, **kwargs):
            raise sqlite3.OperationalError("attempt to write a readonly database")

    lab = build(tmp_path, monkeypatch, journal=Unwritable(":memory:"))

    result = lab.runtime.tick()

    assert result["agent"]["failed"] is True
    assert "readonly database" in result["agent"]["error"]
    # The order is what matters: a journal that cannot record the trade must
    # not leave a trade behind that nothing recorded.
    assert lab.strategy_orders() == []
    assert lab.account.broker.positions() == []
    assert lab.candidate_status() == "PENDING_APPROVAL"


def test_a_journal_that_cannot_open_the_trade_rolls_the_decision_back(tmp_path, monkeypatch):
    class HalfBroken(SMCAgentJournal):
        def open_trade(self, **kwargs):
            raise RuntimeError("trade table is gone")

    lab = build(tmp_path, monkeypatch, journal=HalfBroken(":memory:"))

    result = lab.runtime.tick()

    assert result["agent"]["outcome"] == MISSED
    assert lab.journal.trades() == []
    # Exactly one decision row, and it says the trade was missed rather than
    # leaving a rolled-back TAKEN row behind.
    assert [d["outcome"] for d in lab.decisions()] == [MISSED]
    assert lab.decisions()[0]["reason_code"] == "EXECUTION_FAILED"


def test_an_ungated_account_refuses_to_place_a_size_it_cannot_honour(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch, gated=False)

    result = lab.runtime.tick()

    assert result["agent"]["outcome"] == MISSED
    assert lab.strategy_orders() == []
    assert "cannot place a size the agent decided" in lab.decisions()[0]["reason"]


def test_the_agent_stands_down_in_automatic_mode_instead_of_claiming_the_order(
        tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch, mode="automatic")

    result = lab.runtime.tick()

    # The lab placed its own order, as it always has. The agent did not gate
    # it, so it says so and journals nothing.
    assert len(lab.strategy_orders()) == 1
    assert result["agent"]["enabled"] is False
    assert "manual_approval" in result["agent"]["reason"]
    assert lab.decisions() == []


def test_the_agent_stands_down_in_signals_only_mode(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch, mode="signals_only")

    result = lab.runtime.tick()

    assert result["agent"]["enabled"] is False
    assert lab.strategy_orders() == []
    assert lab.decisions() == []


# ------------------------------------------------------------------ 10. locks
def test_both_smc_protection_locks_hold_after_a_complete_lifecycle(tmp_path, monkeypatch):
    from services import smc_strategy_freeze

    lab = build(tmp_path, monkeypatch)
    lab.runtime.tick()
    lab.runtime.tick()
    assert len(lab.journal.trades()) == 1

    # Lock one and two: the decision-path source manifest and the behaviour
    # fingerprint recorded before the agent was built.
    baseline = json.loads(
        (ROOT / "data/smc_decision_path_freeze.json").read_text())
    verdict = smc_strategy_freeze.verify(
        baseline["source_manifest"],
        fingerprint=baseline["behaviour_fingerprint"])
    assert verdict.intact, verdict.describe()
    assert verdict.source_changed == () and verdict.behaviour_changed is False

    # Lock three: the reviewed PR6 approved-delta manifest, which pins the
    # strategy lab itself. The agent is a subclass in its own module for
    # exactly this reason.
    pr6 = json.loads((ROOT / "data/pr6_real_paper_freeze.json").read_text())
    for relative, expected in pr6["sha256"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected


def test_the_agent_runtime_is_the_lab_runtime_plus_one_step(tmp_path, monkeypatch):
    """Everything the lab returned is still returned, unchanged."""
    lab = build(tmp_path, monkeypatch, agent=False)

    result = lab.runtime.tick()

    assert set(result) >= {"processed", "candidate", "funding",
                           "market_data_health", "paper_only",
                           "real_execution_allowed", "agent"}
    assert result["paper_only"] is True
    assert result["real_execution_allowed"] is False


def test_the_agent_cannot_change_the_decision_it_was_given(tmp_path, monkeypatch):
    """The agent sees the lab's decision after the lab has acted on it."""
    evaluation = entry_ready()
    lab = build(tmp_path, monkeypatch, evaluation=evaluation)
    before = deepcopy(evaluation)

    lab.runtime.tick()

    assert evaluation == before
    staged = lab.account.state()["evaluations"][0]
    assert staged["state"] == "ORDER_SUBMITTED"
    assert staged["strategy_id"] == "SMC_SOURCE_V1"


def test_the_agents_own_arithmetic_matches_what_the_runtime_places(tmp_path, monkeypatch):
    """The size in the journal is reproducible from the live inputs alone."""
    lab = build(tmp_path, monkeypatch, equity=ORDINARY_EQUITY)
    result = lab.runtime.tick()

    session = lab.account.session()
    trade = lab.journal.trades()[0]
    _, sizing = SMCAgent(lab.journal, equity=ORDINARY_EQUITY).evaluate_gates(
        entry_ready()["trade_plan"], "BTCUSDT",
        SizingInputs(equity=ORDINARY_EQUITY,
                     risk_percent=float(session["risk_pct"]),
                     quantity_step=RULES["quantity_step"]))
    assert trade["size"] == pytest.approx(sizing.executed)
    assert lab.account.broker.order(result["agent"]["order_id"])["quantity"] == \
        pytest.approx(sizing.executed)


# ─────────────────── the control plane says who the approver is ───────────────

def test_an_agent_gated_session_does_not_report_itself_blocked(tmp_path, monkeypatch):
    """The lab calls a non-automatic session blocked because, before the agent,
    the only approver was a person. With the agent attached that is no longer
    true, and a status saying BLOCKED about a session creating orders is the
    kind of display that stops being worth reading."""
    lab = build(tmp_path, monkeypatch)
    lab.runtime.tick()

    status = lab.runtime.bot_status()

    assert status["agent"]["is_approver"] is True
    assert status["agent"]["minimum_reward_to_risk"] == 3.0
    assert NOT_AUTOMATIC_BLOCKER not in status["blockers"]
    assert status["agent"]["last_result"]["outcome"] == TAKEN


def test_a_session_without_an_agent_keeps_the_labs_own_verdict(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch, agent=False)
    lab.runtime.tick()

    status = lab.runtime.bot_status()

    assert status["agent"]["attached"] is False
    assert status["agent"]["is_approver"] is False
    assert NOT_AUTOMATIC_BLOCKER in status["blockers"]
    assert status["execution_armed"] is False


def test_an_agent_gated_session_still_reports_every_other_blocker(tmp_path, monkeypatch):
    """Only the stale one is dropped. The entry the agent just placed still
    blocks the next one, and the status has to keep saying so."""
    lab = build(tmp_path, monkeypatch)
    lab.runtime.tick()

    status = lab.runtime.bot_status()

    assert status["pending_orders"] == 1
    assert any("pending paper entry order" in row for row in status["blockers"])
    assert status["execution_state"] == "BLOCKED"
    assert status["execution_armed"] is False
    assert NOT_AUTOMATIC_BLOCKER not in status["blockers"]


def _lab_status(**overrides):
    base = {"session_id": "smc-session-1", "operating_mode": AGENT_APPROVAL_MODE,
            "blockers": [NOT_AUTOMATIC_BLOCKER], "execution_state": "BLOCKED",
            "session_state": "BLOCKED", "execution_armed": False}
    return {**base, **overrides}


def test_clearing_the_mode_blocker_arms_a_session_with_nothing_else_wrong(
        tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch)
    monkeypatch.setattr(SMCStrategyLabRuntime, "bot_status",
                        lambda self: _lab_status())

    status = lab.runtime.bot_status()

    assert status["blockers"] == []
    assert status["execution_state"] == "RUNNING_ARMED"
    assert status["session_state"] == "RUNNING_ARMED"
    assert status["execution_armed"] is True


@pytest.mark.parametrize("overrides, expected", [
    ({"blockers": [NOT_AUTOMATIC_BLOCKER, "market data is not synchronized"]},
     "BLOCKED"),
    ({"execution_state": "ERROR", "blockers": [NOT_AUTOMATIC_BLOCKER]}, "ERROR"),
    ({"session_id": "", "blockers": [NOT_AUTOMATIC_BLOCKER]}, "BLOCKED"),
])
def test_nothing_else_the_lab_blocked_on_is_cleared(tmp_path, monkeypatch,
                                                    overrides, expected):
    lab = build(tmp_path, monkeypatch)
    monkeypatch.setattr(SMCStrategyLabRuntime, "bot_status",
                        lambda self: _lab_status(**overrides))

    status = lab.runtime.bot_status()

    assert status["execution_state"] == expected
    assert status["execution_armed"] is False


def test_an_automatic_session_is_untouched_by_the_agent_status(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch, mode="automatic")
    lab.runtime.tick()

    status = lab.runtime.bot_status()

    assert status["agent"]["is_approver"] is False
    assert status["operating_mode"] == "automatic"


# ----------------------------------------------------------- transport truth
class FakeStream:
    """A market-data subscription reporting per-channel socket state.

    Shaped after the real ``PriceActionPublicStream.status()`` keys, which is
    what ``forward_paper_hub`` passes through to the runtime.
    """

    def __init__(self, status=None, raises=None):
        self._status, self._raises = status or {}, raises

    def status(self):
        if self._raises is not None:
            raise self._raises
        return self._status


def _half_dead_transport():
    """The 2026-09-21 production shape: bookTicker alive, kline channel dead.

    Binance accepted the subscription on both sockets and delivered only
    bookTicker, so the market channel kept reconnecting while the public
    channel sat CONNECTED and fresh.
    """
    return {
        "transport_channels": {"market": "RECONNECTING", "public": "CONNECTED"},
        "transport_errors": {"market": "ConnectionClosedError: keepalive ping timeout",
                             "public": ""},
        "failing_dependency": "BINANCE_USDM_MARKET_WEBSOCKET",
        "transport_state": "DISCONNECTED",
        "public_streams": {"market": ["kline", "markPrice"], "public": ["bookTicker"]},
        "last_candle_update": None, "last_mark_update": None,
        "last_quote_update": "2026-09-21T20:31:04+00:00",
        "last_successful_event": {"kind": "bid_ask", "at": "2026-09-21T20:31:04+00:00"},
        "retry_state": {"attempt": 9, "channel_attempts": {"market": 9, "public": 0},
                        "maximum_backoff_seconds": 30, "automatic_retry": True},
        "last_error": "TimeoutError", "quotes_enabled": True,
    }


def test_one_dead_channel_is_visible_instead_of_a_flat_disconnected(
        tmp_path, monkeypatch):
    """The lab's feed block says DISCONNECTED / BINANCE_USDM_PUBLIC_STREAMS for
    a session with no network and for one whose bookTicker channel is fully
    connected while kline is dead. Those need different fixes by different
    people, so the status has to tell them apart."""
    lab = build(tmp_path, monkeypatch)
    lab.runtime.stream = FakeStream(_half_dead_transport())

    feed = lab.runtime.bot_status()["feed"]
    diagnostics = feed["transport_diagnostics"]

    assert feed["failing_dependency"] == "BINANCE_USDM_PUBLIC_STREAMS"
    assert diagnostics["available"] is True
    assert diagnostics["channels"] == {"market": "RECONNECTING", "public": "CONNECTED"}
    assert diagnostics["transport_failing_dependency"] == "BINANCE_USDM_MARKET_WEBSOCKET"
    assert "keepalive ping timeout" in diagnostics["channel_errors"]["market"]
    assert diagnostics["streams_per_channel"]["market"] == ["kline", "markPrice"]
    assert diagnostics["last_successful_event"]["kind"] == "bid_ask"
    assert diagnostics["retry_state"]["channel_attempts"]["market"] == 9


def test_a_runtime_with_no_subscription_says_so_rather_than_raising(
        tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch)
    assert lab.runtime.stream is None

    diagnostics = lab.runtime.bot_status()["feed"]["transport_diagnostics"]

    assert diagnostics == {"available": False,
                           "reason": "no market-data subscription"}


#: The placeholder the lab seeds ``last_market_health`` with, and serves as the
#: feed block until ``reconcile_visual`` completes a pass. Copied from
#: ``SMCStrategyLabRuntime.__init__`` so a drift there fails the check below.
PLACEHOLDER_FEED = {
    "state": "DISCONNECTED", "transport_state": "DISCONNECTED",
    "health_reason": "SMC market-data runtime has not synchronized",
    "reliable": False, "new_entries_paused": True,
    "failing_dependency": "BINANCE_USDM_PUBLIC_STREAMS",
    "last_successful_event": None,
}


def test_the_placeholder_this_is_built_against_is_still_the_labs_own(
        tmp_path, monkeypatch):
    """If the lab ever starts reporting per-channel state itself, the fixture
    below stops representing production and these tests quietly go fictional."""
    lab = build(tmp_path, monkeypatch, agent=False)

    assert SMCStrategyLabRuntime(Market(1.0), lab.account,
                                 autostart=False).last_market_health == PLACEHOLDER_FEED


def test_a_stream_that_cannot_report_does_not_break_the_status(
        tmp_path, monkeypatch):
    """An operator asking why the feed is down is the worst possible moment
    to answer with a 500."""
    lab = build(tmp_path, monkeypatch)
    monkeypatch.setattr(SMCStrategyLabRuntime, "bot_status",
                        lambda self: _lab_status(feed=dict(PLACEHOLDER_FEED)))
    lab.runtime.stream = FakeStream(raises=RuntimeError("hub lock timed out"))

    diagnostics = lab.runtime.bot_status()["feed"]["transport_diagnostics"]

    assert diagnostics["available"] is False
    assert "hub lock timed out" in diagnostics["reason"]


GATE_FIELDS = ("execution_state", "session_state", "execution_armed", "blockers")


def test_reading_the_transport_cannot_move_a_single_gate(tmp_path, monkeypatch):
    """This is observability and nothing else. If adding it could change what
    the gates see, it would be a trading change wearing a diagnostic's name.

    Both sides run against one fixed lab verdict, so the only difference is
    whether a transport was read -- which is the claim being tested.
    """
    lab = build(tmp_path, monkeypatch)
    monkeypatch.setattr(SMCStrategyLabRuntime, "bot_status",
                        lambda self: _lab_status(feed=dict(PLACEHOLDER_FEED)))

    lab.runtime.stream = None
    before = lab.runtime.bot_status()
    lab.runtime.stream = FakeStream(_half_dead_transport())
    after = lab.runtime.bot_status()

    for field in GATE_FIELDS:
        assert before[field] == after[field], field
    assert {key: value for key, value in after["feed"].items()
            if key != "transport_diagnostics"} == PLACEHOLDER_FEED
    assert after["feed"]["transport_diagnostics"]["channels"]["public"] == "CONNECTED"


def test_a_healthy_transport_reports_both_channels_connected(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch)
    lab.runtime.stream = FakeStream({
        **_half_dead_transport(),
        "transport_channels": {"market": "CONNECTED", "public": "CONNECTED"},
        "failing_dependency": None})

    diagnostics = lab.runtime.bot_status()["feed"]["transport_diagnostics"]

    assert diagnostics["channels"] == {"market": "CONNECTED", "public": "CONNECTED"}
    assert diagnostics["transport_failing_dependency"] is None


# ─────────────────────────────── in-trade stops ───────────────────────────
from services.smc_agent_trade_manager import TradeManagementPolicy  # noqa: E402

MANAGING = TradeManagementPolicy(enabled=True, breakeven_at_r=1.0)


def managed_lab(tmp_path, monkeypatch, *, policy=MANAGING, **kwargs):
    """A lab whose runtime manages open positions."""
    lab = build(tmp_path, monkeypatch, **kwargs)
    lab.runtime.trade_policy = policy.validated()
    return lab


def test_management_is_off_unless_it_is_asked_for(tmp_path, monkeypatch):
    """It changes which trades scratch and which run. Nobody inherits that
    by upgrading."""
    lab = build(tmp_path, monkeypatch)

    assert lab.runtime.trade_policy.enabled is False


def test_a_stale_feed_moves_no_stop(tmp_path, monkeypatch):
    """Fail-closed in the same direction as every other gate: an unreliable
    feed is not a licence to move a stop on a price nobody trusts."""
    lab = managed_lab(tmp_path, monkeypatch)

    moves = lab.runtime._manage_open_trades(
        {"market_data_health": {"reliable": False}},
        {"candles": [{"high": 200.0, "low": 100.0, "close": 199.0}]})

    assert moves == []


def test_no_open_trade_means_no_move(tmp_path, monkeypatch):
    lab = managed_lab(tmp_path, monkeypatch)

    moves = lab.runtime._manage_open_trades(
        {"market_data_health": {"reliable": True}},
        {"candles": [{"high": 200.0, "low": 100.0, "close": 199.0}]})

    assert moves == []


def test_the_forming_candle_is_never_an_execution_input(tmp_path, monkeypatch):
    """_closed_candles reads the reconciled visual, which carries closed
    candles only. A malformed row yields nothing rather than a guess."""
    lab = managed_lab(tmp_path, monkeypatch)

    assert lab.runtime._closed_candles({"candles": []}, 3) == []
    assert lab.runtime._closed_candles({}, 3) == []
    assert lab.runtime._closed_candles(
        {"candles": [{"high": 1.0, "low": None, "close": 1.0}]}, 3) == []


def _with_position(lab, monkeypatch, trade, *, stop=None):
    """Present the open position the broker would hold for this trade.

    The position is injected at the account's own state() seam rather than
    filled through the book: this fixture's synthetic evaluation carries 2025
    timestamps while the candle clock is 2026, so the staged entry expires
    before it can fill, and reworking that time model is a different change.

    What these tests cover is the WIRING -- journal read, position read, move
    computed, book called, journal written. The arithmetic that decides the
    move is covered against real numbers in
    tests/test_smc_agent_trade_manager.py, which needs no lab at all.
    """
    real_state = lab.runtime.account.state
    position = {"symbol": trade["symbol"], "side": "buy",
                "size": float(trade["size"]), "entry_price": float(trade["entry"]),
                "stop_loss": float(trade["stop"] if stop is None else stop),
                "take_profit": float(trade["target"])}

    def state(*args, **kwargs):
        out = dict(real_state(*args, **kwargs))
        out["positions"] = [dict(position)]
        return out

    monkeypatch.setattr(lab.runtime.account, "state", state)
    return position


def _reached_one_r(trade):
    """A closed candle that took the trade a full R in favour and held it.

    The close sits just short of the extreme rather than back at entry: a
    candle closing exactly at entry would put a breakeven stop on top of the
    price, and the manager refuses that on purpose -- sending it would close
    the position at the next tick and record it as a stop-out.
    """
    entry, stop = float(trade["entry"]), float(trade["stop"])
    risk = abs(entry - stop)
    long = str(trade["direction"]) in {"bullish", "buy", "long"}
    extreme = entry + risk if long else entry - risk
    close = entry + risk * 0.9 if long else entry - risk * 0.9
    return {"candles": [{"high": max(entry, extreme), "low": min(entry, extreme),
                         "close": close}]}


RELIABLE = {"market_data_health": {"reliable": True}}


def test_a_real_breakeven_move_reaches_the_book_and_the_journal(tmp_path, monkeypatch):
    """The whole path: an open agent trade a full R in profit has its stop
    moved to entry, the move reaches the book, and the journal records it
    without touching the trade it was opened on."""
    lab = managed_lab(tmp_path, monkeypatch)
    lab.runtime.tick()
    trade = lab.journal.trades(open_only=True)[0]
    _with_position(lab, monkeypatch, trade)
    sent: list = []
    monkeypatch.setattr(lab.runtime, "_move_stop",
                        lambda symbol, stop_loss: sent.append((symbol, stop_loss)))
    entry, original_stop = float(trade["entry"]), float(trade["stop"])

    moves = lab.runtime._manage_open_trades(RELIABLE, _reached_one_r(trade))

    assert len(moves) == 1, moves
    assert moves[0]["applied"] is True
    assert moves[0]["to_price"] == pytest.approx(entry)
    assert sent == [("BTCUSDT", pytest.approx(entry))], "the book was not told"

    recorded = lab.journal.stop_moves(trade_id=trade["id"])
    assert len(recorded) == 1
    assert recorded[0]["reason_code"] == "BREAKEVEN"
    assert recorded[0]["to_price"] == pytest.approx(entry)
    assert recorded[0]["from_price"] == pytest.approx(original_stop)
    assert recorded[0]["applied"] == 1
    # The trade still says what it was opened on.
    assert float(lab.journal.trade(trade["id"])["stop"]) == pytest.approx(original_stop)


def test_the_same_move_is_not_reapplied_every_candle(tmp_path, monkeypatch):
    """Once the stop sits at breakeven the candidate equals the current stop,
    which is not a favourable move -- so a quiet market does not produce one
    journal row per candle for the rest of the trade."""
    lab = managed_lab(tmp_path, monkeypatch)
    lab.runtime.tick()
    trade = lab.journal.trades(open_only=True)[0]
    # The stop has already been moved to entry by an earlier candle.
    _with_position(lab, monkeypatch, trade, stop=float(trade["entry"]))
    monkeypatch.setattr(lab.runtime, "_move_stop", lambda *a, **k: None)

    assert lab.runtime._manage_open_trades(RELIABLE, _reached_one_r(trade)) == []
    assert lab.journal.stop_moves(trade_id=trade["id"]) == []


def test_a_failed_book_move_is_journalled_rather_than_hidden(tmp_path, monkeypatch):
    """A journal claiming a stop the position does not have is worse than a
    journal recording that the move failed."""
    lab = managed_lab(tmp_path, monkeypatch)
    lab.runtime.tick()
    trade = lab.journal.trades(open_only=True)[0]
    _with_position(lab, monkeypatch, trade)

    def refuse(*_args, **_kwargs):
        raise RuntimeError("broker refused")

    monkeypatch.setattr(lab.runtime, "_move_stop", refuse)

    moves = lab.runtime._manage_open_trades(RELIABLE, _reached_one_r(trade))

    assert moves[0]["applied"] is False
    assert "broker refused" in moves[0]["error"]
    recorded = lab.journal.stop_moves(trade_id=trade["id"])
    assert recorded[0]["applied"] == 0
    assert "broker refused" in recorded[0]["error"]


def test_a_stop_is_never_widened_through_the_live_path(tmp_path, monkeypatch):
    """The guard that matters, asserted through the wiring rather than only
    against the pure function: a position whose stop has already trailed past
    entry is not dragged back down to breakeven."""
    lab = managed_lab(tmp_path, monkeypatch)
    lab.runtime.tick()
    trade = lab.journal.trades(open_only=True)[0]
    entry, risk = float(trade["entry"]), abs(float(trade["entry"]) - float(trade["stop"]))
    _with_position(lab, monkeypatch, trade, stop=entry + risk * 0.5)
    monkeypatch.setattr(lab.runtime, "_move_stop", lambda *a, **k: None)

    assert lab.runtime._manage_open_trades(RELIABLE, _reached_one_r(trade)) == []


# ────────────────────────────── context vetoes ────────────────────────────
from services.smc_agent import Gate  # noqa: E402
from services.smc_agent_context import ContextPolicy  # noqa: E402
from services.smc_agent_journal import REJECTED as REJECTED_OUTCOME  # noqa: E402


def test_context_rules_are_off_unless_asked_for(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch)

    assert lab.runtime.context_policy.enabled is False
    assert lab.runtime._context_gates({"candles": []}) == []


def test_a_context_veto_places_nothing_and_is_recorded_as_rejected(
        tmp_path, monkeypatch):
    """The distinction that matters in the journal: this is the agent
    choosing not to trade, not the agent failing to. REJECTED, not MISSED."""
    lab = build(tmp_path, monkeypatch)
    lab.runtime.context_policy = ContextPolicy(
        enabled=True, allowed_hours_utc=((0, 1),)).validated()
    # Force the session hours gate to fail whatever the wall clock says.
    monkeypatch.setattr(lab.runtime, "_context_gates", lambda visual: [
        Gate(name="session_hours", passed=False,
             detail="03:00 UTC is outside the traded session")])

    lab.runtime.tick()

    assert lab.strategy_orders() == [], "a vetoed setup placed an order"
    decisions = lab.decisions()
    assert decisions[0]["outcome"] == REJECTED_OUTCOME
    assert decisions[0]["reason_code"] == "SESSION_HOURS"
    assert "outside the traded session" in decisions[0]["reason"]


def test_a_context_veto_outranks_a_plan_gate_in_the_recorded_reason(
        tmp_path, monkeypatch):
    """A trader who has hit their daily stop did not skip the setup because
    of its geometry, and the journal should not say they did."""
    lab = build(tmp_path, monkeypatch,
                evaluation=plan_with(entry_ready(), target_2=101.5))  # thin RR
    monkeypatch.setattr(lab.runtime, "_context_gates", lambda visual: [
        Gate(name="daily_loss_cap", passed=False,
             detail="the day is at -2.10R against a -2.00R stop")])

    lab.runtime.tick()

    decisions = lab.decisions()
    assert decisions[0]["reason_code"] == "DAILY_LOSS_CAP"
    assert lab.strategy_orders() == []


def test_a_passing_context_gate_cannot_clear_a_failing_plan_gate(
        tmp_path, monkeypatch):
    """Context can only ever ADD a veto. A rule that passed contributes
    nothing, and must not rescue a plan the agent's own gates refused."""
    lab = build(tmp_path, monkeypatch,
                evaluation=plan_with(entry_ready(), target_2=101.5))  # below 3R
    monkeypatch.setattr(lab.runtime, "_context_gates", lambda visual: [
        Gate(name="daily_loss_cap", passed=True, detail="the day is at +1.00R")])

    lab.runtime.tick()

    assert lab.strategy_orders() == []
    assert lab.decisions()[0]["outcome"] == REJECTED_OUTCOME
    assert lab.decisions()[0]["reason_code"] == "MINIMUM_REWARD_TO_RISK"


def test_context_gates_that_all_pass_leave_the_trade_alone(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch)
    monkeypatch.setattr(lab.runtime, "_context_gates", lambda visual: [
        Gate(name="session_hours", passed=True, detail="12:00 UTC is inside"),
        Gate(name="daily_loss_cap", passed=True, detail="the day is at +0.00R")])

    lab.runtime.tick()

    assert len(lab.strategy_orders()) == 1
    assert lab.decisions()[0]["outcome"] == TAKEN


def test_an_unreadable_journal_yields_no_context_verdict(tmp_path, monkeypatch):
    """A context rule that cannot see its inputs must not invent one. The
    plan gates still apply, so the failure direction is safe."""
    lab = build(tmp_path, monkeypatch)
    lab.runtime.context_policy = ContextPolicy(
        enabled=True, max_consecutive_losses=2).validated()

    def broken(*_args, **_kwargs):
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr(lab.runtime.agent.journal, "trades", broken)

    assert lab.runtime._context_gates({"candles": []}) == []


# ──────────────────────────── the frozen session ──────────────────────────
from services.smc_agent_runtime import LIVE_SESSION_MODE  # noqa: E402


def test_a_live_session_gets_no_extra_blocker(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch)

    status = lab.runtime.bot_status()

    assert status["session_mode"] == LIVE_SESSION_MODE
    assert not any("Frozen review" in row for row in status["blockers"])


def test_a_frozen_session_says_so_instead_of_looking_broken(tmp_path, monkeypatch):
    """The failure this exists for: a HISTORICAL session never ticks, so it
    reports a disconnected feed, no candles and no decisions -- identical to
    a dead venue. It is switched off, not broken, and must say which."""
    lab = build(tmp_path, monkeypatch)
    real = lab.runtime.account.session
    monkeypatch.setattr(lab.runtime.account, "session",
                        lambda: {**(real() or {}), "mode": "HISTORICAL"})

    status = lab.runtime.bot_status()

    assert status["session_mode"] == "HISTORICAL"
    frozen = [row for row in status["blockers"] if "Frozen review" in row]
    assert len(frozen) == 1, status["blockers"]
    assert "does not tick" in frozen[0]
    assert "Switch the session to Live paper" in frozen[0]


def test_the_frozen_blocker_is_not_added_twice(tmp_path, monkeypatch):
    """Polling cannot duplicate it -- the list is rebuilt from the lab on
    every call -- so the case the guard actually covers is the lab already
    reporting the same blocker itself."""
    lab = build(tmp_path, monkeypatch)
    real = lab.runtime.account.session
    monkeypatch.setattr(lab.runtime.account, "session",
                        lambda: {**(real() or {}), "mode": "HISTORICAL"})
    seen = lab.runtime.bot_status()
    already = [row for row in seen["blockers"] if "Frozen review" in row][0]
    monkeypatch.setattr(SMCStrategyLabRuntime, "bot_status",
                        lambda self: _lab_status(blockers=[already]))

    status = lab.runtime.bot_status()

    assert len([r for r in status["blockers"] if "Frozen review" in r]) == 1


def test_the_labs_own_blockers_are_never_mutated(tmp_path, monkeypatch):
    """The blocker list is copied before appending. Appending in place would
    accumulate one frozen blocker per poll inside the lab's own state."""
    lab = build(tmp_path, monkeypatch)
    real = lab.runtime.account.session
    monkeypatch.setattr(lab.runtime.account, "session",
                        lambda: {**(real() or {}), "mode": "HISTORICAL"})
    # Non-empty on purpose: an empty list is replaced by `or []` before the
    # append is reached, so an empty fixture cannot reach the guard at all.
    owned: list[str] = ["market data is not synchronized"]
    monkeypatch.setattr(SMCStrategyLabRuntime, "bot_status",
                        lambda self: _lab_status(blockers=owned))

    lab.runtime.bot_status()
    lab.runtime.bot_status()

    assert owned == ["market data is not synchronized"], \
        "the lab's own blocker list was appended to"


def test_naming_the_frozen_mode_cannot_let_it_trade(tmp_path, monkeypatch):
    """Observability only. A frozen session stays blocked and unarmed."""
    lab = build(tmp_path, monkeypatch)
    real = lab.runtime.account.session
    monkeypatch.setattr(lab.runtime.account, "session",
                        lambda: {**(real() or {}), "mode": "HISTORICAL"})

    status = lab.runtime.bot_status()

    assert status["execution_state"] != "RUNNING_ARMED"
    assert status["execution_armed"] is False


def test_the_live_mode_constant_matches_the_labs_own_gate(tmp_path):
    """If the lab ever renames its mode, this must fail rather than leave the
    blocker silently attached to every healthy session."""
    from pathlib import Path

    lab_source = (Path(__file__).resolve().parents[1] / "services"
                  / "smc_strategy_lab.py").read_text()

    assert f'current.get("mode") == "{LIVE_SESSION_MODE}"' in lab_source
