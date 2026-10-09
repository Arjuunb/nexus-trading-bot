"""Retained IDs link evidence; they never confer execution authority."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from copy import deepcopy
from contextlib import closing
import sqlite3

import pytest

from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.smc_execution_links import smc_execution_links_view
from tradexa.guardian.store import GuardianStore

NOW = datetime.now(timezone.utc)


@pytest.fixture
def store(tmp_path):
    instance = GuardianStore(tmp_path / "guardian.db")
    for name in ("guardian_smc_intent_history", "guardian_smc_fill_history", "guardian_smc_journal_history"):
        instance.record_heartbeat(name, "HEALTHY", observed_at=NOW)
    return instance


def intent(store, key="decision-1", *, order="order-1", trade="trade-1", state="COMPLETE", origin="a" * 64):
    from tradexa.guardian.smc_intent_history import FIELDS
    row = dict.fromkeys(FIELDS)
    row.update(source_sequence=store.count() + 1, id=f"intent-event-{store.count()}",
               execution_key=key, state=state, broker_order_id=order, trade_id=trade,
               created_at=NOW.isoformat(), intent_id="intent-1", session_id="session-1",
               symbol="BTCUSDT", timeframe="5m", candle_time=NOW.isoformat(),
               proposal_id=key, error_recorded=False)
    event = GuardianEvent(source_service="guardian_smc_intent_history", source_component="smc_intent_history",
                          event_type="smc_intent_transition_observed", timestamp=NOW,
                          execution_id=key, order_id=order, lab_id="SMC", state_after=state,
                          symbol="BTCUSDT", timeframe="5m", evidence={"transition": row, "journal_origin": origin})
    store.append(event)
    return event


def fill(store, key="decision-1", *, order="order-1", account="account-1", quantity=.01, price=100):
    from tradexa.guardian.lab_fill_history import FIELDS, NUMBER_FIELDS
    row = dict.fromkeys(FIELDS)
    row.update({k: None for k in NUMBER_FIELDS})
    row.update(source_sequence=store.count() + 1, id=f"fill-{store.count()}", order_id=order,
               symbol="BTCUSDT", side="buy", account_id=account, execution_engine="SMC_LAB",
               timeframe="5m", candle_id=key, timestamp=NOW.isoformat(), quantity=quantity,
               price=price, fee=.001, realized_pnl=0)
    event = GuardianEvent(source_service="guardian_smc_fill_history", source_component="smc_fill_history",
                          event_type="lab_paper_fill_observed", timestamp=NOW, order_id=order,
                          lab_id="SMC", symbol="BTCUSDT", timeframe="5m",
                          evidence={"fill": row, "account_id": account, "account_type": "SMC_LAB"})
    store.append(event)
    return event


def closed(store, *, trade="trade-1", order="order-1", direction="long", symbol="BTCUSDT", timeframe="5m"):
    from tradexa.guardian.smc_journal_history import FIELDS
    row = dict.fromkeys(FIELDS)
    row.update(source_sequence=store.count() + 1, id=trade, decision_id="decision-1", order_id=order,
               symbol=symbol, timeframe=timeframe, direction=direction,
               entry=100, stop=90, target=120, planned_rr=2, size=.01, size_capped=0,
               opened_at=(NOW - timedelta(seconds=5)).isoformat(), closed_at=NOW.isoformat(),
               exit_price=120, realised_r=2, result="WIN", close_reason="fixture close")
    event = GuardianEvent(source_service="guardian_smc_journal_history", source_component="smc_journal_history",
                          event_type="smc_closed_journal_observed", timestamp=NOW, order_id=order,
                          lab_id="SMC", symbol=symbol, timeframe=timeframe, correlation_id="decision-1",
                          evidence={"trade": row, "journal_origin": "c" * 64})
    store.append(event)
    return event


def test_explicit_entry_fill_links_keep_partial_quantity_distinct_from_position(store):
    intent(store)
    fill(store, quantity=.01, price=100)
    fill(store, quantity=.02, price=103)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["link_state"] == "EXPLICIT_ENTRY_FILL_LINKS_OBSERVED"
    assert view["observed_entry_quantity"] == pytest.approx(.03)
    assert view["observed_entry_average_price"] == pytest.approx(102)
    assert view["paper_account_ids"] == ["account-1"]
    assert view["latest_recorded_state"] == "COMPLETE"
    assert len(view["entry_fills"]) == 2
    assert view["full_lifecycle_verified"] is view["execution_integrity_verified"] is False
    assert view["position_lifecycle_verified"] is view["exit_link_verified"] is False
    assert view["net_pnl_verified"] is view["currency_verified"] is False
    assert store.count() == 3


def test_missing_fill_is_unknown_not_no_order_or_missed(store):
    intent(store, state="EXECUTION_UNCERTAIN", order=None, trade=None)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["link_state"] == "INSUFFICIENT_EVIDENCE"
    assert view["latest_recorded_state"] == "EXECUTION_UNCERTAIN"
    assert view["observed_entry_quantity"] is None
    assert "ENTRY_FILL_NOT_OBSERVED" in view["findings"]
    assert "MISSED" not in str(view) and "No order was placed" not in str(view)


def test_order_key_conflict_is_not_silently_joined(store):
    intent(store)
    fill(store, order="other-order")
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["link_state"] == "CONFLICTING_EVIDENCE"
    assert "FILL_ORDER_LINK_CONFLICT" in view["findings"]
    assert view["observed_entry_quantity"] is None


def test_same_key_in_two_accounts_is_not_aggregated(store):
    intent(store)
    fill(store)
    fill(store, account="account-2")
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["link_state"] == "CONFLICTING_EVIDENCE"
    assert "MULTIPLE_PAPER_ACCOUNTS" in view["findings"]
    assert view["observed_entry_quantity"] is None


@pytest.mark.parametrize("probe,state", [
    ("guardian_smc_intent_history", "FAILED"),
    ("guardian_smc_fill_history", "DEGRADED"),
    ("guardian_smc_fill_history", "stale"),
    ("guardian_smc_intent_history", "missing"),
])
def test_stale_failed_or_importing_entry_sources_mask_current_numbers(store, probe, state):
    intent(store)
    fill(store)
    if state == "missing":
        with sqlite3.connect(store.path) as db:
            db.execute("DELETE FROM heartbeats WHERE component=?", (probe,))
    else:
        store.record_heartbeat(probe, "HEALTHY" if state == "stale" else state,
                               observed_at=NOW - timedelta(seconds=100) if state == "stale" else NOW)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["link_state"] == "UNKNOWN"
    assert view["observed_entry_quantity"] is view["observed_entry_average_price"] is None
    assert len(view["entry_fills"]) == 1  # cached historical facts remain visible
    assert "ENTRY_SOURCE_OBSERVATION_UNKNOWN" in view["findings"]


def test_source_origin_conflict_does_not_choose_a_current_state(store):
    intent(store)
    intent(store, origin="b" * 64, state="EXECUTION_FAILED")
    fill(store)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["latest_recorded_state"] is None
    assert "MULTIPLE_JOURNAL_ORIGINS" in view["findings"]
    assert view["link_state"] == "CONFLICTING_EVIDENCE"


@pytest.mark.parametrize("changes,code", [
    ({"order": "other-order"}, "JOURNAL_ORDER_LINK_CONFLICT"),
    ({"trade": "other-trade"}, "JOURNAL_TRADE_LINK_CONFLICT"),
    ({"symbol": "ETHUSDT"}, "JOURNAL_MARKET_CONFLICT"),
    ({"timeframe": "15m"}, "JOURNAL_MARKET_CONFLICT"),
    ({"direction": "short"}, "JOURNAL_DIRECTION_CONFLICT"),
])
def test_explicit_trade_and_order_links_do_not_hide_closed_journal_conflicts(store, changes, code):
    intent(store)
    fill(store)
    closed(store, **changes)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert code in view["findings"] and view["link_state"] == "CONFLICTING_EVIDENCE"
    assert len(view["closed_journal_links"]) == 1
    assert view["observed_entry_quantity"] is None


def test_missing_close_probe_does_not_hide_entry_fill_but_masks_close_freshness(store):
    intent(store)
    fill(store)
    closed(store)
    store.record_heartbeat("guardian_smc_journal_history", "FAILED", observed_at=NOW)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["link_state"] == "EXPLICIT_ENTRY_FILL_LINKS_OBSERVED"
    assert view["closed_journal_links"][0]["observation_state"] == "UNKNOWN"
    assert view["closed_journal_links"][0]["broker_exit_verified"] is False


def test_duplicate_key_across_sessions_and_multiple_order_ids_are_conflicts(store):
    event = intent(store)
    evidence = deepcopy(event.evidence)
    evidence["transition"].update(source_sequence=2, id="next-intent-event", session_id="other-session",
                                  broker_order_id="other-order", trade_id="other-trade")
    store.append(replace(event, event_id="different-session-event", order_id="other-order", evidence=evidence))
    fill(store)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert {"INTENT_IDENTITY_CONFLICT", "MULTIPLE_RECORDED_ORDER_IDS", "MULTIPLE_RECORDED_TRADE_IDS"} <= set(view["findings"])
    assert view["observed_entry_quantity"] is None


def test_whole_guardian_database_disappearance_does_not_create_an_empty_file(store):
    path = store.path
    path.rename(path.with_suffix(".saved"))  # only this disposable fixture
    with pytest.raises(sqlite3.OperationalError):
        smc_execution_links_view(store, "decision-1", now=NOW)
    assert not path.exists()


def test_discovered_fill_preserves_pending_intent_without_inventing_order_link(store):
    intent(store, order=None, trade=None, state="EXECUTION_PENDING")
    fill(store)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert "BROKER_FILL_UNRECORDED_ON_INTENT" in view["findings"]
    assert view["latest_recorded_state"] == "EXECUTION_PENDING"
    assert view["entry_fills"][0]["order_id"] == "order-1"
    assert view["entry_fills"][0]["link_state"] == "UNVERIFIED"
    assert view["observed_entry_quantity"] is None


def test_fill_without_intent_is_observed_not_classified_as_missed(store):
    fill(store)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert "EXECUTION_INTENT_NOT_OBSERVED" in view["findings"]
    assert view["entry_fills"] and view["link_state"] == "INSUFFICIENT_EVIDENCE"
    assert view["observed_entry_quantity"] is None


@pytest.mark.parametrize("field,value,code", [
    ("candle_id", "other-decision", "FILL_EXECUTION_KEY_CONFLICT"),
    ("symbol", "ETHUSDT", "FILL_MARKET_CONFLICT"),
    ("timeframe", "15m", "FILL_MARKET_CONFLICT"),
    ("side", "sell", "FILL_SIDE_CONFLICT"),
])
def test_identity_conflicts_are_retained_not_silently_excluded(store, field, value, code):
    intent(store)
    original = fill(store)
    evidence = deepcopy(original.evidence)
    evidence["fill"].update({field: value, "id": "another-fill"})
    store.append(replace(original, event_id="different-event-id", evidence=evidence,
                         symbol=evidence["fill"]["symbol"], timeframe=evidence["fill"]["timeframe"]))
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert code in view["findings"]
    assert view["link_state"] == "CONFLICTING_EVIDENCE"
    assert len(view["entry_fills"]) == 2 and view["observed_entry_quantity"] is None


def test_recorded_failure_with_fill_is_not_missed(store):
    intent(store, state="EXECUTION_FAILED")
    fill(store)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert "FAILED_INTENT_WITH_RECORDED_FILL" in view["findings"]
    assert view["link_state"] == "CONFLICTING_EVIDENCE"
    assert "MISSED" not in str(view)


def test_window_overflow_suppresses_all_completeness_claims(store):
    intent(store)
    for _ in range(129):
        fill(store)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["truncated"] is True and view["link_state"] == "UNKNOWN"
    assert view["observed_entry_quantity"] is view["latest_recorded_state"] is None
    assert "LINK_EVIDENCE_TRUNCATED" in view["findings"]
    assert len(view["entry_fills"]) == 128


def test_pa_and_unknown_source_records_cannot_supply_smc_execution_links(store):
    intent(store)
    event = fill(store, key="other")
    evidence = deepcopy(event.evidence)
    evidence["fill"]["candle_id"] = "decision-1"
    for source, lab in (("guardian_pa_fill_history", "PRICE_ACTION"), ("untrusted_probe", "SMC")):
        store.append(replace(event, source_service=source, lab_id=lab, order_id="foreign-order",
                             event_id=source + "-event", evidence=evidence))
    # The genuine SMC record has the requested order ID but the wrong key and
    # must surface as a conflict; the other sources never enter the result.
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert len(view["entry_fills"]) == 1
    assert "FILL_EXECUTION_KEY_CONFLICT" in view["findings"]
    assert {r["order_id"] for r in view["entry_fills"]} == {"order-1"}


def test_indexed_lookup_survives_unrelated_history_and_refresh_restart_is_read_only(store):
    intent(store)
    fill(store)
    for n in range(160):
        intent(store, key=f"unrelated-{n}", order=f"unrelated-order-{n}")
    before = store.count()
    expected = smc_execution_links_view(store, "decision-1", now=NOW)
    restarted = GuardianStore(store.path)
    for _ in range(100):
        assert smc_execution_links_view(restarted, "decision-1", now=NOW) == expected
    assert store.count() == before
    with sqlite3.connect(store.path) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT sequence FROM events WHERE "
                          "source_service='guardian_smc_intent_history' AND event_type='smc_intent_transition_observed' "
                          "AND json_extract(payload_json,'$.execution_id')=? ORDER BY sequence LIMIT 129", ("decision-1",)).fetchall()
    assert any("smc_link_intent_key" in row[-1] for row in plan)


@pytest.mark.parametrize("key", [None, "", " ", "a" * 257, "Bearer credential", "a/b", "a?x=y"])
def test_invalid_identity_does_not_read_store(store, monkeypatch, key):
    def forbidden(*args):
        raise AssertionError("invalid key reached persistence")
    monkeypatch.setattr(store, "smc_execution_link_snapshot", forbidden)
    with pytest.raises(ValueError):
        smc_execution_links_view(store, key)


def test_byte_bound_and_oversized_related_event_fail_closed(store, monkeypatch):
    import tradexa.guardian.smc_execution_links as module
    intent(store)
    fill(store)
    monkeypatch.setattr(module, "MAX_BYTES", 200)
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["truncated"] is True and view["observed_entry_quantity"] is None
    monkeypatch.setattr(module, "MAX_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(module, "MAX_EVENT_BYTES", 100)
    with pytest.raises(ValueError, match="size bound"):
        smc_execution_links_view(store, "decision-1", now=NOW)


def test_duplicate_intent_transition_and_fill_ids_cannot_double_count(store):
    event = intent(store)
    store.append(replace(event, event_id="duplicate-intent-event"))
    event = fill(store)
    store.append(replace(event, event_id="duplicate-fill-event"))
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["latest_recorded_state"] is view["observed_entry_quantity"] is None
    assert {"DUPLICATE_INTENT_TRANSITION_ID", "DUPLICATE_RETAINED_FILL_ID"} <= set(view["findings"])


def test_query_only_snapshot_is_atomic_during_guardian_write_and_retries_after_lock(store):
    intent(store)
    fill(store)
    before = smc_execution_links_view(store, "decision-1", now=NOW)
    with closing(sqlite3.connect(store.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE heartbeats SET state='FAILED'")
        assert smc_execution_links_view(store, "decision-1", now=NOW) == before
        writer.rollback()
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    with closing(sqlite3.connect(store.path)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError):
            smc_execution_links_view(store, "decision-1", now=NOW)
        writer.rollback()
    assert smc_execution_links_view(store, "decision-1", now=NOW) == before


def test_nonfinite_weighted_sum_is_rejected_not_exposed_as_json(store):
    intent(store)
    for _ in range(2):
        fill(store, quantity=1e308, price=2)
    with pytest.raises(ValueError, match="arithmetic"):
        smc_execution_links_view(store, "decision-1", now=NOW)


@pytest.mark.parametrize("field", ["candle_id", "timeframe"])
def test_missing_fill_provenance_is_unverified_not_a_confirmed_conflict(store, field):
    intent(store)
    original = fill(store, key="unrelated", order="unrelated-order")
    evidence = deepcopy(original.evidence)
    evidence["fill"].update(id="legacy-fill", candle_id="decision-1", order_id="order-1")
    evidence["fill"][field] = None
    store.append(replace(original, event_id="legacy-fill-event", order_id="order-1", evidence=evidence,
                         timeframe=evidence["fill"]["timeframe"]))
    view = smc_execution_links_view(store, "decision-1", now=NOW)
    assert view["link_state"] == "INSUFFICIENT_EVIDENCE"
    assert view["entry_fills"][0]["link_state"] == "UNVERIFIED"
    assert view["observed_entry_quantity"] is None
    assert "FILL_EXECUTION_KEY_CONFLICT" not in view["findings"]
    assert "FILL_MARKET_CONFLICT" not in view["findings"]
