"""Real paper sources -> four collectors -> own-DB exact-ID associations."""
from datetime import datetime, timezone
import json

import pytest

from execution.paper_broker_v2 import PaperBrokerV2
from services.smc_agent_journal import SMCAgentJournal
from services.guardian_smc_intent_read_model import smc_intent_event_page
from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
from services.guardian_smc_exit_fill_read_model import smc_exit_fill_page
from services.guardian_smc_journal_read_model import smc_journal_page
from tradexa.guardian.smc_intent_history import GuardianSMCIntentHistory, SCOPE as INTENT_SCOPE
from tradexa.guardian.smc_fill_positions import GuardianSMCFillPositions, SCOPE as POSITION_SCOPE
from tradexa.guardian.smc_exit_fills import GuardianSMCExitFills, SCOPE as EXIT_SCOPE
from tradexa.guardian.smc_journal_history import GuardianSMCJournalHistory, SCOPE as JOURNAL_SCOPE
from tradexa.guardian.smc_exit_links import smc_exit_links_view
from tradexa.guardian.store import GuardianStore

KEY = "independent-exit-link-observer-key-123456789"


@pytest.fixture
def sources(tmp_path):
    journal = SMCAgentJournal(tmp_path / "journal.db")
    broker = PaperBrokerV2(tmp_path / "broker.db", account_type="SMC_LAB", execution_engine="SMC_LAB",
        leverage=5, fee_rate=0, spread_bps=0, slippage_bps=0, participation_rate=1)
    store = GuardianStore(tmp_path / "guardian.db")
    yield journal, broker, store
    broker._c.close()
    journal.close()


def envelope(scope, page):
    return dict(schema_version=1, scope=scope, observed_at=datetime.now(timezone.utc).isoformat(),
                execution_integrity_verified=False, page=page)


def imported(sources, store=None):
    journal, broker, original = sources
    store = store or original
    readers = (
        GuardianSMCIntentHistory(store, "http://app:8000/guardian/smc-intent-events", KEY,
            fetch=lambda a, h: envelope(INTENT_SCOPE, smc_intent_event_page(journal.path, after=a, anchor=h))),
        GuardianSMCFillPositions(store, "http://app:8000/guardian/smc-fill-transitions", KEY,
            fetch=lambda a, h: envelope(POSITION_SCOPE, smc_fill_transition_page(broker.path, after=a, anchor=h))),
        GuardianSMCExitFills(store, "http://app:8000/guardian/smc-exit-fills", KEY,
            fetch=lambda a, h: envelope(EXIT_SCOPE, smc_exit_fill_page(broker.path, after=a, anchor=h))),
        GuardianSMCJournalHistory(store, "http://app:8000/guardian/smc-journal", KEY,
            fetch=lambda c: envelope(JOURNAL_SCOPE, smc_journal_page(journal.path, cursor=c))),
    )
    for reader in readers:
        for _ in range(40):
            reader.poll()
            if store.heartbeats()[reader.probe]["state"] == "HEALTHY":
                break
        else:
            pytest.fail("fixture import did not finish")
    return readers


def candle(broker, price=100, **changes):
    values = dict(open=price, high=price+1, low=price-1, close=price, volume=100)
    values.update(changes)
    broker.process_candle("BTCUSDT", values)


def entry(sources, *, side="buy", finalized=True):
    journal, broker, _ = sources
    journal.create_execution_intent(execution_key="decision-1", decision_id="distinct-journal-decision",
        session_id="session-1", symbol="BTCUSDT", timeframe="5m")
    journal.transition_execution("decision-1", "EXECUTION_PENDING")
    order = broker.submit(symbol="BTCUSDT", side=side, order_type="market", quantity=1,
        strategy="SMC_M1_SWEEP_REVERSAL", timeframe="5m", candle_id="decision-1")
    candle(broker)
    position = broker.positions()[0]
    trade = None
    if finalized:
        journal.transition_execution("decision-1", "EXECUTED", broker_order_id=order["id"])
        trade = journal.open_trade(decision_id="distinct-journal-decision", symbol="BTCUSDT", timeframe="5m",
            direction="long" if side == "buy" else "short", entry=100, stop=90 if side == "buy" else 110,
            target=120 if side == "buy" else 80, planned_rr=2, size=1,
            order_id=order["id"], why="isolated exit-association fixture")
        journal.transition_execution("decision-1", "COMPLETE", trade_id=trade)
    return order, position, trade


@pytest.mark.parametrize("kind", ["stop", "short_stop", "target", "tick", "trailing", "partial", "reduce", "reverse", "mark", "liquidation"])
def test_actual_exits_associate_exact_origin_without_certifying_journal_close(sources, kind):
    journal, broker, store = sources
    order, pos, trade = entry(sources, side="sell" if kind == "short_stop" else "buy")
    if kind in {"stop", "short_stop", "target", "tick", "partial"}:
        broker.set_protection("BTCUSDT", stop_loss=110 if kind == "short_stop" else 90, take_profit=120 if kind != "short_stop" else 80)
        price = 130 if kind == "target" else 120 if kind == "short_stop" else 80
        if kind == "tick":
            stamp = datetime.now(timezone.utc).isoformat()
            broker.process_tick("BTCUSDT", dict(bid=price, ask=price+.1, mark=price,
                received_at=stamp, event_timestamp=stamp, sequence=1, quote_event_id="exit-quote"))
        elif kind == "partial":
            candle(broker, price, volume=.4)
            candle(broker, price)
        else:
            candle(broker, price)
    elif kind == "trailing":
        broker.set_protection("BTCUSDT", stop_loss=90, trailing_offset=5)
        candle(broker, high=111, low=100)
    elif kind in {"reduce", "reverse"}:
        broker.submit(symbol="BTCUSDT", side="sell", order_type="market", quantity=1 if kind == "reduce" else 2,
            reduce_only=kind == "reduce", timeframe="5m", candle_id="different-exit-decision")
        candle(broker)
    elif kind == "mark":
        broker.close_position_at_mark("BTCUSDT", 100, reason="LEGACY_POSITION_REMEDIATION")
    else:
        broker.process_mark("BTCUSDT", .01)
    fills = broker.fills()
    exit_records = [json.loads(r[0]) for r in broker._c.execute("SELECT fill_exit_json FROM v2_fills WHERE fill_exit_json IS NOT NULL ORDER BY rowid")]
    if kind != "reverse":
        journal.close_trade(trade, exit_price=exit_records[-1]["price"], realised_r=-1, result="LOSS", close_reason="fixture close")
    before = [list(db.iterdump()) for db in (journal._db, broker._c)]
    imported(sources)
    view = smc_exit_links_view(store, "decision-1")
    assert view["link_state"] == "EXPLICIT_EXIT_POSITION_LINKS_OBSERVED"
    assert view["observed_exit_fill_ids"] == [r["fill_id"] for r in exit_records]
    assert len(view["exit_links"]) == (2 if kind == "partial" else 1)  # entry's null exit capture is not an exit
    assert view["origin_position_ids"] == [pos["position_id"]]
    assert view["recorded_order_ids"] == [order["id"]]
    assert all(r["position"]["entry_execution_key"] == "decision-1" for r in exit_records)
    assert view["journal_close_verified"] is view["exit_link_verified"] is view["position_lifecycle_verified"] is False
    if kind == "reverse":
        assert broker.positions()[0]["position_id"] != pos["position_id"] and view["closed_journal_links"] == []
    else:
        assert view["closed_journal_links"][0]["link_state"] == "EXPLICIT_ENTRY_ORDER_TRADE_IDS_OBSERVED"
        assert broker.positions() == []
    assert [list(db.iterdump()) for db in (journal._db, broker._c)] == before
    assert broker.fills() == fills


def test_exit_before_journal_close_restart_and_100_reads_are_idempotent(sources):
    journal, broker, store = sources
    _, _, trade = entry(sources)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    imported(sources)
    before = smc_exit_links_view(store, "decision-1")
    assert before["link_state"] == "EXPLICIT_EXIT_POSITION_LINKS_OBSERVED" and before["closed_journal_links"] == []
    journal.close_trade(trade, exit_price=80, realised_r=-2, result="LOSS", close_reason="delayed fixture close")
    imported(sources)
    original = [list(db.iterdump()) for db in (journal._db, broker._c)]
    count = store.count()
    restarted = GuardianStore(store.path)
    readers = imported(sources, restarted)
    for _ in range(100):
        result = smc_exit_links_view(restarted, "decision-1")
        assert result["observed_exit_fill_ids"] == before["observed_exit_fill_ids"]
        assert result["closed_journal_links"][0]["journal_close_verified"] is False
    assert store.count() == count and all(r.poll() == 0 for r in readers)
    assert [list(db.iterdump()) for db in (journal._db, broker._c)] == original
    assert len(broker.orders()) == 1 and len(broker.fills()) == 2 and not broker.positions()


def test_pending_unfinalized_broker_fill_is_retained_not_no_order(sources):
    journal, broker, store = sources
    entry(sources, finalized=False)
    journal.transition_execution("decision-1", "EXECUTION_UNCERTAIN", error="fixture journal outage")
    imported(sources)
    view = smc_exit_links_view(store, "decision-1")
    assert view["latest_recorded_state"] == "EXECUTION_UNCERTAIN"
    assert view["link_state"] == "INSUFFICIENT_EVIDENCE" and view["current_execution_state_verified"] is False
    assert "MISSED" not in str(view) and "No order was placed" not in str(view)
    assert len(broker.orders()) == len(broker.positions()) == 1 and journal.trades() == []


@pytest.mark.parametrize("legacy", ["exit", "position"])
def test_legacy_capture_is_unverified_never_reconstructed(sources, legacy):
    _, broker, store = sources
    entry(sources)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    if legacy == "exit":
        broker._c.execute("UPDATE v2_fills SET fill_exit_json=NULL")
    else:
        # Atomic exporter correctly requires a position capture for each
        # recorded exit. A genuinely pre-capture source has both null.
        broker._c.execute("UPDATE v2_fills SET fill_exit_json=NULL,fill_position_json=NULL")
    broker._c.commit()
    before = list(broker._c.iterdump())
    imported(sources)
    result = smc_exit_links_view(store, "decision-1")
    assert result["link_state"] == "INSUFFICIENT_EVIDENCE" and result["observed_exit_fill_ids"] == []
    assert result["journal_close_verified"] is False and list(broker._c.iterdump()) == before
