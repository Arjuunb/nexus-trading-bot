"""Task 4 measures immutable episodes; monitoring cannot change execution."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from services.strategy_decay import calculate_decay, bind_monitoring_evidence, decay_policy
from services.strategy_intelligence_metrics import EvidenceCohort
from services.strategy_intelligence_v2 import calculate_context_performance
from services.strategy_evidence_completeness import metrics_episode_watermark
from test_historical_forward_evidence import SCOPE, setup_chain
from test_strategy_intelligence_v2 import episode, context, report

CLASSIFIER = {"classifier_id": "market_context_classifier", "classifier_version": "1.0.0", "parameter_hash": "b" * 64}
AS_OF = "2025-04-07T00:00:00+00:00"


def monitoring_inputs(tmp_path, monkeypatch, count=160, values=None):
    journal, service, filters, paper = setup_chain(tmp_path, monkeypatch)
    identity = {**SCOPE, **filters, "lab_id": None}
    cohort = EvidenceCohort(**identity)
    start = datetime(2025, 3, 17, tzinfo=timezone.utc)
    rows, contexts = [], []
    for i in range(count):
        opened = start + timedelta(hours=i * 2)
        net = values[i] if values is not None else ("9" if i % 3 else "-9")
        from decimal import Decimal
        row = episode(f"paper-{i:04d}", **identity, net_pnl=str(net), gross_pnl=str(Decimal(str(net)) + 1),
            opened_at=opened.isoformat(), closed_at=(opened + timedelta(hours=1)).isoformat())
        rows.append(row)
        contexts.append(context(row["episode_id"], **identity, **CLASSIFIER,
            entry_timestamp=row["opened_at"], signal_timestamp=(opened-timedelta(minutes=1)).isoformat(),
            trend_regime="BULL" if i % 2 else "BEAR", entry_timeframe="5m"))
    now = datetime.now(timezone.utc).isoformat()
    completeness = {**report(rows), "cohort": identity, "calculated_at":now,
                    "metrics_evidence_watermark": metrics_episode_watermark(rows, cohort)}
    paper.clear(); paper.update(calculate_context_performance(rows, contexts, cohort=cohort,
        group_by=["symbol"], source_watermark="source-one", completeness_report=completeness,
        calculation_timestamp=now), cache_status="READY", run_id="parent")
    qualification = service.read("qualification", SCOPE, filters)
    frozen = journal.strategy_identity(filters["strategy_id"], filters["strategy_version"], filters["config_fingerprint"])
    bound = bind_monitoring_evidence(identity, qualification, paper, rows, contexts, classifier=CLASSIFIER, frozen=frozen)
    return journal, service, filters, qualification, paper, rows, contexts, bound


def test_last_20_50_100_closed_episodes_are_exact_and_deterministic(tmp_path, monkeypatch):
    *_, bound = monitoring_inputs(tmp_path, monkeypatch)
    original = deepcopy(bound)
    result = calculate_decay(bound, as_of=AS_OF)
    assert result["status"] == "STABLE"
    assert [w["window_size"] for w in result["windows"]] == [20, 50, 100]
    for w in result["windows"]:
        assert w["episode_ids"] == [f"paper-{i:04d}" for i in range(160-w["window_size"],160)]
        assert w["metrics"]["completed_episode_count"] == w["window_size"]
    assert calculate_decay(bound, as_of=AS_OF) == result
    assert result["orders_allowed"] is False and result["strategy_changes_allowed"] is False
    assert bound == original


def test_continued_decay_uses_two_disjoint_windows_not_repeated_reads(tmp_path, monkeypatch):
    values = ["9" if i % 3 else "-9" for i in range(120)] + ["-9"] * 40
    *_, bound = monitoring_inputs(tmp_path, monkeypatch, values=values)
    result = calculate_decay(bound, as_of=AS_OF)
    assert result["status"] == "PAUSE_REVIEW"
    assert result["suggested_lifecycle_state"] == "PAUSED"
    assert result["action_status"] == "PENDING_APPROVAL"
    a,b = result["persistence_windows"]
    assert not set(a["episode_ids"]) & set(b["episode_ids"])
    assert calculate_decay(bound, as_of=AS_OF) == result


def test_one_bad_window_is_degrading_and_small_windows_remain_unknown(tmp_path, monkeypatch):
    *_, bound = monitoring_inputs(tmp_path, monkeypatch, count=30, values=["9"]*10+["-9"]*20)
    result = calculate_decay(bound, as_of=AS_OF)
    assert result["status"] == "EDGE_DEGRADING"
    assert result["windows"][1]["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["windows"][1]["metrics"] is None
    assert result["suggested_lifecycle_state"] == "DEGRADED"


@pytest.mark.parametrize("change", ["missing_cost", "foreign", "duplicate", "forged_summary", "stale", "missing_history", "unbound_history", "wrong_classifier"])
def test_monitor_rejects_incomplete_or_mismatched_financial_evidence(tmp_path, monkeypatch, change):
    journal, _, f, q, p, rows, contexts, _ = monitoring_inputs(tmp_path, monkeypatch)
    if change == "missing_cost": rows[-1]["slippage"] = None
    if change == "foreign": rows[-1]["owner_id"] = "foreign"
    if change == "duplicate": rows.append(deepcopy(rows[-1]))
    if change == "forged_summary": p["groups"][0]["metrics"]["expectancy_net_r"] = "999"
    if change == "stale": p["cache_status"] = "STALE"
    if change == "missing_history": q["validation_history"] = []
    if change == "unbound_history": q["identity"]["owner_id"] = "foreign"
    classifier={**CLASSIFIER,"parameter_hash":"f"*64} if change=="wrong_classifier" else CLASSIFIER
    frozen = journal.strategy_identity(f["strategy_id"], f["strategy_version"], f["config_fingerprint"])
    with pytest.raises(ValueError):
        bind_monitoring_evidence({**SCOPE, **f, "lab_id":None}, q, p, rows, contexts, classifier=classifier, frozen=frozen)


def test_policy_is_versioned_and_explicit_about_uncalibrated_thresholds():
    policy = decay_policy()
    assert policy["version"] == "edge_decay.v1"
    assert policy["calibration"] == "UNCALIBRATED_RESEARCH_POLICY"
    assert len(policy["fingerprint"]) == 64
    assert policy["windows"] == [20,50,100]


@pytest.mark.parametrize("field,value",[("net_pnl",True),("funding",None),("initial_risk","0"),
    ("gross_pnl","NaN"),("fees","-1"),("gross_pnl","999"),("funding_coverage","UNKNOWN")])
def test_financial_summary_rejects_boolean_nonfinite_missing_and_unreconciled_amounts(field,value):
    from services.strategy_decay import financial_summary
    row=episode(**{field:value})
    with pytest.raises(ValueError): financial_summary([row])


@pytest.mark.parametrize("field,value",[("account_id","foreign"),("instance_id","foreign"),("simulation_session_id","foreign"),
    ("strategy_id","foreign"),("strategy_version","2"),("config_fingerprint","c"*64),("lab_id","lab"),
    ("execution_mode","live"),("source_kind","shadow"),("symbol","ETHUSDT")])
def test_every_paper_identity_boundary_must_match_original_episodes(tmp_path,monkeypatch,field,value):
    journal,_,f,q,p,rows,contexts,_=monitoring_inputs(tmp_path,monkeypatch)
    rows[-1][field]=value
    frozen=journal.strategy_identity(f["strategy_id"],f["strategy_version"],f["config_fingerprint"])
    with pytest.raises(ValueError):
        bind_monitoring_evidence({**SCOPE,**f,"lab_id":None},q,p,rows,contexts,classifier=CLASSIFIER,frozen=frozen)


def test_monitor_reads_actual_partial_and_final_closure_without_financial_changes(tmp_path):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from test_strategy_evidence_runtime import runtime,entry,quote
    ledger,store,decisions,paper,pipeline,capture=runtime(tmp_path)
    payload,at=entry(decisions)
    pipeline.process(payload);paper.process_quote(quote(at))
    paper.reduce(symbol="XRPUSDT",exit_price=105,fraction=.5,execution_id="monitor-partial")
    paper.close(symbol="XRPUSDT",exit_price=110,execution_id="monitor-final")
    service=StrategyIntelligenceService(store)
    service.refresh(ledger,scope=SCOPE)
    c=service.read("cohorts",SCOPE,{})["cohorts"][0]
    before=ledger.get_authoritative_evidence_snapshot()
    result=service.read("decay",SCOPE,{**c,"symbol":"XRPUSDT"})
    assert result["status"]=="UNKNOWN" and result["windows"]==[]
    assert ledger.get_authoritative_evidence_snapshot()==before
    assert service.read("performance",SCOPE,{**c,"symbol":"XRPUSDT","group_by":["symbol"]})["groups"][0]["metrics"]["completed_episode_count"]==1
