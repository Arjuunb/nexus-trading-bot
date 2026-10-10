"""Isolated migration/recovery validation; never opens production connections.

Fixtures deliberately use pre-evidence schema, historical unknown fields, real
paper accounting methods, separate account/session scopes, and process death.
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading

import pytest

from data.journal_store import JournalStore
from data.journal_evidence_migrations import apply_evidence_migrations
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services.decision_journal import DecisionJournal
from services.fill_model import RealisticFill
from services.strategy_evidence_capture import StrategyEvidenceCapture
from services.trading_instances import InstanceLedger


_LEGACY_SCHEMA = """
CREATE TABLE trade_decision_journal (
 trade_id TEXT PRIMARY KEY, created_at TEXT, closed_at TEXT, mode TEXT,
 symbol TEXT, side TEXT, strategy TEXT, timeframe TEXT, entry REAL, stop REAL,
 target REAL, exit REAL, size REAL, risk_amount REAL, planned_rr REAL,
 actual_rr REAL, pnl REAL, result TEXT, confidence REAL, brain_score REAL,
 regime TEXT, grade TEXT, status TEXT, sections_json TEXT);
CREATE TABLE trade_decision_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT, ts TEXT, kind TEXT,
 detail TEXT);
CREATE TABLE evolution_memory (
 setup_key TEXT PRIMARY KEY, strategy TEXT, regime TEXT, side TEXT,
 trades INTEGER, wins INTEGER, net_r REAL, updated_at TEXT, stage TEXT,
 note TEXT);
"""


def _legacy_journal(path, *, instance_columns):
    """Representative historical data, not a reconstruction of unknown facts."""
    connection = sqlite3.connect(path)
    connection.executescript(_LEGACY_SCHEMA)
    if instance_columns:
        for name in ("instance_id", "instance_name", "strategy_id", "strategy_name",
                     "strategy_version", "execution_mode", "market_data_mode",
                     "market_data_source", "exchange", "position_id",
                     "simulation_session_id"):
            connection.execute(f"ALTER TABLE trade_decision_journal ADD COLUMN {name} TEXT")
    for number, status, pnl in ((1, "closed", .00123456789),
                               (2, "open", None), (3, "cancelled", None)):
        connection.execute(
            "INSERT INTO trade_decision_journal "
            "(trade_id,created_at,closed_at,mode,symbol,side,strategy,timeframe,"
            "entry,stop,target,exit,size,risk_amount,planned_rr,actual_rr,pnl,"
            "result,status,sections_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"legacy-{number}", f"2024-01-0{number}T00:00:00+00:00",
             "2024-01-04T00:00:00+00:00" if status == "closed" else None,
             "paper", "XRPUSDT", "long", "Adaptive MTF", "5m", .55, .50,
             .65, .56 if status == "closed" else None, .123456789, .00617283945,
             2.0, .2 if status == "closed" else None, pnl,
             "win" if status == "closed" else None, status,
             json.dumps({"historical_note": "unknown provenance is intentional"})),
        )
        connection.execute(
            "INSERT INTO trade_decision_events(trade_id,ts,kind,detail) VALUES (?,?,?,?)",
            (f"legacy-{number}", "2024-01-04T00:00:00+00:00", "old-event", "retain"),
        )
    if instance_columns:
        connection.execute(
            "UPDATE trade_decision_journal SET instance_id='legacy-instance',"
            "simulation_session_id='old-session',strategy_id='adaptive_trend_pullback',"
            "strategy_version='1.0.0' WHERE trade_id='legacy-1'"
        )
    connection.execute(
        "INSERT INTO evolution_memory VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("old-setup", "Adaptive MTF", "unknown", "long", 12, 5,
         .123456789, "2024-01-04T00:00:00+00:00", "EARLY_SIGNAL", "retain"),
    )
    connection.commit()
    connection.close()


def _table_snapshot(path, tables):
    """Values and original column names, including database REAL precision."""
    connection = sqlite3.connect(path)
    result = {}
    for table in tables:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        result[table] = (columns, connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall())
    connection.close()
    return result


def _assert_original_columns_unchanged(path, original):
    connection = sqlite3.connect(path)
    for table, (columns, rows) in original.items():
        projection = ",".join(columns)
        assert connection.execute(f"SELECT {projection} FROM {table} ORDER BY 1").fetchall() == rows
    connection.close()


@pytest.mark.parametrize("instance_columns", [False, True], ids=["early-schema", "instance-schema"])
def test_migration_preserves_production_shaped_historical_rows(tmp_path, instance_columns):
    path = tmp_path / "journal.db"
    _legacy_journal(path, instance_columns=instance_columns)
    original = _table_snapshot(path, ("trade_decision_journal", "trade_decision_events", "evolution_memory"))
    store = JournalStore(path)
    _assert_original_columns_unchanged(path, original)
    for row in store.list():
        assert row["strategy_config_hash"] is None
        assert row["episode_id"] is None
        assert row["signal_timestamp"] is None
        assert row["decision_timestamp"] is None
        assert row["entry_timestamp"] is None
    assert store.episodes() == []
    assert store._c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store._c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_repeated_upgrade_is_value_preserving_and_old_explicit_sql_still_works(tmp_path):
    path = tmp_path / "journal.db"
    _legacy_journal(path, instance_columns=True)
    first = JournalStore(path)
    original = _table_snapshot(path, ("trade_decision_journal", "trade_decision_events", "evolution_memory"))
    for _ in range(3):
        reopened = JournalStore(path)
        _assert_original_columns_unchanged(path, original)
        reopened._c.close()
    # The pre-evidence application's explicitly named insert/update columns
    # remain usable. No evidence tables/triggers are dropped during rollback.
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO trade_decision_journal "
        "(trade_id,symbol,status,sections_json,strategy_id,instance_id) VALUES (?,?,?,?,?,?)",
        ("old-app-new-row", "BTCUSDT", "open", "{}", "price_action", "old-instance"),
    )
    connection.execute(
        "UPDATE trade_decision_journal SET status='closed',pnl=?,closed_at=? WHERE trade_id=?",
        (.123456789, "2026-01-01T01:00:00+00:00", "old-app-new-row"),
    )
    connection.commit()
    connection.close()
    row = first.get("old-app-new-row")
    assert row["pnl"] == .123456789
    assert row["strategy_config_hash"] is None
    assert row["episode_id"] is None
    assert first._c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_interrupted_additive_migration_supports_forward_recovery(tmp_path):
    path = tmp_path / "journal.db"
    _legacy_journal(path, instance_columns=True)
    original = _table_snapshot(path, ("trade_decision_journal", "trade_decision_events", "evolution_memory"))
    connection = sqlite3.connect(path)

    def interrupt(action, table, *_):
        if action == sqlite3.SQLITE_CREATE_TABLE and table == "strategy_episode_legs":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(interrupt)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        apply_evidence_migrations(connection)
    connection.rollback()
    connection.set_authorizer(None)
    # DDL already committed by executescript may survive the interruption.
    # Reapplication creates only missing pieces instead of deleting captures.
    apply_evidence_migrations(connection)
    connection.commit()
    connection.close()
    _assert_original_columns_unchanged(path, original)
    store = JournalStore(path)
    assert store._c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store._c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.get("legacy-1")["strategy_config_hash"] is None


def test_outbox_upgrade_preserves_legacy_primary_schema_and_unknown_receipts(tmp_path):
    """Restore a pre-outbox schema with representative committed old rows."""
    template = SqliteLedger(tmp_path / "schema-template.db")
    schema = [row[0] for row in template._c.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE '%evidence_outbox%' ORDER BY rowid")]
    template._c.close()
    path = tmp_path / "historical-ledger.db"
    connection = sqlite3.connect(path)
    for statement in schema:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO positions(id,symbol,side,size,entry,stop,target,status,pnl,"
        "opened_at,instance_id,simulation_session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("historical-position", "XRPUSDT", "long", .123456789, .55, .50, .65,
         "open", 0, "2024-01-01T00:00:00+00:00", "old-instance", "old-session"))
    connection.execute(
        "INSERT INTO paper_trades(id,alert_id,symbol,side,size,entry,stop,target,status,"
        "opened_at,strategy_id,instance_id,simulation_session_id,fees,risk_amount_at_entry) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("historical-trade", "old-order", "XRPUSDT", "long", .123456789,
         .55, .50, .65, "open", "2024-01-01T00:00:00+00:00", "legacy-adaptive-alias",
         "old-instance", "old-session", 0, .00617283945))
    connection.execute(
        "INSERT INTO paper_executions(execution_id,action,position_id,trade_id,instance_id,created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("old-order", "OPEN", "historical-position", "historical-trade", "old-instance",
         "2024-01-01T00:00:00+00:00"))
    connection.commit()
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    connection.close()
    original = _table_snapshot(path, tables)

    for _ in range(3):
        upgraded = SqliteLedger(path)
        _assert_original_columns_unchanged(path, original)
        assert upgraded.get_evidence_outbox() == []
        snapshot = upgraded.get_authoritative_evidence_snapshot()
        assert snapshot["executions"][0]["execution_id"] == "old-order"
        assert snapshot["executions"][0]["created_at"] == "2024-01-01T00:00:00+00:00"
        assert upgraded._c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert upgraded._c.execute("PRAGMA foreign_key_check").fetchall() == []
        upgraded._c.close()

    # Previous named writes still operate against the expanded schema. They
    # create no fictitious outbox receipt, strategy version or configuration.
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE paper_trades SET status='closed',exit=?,pnl=?,fees=?,closed_at=? WHERE id=?",
        (.56, .00123456789, .000123456789, "2024-01-02T00:00:00+00:00", "historical-trade"))
    connection.execute(
        "UPDATE positions SET status='closed',pnl=?,closed_at=? WHERE id=?",
        (.00123456789, "2024-01-02T00:00:00+00:00", "historical-position"))
    connection.commit()
    connection.close()
    upgraded = SqliteLedger(path)
    assert upgraded.get_evidence_outbox() == []
    assert upgraded.get_paper_trades()[0]["pnl"] == .00123456789
    assert upgraded.get_paper_trades()[0]["fees"] == .000123456789


def _identity():
    configuration = {"entry_tf": "5m", "target_rr": 2.5, "fixture": "isolated_validation"}
    canonical = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    return {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
            "strategy_config_hash": hashlib.sha256(canonical.encode()).hexdigest(),
            "configuration": configuration, "source_hash": "isolated-test-source",
            "identity_status": "observed"}


def _runtime(directory, *, instance="one", owner="owner-one", session="session-one"):
    directory = Path(directory)
    primary = SqliteLedger(directory / "ledger.db")
    ledger = InstanceLedger(primary, instance, session)
    store = JournalStore(directory / "journal.db")
    capture = StrategyEvidenceCapture(DecisionJournal(store))
    paper = PaperExecutionEngine(ledger, fill_model=RealisticFill(
        spread_pct=0, slippage_pct=0, latency_pct=0,
        taker_fee_pct=.001, maker_fee_pct=.0005))
    paper.evidence_listener = capture.observe_fill
    paper.evidence_prepare_listener = capture.prepare_exit
    context = {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
               "strategy_config_hash": _identity()["strategy_config_hash"],
               "instance_id": instance, "simulation_session_id": session,
               "owner_id": owner, "account_id": f"account:{instance}:{session}",
               "lab_id": None, "source_kind": "forward_paper", "execution_mode": "paper"}
    return primary, ledger, store, capture, paper, context


def _open(capture, paper, context, *, alert_id="open", side="BUY", size=1.23456789):
    prepared = capture.prepare_order(
        {"alert_id": alert_id, "symbol": "XRPUSDT", "side": side, "entry": 1.0,
         "stop": .9 if side == "BUY" else 1.1,
         "target": 1.2 if side == "BUY" else .8, "strategy": "Adaptive MTF",
         "timeframe": "5m", "timestamp": "2026-01-01T00:00:00+00:00",
         "journal_execution": context, "strategy_identity": _identity()}, [], 10000)
    return paper.open(
        symbol="XRPUSDT", side=side, size=size, entry=1.0,
        stop=.9 if side == "BUY" else 1.1, target=1.2 if side == "BUY" else .8,
        alert_id=alert_id, sizing_context={"evidence_context": prepared,
                                         "risk_amount_at_entry": size * .1})


def _unpersisted_open(capture, paper, context, *, alert_id):
    """Actual producer facts survive even when the journal cannot be opened."""
    frozen = capture.order_context(
        {"alert_id": alert_id, "symbol": "XRPUSDT", "side": "BUY", "entry": 1.0,
         "stop": .9, "target": 1.2, "strategy": "Adaptive MTF", "timeframe": "5m",
         "timestamp": "2026-01-01T00:00:00+00:00", "journal_execution": context,
         "strategy_identity": _identity()}, [], 10000)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        capture.persist_order_context(frozen)
    return paper.open(
        symbol="XRPUSDT", side="BUY", size=1.23456789, entry=1.0, stop=.9,
        target=1.2, alert_id=alert_id,
        sizing_context={"evidence_context": frozen, "risk_amount_at_entry": .123456789})


def test_prolonged_entire_evidence_store_outage_reconstructs_every_partial_episode(tmp_path, monkeypatch):
    """January commits are replayed months later; no producer journal survives."""
    from data import ledger as ledger_module
    primary, ledger, store, capture, paper, context = _runtime(tmp_path)
    original_store_snapshot = _table_snapshot(store.path, (
        "trade_decision_journal", "trade_decision_events", "strategy_evidence_versions",
        "strategy_evidence_events", "strategy_position_episodes", "strategy_episode_legs"))
    store._c.close()
    commit_index = iter(range(100))
    monkeypatch.setattr(ledger_module, "_now", lambda: (
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=next(commit_index))).isoformat())
    originals = []
    for number in range(4):
        originals.append(_unpersisted_open(capture, paper, context, alert_id=f"outage-open-{number}"))
        paper.update_stop("XRPUSDT", .95)
        paper.reduce(symbol="XRPUSDT", exit_price=1.01234567 + number * .005, fraction=.25,
                     execution_id=f"outage-first-partial-{number}")
        paper.reduce(symbol="XRPUSDT", exit_price=1.08765432 - number * .001, fraction=.5,
                     execution_id=f"outage-second-partial-{number}")
        paper.close(symbol="XRPUSDT", exit_price=1.12345678 + number * .003,
                    execution_id=f"outage-close-{number}")
    _assert_original_columns_unchanged(store.path, original_store_snapshot)
    assert len(ledger.get_execution_receipts()) == len(ledger.get_evidence_outbox()) == 16
    # Include the immutable metadata outbox in preservation checks, not just
    # accounting balances. Reconciliation never writes to the primary store.
    authoritative_before = _table_snapshot(primary.path, (
        "positions", "paper_trades", "paper_executions", "paper_evidence_outbox"))
    reopened = JournalStore(store.path)
    recovery = StrategyEvidenceCapture(DecisionJournal(reopened))
    from services.strategy_evidence_completeness import assess_evidence_completeness
    before = assess_evidence_completeness(ledger.get_authoritative_evidence_snapshot(), reopened)
    assert before["counts"]["missing_events"] == 16
    assert before["history_complete"] is False
    report = recovery.reconcile_report(ledger)
    assert report["counts"]["expected_authoritative_events"] == 16
    assert report["counts"]["persisted_evidence_events"] == 16
    for field in ("missing_events", "duplicate_events", "conflicting_events",
                  "unresolved_episode_references", "missing_configuration_fingerprints",
                  "unknown_execution_modes", "missing_journals", "missing_close_events"):
        assert report["counts"][field] == 0, (field, report)
    assert report["financial_totals"]["net_pnl_delta"] == "0"
    assert report["financial_totals"]["fees_delta"] == "0"
    # Recovery proves delivery, identity and booked economics. Funding remains
    # unmodeled; it must not be silently converted to verified zero costs.
    assert report["status"] == "PARTIAL"
    assert report["history_complete"] is False
    assert report["counts"]["missing_cost_components"] == 4
    episodes = reopened.completed_evidence_episodes()
    assert len(episodes) == 4
    assert all(episode["realised_leg_count"] == 3 for episode in episodes)
    assert {episode["root_trade_id"] for episode in episodes} == {fill.trade_id for fill in originals}
    assert all(episode["strategy_config_hash"] == context["strategy_config_hash"] for episode in episodes)
    trades_by_id = {row["id"]: row for row in ledger.get_paper_trades()}
    openings_by_id = {fill.trade_id: fill for fill in originals}
    for episode in episodes:
        assert Decimal(episode["initial_risk"]) == Decimal(str(
            openings_by_id[episode["root_trade_id"]].receipt["initial_risk_amount"]))
        for field, primary_field in (("net_pnl", "pnl"), ("fees", "fees")):
            assert Decimal(episode[field]) == sum(
                (Decimal(str(trades_by_id[trade_id][primary_field]))
                 for trade_id in episode["trade_ids"]), Decimal(0))
    for field in ("net_pnl", "fees"):
        source_field = "pnl" if field == "net_pnl" else field
        assert sum((Decimal(episode[field]) for episode in episodes), Decimal(0)) == sum(
            (Decimal(str(row[source_field])) for row in ledger.get_paper_trades()), Decimal(0))
    for receipt in ledger.get_execution_receipts():
        event = reopened.execution_event(receipt["execution_id"], instance_id="one", simulation_session_id="session-one")
        assert event["observed_at"] == receipt["created_at"]
    evidence_after = reopened.get_evidence_completeness_snapshot()
    retry = recovery.reconcile_report(ledger)
    assert retry["financial_totals"] == report["financial_totals"]
    assert reopened.get_evidence_completeness_snapshot()["watermark"] == evidence_after["watermark"]
    _assert_original_columns_unchanged(primary.path, authoritative_before)


def test_episode_partials_reversal_and_replay_reconcile_primary_financial_totals(tmp_path):
    primary, ledger, store, capture, paper, context = _runtime(tmp_path)
    opened = _open(capture, paper, context)
    paper.update_stop("XRPUSDT", .95)
    fills = [paper.reduce(symbol="XRPUSDT", exit_price=1.01234567, fraction=.25,
                          execution_id="scale-out-one"),
             paper.reduce(symbol="XRPUSDT", exit_price=1.08765432, fraction=.5,
                          execution_id="scale-out-two"),
             paper.close(symbol="XRPUSDT", exit_price=1.12345678, execution_id="close-long")]
    _open(capture, paper, context, alert_id="reverse-short", side="SELL", size=.987654321)
    fills.append(paper.close(symbol="XRPUSDT", exit_price=.923456789, execution_id="close-short"))
    financial_tables = ("positions", "paper_trades", "paper_executions", "webhook_events")
    authoritative_before = _table_snapshot(primary.path, financial_tables)
    episodes = store.completed_evidence_episodes(instance_id="one")
    assert len(episodes) == 2
    assert sorted(episode["realised_leg_count"] for episode in episodes) == [1, 3]
    trades = ledger.get_paper_trades()
    assert len(trades) == 4
    for metric, field in (("net_pnl", "pnl"), ("fees", "fees")):
        assert sum((Decimal(episode[metric]) for episode in episodes), Decimal(0)) == sum(
            (Decimal(str(row[field])) for row in trades), Decimal(0))
    captured_gross = sum((Decimal(episode["gross_pnl"]) for episode in episodes), Decimal(0))
    derived_primary_gross = sum(
        (Decimal(str(row["pnl"])) + Decimal(str(row["fees"])) for row in trades), Decimal(0))
    # Gross is not a stored primary column. Original float subtraction can
    # lose a final bit when recovering gross from persisted net + fees.
    # Bound the difference by source REAL resolution, never a currency-cent
    # tolerance, while net and fees above must still reconcile exactly.
    source_roundoff = sum(
        (Decimal.from_float(math.ulp(float(row[field])))
         for row in trades for field in ("pnl", "fees")), Decimal(0))
    assert abs(captured_gross - derived_primary_gross) <= source_roundoff
    long_episode = next(episode for episode in episodes if episode["root_trade_id"] == opened.trade_id)
    assert Decimal(long_episode["initial_risk"]) == Decimal(str(opened.receipt["initial_risk_amount"]))
    for fill in fills:
        capture.observe_fill(fill)
        capture.observe_fill(fill)
    capture.reconcile(ledger)
    capture.reconcile(ledger)
    _assert_original_columns_unchanged(primary.path, authoritative_before)
    assert store.completed_evidence_episodes(instance_id="one") == episodes


@pytest.mark.parametrize("failed_stage", ["entry", "exit"])
def test_journal_transaction_failure_recovers_complete_timeline_once(tmp_path, monkeypatch, failed_stage):
    primary, ledger, store, capture, paper, context = _runtime(tmp_path)
    real_add_event = store.add_event

    def fail_event(*args, **kwargs):
        raise sqlite3.OperationalError("injected journal persistence outage")

    if failed_stage == "entry":
        monkeypatch.setattr(store, "add_event", fail_event)
    opened = _open(capture, paper, context)
    if failed_stage == "entry":
        assert store.get(opened.trade_id) is None
    else:
        monkeypatch.setattr(store, "add_event", fail_event)
        paper.close(symbol="XRPUSDT", exit_price=1.12345678, execution_id="outage-close")
        assert store.get(opened.trade_id)["status"] == "open"
        assert store._c.execute("SELECT COUNT(*) FROM evolution_memory").fetchone()[0] == 0
    before = _table_snapshot(primary.path, ("positions", "paper_trades", "paper_executions"))
    monkeypatch.setattr(store, "add_event", real_add_event)
    capture.reconcile(ledger)
    row = store.get(opened.trade_id)
    assert row is not None
    assert row["status"] == ("open" if failed_stage == "entry" else "closed")
    assert len(row["events"]) == (5 if failed_stage == "entry" else 8)
    capture.reconcile(ledger)
    assert store.get(opened.trade_id) == row
    if failed_stage == "exit":
        assert store._c.execute("SELECT SUM(trades) FROM evolution_memory").fetchone()[0] == 1
    _assert_original_columns_unchanged(primary.path, before)


@pytest.mark.parametrize("other_scope", [
    {"instance": "two", "owner": "owner-two", "session": "session-one"},
    {"instance": "one", "owner": "owner-one", "session": "session-two"},
])
def test_shared_journal_keeps_account_and_session_recovery_isolated(tmp_path, other_scope):
    first = _runtime(tmp_path)
    first_open = _open(first[3], first[4], first[5], alert_id="one-order")
    first[4].close(symbol="XRPUSDT", exit_price=1.1, execution_id="one-close")
    second = _runtime(tmp_path, **other_scope)
    second_open = _open(second[3], second[4], second[5], alert_id="two-order")
    second[4].close(symbol="XRPUSDT", exit_price=.95, execution_id="two-close")
    primary_before = _table_snapshot(first[0].path, ("positions", "paper_trades", "paper_executions"))
    first[3].reconcile(first[1])
    second[3].reconcile(second[1])
    _assert_original_columns_unchanged(first[0].path, primary_before)
    episodes = first[2].completed_evidence_episodes()
    assert len(episodes) == 2
    by_trade = {episode["root_trade_id"]: episode for episode in episodes}
    assert by_trade[first_open.trade_id]["account_id"] == first[5]["account_id"]
    assert by_trade[second_open.trade_id]["account_id"] == second[5]["account_id"]
    assert by_trade[first_open.trade_id]["simulation_session_id"] == "session-one"
    assert by_trade[second_open.trade_id]["simulation_session_id"] == other_scope["session"]


def _killed_worker(directory):
    _, _, _, capture, paper, context = _runtime(directory)
    # The callback runs strictly after paper_executions + position + trade
    # commit. SIGKILL prevents finally handlers and cooperative cleanup.
    paper.evidence_listener = lambda _: os.kill(os.getpid(), signal.SIGKILL)
    _open(capture, paper, context, alert_id="terminated-open")


def _outage_killed_reduction_worker(directory):
    _, _, store, capture, paper, context = _runtime(directory)
    store._c.close()
    _unpersisted_open(capture, paper, context, alert_id="outage-terminated-open")
    # The OPEN and its metadata committed while every journal call failed.
    # Terminate after the indivisible REDUCE + continuation + outbox commit.
    paper.evidence_listener = lambda _: os.kill(os.getpid(), signal.SIGKILL)
    paper.reduce(symbol="XRPUSDT", exit_price=1.12345678, fraction=.25,
                 execution_id="outage-terminated-reduce")


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-boundary proof requires POSIX")
def test_worker_sigkill_after_partial_commit_during_total_outage_recovers_parent_child(tmp_path):
    script = (
        "import runpy,sys; namespace=runpy.run_path(sys.argv[1]); "
        "namespace['_outage_killed_reduction_worker'](sys.argv[2])")
    worker = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve()), str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert worker.returncode == -signal.SIGKILL, worker.stderr
    primary, ledger, store, capture, paper, _ = _runtime(tmp_path)
    assert store.list() == []
    assert len(ledger.get_execution_receipts()) == len(ledger.get_evidence_outbox()) == 2
    original = _table_snapshot(primary.path, (
        "positions", "paper_trades", "paper_executions", "paper_evidence_outbox"))
    report = capture.reconcile_report(ledger)
    assert report["counts"]["missing_events"] == 0
    assert report["counts"]["unresolved_episode_references"] == 0
    assert report["financial_totals"]["net_pnl_delta"] == "0"
    assert report["financial_totals"]["fees_delta"] == "0"
    episode = store.episodes()[0]
    assert len(store.episodes()) == 1
    assert episode["status"] == "open"
    assert episode["realised_leg_count"] == 1
    parent, child = ledger.get_evidence_outbox()
    assert set(episode["trade_ids"]) == {parent["trade_id"], child["remainder_trade_id"]}
    assert store.get(parent["trade_id"])["status"] == "closed"
    assert store.get(child["remainder_trade_id"])["status"] == "open"
    after = store.get_evidence_completeness_snapshot()
    capture.reconcile_report(ledger)
    assert store.get_evidence_completeness_snapshot()["watermark"] == after["watermark"]
    _assert_original_columns_unchanged(primary.path, original)
    # Continuing normal execution after restart closes the recovered episode,
    # rather than counting the continuation as a new completed trade.
    paper.close(symbol="XRPUSDT", exit_price=1.2, execution_id="after-restart-close")
    final_report = capture.reconcile_report(ledger)
    assert final_report["financial_totals"]["net_pnl_delta"] == "0"
    assert final_report["financial_totals"]["fees_delta"] == "0"
    assert len(store.completed_evidence_episodes()) == 1
    assert store.completed_evidence_episodes()[0]["realised_leg_count"] == 2


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-boundary proof requires POSIX")
def test_worker_sigkill_after_commit_recovers_without_any_financial_writes(tmp_path):
    test_file = str(Path(__file__).resolve())
    script = (
        "import runpy,sys; "
        "namespace=runpy.run_path(sys.argv[1]); "
        "namespace['_killed_worker'](sys.argv[2])"
    )
    worker = subprocess.run([sys.executable, "-c", script, test_file, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30)
    assert worker.returncode == -signal.SIGKILL, worker.stderr
    primary, ledger, store, capture, _, _ = _runtime(tmp_path)
    assert len(ledger.get_execution_receipts()) == 1
    assert store.list() == []
    before = _table_snapshot(primary.path, ("positions", "paper_trades", "paper_executions"))
    assert capture.reconcile(ledger) == 1
    rows = store.list()
    assert len(rows) == 1
    assert rows[0]["execution_id"] == "terminated-open"
    assert rows[0]["entry_timestamp"] == ledger.get_execution_receipts()[0]["created_at"]
    assert capture.reconcile(ledger) == 0
    assert store.list() == rows
    _assert_original_columns_unchanged(primary.path, before)


def test_completeness_snapshot_is_stable_complete_and_get_shaped(tmp_path):
    _, _, store, capture, paper, context = _runtime(tmp_path)
    opened = _open(capture, paper, context)
    paper.close(symbol="XRPUSDT", exit_price=1.1, execution_id="snapshot-close")
    first = store.get_evidence_completeness_snapshot()
    second = store.get_evidence_completeness_snapshot()
    assert first["source_complete"] is True
    assert first["bound_exceeded"] == []
    assert first["watermark"] == second["watermark"]
    assert len(first["episodes"]) == len(first["journals"]) == len(first["legs"]) == 1
    assert first["journals"][0] == store.get(opened.trade_id)
    assert first["episodes"] == store.episodes()
    assert first["events"] == store.evidence_events()
    assert first["configurations"][0]["strategy_config_hash"] == context["strategy_config_hash"]
    assert first["source_counts"]["timeline"] == 8


@pytest.mark.parametrize("bound", [0, -1, True, 1.5, None])
def test_completeness_snapshot_rejects_invalid_bounds(bound):
    with pytest.raises(ValueError, match="positive integer"):
        JournalStore().get_evidence_completeness_snapshot(max_records=bound)


def test_snapshot_bound_overflow_never_becomes_silent_complete_projection(tmp_path):
    _, _, store, capture, paper, context = _runtime(tmp_path)
    _open(capture, paper, context)
    snapshot = store.get_evidence_completeness_snapshot(max_records=1)
    assert snapshot["source_complete"] is False
    assert snapshot["source_counts"]["events"] > 1
    assert "events" in snapshot["bound_exceeded"]
    assert "timeline" in snapshot["bound_exceeded"]
    assert len(snapshot["events"]) == 1
    assert len(snapshot["journals"][0]["events"]) == 1


def test_snapshot_transaction_keeps_concurrent_writer_out_of_all_views(tmp_path, monkeypatch):
    from services import strategy_evidence
    _, _, store, capture, paper, context = _runtime(tmp_path)
    _open(capture, paper, context)
    original = store.get_evidence_completeness_snapshot()
    writer_store = JournalStore(store.path)
    requested, committed = threading.Event(), threading.Event()
    failure = []

    def write():
        requested.set()
        try:
            writer_store.record_evidence_event("concurrent-event", kind="DECISION", payload={})
            committed.set()
        except Exception as exc:
            failure.append(exc)

    real_build = strategy_evidence.build_episode
    thread = threading.Thread(target=write)

    def build(*args, **kwargs):
        thread.start()
        assert requested.wait(1)
        assert not committed.wait(.03)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(strategy_evidence, "build_episode", build)
    snapshot = store.get_evidence_completeness_snapshot()
    monkeypatch.setattr(strategy_evidence, "build_episode", real_build)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failure == []
    assert committed.is_set()
    assert snapshot["watermark"] == original["watermark"]
    assert store.get_evidence_completeness_snapshot()["watermark"] != snapshot["watermark"]


def _run_report(run_id="run-one", status="COMPLETE", **updates):
    return {"run_id": run_id, "status": status,
            "calculated_at": "2026-01-01T00:00:00+00:00",
            "scope": {"owner_id": "owner", "instance_id": "one", "simulation_session_id": None},
            "cohort": {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
                       "config_fingerprint": None},
            "authoritative_watermark": "primary-one", "evidence_watermark": "evidence-one",
            "missing_events": [], **updates}


def test_reconciliation_runs_are_append_only_and_retry_idempotent(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    report = _run_report()
    watermark = store.get_evidence_completeness_snapshot()["watermark"]
    assert store.record_reconciliation_run(report) is True
    assert JournalStore(store.path).record_reconciliation_run(report) is False
    assert store.get_evidence_completeness_snapshot()["watermark"] == watermark
    with pytest.raises(ValueError, match="conflict"):
        store.record_reconciliation_run({**report, "missing_events": ["missing"]})
    for statement in ("UPDATE strategy_evidence_reconciliation_runs SET status='PARTIAL'",
                      "DELETE FROM strategy_evidence_reconciliation_runs"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store._c.execute(statement)
    assert store.last_successful_reconciliation() == report
    encoded_watermarks = store._c.execute(
        "SELECT watermarks_json FROM strategy_evidence_reconciliation_runs").fetchone()[0]
    assert json.loads(encoded_watermarks) == {
        "authoritative_watermark": "primary-one", "evidence_watermark": "evidence-one"}


def test_last_successful_reconciliation_ignores_later_partial_and_matches_exact_scope(tmp_path):
    store = JournalStore(tmp_path / "journal.db")
    complete = _run_report()
    store.record_reconciliation_run(complete)
    for number, status in enumerate(("PARTIAL", "UNKNOWN", "CONFLICTED", "RECOVERING"), start=2):
        store.record_reconciliation_run(_run_report(
            run_id=f"run-{number}", status=status, calculated_at=f"2026-01-0{number}T00:00:00+00:00"))
    assert store.last_successful_reconciliation(scope=complete["scope"], cohort=complete["cohort"]) == complete
    assert store.last_successful_reconciliation(scope={**complete["scope"], "owner_id": "other"}) is None
    assert store.last_successful_reconciliation(cohort={**complete["cohort"], "strategy_version": "2.0.0"}) is None
    later = _run_report(run_id="run-later", calculated_at="2026-01-06T01:00:00+01:00")
    store.record_reconciliation_run(later)
    assert store.last_successful_reconciliation() == later


@pytest.mark.parametrize("update", [
    {"run_id": ""}, {"calculated_at": None}, {"calculated_at": "2026-01-01"},
    {"calculated_at": "invalid"}, {"status": "SUCCESS"},
    {"reconciliation_status": "PARTIAL"},
])
def test_reconciliation_run_requires_actual_id_timestamp_and_valid_status(update):
    with pytest.raises(ValueError):
        JournalStore().record_reconciliation_run(_run_report(**update))
