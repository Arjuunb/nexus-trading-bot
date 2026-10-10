"""Immutable intelligence metadata shares the journal, never the financial ledger."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from data.journal_store import JournalStore
from data.journal_context_store import JournalContextStore
from data.journal_context_migrations import apply_context_migrations
from services.strategy_identity import configuration_fingerprint


AT = "2026-01-01T00:00:00+00:00"
CALCULATED_AT = "2026-01-01T00:01:00+00:00"


def _store(path=":memory:"):
    journal = JournalStore(str(path))
    apply_context_migrations(journal._c)
    journal._c.commit()
    return journal, JournalContextStore(journal)


def _definition(version="1.0.0", threshold=1):
    parameters = {"threshold": threshold, "sessions": []}
    return {"classifier_id": "market_context", "classifier_version": version,
            "parameter_hash": configuration_fingerprint(parameters), "parameters": parameters}


def _episode(journal, episode="episode-1", owner="owner-1", instance="instance-1", mode="paper"):
    scope = {"strategy_id": None, "strategy_version": None, "strategy_config_hash": None,
             "owner_id": owner, "account_id": "account-1", "instance_id": instance,
             "simulation_session_id": "session-1", "lab_id": None,
             "source_kind": "forward_paper", "execution_mode": mode, "symbol": "XRPUSDT"}
    journal.record_execution_evidence("fill:" + episode,
        payload={"action": "opened", "price": "1", "size": "1", "initial_risk": "0.1"},
        scope={**scope, "episode_id": episode, "trade_id": "trade:" + episode,
               "position_id": "position:" + episode, "observed_at": AT},
        links=[{"trade_id": "trade:" + episode, "position_id": "position:" + episode}],
        create_episode=True)
    return scope


def _context(scope, episode="episode-1", version="1.0.0", **changes):
    return {**scope, "episode_id": episode, "trade_id": "trade:" + episode,
            "signal_timestamp": AT, "entry_timestamp": CALCULATED_AT,
            "classification_timestamp": CALCULATED_AT, "entry_timeframe": "15m",
            "higher_timeframe": "1h", "session": "ASIA", "trend_regime": "UNKNOWN",
            "volatility_regime": "UNKNOWN", "structure_regime": "UNKNOWN",
            "context_quality": "INSUFFICIENT_HISTORY", "evidence_quality": "UNKNOWN",
            "classifier_id": "market_context", "classifier_version": version,
            "parameter_hash": _definition(version)["parameter_hash"], **changes}


def test_classifier_version_binds_parameters_and_hash_permanently():
    _, store = _store()
    first = store.save_classifier_version(_definition())
    assert store.save_classifier_version(_definition()) == first
    with pytest.raises(ValueError, match="version conflict"):
        store.save_classifier_version(_definition(threshold=2))
    changed = _definition("1.1.0", threshold=2)
    assert store.save_classifier_version(changed)["parameter_hash"] != first["parameter_hash"]
    with pytest.raises(ValueError, match="parameter hash"):
        store.save_classifier_version({**changed, "parameter_hash": "invalid"})


def test_context_exact_retry_is_one_immutable_snapshot_and_future_data_is_rejected():
    journal, store = _store()
    scope = _episode(journal)
    store.save_classifier_version(_definition())
    snapshot = _context(scope)
    assert store.record_market_context(snapshot) is True
    assert store.record_market_context(deepcopy(snapshot)) is False
    fetched = store.get_market_context("episode-1", "market_context", "1.0.0", snapshot["parameter_hash"])
    assert fetched["context_quality"] == "INSUFFICIENT_HISTORY"
    assert fetched["snapshot_id"]
    assert fetched["configuration_hash"] is None
    with pytest.raises(ValueError, match="context conflict"):
        store.record_market_context({**snapshot, "trend_regime": "BULL"})
    with pytest.raises(ValueError, match="future"):
        store.record_market_context({**snapshot, "market_data_timestamp": CALCULATED_AT})


@pytest.mark.parametrize("field", ["owner_id", "instance_id", "execution_mode", "symbol", "strategy_config_hash"])
def test_context_cannot_attach_to_an_episode_in_a_different_cohort(field):
    journal, store = _store()
    scope = _episode(journal)
    store.save_classifier_version(_definition())
    with pytest.raises(ValueError, match="episode scope"):
        store.record_market_context(_context(scope, **{field: "other"}))


def test_unknown_historical_context_keeps_missing_signal_timestamp_unknown():
    journal, store = _store()
    scope = _episode(journal)
    store.save_classifier_version(_definition())
    snapshot = _context(scope, signal_timestamp=None, context_quality="UNKNOWN", session="UNKNOWN")
    store.record_market_context(snapshot)
    assert store.market_contexts(filters={"owner_id": "owner-1"})[0]["signal_timestamp"] is None
    with pytest.raises(ValueError, match="unknown signal"):
        store.record_market_context({**snapshot, "context_quality": "VALID"})
    with pytest.raises(ValueError, match="unknown signal"):
        store.record_market_context({**snapshot, "last_closed_candle_timestamp": AT})
    with pytest.raises(ValueError, match="unknown signal"):
        store.record_market_context({**snapshot, "session": "LONDON"})


def test_scoped_context_queries_bounds_and_incremental_candidates():
    journal, store = _store()
    definition = store.save_classifier_version(_definition())
    for n in range(3):
        episode = f"episode-{n}"
        scope = _episode(journal, episode, owner="owner-1" if n < 2 else "owner-2")
        if n < 2:
            store.record_market_context(_context(scope, episode))
    assert len(store.market_contexts(filters={"owner_id": "owner-1"})) == 2
    assert store.market_contexts(filters={"owner_id": "owner-2"}) == []
    bounded = store.context_snapshot(max_records=1, filters={"owner_id": "owner-1"})
    assert bounded["source_complete"] is False and bounded["source_count"] == 2
    pending = store.unclassified_episodes(**{key: definition[key] for key in
        ("classifier_id", "classifier_version", "parameter_hash")}, filters={"owner_id": "owner-2"})
    assert [row["episode_id"] for row in pending] == ["episode-2"]
    assert store.cached_cohorts(filters={"owner_id": "owner-1"}) == [
        {key: scope.get(key) for key in store.COHORT_FIELDS} | {"owner_id": "owner-1"}]


def test_identity_snapshot_is_bounded_and_cannot_leak_other_owner_entry_keys():
    journal, store = _store()
    definition = store.save_classifier_version(_definition())
    for n, owner in enumerate(("owner-1", "owner-1", "owner-2")):
        episode = f"episode-{n}"
        scope = _episode(journal, episode, owner=owner)
        store.record_market_context(_context(scope, episode))
    bounded = store.context_identity_snapshot(filters={"owner_id": "owner-1"}, max_records=1)
    assert bounded["source_count"] == 2 and bounded["source_complete"] is False
    assert len(bounded["keys"]) == 1
    complete = store.context_identity_snapshot(filters={"owner_id": "owner-2"}, max_records=10)
    assert complete["source_complete"] is True and complete["source_count"] == 1
    assert complete["keys"] == [("episode-2", "trade:episode-2", "market_context", "1.0.0",
                                 definition["parameter_hash"], "ENTRY")]


def _run(run_id="run-1", watermark="input-1", **updates):
    return {"run_id": run_id, "cache_key": "cache-1", "input_watermark": watermark,
            "contract_version": "strategy_intelligence.v2", "calculated_at": CALCULATED_AT,
            "scope": {"owner_id": "owner-1"}, "cohort": {"strategy_id": "adaptive_trend_pullback"},
            "group_by": ["symbol"], "groups": [], **updates}


def test_durable_cache_survives_restart_without_recalculation_or_mutation(tmp_path):
    path = tmp_path / "journal.db"
    journal, store = _store(path)
    report = _run()
    assert store.record_intelligence_run(report) is True
    assert store.record_intelligence_run(deepcopy(report)) is False
    with pytest.raises(ValueError, match="run conflict"):
        store.record_intelligence_run({**report, "groups": [{"net_pnl": "999"}]})
    journal._c.close()
    journal, restarted = _store(path)
    assert restarted.get_intelligence_run("cache-1", "input-1") == report
    assert restarted.get_intelligence_run("cache-1", "input-2") is None
    assert restarted.get_latest_intelligence_run("cache-1", filters={"owner_id": "owner-2"}) is None
    assert restarted.get_latest_intelligence_run("cache-1", filters={"owner_id": "owner-1"}) == report
    second = _run("run-2", "input-2", calculated_at="2026-01-01T00:02:00+00:00")
    restarted.record_intelligence_run(second)
    assert restarted.get_latest_intelligence_run("cache-1") == second


def test_cache_refresh_can_append_a_fresh_run_for_the_same_unchanged_source():
    _, store = _store()
    first = _run()
    second = _run("run-2", calculated_at="2026-01-01T00:06:00+00:00")
    store.record_intelligence_run(first)
    store.record_intelligence_run(second)
    assert store.get_intelligence_run("cache-1", "input-1") == second
    with pytest.raises(ValueError, match="scope conflict"):
        store.record_intelligence_run(_run("run-3", cohort={"owner_id": "owner-2"}))


def test_configuration_aliases_and_explicit_unknown_scope_filters_are_exact():
    journal, store = _store()
    scope = _episode(journal)
    definition = store.save_classifier_version(_definition())
    store.record_market_context(_context(scope))
    assert len(store.market_contexts(filters={"config_fingerprint": None, "lab_id": None})) == 1
    assert store.market_contexts(filters={"owner_id": None}) == []
    assert len(store.market_contexts(filters={"symbol": None})) == 1
    assert store.unclassified_episodes(**{key: definition[key] for key in
        ("classifier_id", "classifier_version", "parameter_hash")}) == []
    with pytest.raises(ValueError, match="alias conflict"):
        store.record_market_context(_context(scope, configuration_hash="unknown-fingerprint"))
    with pytest.raises(ValueError, match="unsupported"):
        store.market_contexts(filters={"pnl": 1})
    with pytest.raises(ValueError, match="limit"):
        store.market_contexts(limit=True)


def test_partial_exit_remainder_cannot_masquerade_as_a_new_context_entry():
    journal, store = _store()
    scope = _episode(journal)
    store.save_classifier_version(_definition())
    journal.record_execution_evidence("reduce", payload={"action": "reduced", "remainder_trade_id": "remainder"},
        scope={**scope, "episode_id": "episode-1", "trade_id": "trade:episode-1",
               "position_id": "position:episode-1", "observed_at": CALCULATED_AT},
        links=[{"trade_id": "remainder", "position_id": "remainder-position",
                "parent_trade_id": "trade:episode-1"}], create_episode=False)
    with pytest.raises(ValueError, match="actual episode entry"):
        store.record_market_context(_context(scope, trade_id="remainder"))


def test_context_research_version_is_an_append_and_preserves_original_entry():
    journal, store = _store()
    scope = _episode(journal)
    store.save_classifier_version(_definition())
    original = _context(scope)
    store.record_market_context(original)
    changed = store.save_classifier_version(_definition("2.0.0", threshold=2))
    research = _context(scope, version="2.0.0", parameter_hash=changed["parameter_hash"],
                        classification_kind="RESEARCH", trend_regime="RANGE")
    store.record_market_context(research)
    assert len(store.market_contexts(filters={"episode_id": "episode-1"})) == 2
    assert store.get_market_context("episode-1", "market_context", "1.0.0", original["parameter_hash"])["trend_regime"] == "UNKNOWN"
    assert store.get_market_context("episode-1", "market_context", "2.0.0", changed["parameter_hash"], "RESEARCH")["trend_regime"] == "RANGE"


def test_real_point_in_time_classifier_result_round_trips_as_immutable_context():
    from bot.types import Bar
    from services.market_context_classifier import (
        CandleObservation, ClassifierParameters, classifier_definition, classify_market_context,
    )
    parameters = ClassifierParameters(ema_period=2, ema_slope_lag=1, adx_period=1,
        atr_period=1, volatility_window=2, htf_ema_period=2)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def rows(count, first, step):
        observations = []
        for n in range(count):
            price = 100 + n * 2
            bar = Bar(first + n * step, price, price + 1, price - 1, price + .5, 10)
            close = bar.timestamp + step
            observations.append(CandleObservation(bar, close, True, close))
        return observations

    definition = classifier_definition(parameters)
    result = classify_market_context(rows(8, start, timedelta(minutes=5)),
        signal_timestamp=start + timedelta(minutes=40), entry_timeframe="5m", higher_timeframe="1h",
        higher_candles=rows(3, start - timedelta(hours=3), timedelta(hours=1)),
        market_data_source="authoritative test observations",
        classification_timestamp=start + timedelta(minutes=41), parameters=parameters)
    assert result["context_quality"] == "VALID"
    journal, store = _store()
    scope = _episode(journal)
    store.save_classifier_version(definition)
    snapshot = {**scope, **result, "episode_id": "episode-1", "trade_id": "trade:episode-1",
                "entry_timestamp": (start + timedelta(minutes=41)).isoformat(), "evidence_quality": "PARTIAL"}
    store.record_market_context(snapshot)
    restored = store.get_market_context("episode-1", definition["classifier_id"],
        definition["classifier_version"], definition["parameter_hash"])
    for field in ("trend_regime", "volatility_regime", "input_data_hash", "classification_input"):
        assert restored[field] == result[field]


def test_interrupted_additive_context_migration_is_recoverable_without_backfilling_unknowns():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""CREATE TABLE strategy_position_episodes
        (episode_id TEXT PRIMARY KEY, metadata_json TEXT);
        INSERT INTO strategy_position_episodes VALUES ('historical-1','{"old":"untouched"}');""")
    before = connection.execute("SELECT * FROM strategy_position_episodes").fetchall()

    def interruption(action, name, _table, _database, _trigger):
        if action == sqlite3.SQLITE_CREATE_INDEX and name == "idx_market_context_time":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(interruption)
    with pytest.raises(sqlite3.DatabaseError):
        apply_context_migrations(connection)
    connection.set_authorizer(None)
    apply_context_migrations(connection)
    apply_context_migrations(connection)
    assert connection.execute("SELECT * FROM strategy_position_episodes").fetchall() == before
    for table in ("regime_classifier_versions", "market_context_snapshots", "intelligence_calculation_runs"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_cached_financial_decimal_strings_retain_all_authoritative_digits():
    _, store = _store()
    amounts = {"net_pnl": "0.123456789012345678901234567890123456789",
               "fees": "0.000000000000000000000000000000000000009"}
    report = _run(groups=[amounts])
    store.record_intelligence_run(report)
    assert store.get_latest_intelligence_run("cache-1")["groups"][0] == amounts


def test_context_migration_reapplication_preserves_evidence_and_old_sql_compatibility(tmp_path):
    journal, store = _store(tmp_path / "journal.db")
    scope = _episode(journal)
    store.save_classifier_version(_definition())
    store.record_market_context(_context(scope))
    before = journal._c.execute("SELECT * FROM strategy_position_episodes").fetchall()
    apply_context_migrations(journal._c)
    journal._c.commit()
    assert journal._c.execute("SELECT * FROM strategy_position_episodes").fetchall() == before
    journal._c.execute("INSERT INTO trade_decision_journal(trade_id,status) VALUES ('legacy','open')")
    journal._c.commit()
    assert journal.get("legacy")["strategy_config_hash"] is None
    for table in ("regime_classifier_versions", "market_context_snapshots", "intelligence_calculation_runs"):
        if table == "intelligence_calculation_runs":
            store.record_intelligence_run(_run())
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            journal._c.execute(f"DELETE FROM {table}")
        journal._c.rollback()
