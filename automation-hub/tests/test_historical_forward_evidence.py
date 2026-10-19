"""Task 1: historical results and real paper episodes share one frozen identity."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from data.journal_store import JournalStore
from services.strategy_identity import observed_strategy_identity
from services.strategy_intelligence_service import StrategyIntelligenceService
from strategies.adaptive_trend_pullback.strategy import AdaptiveTrendPullbackStrategy


SCOPE = {"owner_id": "owner", "instance_id": "one", "account_id": "account-one",
         "simulation_session_id": "session"}


def financial_trades(count=120):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [{"episode_id": f"historical-{i}", "status": "closed", "initial_risk": "10",
             "entry_time": (start + timedelta(hours=i)).isoformat(),
             "exit_time": (start + timedelta(hours=i, minutes=30)).isoformat(),
             "net_pnl": "10" if i % 3 else "-10",
             "gross_pnl": "11" if i % 3 else "-9", "fees": "1", "funding": "0",
             "slippage": "0.2", "fees_coverage": "MODELED", "funding_coverage": "VERIFIED_ZERO",
             "slippage_coverage": "MODELED", "fees_cost_model": "fees.v1",
             "funding_cost_model": "funding.v1", "slippage_cost_model": "fills.v1"}
            for i in range(count)]


def setup_chain(tmp_path, monkeypatch, *, historical_scope=None, scope=None, report_transform=None):
    from services.historical_forward_evidence import historical_metrics
    journal = JournalStore(tmp_path / "journal.sqlite")
    scope = scope or SCOPE
    historical_scope = historical_scope or scope
    frozen = observed_strategy_identity(AdaptiveTrendPullbackStrategy("XRPUSDT"),
        strategy_id="adaptive_trend_pullback", timeframe="5m")
    journal.save_strategy_identity(frozen)
    identity = {"strategy_id": frozen["strategy_id"], "strategy_version": frozen["strategy_version"],
                "config_fingerprint": frozen["strategy_config_hash"]}
    filters = {**identity, "symbol": "XRPUSDT", "execution_mode": "paper", "source_kind": "forward_paper"}
    dataset = {"dataset_id": "dataset-one", "content_hash": "d" * 64, "symbol": "XRPUSDT",
               "timeframe": "5m", "period_start": "2025-01-01T00:00:00+00:00",
               "period_end": "2025-02-01T00:00:00+00:00", "coverage": {"status": "COMPLETE"}}
    metrics = historical_metrics({"trades": financial_trades()})
    def metrics_at(day):
        trades = financial_trades()
        for trade in trades:
            trade["episode_id"] = f"day{day}:" + trade["episode_id"]
            for key in ("entry_time", "exit_time"):
                trade[key] = (datetime.fromisoformat(trade[key]) + timedelta(days=day - 1)).isoformat()
        return historical_metrics({"trades": trades})
    for method in ("historical_backtest", "out_of_sample", "walk_forward", "robustness", "cost_stress"):
        report = {"method": method, "status": "PASS", "identity": frozen,
                  "identity_binding_status": "MATCHED", "dataset": dataset,
                  "metrics": metrics, "base": metrics, "train": metrics, "validation": metrics_at(8), "holdout": metrics_at(15),
                  "windows": [{"evaluation": metrics_at(15), "development": metrics,
                               "evaluation_start": "2025-01-15T00:00:00+00:00",
                               "evaluation_end": "2025-02-01T00:00:00+00:00"}],
                  "partitions": {"train_end": "2025-01-07T00:00:00+00:00",
                                 "validation_end": "2025-01-14T00:00:00+00:00",
                                 "holdout_end": "2025-02-01T00:00:00+00:00"},
                  "concentration": {"concentration_status": "PASS"},
                  "monte_carlo": {"status": "PASS"}, "stress": {"status": "PASS", "gross_r": "40",
                      "net_r": "38", "fee": "1", "slippage": "1", "funding": "0"}}
        report = deepcopy(report)
        if report_transform is not None:
            report_transform(method, report)
        journal.context.record_validation_run({**historical_scope, **identity, "run_id": method,
            "dataset_identity": dataset["dataset_id"], "data_revision": dataset["content_hash"],
            "period_start": dataset["period_start"], "period_end": dataset["period_end"],
            "execution_mode": "backtest", "method": method, "status": "COMPLETE", "report": report,
            "created_at": "2025-02-02T00:00:00+00:00"})
    cohort = {**scope, **filters, "lab_id": None}
    group = {"cohort": cohort, "episode_ids": [f"forward-{i}" for i in range(40)],
             "metrics": {**metrics, "completed_episode_count": 40, "trade_count": 40,
                         "r_sample_size": 40, "financial_evidence_complete": True,
                         "expectancy_net_r": "0.25", "net_r": "10"},
             "profitability_verified": True, "cost_coverage": {"status": "COMPLETE"},
             "evidence_quality": {"status": "COMPLETE", "binding_status": "MATCHED_SUBGROUP"},
             "observation_period": {"first_opened_at": "2025-03-01T00:00:00+00:00",
                                    "last_closed_at": "2025-04-01T00:00:00+00:00"}}
    performance = {"cache_status": "READY", "source_watermark": "paper-source-one", "groups": [group]}
    service = StrategyIntelligenceService(journal)
    original_read = service.read
    monkeypatch.setattr(service, "read", lambda view, scope, query:
        deepcopy(performance) if view == "performance" else original_read(view, scope, query))
    return journal, service, filters, performance


def test_complete_historical_chain_uses_backtest_metrics_and_actual_forward_episodes(tmp_path, monkeypatch):
    journal, service, filters, performance = setup_chain(tmp_path, monkeypatch)
    before = journal.context.validation_runs(filters={"owner_id": "owner"})
    original = deepcopy(performance)
    result = service.read("qualification", SCOPE, filters)
    assert result["status"] == "QUALIFIED"
    assert result["evidence_chain"]["historical"]["metrics"]["completed_episode_count"] == 120
    assert result["evidence_chain"]["forward_paper"]["completed_episodes"] == 40
    assert result["evidence_chain"]["forward_paper"]["independence_status"] == "VERIFIED_INDEPENDENT"
    assert len(result["validation_history"]) == 5
    assert journal.context.validation_runs(filters={"owner_id": "owner"}) == before
    assert performance == original


@pytest.mark.parametrize("field,value", [("net_pnl", None), ("funding", None), ("initial_risk", "0"),
    ("fees", True), ("gross_pnl", "NaN"), ("fees_coverage", "UNKNOWN"), ("slippage_cost_model", None),
    ("gross_pnl", "999"), ("funding_coverage", "NOT_MODELED"), ("fees_cost_model", 7),
    ("fees_cost_model", ["model"]), ("slippage_cost_model", " "), ("fees_coverage", [])])
def test_incomplete_financial_samples_never_become_qualification_metrics(field, value):
    from services.historical_forward_evidence import historical_metrics
    trades = financial_trades()
    trades[0][field] = value
    report = historical_metrics({"trades": trades})
    assert report["financial_evidence_complete"] is False
    assert report["evidence_quality"] != "COMPLETE"
    assert report["expectancy_net_r"] is None
    assert report["net_profit_factor"] is None


def test_missing_simulator_fields_do_not_turn_into_zero():
    from services.research_jobs import _metrics
    result = _metrics({})
    assert result["net_r"] is None
    assert result["expectancy_r"] is None
    assert result["financial_evidence_complete"] is False


def test_historical_profit_factor_uses_booked_money_with_variable_entry_risk():
    from services.historical_forward_evidence import historical_metrics
    trades = financial_trades(3)
    # Two small-risk wins cannot disguise a larger monetary loss.
    trades[0].update(initial_risk="1000", net_pnl="-1000", gross_pnl="-999")
    trades[1].update(initial_risk="1", net_pnl="1", gross_pnl="2")
    trades[2].update(initial_risk="1", net_pnl="1", gross_pnl="2")
    result = historical_metrics({"trades": trades})
    assert result["financial_evidence_complete"] is True
    assert result["net_profit_factor"] == "0.002"
    assert result["net_pnl"] == "-998"


@pytest.mark.parametrize("field,value", [("fee", None), ("slippage", True), ("fee", "-1"),
                                        ("funding", None), ("net_r", "999")])
def test_incomplete_or_conflicting_cost_stress_finances_cannot_qualify(tmp_path, monkeypatch, field, value):
    journal, service, filters, performance = setup_chain(tmp_path, monkeypatch)
    row = journal.context.get_validation_run("cost_stress")
    row.update(run_id="invalid-stress", created_at="2025-02-03T00:00:00+00:00", completed_at="2025-02-03T00:00:00+00:00")
    row["report"]["stress"][field] = value
    journal.context.record_validation_run(row)
    assert service.read("qualification", SCOPE, filters)["status"] == "NOT_QUALIFIED"


@pytest.mark.parametrize("change", ["missing_samples", "changed_sample", "false_count", "malformed_report"])
def test_summary_flags_cannot_replace_original_financial_samples(tmp_path, monkeypatch, change):
    journal, service, filters, performance = setup_chain(tmp_path, monkeypatch)
    row = journal.context.get_validation_run("historical_backtest")
    row.update(run_id="unreliable-history", created_at="2025-02-03T00:00:00+00:00", completed_at="2025-02-03T00:00:00+00:00")
    if change == "missing_samples": row["report"]["metrics"].pop("financial_samples", None)
    if change == "changed_sample": row["report"]["metrics"]["financial_samples"][0]["funding"] = None
    if change == "false_count": row["report"]["metrics"]["completed_episode_count"] = 9999
    if change == "malformed_report": row["report"] = []
    journal.context.record_validation_run(row)
    assert service.read("qualification", SCOPE, filters)["status"] == "NOT_QUALIFIED"


@pytest.mark.parametrize("method", ["out_of_sample", "walk_forward"])
def test_null_financial_partitions_are_rejected_with_a_qualification_result(tmp_path, monkeypatch, method):
    def corrupt(stage, report):
        if stage != method: return
        if method == "out_of_sample": report["holdout"]["financial_samples"] = None
        else: report["windows"] = [None]
    journal, service, filters, performance = setup_chain(tmp_path, monkeypatch, report_transform=corrupt)
    assert service.read("qualification", SCOPE, filters)["status"] == "NOT_QUALIFIED"


@pytest.mark.parametrize("job_type", ["HISTORICAL_BACKTEST", "OUT_OF_SAMPLE_VALIDATION", "ANCHORED_WALK_FORWARD", "ROLLING_WALK_FORWARD"])
def test_research_worker_persists_observed_identity_and_rejects_incomplete_simulator_finances(tmp_path, monkeypatch, job_type):
    from bot.types import Bar
    from data import market_data
    from services.research_jobs import ResearchJobService
    journal = JournalStore(tmp_path / "journal.sqlite")
    frozen = observed_strategy_identity(AdaptiveTrendPullbackStrategy("XRPUSDT"),
        strategy_id="adaptive_trend_pullback", timeframe="5m")
    journal.save_strategy_identity(frozen)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = [Bar(start + timedelta(minutes=i*5), 100, 102, 98, 101, 100) for i in range(600)]
    monkeypatch.setattr(market_data, "get_bars", lambda *args, **kwargs: (bars, "isolated-test-provider"))
    service = ResearchJobService(journal.context)
    job = service.create({"idempotency_key": "task1-" + job_type, "strategy_id": frozen["strategy_id"],
        "strategy_version": frozen["strategy_version"], "config_fingerprint": frozen["strategy_config_hash"],
        "job_type": job_type, "symbol": "XRPUSDT", "timeframe": "5m", "bars": 600,
        "development_size": None, "evaluation_size": None}, owner_id="owner", scope=SCOPE)
    finished = service.run(job["job_id"], owner_id="owner")
    assert finished["status"] == "COMPLETED"
    report = finished["result"]
    # The durable PostgreSQL commit contract requires COMPLETE even when
    # a successfully executed validation finds FAIL or UNKNOWN evidence.
    assert report["status"] == "COMPLETE"
    assert report["result_status"] in {"COMPLETE", "PASS", "FAIL", "UNKNOWN"}
    assert report["identity_binding_status"] == "MATCHED"
    assert report["identity"]["configuration"] == frozen["configuration"]
    assert report["identity"]["strategy_config_hash"] == frozen["strategy_config_hash"]
    assert report["identity"]["source_hash"] == frozen["source_hash"]
    if job_type == "HISTORICAL_BACKTEST":
        assert report["metrics"]["financial_evidence_complete"] is False
    elif job_type == "OUT_OF_SAMPLE_VALIDATION":
        assert report["holdout"]["financial_evidence_complete"] is False
    else:
        assert all(window["evaluation"]["financial_evidence_complete"] is False for window in report["windows"])
    saved = journal.context.get_validation_run(finished["result_reference"])
    assert saved["report"] == report
    assert saved["execution_mode"] == "backtest"


@pytest.mark.parametrize("override", [{"config_fingerprint": "f" * 64}, {"strategy_version": "999"},
                                      {"frozen_config": {"min_score": 1}}])
def test_research_does_not_accept_requested_identity_or_tuning_as_observed_identity(tmp_path, monkeypatch, override):
    from services import strategy_presets
    from data import market_data
    from bot.types import Bar
    from services.research_jobs import ResearchJobService
    frozen = observed_strategy_identity(AdaptiveTrendPullbackStrategy("XRPUSDT"),
        strategy_id="adaptive_trend_pullback", timeframe="5m")
    journal = JournalStore(tmp_path / "journal.sqlite")
    service = ResearchJobService(journal.context)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = [Bar(start + timedelta(minutes=i*5), 100, 102, 98, 101, 100) for i in range(600)]
    monkeypatch.setattr(market_data, "get_bars", lambda *args, **kwargs: (bars, "isolated-test-provider"))
    monkeypatch.setattr(strategy_presets, "_run_on", lambda *args, **kwargs:
        {"_strategy_identity": frozen, "trades": financial_trades()})
    job = service.create({"idempotency_key": "task1-identity-mismatch", "strategy_id": frozen["strategy_id"],
        "strategy_version": frozen["strategy_version"], "config_fingerprint": frozen["strategy_config_hash"],
        "job_type": "HISTORICAL_BACKTEST", "symbol": "XRPUSDT", "timeframe": "5m", **override},
        owner_id="owner", scope=SCOPE)
    assert service.run(job["job_id"], owner_id="owner")["result"]["identity_binding_status"] == "UNVERIFIED"


@pytest.mark.parametrize("change", ["foreign_owner", "foreign_account", "other_version", "other_hash",
    "other_asset", "other_timeframe", "other_dataset", "source_hash", "identity_missing", "partial_costs",
    "late_validation", "overlap", "stale_forward", "unknown_forward_costs", "fake_forward_report"])
def test_chain_rejects_mismatched_or_incomplete_evidence(tmp_path, monkeypatch, change):
    journal, service, filters, performance = setup_chain(tmp_path, monkeypatch)
    if change in {"stale_forward", "unknown_forward_costs", "overlap", "fake_forward_report"}:
        if change == "stale_forward": performance["cache_status"] = "STALE"
        if change == "unknown_forward_costs": performance["groups"][0]["cost_coverage"]["status"] = "PARTIAL"
        if change == "overlap": performance["groups"][0]["observation_period"]["first_opened_at"] = "2025-01-20T00:00:00+00:00"
        if change == "fake_forward_report":
            performance["groups"] = []
            journal.context.record_validation_run({**SCOPE, **filters, "run_id": "invented-forward",
                "dataset_identity": "paper", "method": "forward_paper", "status": "PASS",
                "report": {"status": "PASS", "completed_episodes": 999, "expectancy_net_r": "10",
                           "evidence_quality": "COMPLETE", "cost_coverage_status": "COMPLETE",
                           "independence_status": "VERIFIED_INDEPENDENT"}})
    else:
        record = journal.context.get_validation_run("historical_backtest")
        record.update(run_id="newer-backtest", created_at="2025-02-03T00:00:00+00:00")
        record.pop("completed_at", None)
        if change == "foreign_owner":
            record["owner_id"] = "foreign"
            # Remove the owned OOS by requesting a new strategy hash, then prove no foreign fallback.
            filters["config_fingerprint"] = record["config_fingerprint"] = "f" * 64
        if change == "foreign_account": record["account_id"] = "other-account"
        if change == "other_version": record["report"]["identity"]["strategy_version"] = "2.0.0"
        if change == "other_hash": record["report"]["identity"]["strategy_config_hash"] = "f" * 64
        if change == "other_asset": record["report"]["dataset"]["symbol"] = "BTCUSDT"
        if change == "other_timeframe": record["report"]["dataset"]["timeframe"] = "1h"
        if change == "other_dataset": record["report"]["dataset"]["content_hash"] = "e" * 64
        if change == "source_hash": record["report"]["identity"]["source_hash"] = "e" * 64
        if change == "identity_missing": record["report"].pop("identity")
        if change == "partial_costs": record["report"]["metrics"]["financial_evidence_complete"] = False
        if change == "late_validation": record["created_at"] = "2025-03-02T00:00:00+00:00"
        journal.context.record_validation_run(record)
        if change in {"foreign_account", "other_asset", "other_timeframe"}:
            # A different cohort cannot contaminate or supersede the owned chain.
            assert service.read("qualification", SCOPE, filters)["status"] == "QUALIFIED"
            return
    result = service.read("qualification", SCOPE, filters)
    assert result["status"] == "NOT_QUALIFIED"


def test_owner_only_isolated_research_can_bind_but_partial_scope_cannot(tmp_path, monkeypatch):
    journal, service, filters, performance = setup_chain(tmp_path, monkeypatch,
        historical_scope={"owner_id": "owner", "account_id": None, "instance_id": None, "simulation_session_id": None})
    assert service.read("qualification", SCOPE, filters)["status"] == "QUALIFIED"
    row = journal.context.get_validation_run("out_of_sample")
    row.update(run_id="partly-bound-oos", created_at="2025-02-03T00:00:00+00:00", completed_at="2025-02-03T00:00:00+00:00",
               account_id="account-one")
    journal.context.record_validation_run(row)
    result = service.read("qualification", SCOPE, filters)
    assert result["status"] == "QUALIFIED"
    assert result["evidence_chain"]["validation_run_ids"]["out_of_sample"] == "out_of_sample"


@pytest.mark.parametrize("method,change", [("out_of_sample", "reused_holdout"), ("out_of_sample", "leaked_holdout"),
    ("walk_forward", "missing_window"), ("walk_forward", "overlapping_windows"),
    ("walk_forward", "future_development"), ("historical_backtest", "partial_metrics")])
def test_validation_partitions_and_all_financial_windows_are_required(tmp_path, monkeypatch, method, change):
    def corrupt(stage, report):
        if stage != method: return
        if change == "leaked_holdout":
            from services.historical_forward_evidence import historical_metrics
            trades = deepcopy(report["holdout"]["financial_samples"])
            for trade in trades:
                for key in ("entry_time", "exit_time"):
                    trade[key] = (datetime.fromisoformat(trade[key]) - timedelta(days=14)).isoformat()
            report["holdout"] = historical_metrics({"trades": trades})
        if change == "missing_window": report["windows"].append({"evaluation": {}, "development": report["metrics"]})
        if change == "overlapping_windows": report["windows"] *= 2
        if change == "future_development": report["windows"][0]["development"]["last_closed_at"] = "2025-01-20T00:00:00+00:00"
        if change == "partial_metrics": report["metrics"]["r_sample_size"] = 119
    journal, service, filters, performance = setup_chain(tmp_path, monkeypatch, report_transform=corrupt)
    if change == "reused_holdout":
        row = journal.context.get_validation_run(method)
        row.update(run_id="new-" + method, created_at="2025-02-03T00:00:00+00:00", completed_at="2025-02-03T00:00:00+00:00")
        journal.context.record_validation_run(row)
    assert service.read("qualification", SCOPE, filters)["status"] == "NOT_QUALIFIED"


def test_holdout_access_counts_do_not_include_other_tenants(tmp_path):
    journal = JournalStore(tmp_path / "journal.sqlite")
    row = {"strategy_id": "strategy", "strategy_version": "1", "config_fingerprint": "a" * 64,
           "dataset_identity": "dataset", "execution_mode": "backtest", "method": "out_of_sample",
           "status": "COMPLETE", "report": {"status": "PASS"}}
    journal.context.record_validation_run({**row, "owner_id": "alice", "run_id": "alice-first"})
    journal.context.record_validation_run({**row, "owner_id": "bob", "run_id": "bob-first"})
    journal.context.record_validation_run({**row, "owner_id": "alice", "run_id": "alice-second"})
    assert journal.context.get_validation_run("bob-first")["holdout_access_count"] == 1
    assert journal.context.get_validation_run("alice-second")["holdout_access_count"] == 2


def test_qualification_api_joins_evidence_and_resolves_ownership_on_server(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.strategy_intelligence import create_router
    scope = {**SCOPE, "account_id": "instance:one:session"}
    journal, service, filters, performance = setup_chain(tmp_path, monkeypatch, scope=scope)
    class Manager:
        store = SimpleNamespace(available=True)
        def instance_for(self, instance_id, owner_id):
            if instance_id != "one" or owner_id != "owner": raise KeyError(instance_id)
            return SimpleNamespace(simulation_session_id="session")
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: service, manager_provider=lambda: Manager(),
        owner_resolver=lambda request: request.headers.get("x-test-owner")))
    client = TestClient(app)
    path = "/api/v2/strategy-intelligence/qualification"
    params = {**filters, "instance_id": "one"}
    assert client.get(path, params=params).status_code == 401
    assert client.get(path, params=params, headers={"x-test-owner": "foreign"}).status_code == 404
    response = client.get(path, params=params, headers={"x-test-owner": "owner"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "QUALIFIED"
    assert response.json()["evidence_chain"]["historical"]["metrics"]["completed_episode_count"] == 120
    assert response.json()["contract_version"] == "strategy_intelligence.v2"
    for endpoint in ("oos", "walk-forward", "robustness"):
        projected = client.get("/api/v2/strategy-intelligence/" + endpoint, params=params,
                               headers={"x-test-owner": "owner"})
        assert projected.status_code == 200
        assert projected.json()["status"] == "PASS"
        assert projected.json()["binding_status"] == "BOUND"
        assert projected.json()["result"]["execution_mode"] == "backtest"


def test_newer_unfinished_job_blocks_older_passing_validation(tmp_path, monkeypatch):
    from services.research_jobs import ResearchJobService
    journal, service, filters, performance = setup_chain(tmp_path, monkeypatch)
    ResearchJobService(journal.context).create({**filters, "timeframe": "5m", "job_type": "OUT_OF_SAMPLE_VALIDATION",
        "idempotency_key": "new-unfinished-oos"}, owner_id="owner", scope=SCOPE)
    result = service.read("qualification", SCOPE, filters)
    assert result["status"] == "NOT_QUALIFIED"
    assert "out_of_sample" in result["unknown_requirements"]


def test_actual_partial_close_and_final_close_are_one_episode_and_finances_unchanged(tmp_path):
    from test_strategy_evidence_runtime import entry, quote, runtime
    ledger, journal, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5, execution_id="task1-partial")
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="task1-final")
    before = ledger.get_authoritative_evidence_snapshot()
    service = StrategyIntelligenceService(journal)
    service.refresh(ledger, scope=SCOPE)
    filters = {**service.read("cohorts", SCOPE, {})["cohorts"][0], "symbol": "XRPUSDT"}
    result = service.read("qualification", SCOPE, filters)
    forward = result["evidence_chain"]["forward_paper"]
    assert forward["completed_episodes"] == 1
    assert len(forward["episode_ids"]) == 1
    assert forward["cost_coverage_status"] != "COMPLETE"
    assert result["status"] == "NOT_QUALIFIED"
    assert ledger.get_authoritative_evidence_snapshot() == before
