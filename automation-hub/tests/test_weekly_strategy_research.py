"""Weekly research reports compare closed calendar weeks, with no auto-apply."""
from copy import deepcopy
import pytest
from services.weekly_strategy_research import build_weekly_review, completed_week
from test_strategy_decay import monitoring_inputs, AS_OF


def test_completed_week_uses_monday_utc_and_never_the_unfinished_week():
    assert completed_week("2025-04-09T12:00:00+00:00") == "2025-03-31"
    assert completed_week("2025-04-07T00:00:00+00:00") == "2025-03-31"
    assert completed_week("2025-04-07T00:30:00+01:00") == "2025-03-24"


def test_weekly_report_uses_half_open_periods_and_own_prior_samples(tmp_path, monkeypatch):
    *_, bound = monitoring_inputs(tmp_path, monkeypatch, count=240)
    before = deepcopy(bound)
    result = build_weekly_review(bound, week_start="2025-03-24", as_of=AS_OF)
    assert result["status"] == "COMPLETE"
    assert result["period"] == {"start":"2025-03-24T00:00:00+00:00", "end":"2025-03-31T00:00:00+00:00", "timezone":"UTC"}
    assert result["current"]["metrics"]["completed_episode_count"] == 84
    assert result["previous"]["metrics"]["completed_episode_count"] == 84
    assert not set(result["current"]["episode_ids"]) & set(result["previous"]["episode_ids"])
    assert result["observations"]["mae_r"]["status"] == "UNKNOWN"
    assert result["missed_opportunities"]["status"] == "UNKNOWN"
    assert result["orders_allowed"] is False and result["strategy_changes_allowed"] is False
    assert bound == before


def test_losing_clusters_create_only_evidence_linked_pending_proposals(tmp_path, monkeypatch):
    values=["9" if i%3 else "-9" for i in range(84)]+["-9"]*84+["9"]*72
    *_, bound = monitoring_inputs(tmp_path, monkeypatch, count=240, values=values)
    result = build_weekly_review(bound, week_start="2025-03-24", as_of=AS_OF)
    assert result["comparison"]["expectancy_change_r"] is not None
    assert result["losing_clusters"][0]["length"] == 84
    assert result["proposals"]
    for proposal in result["proposals"]:
        assert proposal["status"] == "PENDING_APPROVAL"
        assert set(proposal["episode_ids"]) <= set(result["review_episode_ids"])
        assert proposal["auto_apply"] is False


@pytest.mark.parametrize("start", ["2025-03-25", "2025-04-07", "2030-01-07", "bad"])
def test_rejects_non_monday_unfinished_or_invalid_week(tmp_path, monkeypatch,start):
    *_, bound = monitoring_inputs(tmp_path, monkeypatch)
    with pytest.raises(ValueError): build_weekly_review(bound,week_start=start,as_of=AS_OF)


def persisted_service(tmp_path,monkeypatch):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from services.strategy_intelligence_metrics import EvidenceCohort
    from test_historical_forward_evidence import SCOPE
    journal,service,filters,q,paper,rows,contexts,bound=monitoring_inputs(tmp_path,monkeypatch,count=240)
    value={**paper,"cache_key":service._cache_key(EvidenceCohort(**paper["cohort"]),["symbol"]),
           "scope":SCOPE,"input_watermark":"fixture","calculated_at":paper["calculation_timestamp"]}
    journal.context.record_intelligence_run(value)
    monkeypatch.setattr(service,"read",StrategyIntelligenceService.read.__get__(service))
    # The real cache/validation/report store is used. These stubs supply only
    # original fixture episodes at the journal source boundary, without a broker.
    monkeypatch.setattr(journal,"get_evidence_completeness_snapshot",lambda **kw:
        {"episodes":deepcopy(rows),"events":[],"journals":[],"source_complete":True,"watermark":"journal-one"})
    monkeypatch.setattr(service,"_enrich_episodes",lambda material:deepcopy(material["episodes"]))
    monkeypatch.setattr(journal.context,"context_snapshot",lambda **kw:
        {"source_complete":True,"watermark":"context-one","contexts":deepcopy(contexts)})
    service._ready_scopes.add(service._scope_key(SCOPE))
    return journal,service,filters,rows


def test_persisted_weekly_report_is_idempotent_survives_restart_and_read_never_writes(tmp_path,monkeypatch):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from test_historical_forward_evidence import SCOPE
    from test_strategy_decay import CLASSIFIER
    journal,service,filters,_=persisted_service(tmp_path,monkeypatch)
    query={**filters,**CLASSIFIER,"week_start":"2025-03-24"}
    first=service.generate_weekly_review(SCOPE,query)
    assert service.generate_weekly_review(SCOPE,query)==first
    assert first["status"]=="COMPLETE" and first["review_id"]
    restarted=StrategyIntelligenceService(journal)
    result=restarted.read("weekly_research",SCOPE,query)
    assert len(result["rows"])==1 and result["rows"][0]["review_id"]==first["review_id"]
    assert result["cache_status"]=="HISTORICAL_SNAPSHOT"
    assert restarted.read("weekly_research",{**SCOPE,"owner_id":"foreign"},query)["rows"]==[]


def test_scope_invalidation_revokes_monitor_and_blocks_weekly_publication(tmp_path,monkeypatch):
    from test_historical_forward_evidence import SCOPE
    from test_strategy_decay import CLASSIFIER
    journal,service,filters,_=persisted_service(tmp_path,monkeypatch)
    query={**filters,**CLASSIFIER,"week_start":"2025-03-24"}
    original=service._monitoring_evidence
    def invalidated(scope,filters):
        result=original(scope,filters)
        service.invalidate_scope({"instance_id":"one"})
        return result
    monkeypatch.setattr(service,"_monitoring_evidence",invalidated)
    result=service.read("decay",SCOPE,query)
    assert result["status"]=="UNKNOWN" and result["windows"]==[]
    with pytest.raises(ValueError): service.generate_weekly_review(SCOPE,query)
    assert service.read("weekly_research",SCOPE,query)["rows"]==[]


def test_due_weekly_loop_is_bounded_idempotent_and_does_not_run_future_weeks(tmp_path,monkeypatch):
    from test_historical_forward_evidence import SCOPE
    from test_strategy_decay import CLASSIFIER
    _,service,filters,_=persisted_service(tmp_path,monkeypatch)
    cohorts=[{**SCOPE,**filters,"lab_id":None}]
    first=service.run_weekly_research(scope=SCOPE,cohorts=cohorts,as_of=AS_OF,classifier=CLASSIFIER)
    assert first["created"]==1 and first["week_start"]=="2025-03-31"
    assert service.run_weekly_research(scope=SCOPE,cohorts=cohorts,as_of=AS_OF,classifier=CLASSIFIER)["created"]==0


def test_bounded_weekly_ticks_eventually_visit_every_cohort_even_when_first_cohorts_are_incomplete(tmp_path,monkeypatch):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from data.journal_store import JournalStore
    from test_historical_forward_evidence import SCOPE
    service=StrategyIntelligenceService(JournalStore(tmp_path/"journal.sqlite"),batch_size=2)
    cohorts=[{**SCOPE,"symbol":f"ASSET{i}","execution_mode":"paper","source_kind":"forward_paper"} for i in range(5)]
    visited=[]
    monkeypatch.setattr(service,"_weekly_key",lambda scope,query,as_of:(query["symbol"],None,None))
    def incomplete(scope,query,**kwargs):
        visited.append(query["symbol"])
        raise ValueError("incomplete observed costs")
    monkeypatch.setattr(service,"generate_weekly_review",incomplete)
    for _ in range(3):
        result=service.run_weekly_research(scope=SCOPE,cohorts=cohorts,as_of=AS_OF)
        assert result["status"]=="COHORT_BOUND_EXCEEDED"
        assert result["incomplete"]==2
    assert set(visited)=={c["symbol"] for c in cohorts}


def test_weekly_source_change_prevents_persisting_a_mixed_snapshot(tmp_path,monkeypatch):
    from test_historical_forward_evidence import SCOPE
    from test_strategy_decay import CLASSIFIER
    journal,service,filters,_=persisted_service(tmp_path,monkeypatch)
    original=journal.get_evidence_completeness_snapshot
    count=0
    def changed(**kwargs):
        nonlocal count
        count+=1
        return {**original(**kwargs),"watermark":str(count)}
    monkeypatch.setattr(journal,"get_evidence_completeness_snapshot",changed)
    query={**filters,**CLASSIFIER,"week_start":"2025-03-24"}
    with pytest.raises(ValueError,match="SOURCE_CHANGED"):service.generate_weekly_review(SCOPE,query)
    assert service.read("weekly_research",SCOPE,query)["rows"]==[]


def test_weekly_processing_outage_does_not_break_existing_evidence_refresh(tmp_path,monkeypatch):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from test_strategy_evidence_runtime import runtime,entry,quote
    from test_historical_forward_evidence import SCOPE
    ledger,store,decisions,paper,pipeline,capture=runtime(tmp_path)
    payload,at=entry(decisions);pipeline.process(payload);paper.process_quote(quote(at))
    paper.close(symbol="XRPUSDT",exit_price=110,execution_id="weekly-outage")
    before=ledger.get_authoritative_evidence_snapshot()
    service=StrategyIntelligenceService(store)
    def unavailable(**kwargs):raise OSError("metadata report store unavailable")
    monkeypatch.setattr(service,"run_weekly_research",unavailable)
    result=service.refresh(ledger,scope=SCOPE)
    assert result["status"]=="READY" and result["weekly_research"]["status"]=="ERROR"
    assert ledger.get_authoritative_evidence_snapshot()==before


def test_recorded_setup_and_final_exit_reason_are_preserved_as_observations():
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from test_historical_forward_evidence import SCOPE
    end="2025-03-24T12:00:00+00:00"
    original={"episodes":[{**SCOPE,"episode_id":"e","root_trade_id":"entry","trade_ids":["entry","final"],"closed_at":end}],
        "events":[],"journals":[{**SCOPE,"trade_id":"entry","sections":{"entry_decision":{"setup_type":"PULLBACK"}}},
            {**SCOPE,"trade_id":"final","closed_at":end,"sections":{"exit_decision":{"exit_reason":"TARGET"}}}]}
    before=deepcopy(original)
    row=StrategyIntelligenceService._enrich_episodes(original)[0]
    assert row["setup"]=="PULLBACK" and row["exit_reason"]=="TARGET"
    assert original==before


def test_weekly_evidence_links_preserve_all_constituent_trade_ids(tmp_path,monkeypatch):
    *_,bound=monitoring_inputs(tmp_path,monkeypatch,count=240)
    for r in bound["episodes"]:
        r["trade_ids"]=[r["episode_id"]+":entry",r["episode_id"]+":remainder"]
    result=build_weekly_review(bound,week_start="2025-03-24",as_of=AS_OF)
    assert result["trade_reference_coverage"]["status"]=="COMPLETE"
    assert len(result["review_trade_ids"])==2*len(result["review_episode_ids"])
    assert all(r["trade_ids"] for r in result["episode_trade_references"])
