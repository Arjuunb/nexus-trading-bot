"""Synthetic decision evidence is not financial performance."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_shadow_router import setup, frame, T
from services.active_decision_evidence import ActiveDecisionSources, normalize_active
from services.shadow_comparison_service import compare_decisions, ShadowComparisonService


def decision(status="NO_SIGNAL", index=0):
    at = T + timedelta(minutes=5 * index)
    return {"decision_id": f"fixture-{index}", "owner_id": "alice", "tenant_id": "alice",
            "strategy_id": "fixture", "strategy_version": "1.0", "config_fingerprint": "a" * 64,
            "venue": "binance_usdm", "market_type": "perpetual", "symbol": "TESTUSDT", "timeframe": "5m",
            "candle_close_timestamp": at.isoformat(), "decision_timestamp": (at + timedelta(seconds=1)).isoformat(),
            "canonical_candle": {"open_timestamp": (at-timedelta(minutes=5)).isoformat(), "ohlcv": [10, 12, 9, 11, 100]},
            "finalized": True, "htf_alignment": "VERIFIED", "lookback_status": "VERIFIED",
            "context_hash": "c" * 64, "evidence_kind": "ACTIVE_DECISION_OBSERVATION",
            "observation_authorized": True, "status": status, "data_freshness": "FRESH",
            "evidence_completeness": "COMPLETE", "market_context": {"session": "LONDON", "trend_regime": "UNKNOWN"},
            "rejection_reasons": [], "missing_conditions": [], "evaluation_latency_ms": 2}


def analyze(active, shadow):
    return compare_decisions(active, [{**s, "evidence_kind":"FORWARD_SHADOW_DECISION"} for s in shadow], owner_id="alice", start=T, end=T + timedelta(minutes=5 * len(shadow)))


def test_missing_active_is_not_neutral():
    result = analyze([], [decision()])
    assert result["timeline"][0]["active_status"] == "NO_DECISION_RECORDED"
    assert result["metrics"]["both_no_signal"] == 0
    assert result["metrics"]["all_eligible_agreement"] is None
    assert result["metrics"]["missing_decision_rate"] == 1


def test_exact_agreement_counts_and_denominators():
    active = [decision(s, i) for i, s in enumerate(["SIGNAL_LONG", "SIGNAL_SHORT", "NO_SIGNAL", "SIGNAL_LONG", "NO_SIGNAL", "SIGNAL_SHORT"])]
    shadow = [decision(s, i) for i, s in enumerate(["SIGNAL_LONG", "SIGNAL_SHORT", "NO_SIGNAL", "SIGNAL_SHORT", "SIGNAL_LONG", "NO_SIGNAL"])]
    result = analyze(active, shadow)
    m = result["metrics"]
    assert [m[k] for k in ("both_long", "both_short", "both_no_signal", "opposite_signals", "shadow_only_signals", "active_only_signals")] == [1, 1, 1, 1, 1, 1]
    assert m["all_eligible_denominator"] == 6
    assert m["signal_relevant_denominator"] == 5
    assert m["all_eligible_agreement"] == .5
    assert m["signal_relevant_agreement"] == .4
    assert result["eligibility"] == "COMPARABLE"
    assert result["breakdowns"]["session"]["LONDON"]["matched_opportunities"] == 6
    assert result["profitability"] == "NOT_ASSESSED"


def test_all_neutral_does_not_claim_signal_agreement():
    result = analyze([decision()], [decision()])
    assert result["metrics"]["all_eligible_agreement"] == 1
    assert result["metrics"]["signal_relevant_agreement"] is None
    assert result["metrics"]["signal_relevant_denominator"] == 0


def test_shadow_success_coverage_does_not_trust_status_over_stale_data():
    s=decision();s["data_freshness"]="STALE"
    result=analyze([decision()],[s])
    assert result["metrics"]["matched_opportunities"]==0
    assert result["coverage"]["successfully_evaluated_shadow_opportunities"]==0
    assert result["coverage"]["shadow_evaluation_coverage"]==0


@pytest.mark.parametrize("field,value,reason", [
    ("status", "BLOCKED_STALE_DATA", "ACTIVE_BLOCKED_STALE_DATA"),
    ("status", "ACTIVE_PAUSED", "ACTIVE_ACTIVE_PAUSED"),
    ("status", "EVALUATION_ERROR", "ACTIVE_EVALUATION_ERROR"),
    ("data_freshness", "STALE", "ACTIVE_FRESHNESS_UNVERIFIED"),
    ("finalized", False, "ACTIVE_FINALITY_UNVERIFIED"),
    ("htf_alignment", "UNKNOWN", "ACTIVE_HTF_UNVERIFIED"),
    ("lookback_status", "UNKNOWN", "ACTIVE_LOOKBACK_UNVERIFIED"),
    ("config_fingerprint", None, "ACTIVE_CONFIGURATION_UNVERIFIED"),
    ("observation_authorized", False, "ACTIVE_AUTHORIZATION_UNVERIFIED"),
    ("evidence_completeness", "PARTIAL", "ACTIVE_EVIDENCE_INCOMPLETE"),
    ("venue", "spot", "SCOPE_MISMATCH:venue"),
    ("tenant_id", "bob", "TENANT_MISMATCH"),
    ("evidence_kind", "FORWARD_SHADOW_DECISION", "ACTIVE_EVIDENCE_CATEGORY_MISMATCH"),
    ("source_evidence_kind", "HISTORICAL_BACKTEST", "ACTIVE_EVIDENCE_CATEGORY_MISMATCH"),
])
def test_invalid_pairs_are_excluded(field, value, reason):
    a = decision(); a[field] = value
    result = analyze([a], [decision()])
    assert result["metrics"]["matched_opportunities"] == 0
    assert reason in result["timeline"][0]["exclusion_reasons"]


def test_canonical_identity_not_representation_or_strategy_equality():
    a, s = decision(), decision()
    a.update(config_fingerprint="b" * 64, strategy_id="other", candle_hash="representation-A")
    s["candle_hash"] = "representation-B"
    a["canonical_candle"]["ohlcv"] = [10.0, 12.0, 9.0, 11.0, 100.0]
    a["canonical_candle"]["open_timestamp"] = a["canonical_candle"]["open_timestamp"].replace("+00:00", "Z")
    assert analyze([a], [s])["metrics"]["matched_opportunities"] == 1
    a["canonical_candle"]["ohlcv"][3] = 11.01
    assert "CANDLE_DATA_CONFLICT" in analyze([a], [s])["timeline"][0]["exclusion_reasons"]


def test_hash_without_verifiable_canonical_data_is_unknown():
    a = decision(); a.pop("canonical_candle"); a["candle_hash"] = "a" * 64
    assert "CANDLE_IDENTITY_UNVERIFIED" in analyze([a], [decision()])["timeline"][0]["exclusion_reasons"]


def test_conflicting_active_decisions_not_arbitrarily_selected():
    a = decision(); other = {**deepcopy(a), "status": "SIGNAL_LONG", "decision_id": "different"}
    result = analyze([a, other], [decision()])
    assert result["metrics"]["matched_opportunities"] == 0
    assert "CONFLICTING_ACTIVE_DECISIONS" in result["timeline"][0]["exclusion_reasons"]


def test_zero_and_missing_shadow_opportunities_are_explicit():
    result = compare_decisions([], [], owner_id="alice", start=T, end=T + timedelta(minutes=10))
    assert result["coverage"]["expected_scheduled_opportunities"] == 2
    assert result["coverage"]["missing_shadow_opportunities"] == 2
    assert result["coverage"]["matched_comparison_coverage"] == 0
    assert result["metrics"]["all_eligible_agreement"] is None


def test_pa_read_only_adapter_preserves_original_decision_not_execution_state(tmp_path):
    path = tmp_path / "pa.sqlite"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE pa_sessions(id TEXT, mode TEXT, symbol TEXT, timeframe TEXT, status TEXT); CREATE TABLE pa_evaluations(correlation_id TEXT,session_id TEXT,candle_time TEXT,symbol TEXT,timeframe TEXT,strategy_id TEXT,strategy_version TEXT,state TEXT,payload_json TEXT,created_at TEXT,missing_conditions_json TEXT,reason TEXT);")
    db.execute("INSERT INTO pa_sessions VALUES ('s','LIVE_PAPER','TESTUSDT','5m','active')")
    p = {"closed_candle": {"timestamp": (T-timedelta(minutes=5)).isoformat(), "open": 10,"high":12,"low":9,"close":11,"volume":100}, "proposal_ids": [], "feed_state": "SYNCHRONIZED"}
    db.execute("INSERT INTO pa_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("d", "s", p["closed_candle"]["timestamp"], "TESTUSDT","5m","PA1_SR_REJECTION","1.1.0","ENTERED",json.dumps(p),T.isoformat(),"[]","executed later")); db.commit()
    before = db.execute("SELECT * FROM pa_evaluations").fetchall()
    sources = ActiveDecisionSources(pa_path=path, lab_owner="alice")
    desc, rows = sources.read("pa-lab:s", owner_id="alice", start=T, end=T+timedelta(seconds=1))
    assert desc["coverage_class"] == "PARTIAL_DECISION_EVIDENCE"
    assert rows[0]["status"] == "NO_SIGNAL"
    assert rows[0]["config_fingerprint"] is None
    assert rows[0]["candle_close_timestamp"] == T.isoformat()
    assert sources.sources(owner_id="bob") == []
    with pytest.raises(KeyError): sources.read("pa-lab:s", owner_id="bob", start=T, end=T+timedelta(seconds=1))
    assert db.execute("SELECT * FROM pa_evaluations").fetchall() == before
    with sources.connection(path) as read:
        with pytest.raises(sqlite3.OperationalError): read.execute("DELETE FROM pa_evaluations")
    db.close()


def test_unknown_fields_not_backfilled_or_direction_inferred():
    r = normalize_active({"strategy_id": "test", "status": "FILLED"}, source_id="fixture", owner_id="alice")
    assert r["status"] == "UNKNOWN"
    assert r["config_fingerprint"] is None
    assert r["evidence_completeness"] == "PARTIAL"
    assert r["candle_close_timestamp"] is None


@pytest.mark.parametrize("state,direction,missing,expected",[("ENTRY_READY","bullish",[],"SIGNAL_LONG"),("ENTRY_READY","bearish",[],"SIGNAL_SHORT"),("PARKED",None,[],"UNKNOWN"),("WATCHING",None,["native closed-bar context"],"BLOCKED_MISSING_CONTEXT")])
def test_smc_consumes_original_native_report_without_invoking_engine(state,direction,missing,expected):
    original={"state":state,"proposal":{"direction":direction},"trade_plan":{"entry":10},"missing_conditions":missing}
    row={"correlation_id":"smc-original","candle_time":(T-timedelta(minutes=5)).isoformat(),"timeframe":"5m","created_at":T.isoformat(),"payload_json":json.dumps({"source_evaluation":original,"feed_state":"SYNCHRONIZED"})}
    result=ActiveDecisionSources._normalize(row,"smc-lab","smc-lab:session","alice")
    assert result["status"]==expected
    assert result["evidence_completeness"]=="PARTIAL"


def test_reliability_uses_host_commit_time_not_exchange_receipt(setup):
    router,store,_,reg,clock,_=setup
    d=router.evaluate(reg["registration_id"],frame(),owner_id="alice")
    service=ShadowComparisonService(store,source_reader=lambda *a,**kw:({"coverage_class":"FULL_DECISION_EVIDENCE"},[{**d,"evidence_kind":"ACTIVE_DECISION_OBSERVATION"}]),clock=router.clock)
    result=service.create({"registration_id":reg["registration_id"],"active_source_id":"fixture","start":T.isoformat(),"end":clock[0].isoformat(),"idempotency_key":"latency-fixture"},owner_id="alice")
    assert result["reliability"]["shadow_close_to_commit_ms"]["mean"]==1000
    assert result["reliability"]["shadow_evaluation_ms"]["sample_count"]==1
    assert result["reliability"]["exchange_to_system_latency_ms"] is None


def test_artifact_idempotency_immutability_and_source_revision(setup):
    router, store, raw, reg, clock, _ = setup
    shadow = router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    source = {"source_id": "fixture", "coverage_class": "FULL_DECISION_EVIDENCE"}
    active = [{**deepcopy(shadow), "decision_id": "active-1", "evidence_kind":"ACTIVE_DECISION_OBSERVATION"}]
    service = ShadowComparisonService(store, source_reader=lambda *a, **kw: (source, deepcopy(active)), clock=router.clock)
    request = {"registration_id": reg["registration_id"], "active_source_id": "fixture", "start": T.isoformat(), "end": clock[0].isoformat(), "idempotency_key": "comparison-key"}
    first = service.create(request, owner_id="alice")
    assert first["metrics"]["matched_opportunities"] == 1
    active[0]["status"] = "NO_SIGNAL"
    assert service.create(request, owner_id="alice") == first
    second = service.create({**request, "idempotency_key": "new-revision-key"}, owner_id="alice")
    assert second["comparison_id"] != first["comparison_id"]
    assert service.get(first["comparison_id"], owner_id="bob") is None
    with pytest.raises(sqlite3.IntegrityError): raw._db.execute("UPDATE shadow_comparison_artifacts SET payload_json='{}'")
    assert len(service.list(owner_id="alice")) == 2


def test_simultaneous_comparison_creation_one_logical_artifact(setup):
    router, store, raw, reg, clock, _ = setup
    d = router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    service = ShadowComparisonService(store, source_reader=lambda *a, **kw: ({"source_id":"fixture","coverage_class":"FULL_DECISION_EVIDENCE"},[{**deepcopy(d),"evidence_kind":"ACTIVE_DECISION_OBSERVATION"}]), clock=router.clock)
    r = {"registration_id":reg["registration_id"],"active_source_id":"fixture","start":T.isoformat(),"end":clock[0].isoformat(),"idempotency_key":"racing-comparison"}
    with ThreadPoolExecutor(2) as pool: results = list(pool.map(lambda _: service.create(r, owner_id="alice"), range(2)))
    assert results[0] == results[1]
    assert len(service.list(owner_id="alice")) == 1


def test_comparison_creation_races_across_independent_connections(setup):
    from threading import Barrier
    from services.shadow_observation_store import ShadowObservationStore
    from services.shadow_research import ShadowResearchStore
    router,store,raw,reg,clock,_=setup
    d=router.evaluate(reg["registration_id"],frame(),owner_id="alice")
    other=ShadowObservationStore(ShadowResearchStore(raw.path))
    barrier=Barrier(2)
    def read(*args,**kw):
        barrier.wait(timeout=10)
        return {"coverage_class":"FULL_DECISION_EVIDENCE"},[{**deepcopy(d),"evidence_kind":"ACTIVE_DECISION_OBSERVATION"}]
    services=[ShadowComparisonService(s,source_reader=read,clock=router.clock) for s in (store,other)]
    request={"registration_id":reg["registration_id"],"active_source_id":"fixture","start":T.isoformat(),"end":clock[0].isoformat(),"idempotency_key":"independent-race"}
    try:
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda s:s.create(request,owner_id="alice"),services))
        assert results[0]==results[1]
        assert len(store.comparison_artifacts(owner_id="alice"))==1
        assert raw._db.execute("SELECT COUNT(*) FROM shadow_comparison_requests").fetchone()[0]==1
        # The exact immutable inputs reproduce the decision calculation even
        # when upstream active records later change.
        artifact=results[0]
        reproduced=compare_decisions(artifact["inputs"]["active"],artifact["inputs"]["shadow"],owner_id="alice",start=artifact["start"],end=artifact["end"])
        assert reproduced["metrics"]==artifact["metrics"]
        assert reproduced["timeline"]==artifact["timeline"]
    finally: other._store._db.close()


@pytest.mark.parametrize("payload",["{invalid-json","[]",'{"closed_candle":5}', '{"source_evaluation":5}'])
def test_malformed_active_evidence_fails_closed_without_execution(payload):
    row={"correlation_id":"bad-source","payload_json":payload,"timeframe":"5m","candle_time":T.isoformat()}
    r=ActiveDecisionSources._normalize(row,"smc-lab","smc-lab:s","alice")
    assert r["status"]=="EVALUATION_ERROR"
    assert r["candle_close_timestamp"]==(T+timedelta(minutes=5)).isoformat()
    assert r["evidence_completeness"]=="PARTIAL"
    assert "INVALID_PERSISTED_DECISION_PAYLOAD" in r["limitations"]


@pytest.mark.parametrize("field",["finalized","observation_authorized"])
def test_integer_is_not_boolean_attestation(field):
    a=decision();a[field]=1
    assert analyze([a],[decision()])["metrics"]["matched_opportunities"]==0


def test_observation_records_clock_boundaries_without_inventing_exchange_latency(setup):
    router, _, _, reg, _, _ = setup
    d = router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    assert d["canonical_candle"]
    assert d["evaluation_started_at"] and d["evaluation_completed_at"] and d["evidence_commit_recorded_at"]
    assert d["data_receipt_timestamp"] is None
    assert d["exchange_to_system_latency_ms"] is None


def test_later_conflict_withholds_metrics_and_marks_timeline_excluded(setup):
    router,store,_,reg,clock,_ = setup
    d = router.evaluate(reg["registration_id"],frame(),owner_id="alice")
    store.event(reg["registration_id"],owner_id="alice",action="CANDLE_REVISION_CONFLICT",payload={"original_decision_id":d["decision_id"]})
    service = ShadowComparisonService(store,source_reader=lambda *a,**kw:({"coverage_class":"FULL_DECISION_EVIDENCE"},[{**d,"evidence_kind":"ACTIVE_DECISION_OBSERVATION"}]),clock=router.clock)
    r = service.create({"registration_id":reg["registration_id"],"active_source_id":"fixture","start":T.isoformat(),"end":clock[0].isoformat(),"idempotency_key":"conflict-fixture"},owner_id="alice")
    assert r["eligibility"] == "NOT_COMPARABLE"
    assert r["timeline"][0]["eligible"] is False
    assert r["metrics"]["excluded_opportunities"] == 1
    assert r["coverage"]["matched_comparison_coverage"] == 0


def test_stale_finalized_candle_counts_market_data_not_eligible_decisions(setup):
    router,store,_,reg,clock,_ = setup
    market = frame(); market["connected"] = False
    d = router.evaluate(reg["registration_id"],market,owner_id="alice")
    r = compare_decisions([], [d], owner_id="alice", start=T, end=clock[0],timeframe="5m")
    assert r["coverage"]["market_data_coverage"] == 1
    assert r["coverage"]["shadow_evaluation_coverage"] == 0


def test_config_changes_do_not_merge_active_cohorts():
    a = [decision(index=i) for i in range(2)]; a[1]["config_fingerprint"] = "b"*64
    r = analyze(a,[decision(index=i) for i in range(2)])
    assert r["metrics"]["matched_opportunities"] == 0
    assert "ACTIVE_CONFIGURATION_CHANGED_WITHIN_WINDOW" in r["timeline"][0]["exclusion_reasons"]


@pytest.mark.parametrize("start,end", [(T,T), (T,T-timedelta(seconds=1)), (T,T+timedelta(days=8))])
def test_invalid_observation_windows_rejected(start,end):
    with pytest.raises(ValueError,match="INVALID_OBSERVATION_WINDOW"):
        compare_decisions([],[],owner_id="alice",start=start,end=end)


def test_actual_financial_state_and_execution_calls_unchanged(setup,tmp_path,monkeypatch):
    from test_strategy_evidence_runtime import runtime
    ledger,journal,decisions,paper,pipeline,_ = runtime(tmp_path / "financial")
    before = (ledger.get_authoritative_evidence_snapshot(),journal.list(),decisions.list(),paper.pending_intents(),paper.equity)
    calls = []
    monkeypatch.setattr(pipeline,"process",lambda *a,**kw:calls.append("order"))
    monkeypatch.setattr(paper,"process_quote",lambda *a,**kw:calls.append("fill"))
    router,store,_,reg,clock,_ = setup
    d = router.evaluate(reg["registration_id"],frame(),owner_id="alice")
    service = ShadowComparisonService(store,source_reader=lambda *a,**kw:({"coverage_class":"PARTIAL_DECISION_EVIDENCE"},[]),clock=router.clock)
    service.create({"registration_id":reg["registration_id"],"active_source_id":"fixture","start":T.isoformat(),"end":clock[0].isoformat(),"idempotency_key":"financial-fixture"},owner_id="alice")
    router.action(reg["registration_id"],"PAUSE",owner_id="alice",reason="pause")
    router.action(reg["registration_id"],"RESUME",owner_id="alice",reason="resume")
    assert before == (ledger.get_authoritative_evidence_snapshot(),journal.list(),decisions.list(),paper.pending_intents(),paper.equity)
    assert calls == []
