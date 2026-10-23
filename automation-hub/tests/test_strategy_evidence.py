"""Journal-local immutable evidence, independent of execution accounting."""
from decimal import Decimal
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from data.journal_store import JournalStore
from services.strategy_evidence import StrategyEvidence


def identity():
    configuration = {"entry_tf": "15m", "risk": "0.01"}
    canonical = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    return {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
            "strategy_config_hash": hashlib.sha256(canonical.encode()).hexdigest(),
            "configuration": configuration, "source_hash": "source-v1",
            "identity_status": "observed"}


def context():
    return {**identity(), "instance_id": "instance-a", "simulation_session_id": "session-a",
            "execution_mode": "FORWARD_PAPER", "source_kind": "forward_paper",
            "account_id": "account-a", "owner_id": "owner-a", "lab_id": None,
            "symbol": "XRPUSDT", "signal_id": "signal-a", "decision_id": "decision-a",
            "order_id": "order-a", "stop": "0.9", "risk_amount_at_entry": "0.2",
            "funding": "0", "funding_coverage": "UNMODELED"}


def fill(action="opened", execution_id="execution-open", **updates):
    facts = dict(action=action, execution_id=execution_id, symbol="XRPUSDT", side="long",
                 price="1", size="2", pnl="0", fee="0", position_id="position-a",
                 trade_id="trade-a", executed_at="2026-01-01T00:00:00+00:00", receipt={})
    facts.update(updates)
    return SimpleNamespace(**facts)


def test_snapshot_is_content_addressed_and_immutable():
    store = JournalStore()
    original = identity()
    assert store.save_strategy_identity(original)["configuration"] == original["configuration"]
    assert store.save_strategy_identity(original)["strategy_config_hash"] == original["strategy_config_hash"]
    tampered = {**original, "configuration": {"risk": "0.02"}}
    with pytest.raises(ValueError):
        store.save_strategy_identity(tampered)
    with pytest.raises(sqlite3.IntegrityError):
        store._c.execute("UPDATE strategy_evidence_versions SET configuration_json='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        store._c.execute("DELETE FROM strategy_evidence_versions")


def test_event_retry_and_conflict_are_durable(tmp_path):
    path = str(tmp_path / "journal.db")
    store = JournalStore(path)
    assert store.record_evidence_event("decision:a", kind="decision", payload={"accepted": False})
    assert not JournalStore(path).record_evidence_event("decision:a", kind="decision", payload={"accepted": False})
    with pytest.raises(ValueError, match="conflict"):
        store.record_evidence_event("decision:a", kind="decision", payload={"accepted": True})
    assert len(store.evidence_events(kind="decision")) == 1


def test_additive_upgrade_preserves_legacy_unknown_fields(tmp_path):
    path = str(tmp_path / "old.db")
    store = JournalStore(path)
    store.record_entry({"trade_id": "legacy", "sections": {}})
    store._c.close()
    store = JournalStore(path)
    assert store.get("legacy")["strategy_config_hash"] is None
    assert store.get("legacy")["episode_id"] is None
    assert store.episodes() == []
    assert JournalStore(path).get("legacy")["trade_id"] == "legacy"


def test_partial_exits_reconcile_one_completed_episode_with_decimal_precision(tmp_path):
    store = JournalStore(str(tmp_path / "journal.db"))
    observer = StrategyEvidence(store)
    observer.observe_fill(fill(), context())
    reduced = fill("reduced", "reduce-a", trade_id="trade-a", size="0.5", price="1.1",
                   pnl="0.0475", fee="0.0025", parent_trade_id="trade-a",
                   remainder_trade_id="trade-b", remainder_position_id="position-b")
    observer.observe_fill(reduced, context())
    partial = store.episodes()[0]
    assert partial["status"] == "open"
    assert partial["realised_leg_count"] == 1
    assert partial["initial_risk"] == "0.2"
    restarted = StrategyEvidence(JournalStore(store.path))
    assert not restarted.observe_fill(reduced, context())
    closed = fill("closed", "close-b", trade_id="trade-b", position_id="position-b",
                  size="1.5", price="1.2", pnl="0.294", fee="0.006",
                  executed_at="2026-01-02T00:00:00+00:00")
    restarted.observe_fill(closed, context())
    episode = store.episodes()[0]
    assert episode["status"] == "closed" and len(store.episodes()) == 1
    assert episode["realised_leg_count"] == 2
    assert episode["net_pnl"] == "0.3415"
    assert episode["fees"] == "0.0085"
    assert episode["gross_pnl"] == "0.35"
    assert Decimal(episode["net_pnl"]) / Decimal(episode["initial_risk"]) == Decimal("1.7075")
    assert set(episode["trade_ids"]) == {"trade-a", "trade-b"}


def test_scope_and_configuration_are_never_inferred_from_symbol():
    store = JournalStore()
    observer = StrategyEvidence(store)
    observer.observe_fill(fill(), context())
    other = {**context(), "instance_id": "instance-b", "simulation_session_id": "session-b"}
    observer.observe_fill(fill(execution_id="other-open", trade_id="other-trade", position_id="other-position"), other)
    assert len(store.episodes()) == 2
    assert len(store.episodes(instance_id="instance-a")) == 1
    with pytest.raises(ValueError, match="scope"):
        observer.observe_fill(fill("closed", "wrong-close", pnl="1"), other)


def test_same_execution_ids_in_different_accounts_have_separate_receipts():
    store = JournalStore()
    observer = StrategyEvidence(store)
    observer.observe_fill(fill(), context())
    other = {**context(), "instance_id": "instance-b", "simulation_session_id": "session-b"}
    observer.observe_fill(fill(trade_id="other-trade", position_id="other-position"), other)
    assert len(store.evidence_events(kind="execution_fill")) == 2
    assert store.execution_event("execution-open", instance_id="instance-a")["trade_id"] == "trade-a"
    assert store.execution_event("execution-open", instance_id="instance-b")["trade_id"] == "other-trade"
    with pytest.raises(ValueError, match="ambiguous"):
        store.execution_event("execution-open")


def test_different_execution_ids_cannot_count_the_same_leg_twice():
    store = JournalStore()
    observer = StrategyEvidence(store)
    observer.observe_fill(fill(), context())
    with pytest.raises(ValueError, match="already opened"):
        observer.observe_fill(fill(execution_id="duplicate-open"), context())
    observer.observe_fill(fill("closed", "close", pnl="0.1", fee="0.01"), context())
    with pytest.raises(ValueError, match="already closed"):
        observer.observe_fill(fill("closed", "duplicate-close", pnl="0.1", fee="0.01"), context())
    assert store.episodes()[0]["net_pnl"] == "0.1"


def test_missing_lineage_close_and_rejected_decisions_are_not_completed_trades():
    store = JournalStore()
    observer = StrategyEvidence(store)
    store.record_evidence_event("rejected-a", kind="decision_rejected", payload={"accepted": False}, **{
        key: value for key, value in context().items() if key in ("instance_id", "execution_mode", "symbol")})
    assert observer.observe_fill(fill("closed", "orphan", pnl="1"), context())
    assert store.episodes() == []
    event = store.execution_event("orphan", instance_id="instance-a", simulation_session_id="session-a")
    assert event["payload"]["lineage_status"] == "unknown"


def test_missing_financial_coverage_stays_unknown():
    store = JournalStore()
    observer = StrategyEvidence(store)
    incomplete = {key: value for key, value in context().items() if key not in ("funding", "funding_coverage")}
    observer.observe_fill(fill(), incomplete)
    observer.observe_fill(fill("closed", "close", pnl="0.1", fee="0.01"), incomplete)
    episode = store.episodes()[0]
    assert episode["funding"] is None
    assert episode["funding_coverage"] == "UNKNOWN"
    assert episode["gross_pnl"] is None
    assert episode["net_pnl"] == "0.1"


def test_receipts_and_episode_links_are_immutable():
    store = JournalStore()
    StrategyEvidence(store).observe_fill(fill(), context())
    with pytest.raises(sqlite3.IntegrityError):
        store._c.execute("UPDATE strategy_evidence_events SET payload_json='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        store._c.execute("DELETE FROM strategy_episode_legs")


def test_risk_basis_is_immutable_while_scale_in_adds_a_separate_risk_receipt():
    store = JournalStore()
    observer = StrategyEvidence(store)
    observer.observe_fill(fill(), context())
    episode_id = store.episodes()[0]["episode_id"]
    observer.observe_fill(fill(execution_id="scale-in", trade_id="trade-c", position_id="position-c", size="1"),
                          {**context(), "episode_id": episode_id, "risk_amount_at_entry": "0.1"})
    episode = store.get_episode(episode_id)
    assert episode["initial_risk"] == "0.3"
    assert episode["root_initial_risk"] == "0.2"


def test_snapshot_foreign_keys_and_frozen_journal_headers():
    store = JournalStore()
    ident = identity()
    with pytest.raises(sqlite3.IntegrityError):
        store.record_evidence_event("missing-snapshot", kind="signal", payload={}, **{
            key: ident[key] for key in ("strategy_id", "strategy_version", "strategy_config_hash")})
    store.save_strategy_identity(ident)
    entry = {**ident, "trade_id": "trade-a", "sections": {}, "initial_risk_amount_text": "0.2"}
    assert store.record_entry(entry)
    assert not store.record_entry(entry)
    with pytest.raises(ValueError, match="conflict"):
        store.record_entry({**entry, "initial_risk_amount_text": "0.3"})
    with pytest.raises(sqlite3.IntegrityError):
        store._c.execute("UPDATE trade_decision_journal SET strategy_config_hash=NULL WHERE trade_id='trade-a'")
    assert store._c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_concurrent_capture_has_one_receipt_and_one_episode(tmp_path):
    path = str(tmp_path / "concurrent.db")
    stores = [JournalStore(path) for _ in range(8)]
    observers = [StrategyEvidence(store) for store in stores]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda observer: observer.observe_fill(fill(), context()), observers))
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert len(stores[0].evidence_events(kind="execution_fill")) == 1
    assert len(stores[0].episodes()) == 1


def test_recovered_known_execution_preserves_original_fill_timestamp():
    store = JournalStore()
    observer = StrategyEvidence(store)
    observer.observe_fill(fill(), context())
    assert not observer.observe_fill(fill("recovered", executed_at="2026-01-01T00:00:01+00:00"), context())
    event = store.execution_event("execution-open", instance_id="instance-a")
    assert event["payload"]["action"] == "opened"
    assert event["observed_at"] == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="fill conflict"):
        observer.observe_fill(fill("recovered", size="3"), context())


def test_inconsistent_remainder_link_rolls_back_the_whole_capture():
    store = JournalStore()
    observer = StrategyEvidence(store)
    observer.observe_fill(fill(), context())
    observer.observe_fill(fill(execution_id="second-entry", trade_id="trade-b", position_id="position-b"), context())
    reduced = fill("reduced", "bad-reduce", size="0.5", remainder_trade_id="trade-b",
                   remainder_position_id="position-b")
    with pytest.raises(ValueError, match="leg conflict"):
        observer.observe_fill(reduced, context())
    assert store.execution_event("bad-reduce", instance_id="instance-a") is None
    assert len(store.episodes()) == 2


def test_original_pre_evidence_schema_upgrade_twice(tmp_path):
    path = str(tmp_path / "historical.db")
    connection = sqlite3.connect(path)
    connection.executescript("""
      CREATE TABLE trade_decision_journal (
        trade_id TEXT PRIMARY KEY, created_at TEXT, closed_at TEXT,
        mode TEXT, symbol TEXT, side TEXT, strategy TEXT, timeframe TEXT,
        entry REAL, stop REAL, target REAL, exit REAL, size REAL,
        risk_amount REAL, planned_rr REAL, actual_rr REAL, pnl REAL,
        result TEXT, confidence REAL, brain_score REAL, regime TEXT,
        grade TEXT, status TEXT, sections_json TEXT);
      INSERT INTO trade_decision_journal(trade_id,mode,symbol,pnl,status,sections_json)
        VALUES ('old','paper','XRPUSDT',0.84,'closed','{}');
    """)
    connection.close()
    for _ in range(2):
        store = JournalStore(path)
        old = store.get("old")
        assert old["pnl"] == 0.84
        assert old["strategy_version"] is None
        assert old["strategy_config_hash"] is None
        assert old["execution_mode"] == "LEGACY / UNVERIFIED"
        store._c.close()


def test_reversal_is_two_episodes_and_preserves_first_episode_risk():
    store = JournalStore()
    observer = StrategyEvidence(store)
    observer.observe_fill(fill(), context())
    observer.observe_fill(fill("closed", "reverse-close", pnl="-0.15", fee="0.01"),
                          {**context(), "risk_amount_at_entry": "999"})
    observer.observe_fill(fill(execution_id="reverse-open", trade_id="short-trade", position_id="short-position",
                               side="short", price="0.9"),
                          {**context(), "risk_amount_at_entry": "0.1"})
    assert len(store.episodes()) == 2
    assert len(store.completed_evidence_episodes()) == 1
    assert store.completed_evidence_episodes()[0]["initial_risk"] == "0.2"
    assert store.completed_evidence_episodes()[0]["net_pnl"] == "-0.15"


def test_authoritative_gross_and_net_receipts_do_not_double_charge_fees():
    store = JournalStore()
    observer = StrategyEvidence(store)
    observer.observe_fill(fill(), context())
    observer.observe_fill(fill("closed", "close", pnl="0.09", fee="0.01", receipt={
        "net_pnl": "0.09", "gross_pnl": "0.1", "booked_fees": "0.01",
        "funding": None, "funding_coverage": "not_modeled"}), context())
    episode = store.episodes()[0]
    assert episode["net_pnl"] == "0.09"
    assert episode["gross_pnl"] == "0.1"
    assert episode["fees"] == "0.01"
    assert episode["funding"] is None
    assert episode["funding_coverage"] == "UNMODELED"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "NaN", True])
def test_invalid_financial_receipts_are_not_saved(bad):
    store = JournalStore()
    observer = StrategyEvidence(store)
    with pytest.raises(ValueError):
        observer.observe_fill(fill(size=bad), context())
    assert store.evidence_events() == []
    assert store.episodes() == []
