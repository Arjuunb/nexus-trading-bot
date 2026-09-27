"""Weekly reviews: deterministic, scoped, idempotent and never self-modifying."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from data.trade_record_store import TradeRecordStore
from services import journal_stats as stats
from services.journal_memory import memory, verified_memory
from services.journal_reviews import (PROPOSAL_MIN_SAMPLE, WeeklyReviewScheduler,
                                      build_weekly_review, review_scopes, run_weekly, week_bounds)

# Monday 2026-09-07 00:00 UTC starts a review week (HUB_REVIEW_WEEK_START_DOW=0).
WEEK = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _trade(store, n, *, closed_at, r, source="INSTANCE", instance="inst-A", strategy="three_candle",
           origin="FORWARD_PAPER", side="long", session="LONDON", risk=10.0, exit_reason=None):
    net = round(r * risk, 4)
    store.upsert_trade({
        "execution_key": f"{source}:{instance}:{n}", "record_source": source,
        "record_origin": origin, "data_completeness": "FULL", "status": "CLOSED",
        "trade_id": f"{source}-{instance}-{n}", "instance_id": instance if source == "INSTANCE" else None,
        "strategy_id": strategy, "strategy_name": strategy, "symbol": "BTCUSDT", "timeframe": "5m",
        "side": side, "trading_session": session, "market_regime": "Trending",
        "position_opened_at": (closed_at - timedelta(minutes=30)).isoformat(),
        "position_closed_at": closed_at.isoformat(), "risk_amount": risk, "realized_r": r,
        "net_pnl": net, "gross_pnl": net, "fees": 0.0, "planned_rr": 2.0, "achieved_rr": r,
        "outcome": "WIN" if r > 0.05 else "LOSS" if r < -0.05 else "BREAKEVEN",
        "exit_reason": exit_reason or ("take-profit" if r > 0 else "stop-loss"),
        "planned_stop_loss": 99.0,
    })


@pytest.fixture
def store(tmp_path):
    s = TradeRecordStore(str(tmp_path / "records.db"))
    # instance A: week 1 -> +2R, -1R, -1R ; week 2 -> +2R
    _trade(s, 1, closed_at=WEEK + timedelta(days=1), r=2.0)
    _trade(s, 2, closed_at=WEEK + timedelta(days=2), r=-1.0)
    _trade(s, 3, closed_at=WEEK + timedelta(days=3), r=-1.0)
    _trade(s, 4, closed_at=WEEK + timedelta(days=8), r=2.0)
    # SMC lab: week 1
    _trade(s, 5, closed_at=WEEK + timedelta(days=2), r=1.0, source="SMC_LAB", instance="-",
           strategy="SMC_M1")
    # contamination that must never appear: a backtest and a research record
    _trade(s, 6, closed_at=WEEK + timedelta(days=2), r=9.0, origin="BACKTEST")
    _trade(s, 7, closed_at=WEEK + timedelta(days=2), r=9.0, origin="RESEARCH")
    return s


def test_scopes_are_per_agent_and_strategy_never_pooled(store):
    scopes = {(s["agent_id"], s["strategy_id"]) for s in review_scopes(store)}
    assert scopes == {("instance_agent:inst-A", "three_candle"), ("smc_agent", "SMC_M1")}


def test_statistics_are_deterministic_and_hand_checkable(store):
    records = store.query_trades(where="record_origin='FORWARD_PAPER' AND instance_id='inst-A'",
                                 limit=100)
    week1 = [r for r in records if r["position_closed_at"] < (WEEK + timedelta(days=7)).isoformat()]
    s = stats.summarize(week1)
    assert (s["trades"], s["wins"], s["losses"]) == (3, 1, 2)
    assert s["win_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert s["total_r"] == pytest.approx(0.0) and s["net_pnl"] == pytest.approx(0.0)
    assert s["profit_factor"] == pytest.approx(1.0)            # 20 won / 20 lost
    assert s["max_drawdown_r"] == pytest.approx(2.0)            # +2 then -1, -1
    assert s["sample_warning"] == "INSUFFICIENT_SAMPLE"
    assert stats.summarize([])["win_rate"] is None             # nothing to compute: None, not 0


def test_weekly_run_writes_one_review_per_scope_week_and_is_idempotent(store):
    now = WEEK + timedelta(days=15)
    first = run_weekly(store, now=now)
    assert len(first["written"]) == 3                          # A wk1, A wk2, SMC wk1
    again = run_weekly(store, now=now)
    assert again["written"] == [] and again["already_done"] == 3
    assert len(store.weekly_reviews(limit=100)) == 3


def test_a_review_only_sees_its_own_scope_and_forward_paper(store):
    run_weekly(store, now=WEEK + timedelta(days=15))
    review = next(r for r in store.weekly_reviews(limit=10)
                  if r["agent_id"] == "instance_agent:inst-A" and r["period_start"] == WEEK.isoformat())
    full = store.weekly_review(review["review_id"])
    ids = set(full["journal_record_ids"])
    for rid in ids:
        rec = store.get(rid)
        assert rec["instance_id"] == "inst-A" and rec["record_origin"] == "FORWARD_PAPER"
    assert len(ids) == 3                                       # backtest and research excluded
    assert full["stats"]["overall"]["total_r"] == pytest.approx(0.0)


def test_every_finding_points_at_records_inside_the_review(store):
    run_weekly(store, now=WEEK + timedelta(days=15))
    for summary in store.weekly_reviews(limit=10):
        review = store.weekly_review(summary["review_id"])
        allowed = set(review["journal_record_ids"])
        for kind in ("facts", "observations", "hypotheses", "recommendations"):
            for item in review["findings"][kind]:
                assert set(item["journal_record_ids"]) <= allowed, (kind, item)
        text = " ".join(i["text"] for i in review["findings"]["recommendations"]).lower()
        assert "changed the strategy" not in text


def test_restart_and_missed_weeks_are_recovered_without_duplicates(tmp_path, store):
    # Scheduler was down for weeks; a new process catches up.
    restarted = TradeRecordStore(store.path)
    out = WeeklyReviewScheduler(restarted).tick(now=WEEK + timedelta(days=30))
    assert len(out["written"]) == 3
    assert WeeklyReviewScheduler(TradeRecordStore(store.path)).tick(
        now=WEEK + timedelta(days=30))["written"] == []


def test_a_dead_running_claim_is_retried_but_a_live_one_is_not(store):
    start, end = week_bounds(WEEK + timedelta(days=1))
    args = ("instance_agent:inst-A", "three_candle", start.isoformat(), end.isoformat())
    assert store.claim_review_run(*args)
    assert not store.claim_review_run(*args)                    # a live claim blocks
    with store._lock:
        store._c.execute("UPDATE review_runs SET started_at=?",
                         ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
        store._c.commit()
    assert store.claim_review_run(*args)                        # the process died; retry


def test_the_unique_key_stops_a_second_review_of_the_same_period(store):
    scope = next(s for s in review_scopes(store) if s["agent_id"] == "smc_agent")
    start, end = week_bounds(WEEK + timedelta(days=1))
    review = build_weekly_review(store, scope, start, end)
    store.save_weekly_review(review)
    store.save_weekly_review({**review, "review_id": "a-different-id"})
    assert len(store.weekly_reviews(agent_id="smc_agent", limit=10)) == 1


def test_proposals_need_evidence_and_a_person(tmp_path):
    s = TradeRecordStore(str(tmp_path / "p.db"))
    for n in range(PROPOSAL_MIN_SAMPLE):                        # a consistently losing session
        _trade(s, n, closed_at=WEEK + timedelta(days=1, minutes=n), r=-1.0, session="ASIA")
    run_weekly(s, now=WEEK + timedelta(days=8))
    [proposal] = s.proposals()
    assert proposal["status"] == "PENDING_APPROVAL" and proposal["sample_size"] == PROPOSAL_MIN_SAMPLE
    assert set(proposal["evidence"]["journal_record_ids"]) <= {
        r["journal_record_id"] for r in s.query_trades(limit=100)}
    before = {r["journal_record_id"]: r["facts_hash"] for r in s.query_trades(limit=100)}
    with pytest.raises(ValueError):
        s.decide_proposal(proposal["proposal_id"], approve=True, actor=" ")
    decided = s.decide_proposal(proposal["proposal_id"], approve=True, actor="arjun", note="ok")
    assert decided["status"] == "APPROVED" and decided["decided_by"] == "arjun"
    with pytest.raises(ValueError):
        s.decide_proposal(proposal["proposal_id"], approve=False, actor="arjun")
    assert {r["journal_record_id"]: r["facts_hash"] for r in s.query_trades(limit=100)} == before


def test_small_samples_produce_no_proposal(store):
    run_weekly(store, now=WEEK + timedelta(days=15))
    assert store.proposals() == []


def test_memory_claims_open_to_the_exact_records(store):
    rows = verified_memory(store)
    a = next(r for r in rows if r["strategy"] == "three_candle")
    assert a["trades"] == 4 and a["provenance"] == "VERIFIED" and a["record_origin"] == "FORWARD_PAPER"
    assert len(a["journal_record_ids"]) == 4
    for rid in a["journal_record_ids"]:
        assert store.get(rid)["record_origin"] == "FORWARD_PAPER"
    assert memory(store)["legacy"] == []


def test_rule_compliance_ignores_reviews_that_could_not_assess_it(store):
    rows = store.query_trades(where="instance_id='inst-A'", limit=10)
    ids = [r["journal_record_id"] for r in rows]
    reviews = {ids[0]: {"strategy_compliance": "COMPLIANT", "risk_compliance": "COMPLIANT"},
               ids[1]: {"strategy_compliance": "VIOLATION", "risk_compliance": "COMPLIANT"},
               ids[2]: {"strategy_compliance": "UNKNOWN", "risk_compliance": "UNKNOWN"}}
    s = stats.summarize(rows, reviews=reviews)
    assert s["reviewed"] == 3 and s["compliance_assessed"] == 2
    assert s["rule_compliance"] == pytest.approx(0.5)


# ──────────── a trade that reaches the journal after its week was reviewed ────────────
def test_a_late_trade_revises_its_weeks_review_through_the_real_pipeline(tmp_path):
    """Two trades close in the same week through the real pipeline and the
    forward engine; the second reaches the journal only after the week was
    reviewed. The next run writes revision 2 with both, the first revision
    stays readable and says what replaced it, and a further run changes
    nothing."""
    from tests.test_journal_integrity import Env

    env = Env(tmp_path)
    rec = env.recorder()
    env.entry(alert="w1"); env.fill(); env.close(102.0, alert="w1c", reason="take-profit")
    rec.reconcile()
    after_week = datetime.now(timezone.utc) + timedelta(days=8)
    first = run_weekly(env.store, now=after_week)
    [rev1] = env.store.weekly_reviews()
    assert len(first["written"]) == 1 and len(rev1["journal_record_ids"]) == 1

    env.entry(alert="w2"); env.fill(); env.close(99.0, alert="w2c", reason="stop-loss")
    rec.reconcile()                                 # the late arrival
    late = [r["journal_record_id"] for r in env.store.query_trades()
            if r["journal_record_id"] not in rev1["journal_record_ids"]]
    second = run_weekly(env.store, now=after_week)
    assert second["revised"] and second["written"] == second["revised"]
    [rev2] = env.store.weekly_reviews()             # only the revision in force is listed
    assert rev2["revision"] == 2 and rev2["review_id"] != rev1["review_id"]
    assert sorted(rev2["journal_record_ids"]) == sorted(rev1["journal_record_ids"] + late)
    assert rev2["stats"]["overall"]["trades"] == 2
    assert late[0] in rev2["revision_reason"]
    old = env.store.weekly_review(rev1["review_id"])
    assert old["superseded_by"] == rev2["review_id"]            # still readable, and says so
    assert run_weekly(env.store, now=after_week)["written"] == []   # nothing more to revise


def test_a_revision_retires_pending_proposals_but_keeps_a_persons_decision(tmp_path):
    s = TradeRecordStore(str(tmp_path / "p.db"))
    for n in range(PROPOSAL_MIN_SAMPLE):                        # a consistently losing session
        _trade(s, n, closed_at=WEEK + timedelta(days=1, minutes=n), r=-1.0, session="ASIA")
    run_weekly(s, now=WEEK + timedelta(days=8))
    [proposal] = s.proposals()
    _trade(s, 99, closed_at=WEEK + timedelta(days=2), r=-1.0, session="ASIA")   # arrives late
    run_weekly(s, now=WEEK + timedelta(days=8))
    statuses = {p["proposal_id"]: p["status"] for p in s.proposals()}
    assert statuses[proposal["proposal_id"]] == "SUPERSEDED"
    [fresh] = [p for p in s.proposals() if p["status"] == "PENDING_APPROVAL"]
    assert fresh["sample_size"] == PROPOSAL_MIN_SAMPLE + 1
    with pytest.raises(ValueError):                             # a retired proposal cannot be approved
        s.decide_proposal(proposal["proposal_id"], approve=True, actor="arjun")

    decided = s.decide_proposal(fresh["proposal_id"], approve=False, actor="arjun")
    _trade(s, 100, closed_at=WEEK + timedelta(days=3), r=-1.0, session="ASIA")
    run_weekly(s, now=WEEK + timedelta(days=8))
    kept = {p["proposal_id"]: p for p in s.proposals()}[decided["proposal_id"]]
    assert (kept["status"], kept["decided_by"]) == ("REJECTED", "arjun")


def test_reviews_written_before_revisions_existed_become_revision_one(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE weekly_reviews(
      review_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
      scope_json TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
      review_version INTEGER NOT NULL, generated_at TEXT NOT NULL,
      journal_record_ids_json TEXT NOT NULL, stats_json TEXT NOT NULL, comparison_json TEXT,
      findings_json TEXT NOT NULL, validation_json TEXT NOT NULL,
      UNIQUE(agent_id, strategy_id, period_start, period_end, review_version));
    INSERT INTO weekly_reviews VALUES ('wr_old','a','s','{}','2026-09-07','2026-09-14',1,
      '2026-09-14T00:00:00+00:00','["tr_1"]','{}',NULL,'{}','{}');
    """)
    c.commit()
    c.close()
    s = TradeRecordStore(path)
    [row] = s.weekly_reviews()
    assert (row["review_id"], row["revision"], row["superseded_by"]) == ("wr_old", 1, None)
    assert row["journal_record_ids"] == ["tr_1"]
