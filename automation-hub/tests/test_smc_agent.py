"""The agent trades the SMC strategy's signals and only its signals.

The hard constraint on this whole layer is that the SMC strategy is read-only.
These tests hold the agent to the half of that which lives in its own code: it
must never manufacture a trade the strategy did not offer, never loosen a
condition, and never take a setup that fails its own risk floors.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services import smc_agent as agent_mod
from services.smc_agent import (BTC_MAX_SIZE, BTC_MIN_SIZE, MIN_REWARD_TO_RISK,
                                SMCAgent, reward_to_risk)
from services.smc_agent_journal import (MISSED, NOT_READY, REJECTED, TAKEN,
                                        SMCAgentJournal)


@pytest.fixture()
def journal():
    j = SMCAgentJournal(":memory:")
    yield j
    j.close()


@pytest.fixture()
def agent(journal):
    return SMCAgent(journal, equity=10_000.0)


def _plan(entry=100.0, stop=99.0, target_2=103.0, risk_percent=0.5, **kw):
    risk = abs(entry - stop)
    return {"entry": entry, "stop": stop, "target_1": entry + (entry - stop) * 2,
            "target_2": target_2,
            # None when the plan has no risk — the strategy would never emit
            # that, but the agent must not divide by it either.
            "target_2_r": (abs(target_2 - entry) / risk) if risk else None,
            "risk_percent": risk_percent, **kw}


def _evaluation(state="ENTRY_READY", plan=None, conditions=None, missing=None,
                strategy_id="SMC_SOURCE_V1"):
    return {
        "strategy_id": strategy_id, "state": state,
        "data_identity": {"symbol": "BTCUSDT", "timeframe": "5m",
                          "selected_candle": "2026-09-19T12:00:00+00:00"},
        "ordered_condition_results": conditions if conditions is not None else
            [{"label": "Bullish FVG", "status": "PASS"},
             {"label": "Exact POI retest", "status": "PASS"}],
        "missing_conditions": missing or [],
        "trade_plan": plan if plan is not None else (_plan() if state == "ENTRY_READY" else None),
        "proposal": {"direction": "bullish"} if state == "ENTRY_READY" else None,
        "setup_id": "setup-1", "proposal_id": "prop-1",
    }


# ──────────────── signals come only from the SMC strategy ────────────────

def test_a_ready_smc_signal_is_taken(agent, journal):
    out = agent.observe(_evaluation())
    assert out["outcome"] == TAKEN and out["trade_id"]
    trade = journal.trade(out["trade_id"])
    assert trade["entry"] == 100.0 and trade["stop"] == 99.0
    assert trade["planned_rr"] == pytest.approx(3.0)


def test_the_agent_never_trades_a_setup_smc_did_not_offer(agent, journal):
    """The core constraint: no agent path turns a non-ready evaluation into a
    trade. If this ever fails, the agent has grown entry logic of its own."""
    for state in ("WATCHING", "PARKED", "", "ALMOST_READY"):
        out = agent.observe(_evaluation(state=state, plan=None))
        assert out["outcome"] == NOT_READY, state
        assert out["trade_id"] == ""
    assert journal.trades() == []


def test_a_ready_state_with_no_plan_is_still_not_a_trade(agent, journal):
    """ENTRY_READY is not enough on its own — without a plan there are no
    prices, and inventing them would be inventing the trade."""
    out = agent.observe(_evaluation(state="ENTRY_READY", plan={}))
    assert out["outcome"] == NOT_READY and journal.trades() == []


def test_an_evaluation_from_another_strategy_is_refused(agent, journal):
    out = agent.observe(_evaluation(strategy_id="SOME_OTHER_STRATEGY"))
    assert out["outcome"] == NOT_READY and journal.trades() == []
    assert journal.decisions()[0]["reason_code"] == "NOT_AN_SMC_SIGNAL"


def test_the_missing_smc_conditions_are_written_down(agent, journal):
    """'Why was this skipped' has to be answerable months later."""
    agent.observe(_evaluation(state="WATCHING", plan=None,
                              missing=["Premium / discount location"]))
    row = journal.decisions(outcome=NOT_READY)[0]
    assert row["missing"] == ["Premium / discount location"]
    assert "Premium / discount location" in row["reason"]


# ─────────────────────── the agent's risk floors ───────────────────────

def test_a_plan_under_three_r_is_declined(agent, journal):
    """The operator's floor is 1:3. A 2R plan is a valid SMC setup and still
    not a trade the agent will take."""
    out = agent.observe(_evaluation(plan=_plan(target_2=102.0)))
    assert out["outcome"] == REJECTED and journal.trades() == []
    row = journal.decisions(outcome=REJECTED)[0]
    assert row["reason_code"] == "MINIMUM_REWARD_TO_RISK"
    assert "2.00R" in row["reason"] and "3.0R floor" in row["reason"]


def test_exactly_three_r_is_accepted(agent):
    """The floor is a minimum, not a threshold to clear."""
    assert agent.observe(_evaluation(plan=_plan(target_2=103.0)))["outcome"] == TAKEN


def test_the_floor_is_judged_on_the_runner_not_the_scale_out(agent):
    """target_1 is a partial exit. Judging the floor on it would admit plans
    whose real objective is nearer than 3R."""
    out = agent.observe(_evaluation(plan=_plan(target_2=102.0)))
    assert out["outcome"] == REJECTED


def test_a_zero_risk_plan_is_declined_not_divided_by(agent, journal):
    out = agent.observe(_evaluation(plan=_plan(stop=100.0)))
    assert out["outcome"] == REJECTED
    assert "no risk to measure" in journal.decisions(outcome=REJECTED)[0]["reason"]


def test_btc_size_below_the_minimum_is_declined(journal):
    """Too small to express the trade without risking more than planned."""
    # 0.5% of 1.00 is 0.005 of risk budget over a 1.00 risk distance, which
    # is half the 0.01 BTC floor.
    tiny = SMCAgent(journal, equity=1.0)
    out = tiny.observe(_evaluation())
    assert out["outcome"] == REJECTED
    row = journal.decisions(outcome=REJECTED)[0]
    assert row["reason_code"] == "POSITION_SIZE_WITHIN_BOUNDS"
    assert "below the 0.01 minimum" in row["reason"]


def test_btc_size_above_the_maximum_is_capped_and_the_trade_still_taken(journal):
    """Capping DOWN reduces risk, which is what a position limit is for."""
    large = SMCAgent(journal, equity=10_000_000.0)
    out = large.observe(_evaluation())
    assert out["outcome"] == TAKEN
    assert out["size"] == BTC_MAX_SIZE
    assert journal.trade(out["trade_id"])["size"] == BTC_MAX_SIZE
    capped = [g for g in out["gates"] if g.name == "position_size_capped"]
    assert capped and "risks less than planned" in capped[0].detail


def test_every_taken_size_sits_inside_the_btc_bounds(journal):
    for equity in (2_000.0, 20_000.0, 200_000.0, 2_000_000.0):
        j = SMCAgentJournal(":memory:")
        out = SMCAgent(j, equity=equity).observe(_evaluation())
        if out["outcome"] == TAKEN:
            assert BTC_MIN_SIZE <= out["size"] <= BTC_MAX_SIZE, equity
        j.close()


# ─────────────────── missed is not the same as skipped ───────────────────

def test_a_valid_setup_the_agent_could_not_act_on_is_recorded_as_missed(agent, journal):
    out = agent.observe(_evaluation(), can_trade=False,
                        blocked_reason="market data feed was not reliable")
    assert out["outcome"] == MISSED and journal.trades() == []
    row = journal.decisions(outcome=MISSED)[0]
    assert "feed was not reliable" in row["reason"]


def test_a_setup_that_fails_a_gate_is_skipped_not_missed(agent, journal):
    """Collapsing the two would hide the agent's own failures inside its
    deliberate decisions."""
    agent.observe(_evaluation(plan=_plan(target_2=102.0)), can_trade=False)
    assert journal.decisions(outcome=REJECTED) and not journal.decisions(outcome=MISSED)


# ───────────────────────── every look is recorded ─────────────────────────

def test_nothing_the_agent_sees_goes_unrecorded(agent, journal):
    agent.observe(_evaluation())
    agent.observe(_evaluation(plan=_plan(target_2=102.0)))
    agent.observe(_evaluation(state="WATCHING", plan=None))
    agent.observe(_evaluation(), can_trade=False, blocked_reason="paused")
    outcomes = [d["outcome"] for d in journal.decisions()]
    assert sorted(outcomes) == sorted([TAKEN, REJECTED, NOT_READY, MISSED])


def test_a_taken_trade_records_why_in_words(agent, journal):
    out = agent.observe(_evaluation())
    why = journal.trade(out["trade_id"])["why"]
    assert "Bullish FVG" in why and "3.00R" in why


def test_every_decision_carries_the_strategy_build_that_made_it(agent, journal):
    agent.observe(_evaluation())
    assert journal.decisions()[0]["strategy_fingerprint"] == agent_mod.strategy_fingerprint()


# ──────────────────── the agent cannot touch the strategy ────────────────────

def test_the_agent_module_never_writes_to_the_strategy():
    """Read the agent's own source: it may import the strategy's names, but it
    must not assign to anything inside those modules. A learning layer that can
    reach into the strategy is not a read-only arrangement."""
    tree = ast.parse(Path(agent_mod.__file__).read_text())
    frozen = {"native_smc", "smc_strategy_v1", "smc_strategy_ladder",
              "smc_strategy_lab", "native_smc_live_visual"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    root = target
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    name = getattr(root, "id", "")
                    assert name not in frozen, (
                        f"the agent assigns into {name} — the strategy is read-only")


def test_the_agents_gates_can_only_refuse():
    """Structural check on intent: a gate is a veto. If `observe` ever gains a
    branch that opens a trade without an ENTRY_READY plan, the test above
    catches it behaviourally — this one keeps the gate vocabulary honest."""
    from services.smc_agent import Gate
    fields = {f for f in Gate.__dataclass_fields__}
    assert fields == {"name", "passed", "detail", "value", "limit"}


def test_reward_to_risk_is_computed_from_the_plans_own_prices():
    assert reward_to_risk(100.0, 99.0, 103.0) == pytest.approx(3.0)
    assert reward_to_risk(100.0, 101.0, 97.0) == pytest.approx(3.0)   # short
    assert reward_to_risk(100.0, 100.0, 103.0) is None
