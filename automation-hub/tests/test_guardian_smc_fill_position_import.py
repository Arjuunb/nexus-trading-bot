"""Read-only retained fill-position import using real isolated paper sources."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path

import pytest

from execution.paper_broker_v2 import PaperBrokerV2
from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
from services.guardian_smc_intent_read_model import smc_intent_event_page
from services.smc_agent_journal import SMCAgentJournal
from tradexa.guardian.smc_intent_history import GuardianSMCIntentHistory, SCOPE as INTENT_SCOPE
from tradexa.guardian.smc_fill_positions import GuardianSMCFillPositions, SCOPE, COMPONENT, PROBE, smc_fill_positions_view
from tradexa.guardian.smc_position_links import smc_position_links_view
from tradexa.guardian.store import GuardianStore

URL = "http://app:8000/guardian/smc-fill-transitions"
KEY = "independent-source-position-key-123456789"


@pytest.fixture
def sources(tmp_path):
    broker = PaperBrokerV2(tmp_path / "broker.db", account_type="SMC_LAB", execution_engine="SMC_LAB",
                           fee_rate=0, spread_bps=0, slippage_bps=0, participation_rate=1)
    journal = SMCAgentJournal(tmp_path / "journal.db")
    store = GuardianStore(tmp_path / "guardian.db")
    yield broker, journal, store
    broker._c.close()
    journal.close()


def envelope(scope, page):
    return {"schema_version": 1, "scope": scope, "execution_integrity_verified": False,
            "observed_at": datetime.now(timezone.utc).isoformat(), "page": page}


def collector(sources, store=None, fetch=None):
    return GuardianSMCFillPositions(store or sources[2], URL, KEY,
        fetch=fetch or (lambda a, h: envelope(SCOPE, smc_fill_transition_page(sources[0].path, after=a, anchor=h))))


def intents(sources):
    return GuardianSMCIntentHistory(sources[2], "http://app:8000/guardian/smc-intent-events", KEY,
        fetch=lambda a, h: envelope(INTENT_SCOPE, smc_intent_event_page(sources[1].path, after=a, anchor=h)))


def candle(broker, price=100, volume=100):
    return broker.process_candle("BTCUSDT", dict(open=price, high=price+1, low=price-1, close=price, volume=volume))


def entry(sources, key="decision-1", quantity=1, side="buy"):
    broker, journal, _ = sources
    journal.create_execution_intent(execution_key=key, session_id="session-1", symbol="BTCUSDT", timeframe="5m")
    journal.transition_execution(key, "EXECUTION_PENDING")
    order = broker.submit(symbol="BTCUSDT", side=side, order_type="market", quantity=quantity,
                          strategy="SMC_M1_SWEEP_REVERSAL", timeframe="5m", candle_id=key)
    candle(broker)
    journal.transition_execution(key, "EXECUTED", broker_order_id=order["id"])
    journal.transition_execution(key, "COMPLETE", trade_id="trade-" + key)
    return order


def test_origin_and_synthetic_exit_import_once_and_link_by_original_ids(sources):
    broker, journal, store = sources
    order = entry(sources)
    origin = broker.positions()[0]["position_id"]
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    before = [list(db.iterdump()) for db in (broker._c, journal._db)]
    assert collector(sources).poll() == 2
    intents(sources).poll()
    view = smc_position_links_view(store, "decision-1")
    assert view["link_state"] == "EXPLICIT_POSITION_EXIT_LINKS_OBSERVED"
    assert view["origin_position_ids"] == [origin]
    assert view["recorded_order_ids"] == [order["id"]]
    assert len(view["position_transitions"]) == 2 and len(view["observed_exit_fill_ids"]) == 1
    assert view["position_transitions"][1]["fill"]["transition"]["before"]["entry_execution_key"] == "decision-1"
    assert view["full_lifecycle_verified"] is view["exit_link_verified"] is False
    count = store.count()
    restarted = GuardianStore(store.path)
    for _ in range(100):
        assert collector(sources, restarted).poll() == 0
        assert smc_position_links_view(restarted, "decision-1")["observed_exit_fill_ids"] == view["observed_exit_fill_ids"]
    assert store.count() == count and broker.positions() == [] and len(broker.orders()) == 1
    assert [list(db.iterdump()) for db in (broker._c, journal._db)] == before


@pytest.mark.parametrize("boundary", ["event", "cursor", "after_commit"])
def test_page_crash_recovery_does_not_duplicate_or_skip_fills(sources, monkeypatch, boundary):
    entry(sources)
    store = sources[2]
    if boundary == "after_commit":
        real = store.record_heartbeat
        def crash(component, state, **kwargs):
            if state == "HEALTHY":
                raise sqlite3.OperationalError("fixture crash after commit")
            return real(component, state, **kwargs)
        monkeypatch.setattr(store, "record_heartbeat", crash)
    else:
        table = "events" if boundary == "event" else "observer_cursors"
        with closing(sqlite3.connect(store.path)) as db:
            db.execute(f"CREATE TRIGGER fault BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'fixture full'); END")
    with pytest.raises(sqlite3.Error):
        collector(sources).poll()
    committed = boundary == "after_commit"
    assert store.count() == int(committed)
    assert store.observer_cursor(COMPONENT)[0] == int(committed)
    assert smc_fill_positions_view(store)["history_state"] == "UNKNOWN"
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TRIGGER IF EXISTS fault")
    assert collector(sources, GuardianStore(store.path)).poll() == int(not committed)
    assert store.count() == 1 and len(sources[0].orders()) == len(sources[0].positions()) == 1


def test_retained_paging_late_fill_and_restart_uses_sequence_not_clock(sources):
    broker, _, store = sources
    for n in range(70):
        broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=.01,
                      timeframe="5m", strategy="SMC_M1_SWEEP_REVERSAL", candle_id=f"decision-{n}")
        candle(broker)
    assert collector(sources).poll() == 32
    assert smc_fill_positions_view(store)["history_state"] == "IMPORTING"
    assert collector(sources, GuardianStore(store.path)).poll() == 32
    assert collector(sources).poll() == 6
    broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=.01, timeframe="5m", candle_id="late")
    candle(broker)
    broker._c.execute("UPDATE v2_fills SET timestamp='2001-01-01T00:00:00Z' WHERE rowid=71")
    broker._c.commit()
    assert collector(sources).poll() == 1
    after, seen = 0, []
    while True:
        page = smc_fill_positions_view(store, after=after)
        seen += page["events"]
        after = page["next_after"]
        if not page["has_more"]:
            break
    assert len(seen) == len({r["event_id"] for r in seen}) == 71
    assert seen[-1]["timestamp"].startswith("2001-")


def test_legacy_null_payload_remains_unverified_not_reconstructed(sources):
    entry(sources)
    broker, _, store = sources
    broker._c.execute("UPDATE v2_fills SET fill_position_json=NULL")
    broker._c.commit()
    assert collector(sources).poll() == 1
    intents(sources).poll()
    record = smc_fill_positions_view(store)["events"][0]["evidence"]["fill"]
    assert record["transition"] is None and record["capture_state"] == "UNVERIFIED_LEGACY_FILL"
    view = smc_position_links_view(store, "decision-1")
    assert view["link_state"] == "INSUFFICIENT_EVIDENCE" and view["observed_exit_fill_ids"] == []
    assert "MISSED" not in str(view) and "No order was placed" not in str(view)


def test_partial_fills_scale_contribution_reductions_and_new_same_symbol_position(sources):
    broker, journal, store = sources
    key = "decision-1"
    journal.create_execution_intent(execution_key=key, session_id="session-1", symbol="BTCUSDT", timeframe="5m")
    journal.transition_execution(key, "EXECUTION_PENDING")
    order = broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=3, timeframe="5m", candle_id=key)
    for _ in range(3): candle(broker, volume=1)
    journal.transition_execution(key, "EXECUTED", broker_order_id=order["id"])
    journal.transition_execution(key, "COMPLETE", trade_id="trade-1")
    origin = broker.positions()[0]["position_id"]
    contribution = entry(sources, "scale-decision", quantity=1)
    reduce = broker.submit(symbol="BTCUSDT", side="sell", order_type="market", quantity=2, reduce_only=True)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    new = entry(sources, "new-position-decision", quantity=1)
    assert collector(sources).poll()==7
    intents(sources).poll()
    view = smc_position_links_view(store, key)
    assert view["link_state"]=="EXPLICIT_POSITION_EXIT_LINKS_OBSERVED"
    assert view["origin_position_ids"]==[origin] and len(view["position_transitions"])==6
    assert [r["fill"]["transition"]["effect"] for r in view["position_transitions"]]==["OPEN", "INCREASE", "INCREASE", "INCREASE", "REDUCE", "CLOSE"]
    assert len(view["observed_exit_fill_ids"])==2
    assert new["id"] not in {r["fill"]["order_id"] for r in view["position_transitions"]}
    scaled = smc_position_links_view(store, "scale-decision")
    assert scaled["link_state"]=="INSUFFICIENT_EVIDENCE" and scaled["origin_position_ids"]==[]
    assert len(scaled["position_transitions"])==1 and scaled["recorded_order_ids"]==[contribution["id"]]
    assert scaled["position_transitions"][0]["link_state"]=="EXPLICIT_ORDER_ID_OBSERVED"
    assert "ORDER_FILL_POSITION_ORIGIN_UNVERIFIED" in scaled["findings"]
    assert broker.positions()[0]["position_id"]!=origin and len(broker.orders())==4
    assert reduce["id"] in {r["fill"]["order_id"] for r in view["position_transitions"]}


def test_reversal_links_old_and_new_origins_without_joining_later_exit_to_old(sources):
    broker, _, store = sources
    entry(sources)
    old = broker.positions()[0]["position_id"]
    reversal = entry(sources, "reversal-decision", quantity=2, side="sell")
    new = broker.positions()[0]["position_id"]
    broker.set_protection("BTCUSDT", stop_loss=110)
    candle(broker, 120)
    collector(sources).poll()
    intents(sources).poll()
    first = smc_position_links_view(store, "decision-1")
    second = smc_position_links_view(store, "reversal-decision")
    assert old!=new and first["origin_position_ids"]==[old] and second["origin_position_ids"]==[new]
    assert len(first["position_transitions"])==2 and len(second["position_transitions"])==2
    assert first["observed_exit_fill_ids"]==[]  # the reversal is not falsely called reduce-only
    assert len(second["observed_exit_fill_ids"])==1 and second["recorded_order_ids"]==[reversal["id"]]
    assert second["link_state"]=="EXPLICIT_POSITION_EXIT_LINKS_OBSERVED"
    assert len(broker.orders())==2 and broker.positions()==[]


@pytest.mark.parametrize("change", ["account", "first_fill", "previous_fill", "deleted_previous"])
def test_source_cursor_material_changes_fail_closed_without_reset_or_alteration(sources, change):
    broker, _, store = sources
    entry(sources)
    entry(sources, "scale-decision")
    assert collector(sources).poll()==2
    expected = store.observer_cursor(COMPONENT)
    if change=="account": broker._c.execute("UPDATE v2_account SET account_id='another-account'")
    elif change=="deleted_previous": broker._c.execute("DELETE FROM v2_fills WHERE rowid=2")
    else: broker._c.execute("UPDATE v2_fills SET timestamp='2000-01-01T00:00:00Z' WHERE rowid=?", (1 if change=="first_fill" else 2,))
    broker._c.commit()
    before = list(broker._c.iterdump())
    with pytest.raises(ValueError): collector(sources, GuardianStore(store.path)).poll()
    assert store.count()==2 and store.observer_cursor(COMPONENT)==expected
    assert smc_fill_positions_view(store)["history_state"]=="UNKNOWN"
    assert list(broker._c.iterdump())==before


def test_source_lock_fail_closed_then_retry_preserves_broker_and_checkpoint(sources):
    broker, _, store = sources
    entry(sources)
    broker._c.close()  # switch this disposable fixture to rollback journal mode
    with closing(sqlite3.connect(broker.path)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError): collector(sources).poll()
        assert store.count()==0 and store.observer_cursor(COMPONENT)==(0, "")
        db.rollback()
    assert collector(sources).poll()==1
    broker._c = sqlite3.connect(broker.path)
    broker._c.row_factory = sqlite3.Row
    assert len(broker.orders())==len(broker.positions())==1


def test_missing_source_is_not_recreated_or_empty_success(sources):
    broker, _, store = sources
    entry(sources)
    path = Path(broker.path)
    path.rename(path.with_suffix(".fixture-saved"))
    with pytest.raises(sqlite3.OperationalError): collector(sources).poll()
    assert not path.exists() and store.count()==0


def test_source_and_standalone_validators_have_same_material_cursor_projection(sources):
    from tradexa.guardian.smc_fill_positions import project_fill_position, cursor_anchor
    broker, _, _ = sources
    entry(sources)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    page = smc_fill_transition_page(broker.path)
    assert [project_fill_position(r, page["account_id"]) for r in page["fills"]]==page["fills"]
    assert cursor_anchor(page["account_id"], page["first_fill"], page["fills"][-1])==page["next_anchor"]
