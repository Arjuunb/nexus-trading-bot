"""Guardian Phase 5: observations become hypotheses, never changes.

The research engine reads the journal's finished forward-paper trades --
the records the journal recorder writes, seeded here through the journal's
own store -- and writes only Guardian's research tables. Every stage runs in
order, a failed stage ends the idea, a rejected idea is kept so it is never
rediscovered, and approval is the owner's alone and changes no strategy.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data.trade_record_store import TradeRecordStore
from services.guardian.bus import EventBus
from services.guardian.research import (APPROVED, RECOMMENDED, REJECTED_EVIDENCE, REJECTED_OWNER,
                                        STAGES, TESTING, UNPROVEN, ResearchEngine, closed_trades)
from services.guardian.service import GuardianService
from services.guardian.store import GuardianStore

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
_OTHER = (2.0, -1.0, 1.5, -1.0, 2.0)          # the rest of the strategy: mean +0.7R
_LOSING = (-1.0, -1.0, -1.0, 1.0, -1.0)       # the Asia cohort: mean -0.6R
_WINNING = (2.0, 1.5, 2.0, -1.0, 2.0)


class _Clock:
    def __init__(self, at: datetime):
        self.t = at.timestamp()

    def __call__(self) -> float:
        return self.t

    def at(self, when: datetime) -> None:
        self.t = when.timestamp()


def _seed(store, *, start: datetime, n: int, asia=_LOSING, version="v1", key="h", origin="FORWARD_PAPER",
          strategy="three_candle"):
    """n trades one hour apart; every third one in the Asia session."""
    counts = {"asia": 0, "other": 0}
    for i in range(n):
        asia_trade = i % 3 == 2
        pattern, bucket = (asia, "asia") if asia_trade else (_OTHER, "other")
        r = pattern[counts[bucket] % len(pattern)]
        counts[bucket] += 1
        closed = start + timedelta(hours=i)
        store.upsert_trade({
            "execution_key": f"INSTANCE:{key}:{version}:{i}", "record_source": "INSTANCE",
            "record_origin": origin, "data_completeness": "FULL", "status": "CLOSED",
            "trade_id": f"{key}-{version}-{i}", "instance_id": "inst-A", "strategy_id": strategy,
            "strategy_name": strategy, "strategy_version": version, "symbol": "BTCUSDT", "timeframe": "5m",
            "side": "long", "trading_session": "ASIA" if asia_trade else ("LONDON" if i % 3 == 0 else "NEW_YORK"),
            "market_regime": "Trending", "position_opened_at": (closed - timedelta(minutes=30)).isoformat(),
            "position_closed_at": closed.isoformat(), "risk_amount": 10.0, "realized_r": r,
            "net_pnl": r * 10.0, "gross_pnl": r * 10.0, "fees": 0.0, "planned_rr": 2.0, "achieved_rr": r,
            "outcome": "WIN" if r > 0 else "LOSS", "exit_reason": "take-profit" if r > 0 else "stop-loss",
            "planned_stop_loss": 99.0})


def _digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def journal(tmp_path):
    path = tmp_path / "records.db"
    return path, TradeRecordStore(str(path))


def _engine(path, clock) -> ResearchEngine:
    return ResearchEngine(GuardianStore(), journal_path=str(path), clock=clock)


# --------------------------------------------------- §12-13: the observation
def test_a_losing_cohort_becomes_an_unproven_hypothesis_from_the_discovery_window_only(journal):
    path, store = journal
    _seed(store, start=T0, n=150)
    engine = _engine(path, _Clock(T0 + timedelta(days=10)))
    before = _digest(path)
    [hid] = engine.discover(closed_trades(str(path)))
    h = engine.get(hid)
    assert (h["dimension"], h["value"], h["status"]) == ("trading_session", "ASIA", UNPROVEN)
    assert h["sample"] == 90                                  # the first 60% only
    assert h["discovery_cutoff"] == (T0 + timedelta(hours=89)).isoformat()
    assert h["stage"] == "HISTORICAL_BACKTEST"
    assert h["stages"]["OBSERVATION"]["evidence"]["p"] < 0.05
    assert "may improve" in h["hypothesis"]                   # a hypothesis, not a finding
    assert _digest(path) == before                            # the journal is read, never written


def test_only_forward_paper_trades_are_evidence(journal):
    path, store = journal
    _seed(store, start=T0, n=30)
    _seed(store, start=T0, n=30, key="bt", origin="BACKTEST", asia=_WINNING)
    _seed(store, start=T0, n=30, key="rs", origin="RESEARCH", asia=_WINNING)
    rows = closed_trades(str(path))
    assert len(rows) == 30 and {r["record_origin"] for r in rows} == {"FORWARD_PAPER"}


def test_versions_of_a_strategy_are_never_combined(journal):
    """PRD §16. The Asia problem exists in v1 only; v2 gets no hypothesis."""
    path, store = journal
    _seed(store, start=T0, n=150, version="v1")
    _seed(store, start=T0, n=150, version="v2", asia=_OTHER)
    engine = _engine(path, _Clock(T0 + timedelta(days=10)))
    created = engine.discover(closed_trades(str(path)))
    assert [engine.get(h)["strategy_version"] for h in created] == ["v1"]
    [v1, v2] = sorted(engine.analyst(), key=lambda s: s["strategy_version"])
    assert v1["overall"]["trades"] == v2["overall"]["trades"] == 150
    assert v1["winners"]["trades"] + v1["losers"]["trades"] == 150


# ------------------------------------------------------- §14: the pipeline
def test_every_stage_runs_in_order_and_forward_paper_waits_for_new_trades(journal):
    path, store = journal
    _seed(store, start=T0, n=150)
    clock = _Clock(T0 + timedelta(days=10))
    engine = _engine(path, clock)
    result = engine.cycle()
    assert len(result["created"]) == 1
    h = engine.get(result["created"][0])
    passed = [s for s in STAGES if h["stages"][s]["state"] == "PASS"]
    assert passed == list(STAGES[:STAGES.index("FORWARD_PAPER")])       # in order, none skipped
    assert h["stage"] == "FORWARD_PAPER" and h["status"] == TESTING       # waiting, not assumed
    assert "not a candle-level backtest" in h["stages"]["HISTORICAL_BACKTEST"]["method"]
    assert h["stages"]["STRESS_TEST"]["evidence"]["bootstrap_probability_of_improvement"] >= 0.95

    engine.cycle()                                            # nothing new: still waiting
    assert engine.get(h["id"])["stage"] == "FORWARD_PAPER"

    # Trades closed after the hypothesis was created are the forward test.
    _seed(store, start=T0 + timedelta(days=12), n=60, key="fwd")
    clock.at(T0 + timedelta(days=16))
    engine.cycle()
    h = engine.get(h["id"])
    assert h["stages"]["FORWARD_PAPER"]["state"] == "PASS"
    assert h["stages"]["FORWARD_PAPER"]["evidence"]["trades"] == 60
    assert h["stages"]["STATISTICAL_COMPARISON"]["evidence"]["p"] < 0.01
    assert (h["stage"], h["status"]) == ("OWNER_APPROVAL", RECOMMENDED)  # Guardian stops here
    assert [e["entry"] for e in h["log"]][:2] == ["created", "HISTORICAL_BACKTEST PASS"]


def test_an_idea_that_fails_out_of_sample_is_rejected_by_evidence_and_never_rediscovered(journal):
    path, store = journal
    _seed(store, start=T0, n=90)                              # Asia loses in the discovery window...
    _seed(store, start=T0 + timedelta(hours=90), n=60, key="later", asia=_WINNING)  # ...not after it
    engine = _engine(path, _Clock(T0 + timedelta(days=10)))
    [hid] = engine.cycle()["created"]
    h = engine.get(hid)
    assert h["status"] == REJECTED_EVIDENCE and h["stage"] == "OUT_OF_SAMPLE"
    assert h["stages"]["OUT_OF_SAMPLE"]["state"] == "FAIL"
    assert h["stages"]["WALK_FORWARD"]["state"] == "PENDING"   # a failed stage ends the idea

    assert engine.cycle()["created"] == []                    # kept, so never rediscovered
    with pytest.raises(sqlite3.DatabaseError):
        with engine.store._lock:
            engine.store._c.execute("DELETE FROM guardian_hypotheses")
    with pytest.raises(sqlite3.DatabaseError):
        with engine.store._lock:
            engine.store._c.execute("UPDATE guardian_hypothesis_log SET detail='rewritten'")


# ------------------------------------------------------ §38: owner controls
def test_owner_controls_never_skip_a_stage_and_approval_changes_no_strategy(journal):
    path, store = journal
    _seed(store, start=T0, n=150)
    clock = _Clock(T0 + timedelta(days=10))
    engine = _engine(path, clock)
    [hid] = engine.discover(closed_trades(str(path)))       # discovered, not yet tested
    with pytest.raises(ValueError, match="no stage may be skipped"):
        engine.owner_action(hid, "SEND_TO_FORWARD_PAPER")
    engine.owner_action(hid, "SEND_TO_BACKTEST")
    engine.advance(closed_trades(str(path)))
    with pytest.raises(ValueError, match="already past"):
        engine.owner_action(hid, "SEND_TO_BACKTEST")
    with pytest.raises(ValueError, match="only a hypothesis Guardian recommends"):
        engine.owner_action(hid, "APPROVE_FOR_DEVELOPMENT")
    with pytest.raises(ValueError, match="unknown owner action"):
        engine.owner_action(hid, "AUTO_OPTIMIZE_PRODUCTION")
    engine.owner_action(hid, "REVIEW", note="looked at it")

    _seed(store, start=T0 + timedelta(days=12), n=60, key="fwd")
    clock.at(T0 + timedelta(days=16))
    engine.cycle()
    before = _digest(path)
    h = engine.owner_action(hid, "APPROVE_FOR_DEVELOPMENT", note="build it as a candidate")
    assert (h["status"], h["stage"]) == (APPROVED, "PRODUCTION_CANDIDATE")
    assert "production code is unchanged" in h["stages"]["PRODUCTION_CANDIDATE"]["why"]
    assert [n["action"] for n in h["owner_notes"]] == ["SEND_TO_BACKTEST", "REVIEW", "APPROVE_FOR_DEVELOPMENT"]
    assert [e["entry"] for e in h["log"] if e["actor"] == "owner"] == [
        "SEND_TO_BACKTEST", "REVIEW", "APPROVE_FOR_DEVELOPMENT"]
    assert _digest(path) == before
    with pytest.raises(ValueError, match="kept as history"):
        engine.owner_action(hid, "REJECT")


def test_the_owner_can_reject_an_idea_at_any_open_stage(journal):
    path, store = journal
    _seed(store, start=T0, n=150)
    engine = _engine(path, _Clock(T0 + timedelta(days=10)))
    [hid] = engine.cycle()["created"]
    assert engine.owner_action(hid, "REJECT", note="not worth it")["status"] == REJECTED_OWNER
    assert engine.cycle()["created"] == []


# --------------------------------------------------- in the Guardian loop
def test_research_runs_hourly_in_guardians_own_loop_and_publishes_what_it_found(journal):
    path, store = journal
    _seed(store, start=T0, n=150)
    clock = _Clock(T0 + timedelta(days=10))
    gstore = GuardianStore()
    bus = EventBus(gstore)
    svc = GuardianService(gstore, bus, performance_path=str(path), clock=clock)
    svc.cycle()
    bus.flush()
    [event] = gstore.events(event_type="hypothesis_created")
    assert event["strategy_id"] == "three_candle" and event["strategy_version"] == "v1"
    first = gstore.meta("research.last_run")
    assert first["created"] == 1 and first["trades"] == 150
    svc.cycle()                                               # within the hour: not re-run
    assert gstore.meta("research.last_run") == first
    clock.at(T0 + timedelta(days=10, hours=1, seconds=1))
    svc.cycle()
    assert gstore.meta("research.last_run")["at"] > first["at"]
    assert svc.snapshot()["research"]["hypotheses"] == {TESTING: 1}


def test_a_journal_guardian_cannot_read_is_a_failing_collector_not_an_empty_answer(tmp_path):
    bad = tmp_path / "records.db"
    bad.write_text("not a database")
    gstore = GuardianStore()
    svc = GuardianService(gstore, EventBus(gstore), performance_path=str(bad))
    svc.cycle()
    assert "research" in svc.snapshot()["self"]["collectors_failing"]


# -------------------------------------------------------------------- API
def test_the_owner_controls_need_the_control_credential_and_there_is_no_optimize_button():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    import routers.guardian
    import routers.guardian_research
    assert all(r.methods == {"POST"} for r in routers.guardian_research.router.routes)
    paths = {r.path for r in routers.guardian.router.routes + routers.guardian_research.router.routes}
    assert not [p for p in paths if any(w in p for w in ("optimi", "apply", "deploy", "auto"))]
    app = FastAPI()
    app.include_router(webhook_api.router)
    client = TestClient(app)
    body = client.get("/guardian/research").json()
    assert body["stages"] == list(STAGES) and "hypotheses" in body
    assert client.post("/guardian/research/1/action", json={"action": "REVIEW"}).status_code == 401
    key = {"x-webhook-secret": webhook_api.settings.admin_key}
    assert client.post("/guardian/research/999999/action", json={"action": "REVIEW"},
                       headers=key).status_code == 404
    assert client.post("/guardian/research/999999/action", json={"action": "DEPLOY"},
                       headers=key).status_code == 409
    assert client.get("/guardian/research/analyst").status_code == 200


def test_a_clock_that_steps_back_never_stalls_research(journal):
    path, store = journal
    _seed(store, start=T0, n=150)
    clock = _Clock(T0 + timedelta(days=10))
    gstore = GuardianStore()
    svc = GuardianService(gstore, EventBus(gstore), performance_path=str(path), clock=clock)
    gstore.set_meta("research.last_run", {"at": clock() + 86_400})   # recorded "in the future"
    svc.cycle()
    assert gstore.meta("research.last_run")["created"] == 1
