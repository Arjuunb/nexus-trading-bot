"""Reviewing the agent, not the market.

The distinction these tests defend: RESULT is what the market did, VERDICT is
what the agent did. A trader who calls every loss a mistake learns to avoid
good trades; one who calls every win validation learns nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.smc_agent_journal import (CORRECT, CORRECT_BUT_LOST, LUCKY,
                                        MISSED, MISTAKE, NOT_READY, REJECTED,
                                        TAKEN, SMCAgentJournal)
from services.smc_agent_review import (is_mistake, review_closed_trades,
                                       review_trade, rule_violations,
                                       weekly_review)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def journal():
    j = SMCAgentJournal(":memory:")
    yield j
    j.close()


def _taken(journal, *, entry=100.0, stop=99.0, target=103.0, size=0.05,
           smc_state="ENTRY_READY", missing=None, at=None):
    d = journal.record_decision(
        symbol="BTCUSDT", timeframe="5m", smc_state=smc_state, outcome=TAKEN,
        reason_code="ALL_GATES_PASSED", reason="all gates passed",
        missing=missing or [], at=(at or NOW).isoformat())
    rr = abs(target - entry) / abs(entry - stop)
    t = journal.open_trade(
        decision_id=d, symbol="BTCUSDT", timeframe="5m", direction="bullish",
        entry=entry, stop=stop, target=target, planned_rr=rr, size=size,
        why="test setup", opened_at=(at or NOW).isoformat())
    return d, t


def _close(journal, trade_id, *, r, result, at=None):
    journal.close_trade(trade_id, exit_price=100.0 + r, realised_r=r,
                        result=result, close_reason="test",
                        closed_at=(at or NOW).isoformat())


# ─────────────────── result and verdict are judged apart ───────────────────

def test_a_loss_that_followed_the_rules_is_not_a_mistake(journal):
    """The heart of it. Recording this as an error teaches the agent to avoid
    the trades it exists to take."""
    _d, t = _taken(journal)
    _close(journal, t, r=-1.0, result="LOSS")
    out = review_trade(journal, t)
    assert out["verdict"] == CORRECT_BUT_LOST
    assert out["followed_rules"] is True
    assert out["is_mistake"] is False
    assert "NOT a mistake" in out["why"]
    assert "market went the other way" in out["why"]


def test_a_win_that_broke_the_rules_is_still_a_mistake(journal):
    """A profit earned by ignoring the rules rewards the behaviour that will
    eventually cost the account."""
    _d, t = _taken(journal, target=101.5)          # 1.5R, under the 3R floor
    _close(journal, t, r=1.5, result="WIN")
    out = review_trade(journal, t)
    assert out["verdict"] == LUCKY
    assert out["is_mistake"] is True and out["followed_rules"] is False
    assert "should not have been taken" in out["why"]


def test_a_rule_following_win_is_simply_correct(journal):
    _d, t = _taken(journal)
    _close(journal, t, r=3.0, result="WIN")
    out = review_trade(journal, t)
    assert out["verdict"] == CORRECT and out["is_mistake"] is False


def test_a_rule_breaking_loss_is_a_mistake(journal):
    _d, t = _taken(journal, target=101.5)
    _close(journal, t, r=-1.0, result="LOSS")
    out = review_trade(journal, t)
    assert out["verdict"] == MISTAKE and out["is_mistake"] is True


def test_only_broken_rules_count_against_the_agent():
    assert is_mistake(MISTAKE) and is_mistake(LUCKY)
    assert not is_mistake(CORRECT) and not is_mistake(CORRECT_BUT_LOST)


# ───────────────────── what counts as a violation ─────────────────────

def test_a_trade_under_the_rr_floor_is_a_violation():
    v = rule_violations({"entry": 100.0, "stop": 99.0, "target": 101.5,
                         "symbol": "BTCUSDT", "size": 0.05, "realised_r": 1.5})
    assert [x.rule for x in v] == ["minimum_reward_to_risk"]
    assert "under the 3.0R floor" in v[0].detail


def test_a_size_outside_the_btc_bounds_is_a_violation():
    for size in (0.005, 1.2):
        v = rule_violations({"entry": 100.0, "stop": 99.0, "target": 103.0,
                             "symbol": "BTCUSDT", "size": size, "realised_r": 1.0})
        assert [x.rule for x in v] == ["position_size_within_bounds"], size


def test_entering_before_smc_completed_is_a_violation(journal):
    _d, t = _taken(journal, smc_state="WATCHING")
    _close(journal, t, r=2.0, result="WIN")
    out = review_trade(journal, t)
    rules = {v["rule"] for v in out["violations"]}
    assert "signal_was_a_completed_smc_setup" in rules
    assert out["verdict"] == LUCKY


def test_entering_with_smc_conditions_still_missing_is_a_violation(journal):
    _d, t = _taken(journal, missing=["Premium / discount location"])
    _close(journal, t, r=-1.0, result="LOSS")
    out = review_trade(journal, t)
    rules = {v["rule"] for v in out["violations"]}
    assert "no_missing_smc_conditions" in rules


def test_the_strategys_own_conditions_are_never_second_guessed(journal):
    """If SMC said the setup was valid, the review takes that as given. It
    reviews the agent, not the strategy."""
    _d, t = _taken(journal)
    _close(journal, t, r=-1.0, result="LOSS")
    out = review_trade(journal, t)
    assert out["violations"] == []


def test_an_open_trade_cannot_be_reviewed(journal):
    _d, t = _taken(journal)
    with pytest.raises(ValueError, match="before it closes"):
        review_trade(journal, t)


def test_reviewing_the_backlog_skips_what_is_already_reviewed(journal):
    _d, t = _taken(journal)
    _close(journal, t, r=3.0, result="WIN")
    assert len(review_closed_trades(journal)) == 1
    assert review_closed_trades(journal) == []


# ──────────────────────────── the weekly review ────────────────────────────

def test_the_weekly_review_counts_taken_skipped_and_missed(journal):
    _d, t = _taken(journal)
    _close(journal, t, r=3.0, result="WIN")
    for _ in range(2):
        journal.record_decision(symbol="BTCUSDT", timeframe="5m",
                                smc_state="ENTRY_READY", outcome=REJECTED,
                                reason_code="MINIMUM_REWARD_TO_RISK",
                                reason="1.8R", at=NOW.isoformat())
    journal.record_decision(symbol="BTCUSDT", timeframe="5m", smc_state="WATCHING",
                            outcome=NOT_READY, reason_code="SMC_NOT_READY",
                            reason="watching", at=NOW.isoformat())
    review_closed_trades(journal)
    out = weekly_review(journal, end=NOW + timedelta(minutes=1))
    s = out["summary"]
    assert s["taken"] == 1 and s["skipped"] == 2 and s["not_ready"] == 1
    assert s["wins"] == 1 and s["net_r"] == 3.0 and s["mistakes"] == 0


def test_repeated_skips_for_the_same_reason_become_a_lesson(journal):
    for _ in range(4):
        journal.record_decision(symbol="BTCUSDT", timeframe="5m",
                                smc_state="ENTRY_READY", outcome=REJECTED,
                                reason_code="MINIMUM_REWARD_TO_RISK",
                                reason="under the floor", at=NOW.isoformat())
    out = weekly_review(journal, end=NOW + timedelta(minutes=1))
    patterns = [p["pattern"] for p in out["repeated"]]
    assert "REPEATED_SKIP::MINIMUM_REWARD_TO_RISK" in patterns
    assert journal.lessons()[0]["occurrences"] == 4


def test_one_skip_is_an_incident_not_a_habit(journal):
    journal.record_decision(symbol="BTCUSDT", timeframe="5m",
                            smc_state="ENTRY_READY", outcome=REJECTED,
                            reason_code="MINIMUM_REWARD_TO_RISK", reason="once",
                            at=NOW.isoformat())
    assert weekly_review(journal, end=NOW + timedelta(minutes=1))["repeated"] == []


def test_repeated_misses_are_reported_as_the_agents_own_failure(journal):
    for _ in range(3):
        journal.record_decision(symbol="BTCUSDT", timeframe="5m",
                                smc_state="ENTRY_READY", outcome=MISSED,
                                reason_code="AGENT_COULD_NOT_ACT",
                                reason="feed down", at=NOW.isoformat())
    out = weekly_review(journal, end=NOW + timedelta(minutes=1))
    assert any(p["pattern"] == "REPEATED_MISS" for p in out["repeated"])
    areas = {f["area"] for f in out["agent_findings"]}
    assert "execution" in areas


def test_disciplined_losses_are_reported_as_a_cost_not_an_error(journal):
    """The review must not recommend changing anything in response to them."""
    for i in range(2):
        _d, t = _taken(journal, at=NOW - timedelta(minutes=i))
        _close(journal, t, r=-1.0, result="LOSS", at=NOW)
    review_closed_trades(journal)
    out = weekly_review(journal, end=NOW + timedelta(minutes=1))
    assert out["summary"]["mistakes"] == 0
    assert out["summary"]["disciplined_losses"] == 2
    discipline = [f for f in out["agent_findings"] if f["area"] == "discipline"]
    assert discipline and "Do not tune anything" in discipline[0]["action"]


def test_the_weekly_review_is_stored(journal):
    weekly_review(journal, end=NOW)
    assert journal.weekly_reviews()[0]["summary"]["observations"] == 0


def test_findings_are_about_the_agent_never_the_strategy(journal):
    """Every finding has to be something the agent can change about itself."""
    for _ in range(3):
        journal.record_decision(symbol="BTCUSDT", timeframe="5m",
                                smc_state="ENTRY_READY", outcome=MISSED,
                                reason_code="AGENT_COULD_NOT_ACT",
                                reason="down", at=NOW.isoformat())
    out = weekly_review(journal, end=NOW + timedelta(minutes=1))
    allowed = {"execution", "journalling", "risk management", "detection",
               "discipline", "monitoring"}
    assert {f["area"] for f in out["agent_findings"]} <= allowed
    assert journal.proposed_improvements() == [], (
        "the weekly review must not file strategy changes on its own — a human "
        "raises those after reading it")
