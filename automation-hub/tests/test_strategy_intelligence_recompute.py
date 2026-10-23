"""Read-only persisted evidence inspection and deterministic projection."""
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from services.strategy_intelligence_projection import inspect_completeness, inspect_persisted_evidence, recompute
from services.strategy_intelligence_metrics import EvidenceCohort


def sources(tmp_path, *, closed=0):
    ledger = tmp_path / "ledger.db"
    journal = tmp_path / "journal.db"
    with sqlite3.connect(ledger) as c:
        c.execute("CREATE TABLE paper_trades(id TEXT, symbol TEXT, status TEXT, source TEXT, strategy_id TEXT)")
        c.executemany("INSERT INTO paper_trades VALUES (?,?,?,?,?)", [
            (f"trade-{i}", "XRPUSDT", "closed", "paper", "adaptive_trend_pullback") for i in range(closed)])
    with sqlite3.connect(journal) as c:
        c.execute("CREATE TABLE trade_decision_journal(trade_id TEXT, symbol TEXT, strategy_id TEXT, strategy_version TEXT)")
    return ledger, journal


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_empty_development_databases_do_not_validate_production_xrp_claims(tmp_path):
    ledger, journal = sources(tmp_path)
    report = inspect_persisted_evidence(ledger, journal, source_classification="development")
    assert report["reconciliation_status"] == "UNVERIFIED"
    assert report["observations"]["closed_ledger_rows"] == 0
    assert all(row["status"] == "UNVERIFIED" and row["value"] is None
               for row in report["metrics"].values())
    assert "configuration_fingerprint" in report["metrics"]
    assert "time_period" in report["metrics"]


def test_legacy_leg_counts_are_not_reported_as_completed_episode_count(tmp_path):
    ledger, journal = sources(tmp_path, closed=2)
    report = inspect_persisted_evidence(ledger, journal, source_classification="production_export")
    assert report["observations"]["closed_ledger_rows"] == 2
    assert report["metrics"]["trade_count"]["value"] is None
    assert report["metrics"]["episode_count"]["value"] is None
    assert "missing_episode_lineage" in report["blockers"]


def test_inspection_is_read_only_and_deterministic(tmp_path):
    ledger, journal = sources(tmp_path, closed=2)
    before = digest(ledger), digest(journal)
    first = inspect_persisted_evidence(ledger, journal, source_classification="development")
    second = inspect_persisted_evidence(ledger, journal, source_classification="development")
    assert first == second
    assert before == (digest(ledger), digest(journal))


def test_missing_files_are_reported_without_creating_databases(tmp_path):
    ledger, journal = tmp_path / "absent-ledger.db", tmp_path / "absent-journal.db"
    report = inspect_persisted_evidence(ledger, journal, source_classification="development")
    assert report["reconciliation_status"] == "UNVERIFIED"
    assert not ledger.exists() and not journal.exists()


def test_cli_does_not_import_application_or_mutate_sources(tmp_path):
    ledger, journal = sources(tmp_path)
    before = digest(ledger), digest(journal)
    command = [sys.executable, str(Path(__file__).parents[1] / "scripts/recompute_strategy_intelligence.py"),
               "reconcile", "--ledger-db", str(ledger), "--journal-db", str(journal),
               "--source-classification", "development"]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["reconciliation_status"] == "UNVERIFIED"
    assert before == (digest(ledger), digest(journal))


def test_source_classification_must_be_explicit(tmp_path):
    ledger, journal = sources(tmp_path)
    with pytest.raises(ValueError, match="classification"):
        inspect_persisted_evidence(ledger, journal, source_classification="guessed")


def captured_episode(tmp_path):
    from types import SimpleNamespace
    from data.journal_store import JournalStore
    from services.strategy_evidence import StrategyEvidence
    path = tmp_path / "captured-journal.db"
    store = JournalStore(path)
    cohort = EvidenceCohort(
        strategy_id="adaptive_trend_pullback", strategy_version="1.0.0", config_fingerprint=hashlib.sha256(b"{}").hexdigest(),
        instance_id="one", lab_id=None, simulation_session_id="session", execution_mode="paper",
        source_kind="forward_paper", owner_id="owner", account_id="account", symbol="XRPUSDT")
    context = {**cohort.as_dict(), "strategy_config_hash": cohort.config_fingerprint,
               "configuration": {}, "identity_status": "observed",
               "funding": "0", "funding_coverage": "MODELED", "risk_amount_at_entry": "10"}
    fill = dict(action="opened", execution_id="z-open", symbol="XRPUSDT", side="long", price="100",
                size="2", position_id="position", trade_id="trade", fee="0", pnl="0",
                executed_at="2026-09-01T00:00:00Z", receipt={})
    observer = StrategyEvidence(store)
    observer.observe_fill(SimpleNamespace(**fill), context)
    close = {**fill, "action": "closed", "execution_id": "a-close", "price": "105", "pnl": "9",
             "fee": "1", "executed_at": "2026-09-01T01:00:00Z"}
    return path, store, cohort, observer, context, close


def test_recompute_uses_same_episode_projection_as_live_store_and_is_read_only(tmp_path):
    from types import SimpleNamespace
    path, store, cohort, observer, context, close = captured_episode(tmp_path)
    observer.observe_fill(SimpleNamespace(**close), context)
    before = digest(path)
    first = recompute(path, cohort=cohort, source_classification="development")
    second = recompute(path, cohort=cohort, source_classification="development")
    assert {key: value for key, value in first.items() if key not in {"calculated_at", "calculation_timestamp"}} == {
        key: value for key, value in second.items() if key not in {"calculated_at", "calculation_timestamp"}}
    assert first["calculation_timestamp"] <= second["calculation_timestamp"]
    assert digest(path) == before
    assert first["trade_count"] == 1
    assert first["net_pnl"] == store.episodes()[0]["net_pnl"] == "9"
    assert first["expectancy_net_r"] == "0.9"
    assert first["history_complete"] is False
    assert first["ready_for_evidence_review"] is False


def test_new_closure_invalidates_watermark_and_partial_episode_is_excluded(tmp_path):
    from types import SimpleNamespace
    path, store, cohort, observer, context, close = captured_episode(tmp_path)
    before = recompute(path, cohort=cohort, source_classification="development")
    assert before["trade_count"] == 0
    observer.observe_fill(SimpleNamespace(**close), context)
    after = recompute(path, cohort=cohort, source_classification="development")
    assert after["trade_count"] == 1
    assert before["source_watermark"] != after["source_watermark"]


def test_recompute_is_legacy_compatible_without_migrating_old_database(tmp_path):
    from dataclasses import replace
    ledger, journal = sources(tmp_path)
    _, _, cohort, _, _, _ = captured_episode(tmp_path)
    before = digest(journal)
    result = recompute(journal, cohort=replace(cohort, config_fingerprint=None), source_classification="development")
    assert result["capture_status"] == "LEGACY_NO_EPISODE_TABLES"
    assert result["trade_count"] == 0
    assert digest(journal) == before


def test_recompute_refuses_a_truncated_cohort_instead_of_reporting_subset(tmp_path):
    from types import SimpleNamespace
    path, store, cohort, observer, context, close = captured_episode(tmp_path)
    observer.observe_fill(SimpleNamespace(**close), context)
    observer.observe_fill(SimpleNamespace(action="opened", execution_id="second-open", symbol="XRPUSDT",
                                          side="long", price="100", size="1", position_id="second-position",
                                          trade_id="second-trade", fee="0", pnl="0",
                                          executed_at="2026-09-02T00:00:00Z", receipt={}), context)
    with pytest.raises(ValueError, match="truncated"):
        recompute(path, cohort=cohort, source_classification="development", max_episodes=1)


def live_sources(tmp_path):
    from test_strategy_evidence_runtime import entry, quote, runtime
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5, execution_id="reduce")
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="close")
    row = store.episodes()[0]
    cohort = EvidenceCohort(**{key: row.get(key) for key in EvidenceCohort.__dataclass_fields__})
    return Path(ledger.path), Path(store.path), cohort


def test_authoritative_projection_reconciles_actual_partial_legs_and_discloses_unmodeled_funding(tmp_path):
    ledger, journal, cohort = live_sources(tmp_path)
    before = digest(ledger), digest(journal)
    result = recompute(journal, ledger_path=ledger, cohort=cohort, source_classification="development")
    assert result["trade_count"] == 1
    assert result["evidence_completeness_status"] == "PARTIAL"
    assert result["completeness_binding_status"] == "MATCHED"
    assert result["profitability_verified"] is False
    report = result["evidence_completeness"]
    assert report["counts"]["expected_authoritative_events"] == 3
    assert report["counts"]["missing_events"] == 0
    assert report["counts"]["missing_journals"] == 0
    assert report["financial_totals"]["net_pnl_delta"] == "0"
    assert report["financial_totals"]["fees_delta"] == "0"
    assert before == (digest(ledger), digest(journal))


def test_completeness_cli_reads_two_stores_without_application_import_or_migration(tmp_path):
    ledger, journal, cohort = live_sources(tmp_path)
    cohort_path = tmp_path / "cohort.json"
    cohort_path.write_text(json.dumps(cohort.as_dict()))
    before = digest(ledger), digest(journal)
    command = [sys.executable, str(Path(__file__).parents[1] / "scripts/recompute_strategy_intelligence.py"),
               "completeness", "--ledger-db", str(ledger), "--journal-db", str(journal),
               "--cohort-json", str(cohort_path), "--source-classification", "development"]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "PARTIAL"
    assert result["counts"]["expected_authoritative_events"] == 3
    assert before == (digest(ledger), digest(journal))


def test_completeness_on_legacy_schema_never_creates_missing_tables(tmp_path):
    ledger, journal = sources(tmp_path, closed=2)
    before = digest(ledger), digest(journal)
    result = inspect_completeness(ledger, journal, source_classification="production_export")
    assert result["status"] == "UNKNOWN"
    assert result["history_complete"] is False
    assert before == (digest(ledger), digest(journal))


def test_bounded_completeness_discloses_truncation(tmp_path):
    ledger, journal, cohort = live_sources(tmp_path)
    report = inspect_completeness(ledger, journal, cohort=cohort,
                                  source_classification="development", max_records=1)
    assert report["status"] != "COMPLETE"
    assert report["history_complete"] is False


def test_actual_legacy_sqlite_close_omitting_instance_is_reconciled_without_rewriting_receipt(tmp_path):
    from test_strategy_evidence_runtime import entry, quote, runtime
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    pnl = (105 - opened.price) * opened.size
    ledger._ledger.close_position_and_trade(position_id=opened.position_id, trade_id=opened.trade_id,
        exit_price=105, pnl=pnl, rr=.96, execution_id="old-close-without-instance")
    original = ledger._ledger.get_execution_receipts()
    assert original[-1]["instance_id"] == ""
    report = capture.reconcile_report(ledger)
    assert report["counts"]["expected_authoritative_events"] == 2
    assert report["counts"]["missing_events"] == 0
    assert report["counts"]["conflicting_events"] == 0
    assert report["financial_totals"]["net_pnl_delta"] == "0"
    assert store.get(opened.trade_id)["status"] == "closed"
    assert ledger._ledger.get_execution_receipts() == original
