"""Read-only persisted-source inspection and disposable metrics projection.

No application import, startup, account mutation, or migrations occur here.
Small scoped projections are rebuilt on demand; a second persistent accounting
store and a cache invalidation protocol are unnecessary for this first slice.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from services.strategy_intelligence_metrics import EvidenceCohort, calculate_metrics


SOURCE_CLASSIFICATIONS = {"development", "production_export"}
_METRIC_NAMES = ("trade_count", "win_rate_pct", "gross_profit_factor", "net_profit_factor",
                 "gross_pnl", "fees", "funding", "net_pnl", "episode_count",
                 "strategy_version", "configuration_fingerprint", "execution_mode", "time_period")


@contextmanager
def _read_only(path):
    location = Path(path).resolve()
    if not location.is_file():
        raise FileNotFoundError(str(location))
    connection = sqlite3.connect(location.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _tables(connection):
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(connection, table):
    # Table names are fixed internal schema constants, never caller input.
    return {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}


def _classification(value):
    if value not in SOURCE_CLASSIFICATIONS:
        raise ValueError("source classification must be development or production_export")


def inspect_persisted_evidence(
    ledger_path, journal_path, *, source_classification: str,
    symbol: str = "XRPUSDT", strategy_id: str = "adaptive_trend_pullback",
    strategy_version: str = "1.0.0",
) -> dict:
    """Inspect authoritative stores without promoting legacy legs into trades.

    Source classification is an explicit caller declaration, not independent
    proof of production provenance. An unscoped reconciliation cannot verify a
    claimed strategy cohort without version/config/mode/episode evidence.
    Use ``recompute`` with an exact cohort for captured episode calculations.
    """
    _classification(source_classification)
    blockers = []
    observations = {"closed_ledger_rows": None, "open_ledger_rows": None,
                    "journal_rows": None, "matching_declared_version_rows": None,
                    "fingerprinted_journal_rows": None, "episode_headers": None}
    for kind, path in (("ledger", ledger_path), ("journal", journal_path)):
        try:
            with _read_only(path) as connection:
                tables = _tables(connection)
                if kind == "ledger":
                    if "paper_trades" not in tables:
                        blockers.append("missing_paper_trades_table")
                        continue
                    columns = _columns(connection, "paper_trades")
                    if not {"symbol", "status"} <= columns:
                        blockers.append("incompatible_ledger_schema")
                        continue
                    where, args = "symbol=?", [symbol]
                    if "strategy_id" in columns:
                        where += " AND strategy_id=?"
                        args.append(strategy_id)
                    else:
                        blockers.append("missing_ledger_strategy_identity")
                    for status in ("closed", "open"):
                        observations[f"{status}_ledger_rows"] = connection.execute(
                            f"SELECT COUNT(*) FROM paper_trades WHERE {where} AND status=?", (*args, status)).fetchone()[0]
                else:
                    if "trade_decision_journal" not in tables:
                        blockers.append("missing_decision_journal_table")
                        continue
                    columns = _columns(connection, "trade_decision_journal")
                    if not {"symbol", "strategy_id"} <= columns:
                        blockers.append("incompatible_journal_schema")
                        continue
                    where, args = "symbol=? AND strategy_id=?", (symbol, strategy_id)
                    observations["journal_rows"] = connection.execute(
                        f"SELECT COUNT(*) FROM trade_decision_journal WHERE {where}", args).fetchone()[0]
                    if "strategy_version" in columns:
                        observations["matching_declared_version_rows"] = connection.execute(
                            f"SELECT COUNT(*) FROM trade_decision_journal WHERE {where} AND strategy_version=?",
                            (*args, strategy_version)).fetchone()[0]
                    if "strategy_config_hash" in columns:
                        observations["fingerprinted_journal_rows"] = connection.execute(
                            f"SELECT COUNT(*) FROM trade_decision_journal WHERE {where} AND strategy_config_hash IS NOT NULL",
                            args).fetchone()[0]
                    else:
                        blockers.append("missing_historical_configuration")
                    if "strategy_position_episodes" in tables:
                        observations["episode_headers"] = connection.execute(
                            "SELECT COUNT(*) FROM strategy_position_episodes WHERE symbol=? AND strategy_id=?",
                            args).fetchone()[0]
                    else:
                        blockers.append("missing_episode_lineage")
        except (FileNotFoundError, sqlite3.DatabaseError):
            blockers.append(f"{kind}_source_missing_or_unreadable")
    if source_classification != "production_export":
        blockers.append("development_sources_do_not_verify_production_performance")
    if not observations["closed_ledger_rows"]:
        blockers.append("no_persisted_closed_trades_for_requested_strategy_symbol")
    blockers.append("exact_configuration_instance_account_mode_and_period_scope_required")
    return {
        "report_version": "strategy_intelligence.reconciliation.v1",
        "reconciliation_status": "UNVERIFIED", "symbol": symbol, "strategy_id": strategy_id,
        "requested_strategy_version": strategy_version,
        "source_classification": source_classification,
        "source_classification_basis": "explicit_caller_declaration",
        "sources": {"ledger": str(Path(ledger_path).resolve()), "journal": str(Path(journal_path).resolve())},
        "observations": observations, "blockers": sorted(set(blockers)),
        "metrics": {name: {"status": "UNVERIFIED", "value": None} for name in _METRIC_NAMES},
        "note": "Ledger rows can be partial-exit legs. No historical configuration, episode, execution mode or period is inferred.",
    }


def recompute(
    journal_path, *, cohort: EvidenceCohort, source_classification: str,
    max_episodes: int = 100_000, ledger_path=None,
) -> dict:
    """Rebuild one bounded cohort from immutable journal receipts in RO mode.

    Source history completeness is deliberately unknown: existence of captured
    episodes alone cannot prove that the external accounting store has no lost
    captures or earlier history. A complete-history reconciliation is required
    before a downstream consumer may treat the projection as complete.
    """
    _classification(source_classification)
    if isinstance(max_episodes, bool) or not isinstance(max_episodes, int) or max_episodes < 1:
        raise ValueError("max_episodes must be a positive integer")
    if ledger_path is not None:
        primary, journal, last = _completeness_sources(ledger_path, journal_path, max_episodes, cohort)
        from services.strategy_evidence_completeness import assess_evidence_completeness
        timestamp = datetime.now(timezone.utc).isoformat()
        report = assess_evidence_completeness(primary, journal, cohort=cohort, calculated_at=timestamp,
            max_records=max_episodes, last_successful_reconciliation=last)
        result = calculate_metrics(journal.get("episodes", []), cohort=cohort,
            source_watermark=report["source_watermark"], completeness_report=report,
            calculation_timestamp=timestamp)
        result.update(capture_status="AUTHORITATIVE_RECONCILIATION_ASSESSED",
                      evidence_completeness=report, source_classification=source_classification,
                      source_classification_basis="explicit_caller_declaration")
        return result
    from services.strategy_evidence import build_episode
    with _read_only(journal_path) as connection:
        if not {"strategy_position_episodes", "strategy_episode_legs", "strategy_evidence_events"} <= _tables(connection):
            result = calculate_metrics([], cohort=cohort, history_complete=False)
            result.update(capture_status="LEGACY_NO_EPISODE_TABLES", source_classification=source_classification)
            return result
        keys = cohort.as_dict()
        keys["strategy_config_hash"] = keys.pop("config_fingerprint")
        if cohort.symbol is None:
            keys.pop("symbol")
        where = " AND ".join(f"{key} IS ?" for key in keys)
        headers = [dict(row) for row in connection.execute(
            f"SELECT * FROM strategy_position_episodes WHERE {where} ORDER BY episode_id LIMIT ?",
            (*keys.values(), max_episodes + 1))]
        if len(headers) > max_episodes:
            raise ValueError("cohort exceeds episode bound; refusing a truncated performance projection")
        episodes, watermark_data = [], []
        for header in headers:
            episode_id = header["episode_id"]
            legs = [dict(row) for row in connection.execute(
                "SELECT * FROM strategy_episode_legs WHERE episode_id=? ORDER BY leg_key", (episode_id,))]
            events = [dict(row) for row in connection.execute(
                "SELECT * FROM strategy_evidence_events WHERE episode_id=? AND kind='execution_fill' ORDER BY rowid", (episode_id,))]
            episodes.append(build_episode(header, legs=legs, events=events))
            watermark_data.append({"header": header, "legs": legs, "events": events})
        watermark = hashlib.sha256(json.dumps(watermark_data, sort_keys=True, separators=(",", ":"),
                                               allow_nan=False).encode()).hexdigest()
    result = calculate_metrics(episodes, cohort=cohort, history_complete=False, source_watermark=watermark)
    result.update(capture_status="CAPTURED_EPISODES_HISTORY_COMPLETENESS_UNVERIFIED",
                  source_classification=source_classification)
    return result


def _bounded_rows(connection, table, bound):
    """Fixed internal table names only; retain a sentinel to detect overflow."""
    if table not in _tables(connection):
        return [], False
    rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid LIMIT ?", (bound + 1,))]
    return rows[:bound], len(rows) <= bound


def _primary_snapshot(connection, bound):
    from data.paper_evidence_outbox import decode_row
    snapshot = {}
    complete = True
    for key, table in (("trades", "paper_trades"), ("positions", "positions"),
                       ("executions", "paper_executions"), ("outbox", "paper_evidence_outbox")):
        snapshot[key], finished = _bounded_rows(connection, table, bound)
        complete = complete and finished
    snapshot["outbox"] = [decode_row(row) for row in snapshot["outbox"]]
    snapshot.update(source_complete=complete, consistent_snapshot=True,
                    outbox_supported="paper_evidence_outbox" in _tables(connection), source_kind="sqlite_read_only_export")
    return snapshot


def _journal_snapshot(connection, bound):
    """Read the same immutable facts as JournalStore, without constructing it.

    Opening JournalStore would migrate an old export. This adapter performs
    SELECTs only and uses the shared episode projector for financial meaning.
    """
    from services.strategy_evidence import build_episode
    raw, complete = {}, True
    for key, table in (("events", "strategy_evidence_events"), ("headers", "strategy_position_episodes"),
                       ("legs", "strategy_episode_legs"), ("journals", "trade_decision_journal"),
                       ("timeline", "trade_decision_events"), ("configurations", "strategy_evidence_versions")):
        raw[key], finished = _bounded_rows(connection, table, bound)
        complete = complete and finished
    events, by_episode = [], {}
    for original in raw["events"]:
        event = dict(original)
        event["payload"] = json.loads(event.pop("payload_json"))
        event.pop("envelope_json", None)
        events.append(event)
        if event.get("kind") == "execution_fill":
            by_episode.setdefault(event.get("episode_id"), []).append(event)
    episodes = [build_episode(header, events=by_episode.get(header["episode_id"], [])) for header in raw["headers"]]
    by_trade = {}
    for event in raw["timeline"]:
        by_trade.setdefault(event.get("trade_id"), []).append(event)
    for row in raw["journals"]:
        row["events"] = by_trade.get(row.get("trade_id"), [])
    legs = []
    for original in raw["legs"]:
        leg = dict(original)
        leg["metadata"] = json.loads(leg.pop("metadata_json"))
        legs.append(leg)
    return {"source_complete": complete, "events": events, "episodes": episodes, "legs": legs,
            "journals": raw["journals"], "configurations": [json.loads(row["identity_json"]) for row in raw["configurations"]]}


def _last_reconciliation(connection, cohort):
    if "strategy_evidence_reconciliation_runs" not in _tables(connection):
        return None
    query = "SELECT calculated_at FROM strategy_evidence_reconciliation_runs WHERE status='COMPLETE'"
    arguments = []
    if cohort is not None:
        query += " AND cohort_json=?"
        from services.strategy_evidence import evidence_json
        arguments.append(evidence_json(cohort.as_dict()))
    row = connection.execute(query + " ORDER BY calculated_at DESC,rowid DESC LIMIT 1", arguments).fetchone()
    return row[0] if row else None


def _completeness_sources(ledger_path, journal_path, bound, cohort):
    from services.strategy_evidence_completeness import _hash
    with _read_only(ledger_path) as primary_connection, _read_only(journal_path) as journal_connection:
        primary = _primary_snapshot(primary_connection, bound)
        journal = _journal_snapshot(journal_connection, bound)
        last = _last_reconciliation(journal_connection, cohort)
    # Different stores cannot provide one atomic snapshot. Fence the journal
    # assessment with a second authoritative read; concurrent financial changes
    # become UNKNOWN and require another assessment, never cached verification.
    with _read_only(ledger_path) as primary_connection:
        current = _primary_snapshot(primary_connection, bound)
    if _hash(primary) != _hash(current):
        primary.update(source_complete=False, read_error="authoritative_changed_during_assessment")
    return primary, journal, last


def inspect_completeness(ledger_path, journal_path, *, source_classification: str,
                         cohort: EvidenceCohort | None = None, max_records=100_000):
    """Assess local/exported files read-only; never migrate or replay accounting."""
    _classification(source_classification)
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
        raise ValueError("max_records must be a positive integer")
    from services.strategy_evidence_completeness import assess_evidence_completeness
    primary, journal, last = _completeness_sources(ledger_path, journal_path, max_records, cohort)
    result = assess_evidence_completeness(primary, journal, cohort=cohort,
        max_records=max_records, last_successful_reconciliation=last)
    result.update(source_classification=source_classification,
                  source_classification_basis="explicit_caller_declaration",
                  sources={"ledger": str(Path(ledger_path).resolve()), "journal": str(Path(journal_path).resolve())})
    return result
