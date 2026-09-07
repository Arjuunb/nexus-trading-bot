import copy
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from services.price_action_governance import PersistenceBlocked, PriceActionJournalStore
from bot.types import Bar
from services.native_price_action import NativePriceActionEngine, PriceActionConfig
from services.price_action_lab import PriceActionLabRuntime, PriceActionPaperAccount


def evidence(*, status="LOST", net_r=-1.05, health="SYNCHRONIZED",
             partition="development"):
    setup = {
        "id": "setup-1", "strategy_id": "PA1_SR_REJECTION",
        "direction": "bullish", "phase": status,
        "created_at": "2026-08-24T10:00:00+00:00", "zone_id": "zone-1",
        "trigger_event_id": "event-1", "reasons": ["confirmed rejection"],
        "missing_conditions": [], "pattern_metadata": [{"name": "bullish_pin_bar"}],
        "transitions": [{"from_phase": "ORDER_PENDING", "to_phase": status}],
    }
    state = {
        "symbol": "BTCUSDT", "timeframe": "5m",
        "metrics_scope": {
            "configuration_id": "config-1", "engine_fingerprint": "engine-1",
            "dataset_fingerprint": "dataset-1", "experiment_id": "experiment-1",
        },
        "data_provenance": {
            "market_data_mode": "LIVE", "market_data_source": "Binance public",
            "exchange": "Binance USDⓈ-M Futures",
        },
        "snapshot": {"structure_bias": "bullish"},
        "zones": [{"id": "zone-1", "role": "support", "original_role": "support",
                   "low": 99, "high": 100, "touch_count": 1}],
        "events": [{"id": "event-1", "event_type": "zone_rejection",
                    "occurred_at": "2026-08-24T10:00:00+00:00"}],
        "setups": [setup],
        "proposals": [{"id": "proposal-1", "setup_id": "setup-1",
                       "entry": 101, "stop": 99, "target": 106,
                       "signal_at": "2026-08-24T10:00:00+00:00",
                       "entry_model": "confirmation", "valid_until_index": 3}],
        "orders": [],
        "trades": [{"id": "research-1", "setup_id": "setup-1", "status": status,
                    "net_r": net_r, "gross_r": -1 if net_r is not None else None,
                    "costs_r": .05, "closed_at": "2026-08-24T10:10:00+00:00",
                    "reason": "conservative test outcome"}],
    }
    session = {"id": "session-1", "strategy_config": {"entry_model": "confirmation"}}
    paper = {"order_metadata": [], "orders": [], "trades": []}
    feed = {"state": health, "health_reason": "test evidence"}
    return state, session, paper, feed, partition


def capture(store, **changes):
    state, session, paper, feed, partition = evidence(**changes)
    ids = store.capture(visual_state=state, session=session, paper_state=paper,
                        feed_status=feed, partition_label=partition)
    return ids[0]


def revision_count(store, journal_id):
    return store._db.execute(
        "SELECT COUNT(*) FROM pa_journal_revisions WHERE journal_id=?", (journal_id,)
    ).fetchone()[0]


def test_capture_material_projection_deduplicates_refresh_and_transient_state(tmp_path):
    store = PriceActionJournalStore(tmp_path / "pa.db")
    state, session, paper, feed, partition = evidence(status="WATCHING_LOCATION", net_r=None)
    state["trades"] = []
    state["candles"] = [{"timestamp": "2026-08-24T09:55:00+00:00", "close": 100}]
    state["live_display"] = {"bid": 100, "ask": 101, "heartbeat": "first"}
    journal_id = store.capture(
        visual_state=state, session=session, paper_state=paper,
        feed_status=feed, partition_label=partition)[0]

    for index in range(100):
        refreshed = copy.deepcopy(state)
        refreshed["live_display"] = {
            "bid": 200 + index, "ask": 201 + index, "heartbeat": f"tick-{index}",
        }
        refreshed["candles"].append({
            "timestamp": f"2026-08-24T11:{index % 60:02d}:00+00:00",
            "close": 200 + index,
        })
        assert store.capture(
            visual_state=refreshed, session=session, paper_state=paper,
            feed_status={"state": "SYNCHRONIZED", "health_reason": f"heartbeat {index}"},
            partition_label=partition) == []
        store.list(session_id=session["id"])

    assert revision_count(store, journal_id) == 1
    payload = store.get(journal_id)["latest"]
    assert "candles" not in payload
    assert payload["identity"]["dataset_fingerprint"].startswith("decision-evidence-")


def test_native_mtf_identity_is_immutable_material_journal_evidence(tmp_path):
    store = PriceActionJournalStore(tmp_path / "mtf.db")
    state, session, paper, feed, partition = evidence(
        status="WATCHING_LOCATION", net_r=None)
    state["trades"] = []
    state["mtf_evidence"] = {
        "entry_timeframe": "5m",
        "primary": {
            "htf_timeframe": "1h", "htf_candle_id": "one-hour-12",
            "htf_close_timestamp": "2026-08-24T12:00:00+00:00",
            "htf_bias": "BULLISH", "heartbeat": "ignore-me",
        },
        "secondary": {
            "htf_timeframe": "4h", "htf_candle_id": "four-hour-12",
            "htf_close_timestamp": "2026-08-24T12:00:00+00:00",
            "htf_bias": "BEARISH",
        },
    }
    journal_id = store.capture(
        visual_state=state, session=session, paper_state=paper,
        feed_status=feed, partition_label=partition)[0]

    saved = store.get(journal_id)["latest"]["market_context"]["mtf_evidence"]
    assert saved["primary"]["htf_candle_id"] == "one-hour-12"
    assert saved["secondary"]["htf_candle_id"] == "four-hour-12"
    assert "heartbeat" not in saved["primary"]


def test_legacy_full_payload_hash_does_not_force_migration_revision(tmp_path):
    store = PriceActionJournalStore(tmp_path / "legacy-hash.db")
    journal_id = capture(store)
    # Production revisions predate material hashes. Simulate one without
    # changing its immutable payload evidence.
    store._db.execute("DROP TRIGGER pa_journal_revisions_no_update")
    store._db.execute(
        "UPDATE pa_journal_revisions SET payload_hash=? WHERE journal_id=?",
        ("revision-legacy-full-payload", journal_id))
    store._create_immutability_triggers()
    state, session, paper, feed, partition = evidence()

    assert store.capture(visual_state=state, session=session, paper_state=paper,
                         feed_status=feed, partition_label=partition) == []
    assert revision_count(store, journal_id) == 1


def test_one_hundred_dashboard_status_reads_create_no_revision(tmp_path):
    class Market:
        @staticmethod
        def public_usdm_window(*_args, **_kwargs):
            return []

    account = PriceActionPaperAccount(tmp_path / "dashboard.db")
    state, session, paper, feed, partition = evidence()
    journal_id = account.journal.capture(
        visual_state=state, session=session, paper_state=paper,
        feed_status=feed, partition_label=partition)[0]
    runtime = PriceActionLabRuntime(Market(), account, autostart=False)

    for _ in range(100):
        status = runtime.bot_status()
        assert status["persistence"]["state"] == "AVAILABLE"

    assert revision_count(account.journal, journal_id) == 1


def test_material_lifecycle_fill_and_close_each_create_one_revision(tmp_path):
    store = PriceActionJournalStore(tmp_path / "pa.db")
    state, session, paper, feed, partition = evidence(status="WATCHING_LOCATION", net_r=None)
    state["trades"] = []
    journal_id = store.capture(
        visual_state=state, session=session, paper_state=paper,
        feed_status=feed, partition_label=partition)[0]

    state["setups"][0]["phase"] = "ORDER_PENDING"
    state["setups"][0]["transitions"].append({
        "from_phase": "WATCHING_LOCATION", "to_phase": "ORDER_PENDING",
    })
    assert store.capture(visual_state=state, session=session, paper_state=paper,
                         feed_status=feed, partition_label=partition) == [journal_id]
    assert store.capture(visual_state=state, session=session, paper_state=paper,
                         feed_status=feed, partition_label=partition) == []

    paper.update({
        "order_metadata": [{"order_id": "order-1", "config": {
            "proposal": {"setup_id": "setup-1"}, "entry": 101, "stop": 99,
            "target": 106, "risk_amount": 20, "quantity": 1,
        }}],
        "orders": [{"id": "order-1", "average_price": 101, "quantity": 1,
                    "symbol": "BTCUSDT"}],
        "trades": [{"order_id": "order-1", "fee": .04, "price": 101,
                    "timestamp": "2026-08-24T10:05:00+00:00"}],
        "activity": [], "funding_events": [],
    })
    assert store.capture(visual_state=state, session=session, paper_state=paper,
                         feed_status=feed, partition_label=partition) == [journal_id]
    assert store.capture(visual_state=state, session=session, paper_state=paper,
                         feed_status=feed, partition_label=partition) == []

    state["setups"][0]["phase"] = "TARGET_HIT"
    state["setups"][0]["transitions"].append({
        "from_phase": "ORDER_PENDING", "to_phase": "TARGET_HIT",
    })
    state["trades"] = [{
        "id": "research-1", "setup_id": "setup-1", "status": "TARGET_HIT",
        "net_r": 2.4, "gross_r": 2.5, "costs_r": .1,
        "closed_at": "2026-08-24T10:20:00+00:00", "reason": "target",
    }]
    assert store.capture(visual_state=state, session=session, paper_state=paper,
                         feed_status=feed, partition_label=partition) == [journal_id]
    assert store.capture(visual_state=state, session=session, paper_state=paper,
                         feed_status=feed, partition_label=partition) == []
    assert revision_count(store, journal_id) == 4


def test_wal_allows_read_during_write_and_capture_retries_after_lock(tmp_path):
    path = tmp_path / "pa.db"
    store = PriceActionJournalStore(path, busy_timeout_ms=20)
    journal_id = capture(store)
    writer = sqlite3.connect(path, timeout=.02, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO pa_candidate_events VALUES (?,?,?,?,?,?,?)", (
        "lock-holder", "candidate", "A", "B", "test", "test", "now"))

    read_result = []
    reader = threading.Thread(
        target=lambda: read_result.append(store.list(session_id="session-1")))
    reader.start()
    reader.join(timeout=1)
    assert not reader.is_alive()
    assert read_result[0]["entries"]

    state, session, paper, feed, partition = evidence(status="WON", net_r=2.4)
    with pytest.raises(PersistenceBlocked) as blocked:
        store.capture(visual_state=state, session=session, paper_state=paper,
                      feed_status=feed, partition_label=partition)
    assert blocked.value.code == "PERSISTENCE_BLOCKED"
    writer.execute("ROLLBACK")
    writer.close()

    assert store.capture(visual_state=state, session=session, paper_state=paper,
                         feed_status=feed, partition_label=partition) == [journal_id]
    assert revision_count(store, journal_id) == 2


def test_journal_is_immutable_filterable_and_revisioned(tmp_path):
    store = PriceActionJournalStore(tmp_path / "pa.db")
    journal_id = capture(store)
    row = store.get(journal_id)
    assert row["immutable"] is True
    assert row["latest"]["identity"]["execution_mode"] == "PAPER"
    assert row["latest"]["identity"]["market_data_mode"] == "LIVE"
    assert row["latest"]["review"]["learning_classification"] == "STRATEGY_LOSS"
    filtered = store.list(session_id="session-1", strategy_id="PA1_SR_REJECTION",
                          symbol="BTCUSDT", timeframe="5m", result="lost")
    assert filtered["statistics"] == {
        "setups": 1, "completed": 1, "wins": 0, "losses": 1,
        "net_r": -1.05, "expectancy_r": -1.05,
    }
    assert store.list(symbol="ETHUSDT")["entries"] == []
    assert store.list(direction="bullish", trigger_type="zone_rejection",
                      zone_type="support", touch_count=1, regime="bullish",
                      rule_compliance=True, entry_model="confirmation",
                      strategy_version="1.1.0")["statistics"]["setups"] == 1
    revised = store.revise(journal_id, notes="reviewed", tags=["loss", "loss"],
                           initiated_by="tester")
    assert revised["latest"]["review"]["researcher_notes"] == "reviewed"
    assert revised["latest"]["review"]["tags"] == ["loss"]
    assert len(revised["revisions"]) == 2
    state, session, paper, feed, partition = evidence()
    assert store.capture(visual_state=state, session=session, paper_state=paper,
                         feed_status=feed, partition_label=partition) == []
    assert store.get(journal_id)["latest"]["review"]["researcher_notes"] == "reviewed"
    assert len(store.get(journal_id)["revisions"]) == 2
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._db.execute("UPDATE pa_journal_entries SET symbol='ETHUSDT'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._db.execute("DELETE FROM pa_journal_revisions")


def test_data_failure_is_excluded_and_classified_without_hiding_it(tmp_path):
    store = PriceActionJournalStore(tmp_path / "pa.db")
    journal_id = capture(store, health="STALE_CANDLES")
    record = store.get(journal_id)["latest"]
    assert record["review"]["learning_classification"] == "DATA_QUALITY_FAILURE"
    assert record["review"]["include_in_research_statistics"] is False
    assert store.list(data_quality="STALE_CANDLES")["statistics"]["setups"] == 1


def test_journal_preserves_proven_excursion_quote_timing_and_funding(tmp_path):
    store = PriceActionJournalStore(tmp_path / "evidence.db")
    state, session, paper, feed, partition = evidence()
    state["trades"][0].update({
        "maximum_favourable_excursion_r": 1.4,
        "maximum_adverse_excursion_r": .6,
        "bars_to_entry": 2, "bars_in_trade": 5,
        "excursion_model": "completed_ohlc_conservative",
    })
    paper.update({
        "order_metadata": [{
            "order_id": "order-1", "config": {
                "proposal": {"setup_id": "setup-1"}, "entry": 101, "stop": 99,
                "target": 106, "risk_amount": 20, "quantity": 1,
            },
        }],
        "orders": [{"id": "order-1", "average_price": 101, "quantity": 1,
                    "symbol": "BTCUSDT"}],
        "trades": [{"order_id": "order-1", "fee": .04, "price": 101,
                    "timestamp": "2026-08-24T10:05:00+00:00"}],
        "activity": [{
            "kind": "paper_order_filled", "object_id": "order-1",
            "payload": {"execution_quote": {
                "bid": 100.9, "ask": 101.1, "mark": 101,
                "source": "Binance USDⓈ-M reconciled public streams",
            }},
        }],
        "funding_events": [{
            "order_id": "order-1", "applied": 1, "amount": -.02,
            "funding_time": "2026-08-24T16:00:00+00:00", "funding_rate": .0001,
            "mark_price": 102,
        }],
    })
    journal_id = store.capture(
        visual_state=state, session=session, paper_state=paper,
        feed_status=feed, partition_label=partition)[0]
    row = store.get(journal_id)["latest"]
    assert row["outcome"]["maximum_favourable_excursion"] == 1.4
    assert row["outcome"]["maximum_adverse_excursion"] == .6
    assert row["outcome"]["bars_to_entry"] == 2
    assert row["outcome"]["bars_in_trade"] == 5
    assert row["order_risk"]["bid_ask_fill"]["mark"] == 101
    assert row["order_risk"]["fill_quote_evidence_status"] == \
        "RECONCILED_PUBLIC_STREAM_QUOTE"
    assert row["order_risk"]["funding"]["amount_usdt"] == -.02
    assert row["order_risk"]["funding"]["normalized_r"] == pytest.approx(-.001)


def test_decision_evidence_is_frozen_and_later_data_pause_stays_excluded(tmp_path):
    store = PriceActionJournalStore(tmp_path / "pa.db")
    state, session, paper, feed, partition = evidence()
    journal_id = store.capture(
        visual_state=state, session=session, paper_state=paper,
        feed_status=feed, partition_label=partition)[0]
    original = store.get(journal_id)["latest"]
    state["metrics_scope"]["dataset_fingerprint"] = "later-rolling-window"
    assert store.capture(
        visual_state=state, session=session, paper_state=paper,
        feed_status=feed, partition_label=partition) == []
    assert len(store.get(journal_id)["revisions"]) == 1
    assert store.get(journal_id)["latest"]["identity"]["dataset_fingerprint"] == \
        original["identity"]["dataset_fingerprint"]

    paper["candidates"] = [{
        "source_proposal_id": "proposal-1", "status": "DATA_PAUSED",
        "created_at": "2026-08-24T10:05:00+00:00",
        "payload": {"reason": "closed-candle stream became stale"},
    }]
    store.capture(
        visual_state=state, session=session, paper_state=paper,
        feed_status={"state": "STALE_CANDLES", "health_reason": "test outage"},
        partition_label=partition)
    paused = store.get(journal_id)["latest"]
    assert paused["review"]["learning_classification"] == "DATA_QUALITY_FAILURE"
    assert paused["review"]["include_in_research_statistics"] is False


def test_rule_and_execution_failures_are_not_relabelled_as_strategy_losses(tmp_path):
    rule_store = PriceActionJournalStore(tmp_path / "rule.db")
    state, session, paper, feed, partition = evidence()
    state["setups"][0]["missing_conditions"] = ["later_confirmation"]
    rule_id = rule_store.capture(visual_state=state, session=session, paper_state=paper,
                                 feed_status=feed, partition_label=partition)[0]
    assert rule_store.get(rule_id)["latest"]["review"]["learning_classification"] == "RULE_VIOLATION"

    execution_store = PriceActionJournalStore(tmp_path / "execution.db")
    state, session, paper, feed, partition = evidence(net_r=-.1)
    state["trades"][0]["gross_r"] = .2
    execution_id = execution_store.capture(
        visual_state=state, session=session, paper_state=paper,
        feed_status=feed, partition_label=partition)[0]
    assert execution_store.get(execution_id)["latest"]["review"]["learning_classification"] == "EXECUTION_LOSS"


def test_candidate_governance_is_one_change_development_only_and_never_live(tmp_path):
    store = PriceActionJournalStore(tmp_path / "pa.db")
    journal_id = capture(store)
    common = {
        "parent_strategy_version": "1.1.0", "evidence_ids": [journal_id],
        "contradicting_evidence": [], "development_period": {"start": "2026-01-01"},
        "validation_period": {"start": "2026-07-01"}, "code_fingerprint": "engine-1",
        "dataset_fingerprint": "dataset-1", "expected_benefit": "reduce weak retests",
        "risks": ["lower sample size"], "source_partition": "development",
    }
    with pytest.raises(ValueError, match="exactly one"):
        store.propose_candidate(rule_difference={"first_touch_only": True,
                                                 "zone_expiry_bars": 10}, **common)
    with pytest.raises(ValueError, match="development"):
        store.propose_candidate(rule_difference={"first_touch_only": True},
                                **{**common, "source_partition": "untouched_oos"})
    candidate = store.propose_candidate(rule_difference={"first_touch_only": True}, **common)
    assert candidate["status"] == "DRAFT"
    assert candidate["live_execution_allowed"] is False
    with pytest.raises(ValueError, match="not allowed"):
        store.candidate_transition(candidate["id"], action="approve_shadow",
                                   reason="cannot skip evidence gates", initiated_by="tester")
    for action in ("record_development", "pass_validation", "pass_robustness"):
        candidate = store.candidate_transition(candidate["id"], action=action,
                                               reason=f"explicit {action} evidence",
                                               initiated_by="tester")
    approved = store.candidate_transition(candidate["id"], action="approve_shadow",
                                          reason="manual research approval",
                                          initiated_by="tester")
    assert approved["status"] == "APPROVED_FOR_SHADOW"
    store.record_shadow(run_id="run-1", candidate_id=candidate["id"],
                        session_id="session-1", candle_identity="candle-1",
                        baseline={"proposal_ids": []}, candidate={"proposal_ids": ["p1"]})
    store.record_shadow(
        run_id="run-1", candidate_id=candidate["id"], session_id="session-1",
        candle_identity="candle-2",
        baseline={
            "proposals": [{"setup_id": "setup-loss", "entry": 101, "stop": 99}],
            "trades": [{"setup_id": "setup-loss", "status": "LOST", "net_r": -1.1,
                        "costs_r": .1, "closed_at": "2026-08-24T10:10:00+00:00"}],
        },
        candidate={"proposals": [], "trades": []},
    )
    report = store.shadow_report(candidate["id"])
    assert report["candidate_only_signals"] == 1
    assert report["baseline_only_signals"] == 1
    assert report["avoided_losses"] == 1
    assert report["net_effect_r"] == 1.1
    assert report["trade_count_change"] == -1
    assert report["changed_entries"] == 0
    assert report["changed_stops"] == 0
    assert report["net_oos_effect"] is None
    assert report["net_oos_effect_status"] == "NOT_OOS_PAPER_SHADOW"
    assert report["official_paper_account_affected"] is False
    assert report["promotion_automatic"] is False


def test_factory_reset_is_the_only_supported_journal_deletion_path(tmp_path):
    store = PriceActionJournalStore(tmp_path / "pa.db")
    capture(store)
    assert store.list()["entries"]
    store.factory_reset()
    assert store.list()["entries"] == []
    triggers = {row[0] for row in store._db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "pa_journal_entries_no_delete" in triggers


def test_shadow_candidate_observes_same_later_candle_without_touching_account(tmp_path):
    class Market:
        def public_usdm_window(self, *_args, **_kwargs):
            return []

    account = PriceActionPaperAccount(tmp_path / "shadow.db")
    runtime = PriceActionLabRuntime(Market(), account)
    runtime.identity = ("BTCUSDT", "5m")
    runtime.engine = NativePriceActionEngine(PriceActionConfig("BTCUSDT", timeframe="5m"))
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    runtime.engine.ingest_closed_bars([
        Bar(start + timedelta(minutes=5 * index), 100 + index, 102 + index,
            99 + index, 101 + index, 1000)
        for index in range(12)
    ])
    development_id = capture(account.journal)
    candidate = account.journal.propose_candidate(
        parent_strategy_version="1.1.0", rule_difference={"first_touch_only": True},
        evidence_ids=[development_id], contradicting_evidence=[], development_period={"start": "2026-01-01"},
        validation_period={"start": "2026-07-01"}, code_fingerprint="engine-1",
        dataset_fingerprint="dataset-1", expected_benefit="test isolation",
        risks=["reduced signals"], source_partition="development")
    for action in ("record_development", "pass_validation", "pass_robustness", "approve_shadow"):
        candidate = account.journal.candidate_transition(
            candidate["id"], action=action, reason=f"explicit {action}", initiated_by="tester")
    before = account.state()["account"]
    started = runtime.start_shadow(candidate["id"])
    later = Bar(start + timedelta(minutes=60), 112, 114, 111, 113, 1000)
    runtime.engine.process_closed_bar(later)
    runtime._advance_shadows(later)
    report = runtime.stop_shadow(candidate["id"])
    after = account.state()["account"]
    assert started["official_account_affected"] is False
    assert report["observations"] == 1
    assert report["official_paper_account_affected"] is False
    assert before == after
