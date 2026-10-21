"""Contemporaneous evidence fixtures, never historical performance claims."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from bot.types import Bar
from services.native_price_action import NativePriceActionEngine, PriceActionConfig
from services.price_action_lab import PriceActionPaperAccount, PaperExecutionConfig
from services.active_evidence_capture import native_input, reconcile_active_evidence
from services.active_decision_evidence import ActiveDecisionSources
from services.shadow_comparison_service import compare_decisions
from test_shadow_router import setup,frame

T = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def history(tf_minutes, count, end=T):
    return [Bar(end-timedelta(minutes=tf_minutes*(count-i)), 100, 102, 99, 101, 100)
            for i in range(count)]


def evaluated_state():
    engine=NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT"))
    engine.set_native_mtf_context({"1h":history(60,60),"4h":history(240,55)})
    engine.ingest_closed_bars(history(5,400))
    state=engine.visual_state()
    state["active_evidence_input"]=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    return engine,state


@pytest.fixture
def captured(tmp_path,monkeypatch):
    import services.price_action_lab as lab
    monkeypatch.setattr(lab,"_iso",lambda:(T+timedelta(seconds=1)).isoformat())
    account=PriceActionPaperAccount(tmp_path/"pa.db",evidence_owner="alice")
    engine,state=evaluated_state()
    row=account.record_evaluation(state,engine.bars[-1],{"state":"SYNCHRONIZED","reliable":True})
    reconcile_active_evidence(account._db,"pa")
    reader=ActiveDecisionSources(pa_path=account.path,lab_owner="alice")
    source_id="pa-lab:"+account.session()["id"]
    return account,engine,state,row,reader,source_id


def read(captured):
    *_,reader,source_id=captured
    return reader.read(source_id,owner_id="alice",start=T,end=T+timedelta(minutes=5))


def test_actual_configuration_and_candle_reach_adapter(captured):
    descriptor,rows=read(captured)
    row=rows[0]
    assert descriptor["coverage_class"]=="FULL_DECISION_EVIDENCE"
    assert row["strategy_version"]=="1.1.0"
    assert row["evidence_completeness"]=="COMPLETE"
    assert row["status"]=="NO_SIGNAL"
    assert row["configuration_snapshot"]["native_config"]["zone_expiry_bars"]==300
    assert row["canonical_candle"]["ohlcv"]==[100,102,99,101,100]
    assert row["context_hash"] and row["htf_alignment"]=="VERIFIED"


def test_later_settings_do_not_rewrite_earlier_fingerprint(captured):
    account=captured[0]
    before=read(captured)[1][0]
    account.configure(execution_config=PaperExecutionConfig(operating_mode="signals_only"))
    after=read(captured)[1][0]
    assert before==after


def test_projection_outage_recovers_original_receipt(captured):
    account,engine,state,_,reader,source_id=captured
    account._db.set_authorizer(lambda action,table,*_: sqlite3.SQLITE_DENY
        if action==sqlite3.SQLITE_INSERT and table=="active_decision_receipts" else sqlite3.SQLITE_OK)
    bar=history(5,1,end=T+timedelta(minutes=5))[0]
    engine.process_closed_bar(bar)
    state=engine.visual_state();state["active_evidence_input"]=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    original=account.record_evaluation(state,bar,{"state":"SYNCHRONIZED","reliable":True})
    assert reconcile_active_evidence(account._db,"pa")["backlog"]==1
    account._db.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
    report=reconcile_active_evidence(account._db,"pa")
    assert report["recovered"]==1
    assert reconcile_active_evidence(account._db,"pa")["recovered"]==0
    assert account._db.execute("SELECT COUNT(*) FROM active_decision_receipts WHERE decision_id=?",(original["correlation_id"],)).fetchone()[0]==1


def test_mutable_lifecycle_cannot_change_capture(captured):
    account,_,_,original,*_=captured
    account._advance_evaluation(original["correlation_id"],"ENTERED","existing lifecycle")
    assert read(captured)[1][0]["status"]=="NO_SIGNAL"
    payload=json.loads(original["payload_json"])
    payload["active_evidence"]["config_fingerprint"]="f"*64
    with pytest.raises(sqlite3.IntegrityError,match="immutable"):
        account._db.execute("UPDATE pa_evaluations SET payload_json=? WHERE correlation_id=?",(json.dumps(payload),original["correlation_id"]))
    with pytest.raises(sqlite3.IntegrityError,match="immutable"):
        account._db.execute("DELETE FROM active_decision_receipts")


def test_complete_source_can_activate_existing_comparison(captured):
    a=read(captured)[1][0]
    s={**deepcopy(a),"evidence_kind":"FORWARD_SHADOW_DECISION","config_fingerprint":"b"*64}
    result=compare_decisions([a],[s],owner_id="alice",start=T,end=T+timedelta(minutes=5))
    assert result["eligibility"]=="COMPARABLE"
    assert result["metrics"]["both_no_signal"]==1
    assert result["metrics"]["signal_relevant_agreement"] is None
    s["canonical_candle"]["ohlcv"][3]=100
    invalid=compare_decisions([a],[s],owner_id="alice",start=T,end=T+timedelta(minutes=5))
    assert invalid["eligibility"]=="NOT_COMPARABLE"
    assert "CANDLE_DATA_CONFLICT" in invalid["timeline"][0]["exclusion_reasons"]


def test_historical_missing_capture_cannot_gain_current_settings(captured):
    account,_,state,_,reader,source_id=captured
    state.pop("active_evidence_input")
    old=account.record_evaluation(state,history(5,1,end=T-timedelta(minutes=5))[0],{"state":"SYNCHRONIZED"})
    before=reader.read(source_id,owner_id="alice",start=T-timedelta(minutes=5),end=T)[1][0]
    account.configure(execution_config=PaperExecutionConfig(operating_mode="signals_only"))
    after=reader.read(source_id,owner_id="alice",start=T-timedelta(minutes=5),end=T)[1][0]
    assert before==after
    assert after["config_fingerprint"] is None
    assert after["evidence_completeness"]=="PARTIAL"


def test_owner_is_not_inferred_from_another_tenant(captured):
    assert captured[-2].sources(owner_id="bob")==[]
    with pytest.raises(KeyError):
        captured[-2].read(captured[-1],owner_id="bob",start=T,end=T+timedelta(minutes=5))


def test_window_health_does_not_certify_missing_opportunities(captured):
    health=captured[-2].health(captured[-1],owner_id="alice",start=T,end=T+timedelta(minutes=10))
    assert health["status"]=="PARTIAL"
    assert health["expected_opportunities"]==2
    assert health["missing_decisions"]==1
    assert health["configuration_completeness"]==1
    assert health["active_decision_coverage"]==.5
    assert health["field_matrix"]["session"]=="UNKNOWN"
    assert "session" in health["optional_fields"]


def test_verified_window_has_real_configuration_health(captured):
    health=captured[-2].health(captured[-1],owner_id="alice",start=T,end=T+timedelta(minutes=5))
    assert health["status"]=="VERIFIED"
    assert health["comparison_readiness"]=="ACTIVE_EVIDENCE_READY"
    assert health["last_verified_observation"]==T.isoformat()
    assert health["field_matrix"]["config_fingerprint"]=="PRESENT_VERIFIED"
    assert health["field_matrix"]["canonical_candle"]=="PRESENT_VERIFIED"
    assert health["profitability"]=="NOT_ASSESSED"


def test_duplicate_opportunity_cannot_display_verified_readiness(captured):
    account=captured[0]
    duplicate=dict(account._db.execute("SELECT * FROM pa_evaluations LIMIT 1").fetchone())
    payload=json.loads(duplicate["payload_json"])
    duplicate.update(correlation_id="duplicate-source-opportunity",idempotency_key="duplicate-source-opportunity")
    payload["active_evidence"]["decision_id"]=duplicate["correlation_id"]
    duplicate["payload_json"]=json.dumps(payload)
    account._db.execute("INSERT INTO pa_evaluations VALUES ("+",".join("?" for _ in duplicate)+")",tuple(duplicate.values()))
    reconcile_active_evidence(account._db,"pa")
    health=captured[-2].health(captured[-1],owner_id="alice",start=T,end=T+timedelta(minutes=5))
    assert health["duplicate_evidence_count"]==1
    assert health["blocking_reasons"]["DUPLICATE_ACTIVE_OPPORTUNITY"]==1
    assert health["status"]!="VERIFIED"
    assert health["comparison_readiness"]=="INCOMPLETE_ACTIVE_EVIDENCE"


def test_native_smc_capture_preserves_rules_and_financial_state(tmp_path,monkeypatch):
    from services.native_smc import SMCMarketStructureEngine,SMCConfig
    from services.smc_strategy_v1 import evaluate,STRATEGY_VERSION
    from services.smc_strategy_lab import SMCPaperAccount
    import services.smc_strategy_lab as lab
    monkeypatch.setattr(lab,"_iso",lambda:(T+timedelta(seconds=1)).isoformat())
    engine=SMCMarketStructureEngine(SMCConfig(symbol="BTCUSDT"))
    engine.set_native_mtf_context({"1h":history(60,60),"4h":history(240,55)})
    for bar in history(5,400): engine.process_closed_bar(bar)
    from services.mtf_policy import evidence_at
    report=evaluate(engine,mtf_evidence=evidence_at("BTCUSDT","5m",engine._native_mtf_context,T))
    original=deepcopy(report)
    frozen_state=deepcopy(engine.checkpoint())
    report["active_evidence_input"]=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    assert engine.checkpoint()==frozen_state
    assert {k:v for k,v in report.items() if k!="active_evidence_input"}==original
    account=SMCPaperAccount(tmp_path/"smc.db",evidence_owner="alice")
    before=financial(account)
    monkeypatch.setattr(account.broker,"submit",lambda **kw:pytest.fail("evidence placed an order"))
    account.record_evaluation(report,candle_time=engine.bars[-1].timestamp.isoformat(),feed_status={"state":"SYNCHRONIZED","reliable":True})
    reconcile_active_evidence(account._db,"smc")
    reader=ActiveDecisionSources(smc_path=account.path,lab_owner="alice")
    desc,rows=reader.read("smc-lab:"+account.session()["id"],owner_id="alice",start=T,end=T+timedelta(minutes=5))
    assert desc["coverage_class"]=="FULL_DECISION_EVIDENCE"
    assert rows[0]["strategy_version"]==STRATEGY_VERSION
    assert rows[0]["evidence_completeness"]=="COMPLETE"
    assert rows[0]["configuration_snapshot"]["native_config"]["swing_pivot_length"]==50
    assert rows[0]["market_context"]["session"]==engine.snapshots[engine.bars[-1].timestamp].session
    assert before==financial(account)


def financial(account):
    names=[r[0] for r in account._db.execute("SELECT name FROM sqlite_master WHERE type='table'")
           if r[0].startswith("v2_") or "journal" in r[0]]
    return {name:[tuple(r) for r in account._db.execute('SELECT * FROM "'+name+'" ORDER BY rowid')] for name in names}


@pytest.mark.parametrize("mutation,error",[("gap","DATA_GAP:5m"),("duplicate","DUPLICATE_CANDLE:5m"),("stale_htf","MISSING_OR_STALE_CONTEXT:1h"),("warmup","INSUFFICIENT_HISTORY:5m")])
def test_bad_native_context_blocks_attestation(mutation,error):
    engine,_=evaluated_state()
    if mutation=="gap": engine.bars.pop(-5)
    elif mutation=="duplicate": engine.bars.insert(-5,engine.bars[-5])
    elif mutation=="stale_htf": engine._native_mtf_context["1h"].pop()
    else: engine.bars=engine.bars[-25:]
    evidence=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    assert error in evidence["errors"]
    assert evidence["lookback_status"]=="UNKNOWN"


@pytest.mark.parametrize("implementation,parameter,value,minimum",[
    ("pa","confusion_candles",600,602),("pa","entry_expiry_bars",500,502),
    ("smc","internal_pivot_length",300,601),("smc","sweep_lookback",700,701)])
def test_nondefault_dependencies_expand_reviewed_lookback(implementation,parameter,value,minimum):
    from dataclasses import replace
    if implementation=="pa":
        engine,_=evaluated_state()
    else:
        from services.native_smc import SMCMarketStructureEngine,SMCConfig
        engine=SMCMarketStructureEngine(SMCConfig(symbol="BTCUSDT"))
        engine.set_native_mtf_context({"1h":history(60,60),"4h":history(240,55)})
        for bar in history(5,400): engine.process_closed_bar(bar)
    engine.config=replace(engine.config,**{parameter:value})
    captured=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    assert captured["required_candles"]["5m"]>=minimum
    assert "INSUFFICIENT_HISTORY:5m" in captured["errors"]
    assert captured["lookback_status"]=="UNKNOWN"


def test_future_htf_changes_cannot_change_earlier_input():
    engine,_=evaluated_state()
    before=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    engine._native_mtf_context["1h"].append(Bar(T,100,200,50,150,100))
    after=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    assert before==after


def test_optional_context_absence_does_not_become_required():
    engine,_=evaluated_state();engine._native_mtf_context["4h"]=[]
    evidence=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    assert evidence["errors"]==[]
    assert evidence["htf_alignment"]=="VERIFIED"


def test_entry_capture_never_copies_unbounded_live_history():
    engine,_=evaluated_state()
    class BoundedAccess(list):
        def __iter__(self):
            pytest.fail("capture iterated entire live engine history")
    engine.bars=BoundedAccess(history(5,2500))
    captured=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    assert len(captured["context_material"]["5m"])==1500
    assert captured["canonical_candle"]["open_timestamp"]==(T-timedelta(minutes=5)).isoformat()


def test_actual_decision_htf_evidence_must_match_captured_native_input(captured):
    account,engine,state,*_=captured
    bar=history(5,1,end=T+timedelta(minutes=5))[0]
    engine.process_closed_bar(bar)
    changed=engine.visual_state()
    changed["active_evidence_input"]=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    changed["mtf_evidence"]["primary"]["htf_bias"]="BULLISH"
    original_proposals=deepcopy(changed["proposals"])
    row=account.record_evaluation(changed,bar,{"state":"SYNCHRONIZED","reliable":True})
    capture=json.loads(row["payload_json"])["active_evidence"]
    assert "DECISION_HTF_CONTEXT_CONFLICT" in capture["errors"]
    assert capture["evidence_completeness"]=="PARTIAL"
    assert changed["proposals"]==original_proposals


def test_unavailable_native_verification_does_not_invent_context_conflict(captured):
    from services.active_evidence_capture import lab_capture
    account,engine,state,*_=captured
    native=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    native["native_mtf_material"]=None
    native["errors"]=["INSUFFICIENT_HISTORY:5m"]
    capture=lab_capture(native,proposals=state["proposals"],decision_mtf_evidence=state["mtf_evidence"],feed_status={"state":"SYNCHRONIZED","reliable":True},owner="alice",session=account.session(),strategy_id="PA1_SR_REJECTION",version="1.1.0",execution_config=deepcopy(read(captured)[1][0]["configuration_snapshot"]["execution_config"]),missing=[],reasons=[],decision_id="unknown-context",recorded_at=(T+timedelta(seconds=1)).isoformat())
    assert "DECISION_HTF_CONTEXT_UNVERIFIED" in capture["errors"]
    assert "DECISION_HTF_CONTEXT_CONFLICT" not in capture["errors"]
    assert capture["evidence_completeness"]=="PARTIAL"


def test_smc_concurrent_context_change_cannot_attest_decision(tmp_path,monkeypatch):
    from services.native_smc_live_visual import live_visual_state
    import services.smc_strategy_v1 as strategy
    from services.mtf_policy import evidence_at
    context={"1h":history(60,60),"4h":history(240,55)}
    original=strategy.evaluate
    reports=[]
    def evaluate_then_change_context(engine,*args,**kwargs):
        report=original(engine,*args,**kwargs)
        reports.append(deepcopy(report))
        engine.set_native_mtf_context(context)
        return report
    monkeypatch.setattr(strategy,"evaluate",evaluate_then_change_context)
    state=live_visual_state("BTCUSDT","5m","binance_usdm",now=T+timedelta(seconds=1),
        fetcher=lambda *args,**kwargs:history(5,400),mtf_context=context,
        mtf_evidence=evidence_at("BTCUSDT","5m",context,T))
    report=state["source_strategy"]
    assert {k:v for k,v in report.items() if k!="active_evidence_input"}==reports[0]
    assert "EVALUATOR_STATE_CHANGED_DURING_DECISION" in report["active_evidence_input"]["errors"]
    assert report["active_evidence_input"]["htf_alignment"]=="UNKNOWN"


def test_source_configuration_change_produces_new_fingerprint(captured):
    from dataclasses import replace
    from services.active_evidence_capture import decision_capture
    account,engine,_,_,*_=captured
    first=read(captured)[1][0]["config_fingerprint"]
    engine.config=replace(engine.config,zone_expiry_bars=250)
    native=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    observed=decision_capture(native,owner="alice",session=account.session(),strategy_id="PA1_SR_REJECTION",version="1.1.0",execution_config=deepcopy(read(captured)[1][0]["configuration_snapshot"]["execution_config"]),status="NO_SIGNAL",direction=None,missing=[],reasons=[],decision_id="new-source-config",recorded_at=(T+timedelta(seconds=1)).isoformat())
    assert observed["config_fingerprint"]!=first


def test_duplicate_delivery_and_restart_do_not_duplicate_receipts(captured):
    account,engine,state,row,*_=captured
    before=financial(account)
    for _ in range(3):
        assert account.record_evaluation(state,engine.bars[-1],{"state":"SYNCHRONIZED","reliable":True})["correlation_id"]==row["correlation_id"]
    assert financial(account)==before
    reopened=PriceActionPaperAccount(account.path,evidence_owner="alice")
    assert reopened._db.execute("SELECT COUNT(*) FROM active_decision_receipts").fetchone()[0]==1
    assert financial(reopened)==before


def test_capture_failure_does_not_fail_original_decision(captured):
    account,engine,state,*_=captured
    bar=history(5,1,end=T+timedelta(minutes=5))[0]
    state=deepcopy(state);state["active_evidence_input"]="invalid untrusted metadata"
    row=account.record_evaluation(state,bar,{"state":"SYNCHRONIZED","reliable":True})
    assert row["state"]=="WATCHING"
    assert json.loads(row["payload_json"])["active_evidence"]["errors"]==["DECISION_CAPTURE_UNAVAILABLE"]


def test_supersession_is_append_only_and_excludes_prior_evidence(captured):
    from services.active_evidence_capture import supersede_active_receipt
    account,engine,state,row,*_=captured
    second=account.record_evaluation(state,history(5,1,end=T+timedelta(minutes=5))[0],{"state":"SYNCHRONIZED","reliable":True})
    reconcile_active_evidence(account._db,"pa")
    event=supersede_active_receipt(account._db,decision_id=row["correlation_id"],replacement_reference=second["correlation_id"],owner="alice",actor="human-reviewer",reason="explicitly invalidate original observation",event_id="review-event-001")
    assert event["decision_id"]==row["correlation_id"]
    assert read(captured)[1][0]["status"]=="UNKNOWN"
    assert read(captured)[1][0]["limitations"]==["ACTIVE_EVIDENCE_SUPERSEDED"]
    with pytest.raises(sqlite3.IntegrityError): account._db.execute("UPDATE active_evidence_supersessions SET reason='silent rewrite'")


def test_current_timeframe_cannot_relabel_old_evidence(captured):
    account=captured[0]
    before=read(captured)[1][0]
    account.configure(timeframe="1h")
    assert read(captured)[1][0]==before
    assert captured[-2].health(captured[-1],owner_id="alice",start=T,end=T+timedelta(minutes=5))["expected_opportunities"]==1


def test_configuration_race_keeps_existing_outcome_but_blocks_verification(captured):
    account,engine,state,*_=captured
    bar=history(5,1,end=T+timedelta(minutes=5))[0]
    original=account.record_evaluation(state,bar,{"state":"SYNCHRONIZED","reliable":True},
        evidence_execution_config=PaperExecutionConfig(operating_mode="signals_only"))
    assert original["state"]=="WATCHING"
    frozen=json.loads(original["payload_json"])["active_evidence"]
    assert "CONFIGURATION_CHANGED_AT_DECISION_BOUNDARY" in frozen["errors"]
    assert frozen["evidence_completeness"]=="PARTIAL"
    assert frozen["configuration_snapshot"]["execution_config"]["operating_mode"]=="signals_only"


def test_restart_recovers_receipt_without_new_financial_writes(captured):
    account,engine,state,*_=captured
    next_bar=history(5,1,end=T+timedelta(minutes=5))[0]
    account.record_evaluation(state,next_bar,{"state":"SYNCHRONIZED","reliable":True})
    before=financial(account)
    reopened=PriceActionPaperAccount(account.path,evidence_owner="alice")
    assert reopened._db.execute("SELECT COUNT(*) FROM active_decision_receipts").fetchone()[0]==2
    assert financial(reopened)==before
    assert reconcile_active_evidence(reopened._db,"pa")["backlog"]==0


def test_partial_receipt_cannot_be_used_as_completed_evidence(captured):
    account=captured[0]
    # Clear only the optional projection by preventing the next insert; source
    # capture still commits and is visible as pending, never COMPLETE.
    state=deepcopy(captured[2])
    bar=history(5,1,end=T+timedelta(minutes=5))[0]
    account.record_evaluation(state,bar,{"state":"SYNCHRONIZED","reliable":True})
    _,rows=captured[-2].read(captured[-1],owner_id="alice",start=T+timedelta(minutes=5),end=T+timedelta(minutes=10))
    assert rows[0]["persistence_status"]=="PENDING_PROJECTION"
    assert rows[0]["evidence_completeness"]=="PARTIAL"
    assert rows[0]["config_fingerprint"] is None


def test_receipts_survive_existing_operational_factory_reset(captured):
    account=captured[0]
    old=read(captured)[1][0]
    account.factory_reset()
    assert read(captured)[1][0]==old
    assert account._db.execute("SELECT COUNT(*) FROM active_decision_receipts").fetchone()[0]==1


def test_two_reconcilers_commit_one_logical_receipt(captured):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    account,_,state,*_=captured
    new=account.record_evaluation(state,history(5,1,end=T+timedelta(minutes=5))[0],{"state":"SYNCHRONIZED","reliable":True})
    barrier=Barrier(2)
    def replay():
        db=sqlite3.connect(account.path,isolation_level=None,timeout=5);db.row_factory=sqlite3.Row
        try:
            barrier.wait();return reconcile_active_evidence(db,"pa")
        finally: db.close()
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(replay) for _ in range(2)]
        for f in futures: assert f.result()["backlog"]==0
    assert account._db.execute("SELECT COUNT(*) FROM active_decision_receipts WHERE decision_id=?",(new["correlation_id"],)).fetchone()[0]==1


@pytest.mark.parametrize("partial",[False,True])
def test_additive_migration_and_interruption_preserve_prior_rows(tmp_path,partial):
    from services.active_evidence_capture import install_active_evidence
    db=sqlite3.connect(tmp_path/"prior.db",isolation_level=None);db.row_factory=sqlite3.Row
    db.execute("CREATE TABLE pa_evaluations(correlation_id TEXT PRIMARY KEY,session_id TEXT,candle_time TEXT,symbol TEXT,timeframe TEXT,strategy_id TEXT,strategy_version TEXT,payload_json TEXT,created_at TEXT)")
    db.execute("INSERT INTO pa_evaluations VALUES ('legacy','s','2020-01-01T00:00:00+00:00','BTCUSDT','5m','PA1_SR_REJECTION','1.0','{}','2020-01-01T00:00:01+00:00')")
    before=[tuple(r) for r in db.execute("SELECT * FROM pa_evaluations")]
    if partial:
        db.set_authorizer(lambda action,table,*_:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_TABLE and table=="active_evidence_supersessions" else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError): install_active_evidence(db,"pa")
        db.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
    for _ in range(2): install_active_evidence(db,"pa")
    assert [tuple(r) for r in db.execute("SELECT * FROM pa_evaluations")]==before
    assert db.execute("SELECT COUNT(*) FROM active_decision_receipts").fetchone()[0]==0
    assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
    assert list(db.execute("PRAGMA foreign_key_check"))==[]
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='active_receipt_tenant_session_candle'").fetchone()
    with pytest.raises(sqlite3.IntegrityError,match="immutable"):
        db.execute("UPDATE pa_evaluations SET payload_json=?",(json.dumps({"active_evidence":{"config_fingerprint":"a"*64}}),))


def test_real_active_and_shadow_pipeline_commits_comparable_artifact(setup,tmp_path,monkeypatch):
    from services.shadow_comparison_service import ShadowComparisonService
    import services.price_action_lab as lab
    router,store,_,registration,clock,_=setup
    market=frame();shadow=router.evaluate(registration["registration_id"],market,owner_id="alice")
    monkeypatch.setattr(lab,"_iso",lambda:clock[0].isoformat())
    account=PriceActionPaperAccount(tmp_path/"matched-pa.db",evidence_owner="alice")
    account.configure(symbol="XRPUSDT")
    engine=NativePriceActionEngine(PriceActionConfig(symbol="XRPUSDT"))
    engine.set_native_mtf_context({tf:market["context"][tf] for tf in ("1h","4h")})
    entries=history(5,220,end=market["context"]["5m"][0].timestamp)+market["context"]["5m"]
    engine.ingest_closed_bars(entries)
    state=engine.visual_state();state["active_evidence_input"]=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    before=financial(account)
    monkeypatch.setattr(account.broker,"submit",lambda **kw:pytest.fail("comparison placed order"))
    original=account.record_evaluation(state,engine.bars[-1],{"state":"SYNCHRONIZED","reliable":True})
    reconcile_active_evidence(account._db,"pa")
    reader=ActiveDecisionSources(pa_path=account.path,lab_owner="alice")
    service=ShadowComparisonService(store,source_reader=reader.read,clock=router.clock)
    request={"registration_id":registration["registration_id"],"active_source_id":"pa-lab:"+account.session()["id"],"start":T.isoformat(),"end":clock[0].isoformat(),"idempotency_key":"real-matched-evidence-key"}
    artifact=service.create(request,owner_id="alice")
    assert artifact["eligibility"]=="COMPARABLE"
    assert artifact["inputs"]["active"][0]["decision_id"]==original["correlation_id"]
    assert artifact["inputs"]["shadow"][0]["decision_id"]==shadow["decision_id"]
    assert artifact["inputs"]["active"][0]["config_fingerprint"]!=registration["config_fingerprint"]
    assert artifact["metrics"]["matched_opportunities"]==1
    assert artifact["profitability"]=="NOT_ASSESSED"
    assert service.create(request,owner_id="alice")==artifact
    assert service.get(artifact["comparison_id"],owner_id="alice")==artifact
    assert financial(account)==before


def test_incomplete_configuration_cannot_get_authoritative_fingerprint(captured):
    from services.active_evidence_capture import decision_capture
    account,engine,*_=captured
    native=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    native["native_config"].pop("zone_expiry_bars")
    capture=decision_capture(native,owner="alice",session=account.session(),strategy_id="PA1_SR_REJECTION",version="1.1.0",execution_config=deepcopy(read(captured)[1][0]["configuration_snapshot"]["execution_config"]),status="NO_SIGNAL",direction=None,missing=[],reasons=[],decision_id="incomplete",recorded_at=(T+timedelta(seconds=1)).isoformat())
    assert capture.get("config_fingerprint") is None


@pytest.mark.parametrize("session_status,decision_status",[("paused","ACTIVE_PAUSED"),("ended","ACTIVE_STOPPED")])
def test_inactive_source_cannot_become_verified_neutral(captured,session_status,decision_status):
    from services.active_evidence_capture import decision_capture
    account,engine,*_=captured
    session={**account.session(),"status":session_status}
    capture=decision_capture(native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC"),owner="alice",session=session,strategy_id="PA1_SR_REJECTION",version="1.1.0",execution_config=deepcopy(read(captured)[1][0]["configuration_snapshot"]["execution_config"]),status="NO_SIGNAL",direction=None,missing=[],reasons=[],decision_id="inactive",recorded_at=(T+timedelta(seconds=1)).isoformat())
    assert capture["status"]==decision_status
    assert capture["observation_authorized"] is False
    assert capture["evidence_completeness"]=="PARTIAL"


def _crashing_producer(path):
    import os
    import services.price_action_lab as lab
    close=T+timedelta(minutes=5)
    lab._iso=lambda:(close+timedelta(seconds=1)).isoformat()
    child=PriceActionPaperAccount(path,evidence_owner="alice")
    engine,_=evaluated_state()
    bar=history(5,1,end=close)[0];engine.process_closed_bar(bar)
    state=engine.visual_state();state["active_evidence_input"]=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    child.record_evaluation(state,bar,{"state":"SYNCHRONIZED","reliable":True})
    os._exit(23)


def test_actual_process_exit_leaves_recoverable_source_only(captured):
    import multiprocessing
    account=captured[0]
    before=financial(account)
    # Spawn avoids inheriting connections or running threads from the suite.
    process=multiprocessing.get_context("spawn").Process(target=_crashing_producer,args=(account.path,))
    process.start();process.join(10)
    assert process.exitcode==23
    assert account._db.execute("SELECT COUNT(*) FROM active_decision_receipts").fetchone()[0]==1
    reopened=PriceActionPaperAccount(account.path,evidence_owner="alice")
    assert reopened._db.execute("SELECT COUNT(*) FROM active_decision_receipts").fetchone()[0]==2
    assert financial(reopened)==before


def test_prolonged_projection_outage_has_bounded_recovery(captured,monkeypatch):
    import services.price_action_lab as lab
    account,engine,*_=captured
    before=financial(account)
    for i in range(1,9):
        close=T+timedelta(minutes=5*i)
        monkeypatch.setattr(lab,"_iso",lambda close=close:(close+timedelta(seconds=1)).isoformat())
        bar=history(5,1,end=close)[0];engine.process_closed_bar(bar)
        state=engine.visual_state();state["active_evidence_input"]=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
        account.record_evaluation(state,bar,{"state":"SYNCHRONIZED","reliable":True})
    original=[tuple(r) for r in account._db.execute("SELECT * FROM pa_evaluations ORDER BY candle_time")]
    first=reconcile_active_evidence(account._db,"pa",limit=3)
    assert first=={"recovered":3,"backlog":5,"status":"RECOVERING"}
    assert reconcile_active_evidence(account._db,"pa",limit=3)["backlog"]==2
    assert reconcile_active_evidence(account._db,"pa",limit=3)["status"]=="COMPLETE"
    assert [tuple(r) for r in account._db.execute("SELECT * FROM pa_evaluations ORDER BY candle_time")]==original
    assert financial(account)==before


def test_malformed_historical_payload_does_not_block_valid_recovery(captured):
    account=captured[0]
    original=dict(account._db.execute("SELECT * FROM pa_evaluations LIMIT 1").fetchone())
    original.update(correlation_id="malformed-old",idempotency_key="malformed-old",payload_json="{invalid")
    account._db.execute("INSERT INTO pa_evaluations VALUES ("+",".join("?" for _ in original)+")",tuple(original.values()))
    assert reconcile_active_evidence(account._db,"pa")["status"]=="COMPLETE"
    descriptor,rows=read(captured)
    bad=next(r for r in rows if r["decision_id"]=="malformed-old")
    assert bad["status"]=="EVALUATION_ERROR"
    assert bad["config_fingerprint"] is None
    assert descriptor["coverage_class"]=="PARTIAL_DECISION_EVIDENCE"


def test_evidence_delta_preserves_all_other_original_lab_methods():
    import ast,hashlib
    from pathlib import Path
    from reviewed_acceptance_delta import reviewed_hash
    root=Path(__file__).parents[1]
    manifest=json.loads((root/"data/pr6_real_paper_freeze.json").read_text())
    delta=next(row for row in manifest["subsequent_approved_deltas"] if row.get("delta_id")=="sprint8.5-active-evidence.v1")
    assert delta["alpha_rules_changed"] is False
    assert delta["fill_timing_changed"] is False
    assert delta["prior_sha256"]=={
        "services/price_action_lab.py":"85b06d105e025769cffd0c2234862a6d90772ab70813b15713f3c1df15657d6d",
        "services/smc_strategy_lab.py":"009bd4f48caeb443adc9849b4708001e2414ad6119104c6916572f0169de1da1"}
    verified=0
    for relative,hashes in delta["unchanged_method_sha256"].items():
        text=(root/relative).read_text();actual={}
        for parent in ast.parse(text).body:
            if isinstance(parent,ast.ClassDef):
                for node in parent.body:
                    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                        actual[parent.name+"."+node.name]=ast.get_source_segment(text,node)
            elif isinstance(parent,(ast.FunctionDef,ast.AsyncFunctionDef)):
                actual[parent.name]=ast.get_source_segment(text,parent)
        assert set(actual)==set(hashes)|set(delta["changed_methods"][relative])
        subsequent=next((row for row in manifest['subsequent_approved_deltas'] if row.get('delta_id')=='sprint11-native-financial-evidence.v1'),None)
        for name,expected in hashes.items():
            if subsequent and name in subsequent['method_updates'].get(relative,{}):
                update=subsequent['method_updates'][relative][name]
                assert update['prior_sha256']==expected  # Original Sprint 8.5 proof remains intact.
                assert name in {'PriceActionPaperAccount._place_proposal','PriceActionPaperAccount.apply_funding_once',
                    'SMCPaperAccount.submit_order','SMCPaperAccount.apply_funding_once'}
                expected=update['sha256']
            expected=reviewed_hash(root,relative,expected,method=name)
            assert hashlib.sha256(actual[name].encode()).hexdigest()==expected, name
            verified+=1
    assert verified==113


def test_source_model_change_cannot_fingerprint_current_settings(captured):
    from services.active_evidence_capture import lab_capture
    from services.smc_strategy_lab import SMCPaperConfig
    from services.native_smc import SMCMarketStructureEngine,SMCConfig
    from services.smc_strategy_v1 import evaluate
    from services.mtf_policy import evidence_at
    from dataclasses import asdict
    account=captured[0]
    engine=SMCMarketStructureEngine(SMCConfig(symbol="BTCUSDT"))
    engine.set_native_mtf_context({"1h":history(60,60),"4h":history(240,55)})
    for bar in history(5,400): engine.process_closed_bar(bar)
    # A stale SMC source report must not inherit a later lab model selection,
    # even when complete contemporaneous native material is attached.
    native=native_input(engine,venue="binance_usdm",source="BINANCE_USDM_PUBLIC")
    report=evaluate(engine,mtf_evidence=evidence_at("BTCUSDT","5m",engine._native_mtf_context,T))
    policy={**asdict(SMCPaperConfig()),"model_id":"later-source-model"}
    snapshot=deepcopy(report)
    capture=lab_capture(native,report=report,feed_status={"state":"SYNCHRONIZED","reliable":True},owner="alice",session=account.session(),strategy_id=report["strategy_id"],version=report["version"],execution_config=policy,missing=[],reasons=[],decision_id="model-race",recorded_at=(T+timedelta(seconds=1)).isoformat())
    assert capture.get("config_fingerprint") is None
    assert capture["errors"]==["DECISION_MODEL_CONFIGURATION_UNVERIFIED"]
    assert report==snapshot
