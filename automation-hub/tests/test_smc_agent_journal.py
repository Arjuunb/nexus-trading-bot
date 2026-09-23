"""The agent's journal: append-only, and it records the skips too."""
from __future__ import annotations

import sqlite3

import pytest

from services.smc_agent_journal import (MISSED, NOT_READY, REJECTED, TAKEN,
                                        CORRECT, CORRECT_BUT_LOST, LUCKY,
                                        MISTAKE, SMCAgentJournal)


@pytest.fixture()
def journal():
    j = SMCAgentJournal(":memory:")
    yield j
    j.close()


def _decision(journal, **kw):
    kw.setdefault("symbol", "BTCUSDT")
    kw.setdefault("timeframe", "5m")
    kw.setdefault("smc_state", "ENTRY_READY")
    kw.setdefault("outcome", TAKEN)
    kw.setdefault("reason_code", "ALL_GATES_PASSED")
    kw.setdefault("reason", "SMC ready and every agent gate passed")
    return journal.record_decision(**kw)


def _trade(journal, decision_id, **kw):
    kw.setdefault("symbol", "BTCUSDT")
    kw.setdefault("timeframe", "5m")
    kw.setdefault("direction", "bullish")
    kw.setdefault("entry", 100.0)
    kw.setdefault("stop", 99.0)
    kw.setdefault("target", 103.0)
    kw.setdefault("planned_rr", 3.0)
    kw.setdefault("size", 0.05)
    kw.setdefault("why", "sweep -> CHoCH -> FVG retest, 3.0R to the runner")
    return journal.open_trade(decision_id=decision_id, **kw)


# ───────────────────── it records more than the trades ─────────────────────

def test_a_skip_is_recorded_with_its_reason(journal):
    """A journal holding only taken trades cannot answer 'why did I skip
    that?', and that is the question that changes behaviour."""
    _decision(journal, outcome=REJECTED, reason_code="RR_BELOW_MINIMUM",
              reason="plan offered 1.8R against a 3.0R minimum",
              missing=[], plan={"target_2_r": 1.8})
    row = journal.decisions(outcome=REJECTED)[0]
    assert row["reason_code"] == "RR_BELOW_MINIMUM"
    assert "1.8R" in row["reason"] and row["plan"]["target_2_r"] == 1.8


def test_a_setup_smc_never_offered_is_recorded_with_what_was_missing(journal):
    _decision(journal, outcome=NOT_READY, smc_state="WATCHING",
              reason_code="SMC_NOT_READY", reason="SMC is still watching",
              missing=["Premium / discount location"])
    row = journal.decisions(outcome=NOT_READY)[0]
    assert row["missing"] == ["Premium / discount location"]


def test_a_missed_trade_is_its_own_outcome(journal):
    """A valid signal the agent failed to act on is not the same as one it
    chose to skip, and collapsing them would hide the agent's own failures."""
    _decision(journal, outcome=MISSED, reason_code="FEED_UNRELIABLE",
              reason="entry was valid; the agent had no reliable feed to act on")
    assert journal.decisions(outcome=MISSED)[0]["reason_code"] == "FEED_UNRELIABLE"


def test_a_decision_must_say_why(journal):
    with pytest.raises(ValueError, match="why"):
        journal.record_decision(symbol="BTCUSDT", timeframe="5m",
                                smc_state="WATCHING", outcome=NOT_READY,
                                reason_code="", reason="")


def test_an_unknown_outcome_is_refused(journal):
    with pytest.raises(ValueError, match="unknown decision outcome"):
        _decision(journal, outcome="PROBABLY_FINE")


# ────────────────────────────── the trade record ──────────────────────────────

def test_a_trade_records_everything_the_review_will_need(journal):
    d = _decision(journal)
    t = _trade(journal, d, conditions=[{"label": "Bullish FVG", "status": "PASS"}],
               market={"session": "London"}, risk_amount=50.0)
    row = journal.trade(t)
    assert row["entry"] == 100.0 and row["stop"] == 99.0 and row["target"] == 103.0
    assert row["planned_rr"] == 3.0 and row["size"] == 0.05
    assert row["conditions"][0]["label"] == "Bullish FVG"
    assert row["market"]["session"] == "London"
    assert row["opened_at"] and row["open"] is True
    assert "sweep" in row["why"]


def test_a_trade_must_record_why_it_was_taken(journal):
    d = _decision(journal)
    with pytest.raises(ValueError, match="why"):
        _trade(journal, d, why="")


def test_closing_records_the_outcome(journal):
    d = _decision(journal)
    t = _trade(journal, d)
    journal.close_trade(t, exit_price=103.0, realised_r=3.0, result="WIN",
                        close_reason="target_2 reached")
    row = journal.trade(t)
    assert row["result"] == "WIN" and row["realised_r"] == 3.0
    assert row["open"] is False and row["closed_at"]


# ───────────────────────── append-only means append-only ─────────────────────

def test_a_decision_cannot_be_rewritten_after_the_fact(journal):
    """A journal editable once the outcome is known is a story written
    backwards, not evidence."""
    d = _decision(journal, outcome=REJECTED, reason_code="RR_BELOW_MINIMUM",
                  reason="1.8R against a 3.0R minimum")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._db.execute("UPDATE agent_decisions SET reason='looked fine' WHERE id=?", (d,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._db.execute("DELETE FROM agent_decisions WHERE id=?", (d,))


def test_a_review_cannot_be_rewritten(journal):
    d = _decision(journal)
    t = _trade(journal, d)
    r = journal.record_review(trade_id=t, verdict=MISTAKE, followed_rules=False,
                              why="took a 1.8R plan under the 3R floor")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._db.execute("UPDATE agent_reviews SET verdict='CORRECT' WHERE id=?", (r,))


def test_the_plan_a_trade_opened_on_cannot_be_moved(journal):
    """Editing the stop after the fact is how a losing trade becomes a
    'winner' in a journal nobody can trust."""
    d = _decision(journal)
    t = _trade(journal, d)
    for column, value in (("stop", 95.0), ("entry", 101.0),
                          ("target", 110.0), ("planned_rr", 9.0), ("size", 0.9)):
        with pytest.raises(sqlite3.IntegrityError, match="fixed"):
            journal._db.execute(
                f"UPDATE agent_trades SET {column}=? WHERE id=?", (value, t))


def test_a_trade_closes_exactly_once(journal):
    d = _decision(journal)
    t = _trade(journal, d)
    journal.close_trade(t, exit_price=103.0, realised_r=3.0, result="WIN",
                        close_reason="target")
    with pytest.raises(sqlite3.IntegrityError, match="closed trade cannot be changed"):
        journal._db.execute("UPDATE agent_trades SET realised_r=9.0 WHERE id=?", (t,))


def test_closing_an_unknown_or_already_closed_trade_is_refused(journal):
    d = _decision(journal)
    t = _trade(journal, d)
    journal.close_trade(t, exit_price=99.0, realised_r=-1.0, result="LOSS",
                        close_reason="stop")
    with pytest.raises(ValueError, match="no open trade"):
        journal.close_trade(t, exit_price=1.0, realised_r=0.0, result="WIN",
                            close_reason="again")


# ───────────────────── improvements are filed, never applied ─────────────────

def test_a_strategy_improvement_is_recorded_as_proposed_and_not_applied(journal):
    journal.propose_improvement(
        target="STRATEGY", title="Consider allowing premium-location longs",
        rationale="6 of 9 skipped setups failed only the location gate")
    row = journal.proposed_improvements(target="STRATEGY")[0]
    assert row["status"] == "PROPOSED" and row["applied"] is False


def test_nothing_in_the_journal_can_mark_an_improvement_applied(journal):
    """The store offers no way to apply one. If a row ever reads applied=True
    something outside this system did it, and that is worth seeing."""
    assert not any(name for name in dir(journal)
                   if "appl" in name.lower() and not name.startswith("_"))


def test_lessons_and_weekly_reviews_persist(journal):
    journal.record_lesson(pattern="RR_BELOW_MINIMUM", occurrences=4,
                          detail="four skips in a week for the same reason")
    journal.record_weekly_review(period_start="2026-09-13", period_end="2026-09-19",
                                 summary={"taken": 2, "skipped": 4})
    assert journal.lessons()[0]["occurrences"] == 4
    assert journal.weekly_reviews()[0]["summary"]["skipped"] == 4
