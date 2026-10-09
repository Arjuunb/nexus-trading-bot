"""Real paper broker/journal imports, downstream of untouched SMC rules."""
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from execution.paper_broker_v2 import PaperBrokerV2
from services.smc_agent_journal import SMCAgentJournal
from services.guardian_lab_fill_read_model import lab_fill_page
from services.guardian_smc_intent_read_model import smc_intent_event_page
from services.guardian_smc_journal_read_model import smc_journal_page
from tradexa.guardian.smc_intent_history import GuardianSMCIntentHistory, SCOPE as INTENT_SCOPE
from tradexa.guardian.smc_journal_history import GuardianSMCJournalHistory, SCOPE as JOURNAL_SCOPE
from tradexa.guardian.lab_fill_history import GuardianLabFillHistory, SCOPE as FILL_SCOPE
from tradexa.guardian.smc_execution_links import smc_execution_links_view
from tradexa.guardian.store import GuardianStore

KEY = "independent-smc-entry-link-key-123456789"


@pytest.fixture
def sources(tmp_path):
    journal = SMCAgentJournal(tmp_path / "journal.db")
    broker = PaperBrokerV2(tmp_path / "broker.db", account_type="SMC_LAB", execution_engine="SMC_LAB",
                           spread_bps=0, slippage_bps=0, fee_rate=0)
    store = GuardianStore(tmp_path / "guardian.db")
    yield journal, broker, store
    journal.close()
    broker._c.close()


def imported(sources):
    journal, broker, store = sources
    def envelope(scope, page):
        return {"schema_version": 1, "scope": scope, "observed_at": datetime.now(timezone.utc).isoformat(),
                "execution_integrity_verified": False, "page": page}
    collectors = (
        GuardianSMCIntentHistory(store, "http://app:8000/guardian/smc-intent-events", KEY,
            fetch=lambda a, h: envelope(INTENT_SCOPE, smc_intent_event_page(journal.path, after=a, anchor=h))),
        GuardianLabFillHistory(store, "http://app:8000/guardian/lab-fills", KEY, "SMC",
            fetch=lambda a, h: envelope(FILL_SCOPE, lab_fill_page(broker.path, "SMC", after=a, anchor=h))),
        GuardianSMCJournalHistory(store, "http://app:8000/guardian/smc-journal", KEY,
            fetch=lambda c: envelope(JOURNAL_SCOPE, smc_journal_page(journal.path, cursor=c))),
    )
    for reader in collectors:
        reader.poll()
    return collectors


def entry(sources, *, filled=True, finalized=True):
    journal, broker, _ = sources
    stamp = datetime.now(timezone.utc)
    journal.create_execution_intent(execution_key="decision-1", decision_id="decision-1",
                                    session_id="session-1", symbol="BTCUSDT", timeframe="5m")
    journal.transition_execution("decision-1", "EXECUTION_PENDING")
    order = broker.submit(symbol="BTCUSDT", side="buy", order_type="limit", quantity=.01,
                          limit_price=100, protection_stop_loss=90, protection_take_profit=120,
                          strategy="SMC_M1_SWEEP_REVERSAL", timeframe="5m", candle_id="decision-1")
    if filled:
        broker.process_candle("BTCUSDT", dict(open=100, high=101, low=99, close=100, volume=100,
                                              timestamp=stamp.isoformat()))
    if finalized:
        journal.transition_execution("decision-1", "EXECUTED", broker_order_id=order["id"])
        trade = journal.open_trade(decision_id="decision-1", symbol="BTCUSDT", timeframe="5m", direction="long",
                                   entry=100, stop=90, target=120, planned_rr=2, size=.01,
                                   order_id=order["id"], why="isolated entry-link fixture")
        journal.transition_execution("decision-1", "COMPLETE", trade_id=trade)
    else:
        trade = None
    return order, trade, stamp


def test_actual_filled_position_is_linked_without_certifying_whole_position(sources):
    order, trade, _ = entry(sources)
    imported(sources)
    view = smc_execution_links_view(sources[2], "decision-1")
    assert view["link_state"] == "EXPLICIT_ENTRY_FILL_LINKS_OBSERVED"
    assert view["recorded_order_ids"] == [order["id"]] and view["recorded_trade_ids"] == [trade]
    assert view["observed_entry_quantity"] == pytest.approx(.01)
    assert view["observed_entry_average_price"] == pytest.approx(100)
    assert sources[1].positions()[0]["entry_order_id"] == order["id"]
    assert view["position_lifecycle_verified"] is False


def test_resting_complete_journal_is_not_treated_as_a_fill(sources):
    order, _, _ = entry(sources, filled=False)
    readers = imported(sources)
    view = smc_execution_links_view(sources[2], "decision-1")
    assert view["latest_recorded_state"] == "COMPLETE" and view["link_state"] == "INSUFFICIENT_EVIDENCE"
    assert view["observed_entry_quantity"] is None
    assert len(sources[1].orders()) == 1 and sources[1].positions() == []
    sources[1].process_candle("BTCUSDT", dict(open=100, high=101, low=99, close=100, volume=100,
                                            timestamp=datetime.now(timezone.utc).isoformat()))
    assert readers[1].poll() == 1
    assert readers[0].poll() == 0  # intent did not need another transition
    assert smc_execution_links_view(sources[2], "decision-1")["observed_entry_quantity"] == pytest.approx(.01)
    assert len(sources[1].orders()) == len(sources[1].positions()) == 1


def test_broker_commit_before_journal_finalization_is_not_no_order(sources):
    order, _, _ = entry(sources, finalized=False)
    imported(sources)
    view = smc_execution_links_view(sources[2], "decision-1")
    assert view["latest_recorded_state"] == "EXECUTION_PENDING"
    assert "BROKER_FILL_UNRECORDED_ON_INTENT" in view["findings"]
    assert view["entry_fills"][0]["order_id"] == order["id"]
    assert len(sources[1].orders()) == len(sources[1].positions()) == 1
    assert sources[0].trades() == []
    assert "MISSED" not in str(view) and "No order was placed" not in str(view)


def test_closed_journal_link_does_not_invent_protective_exit_origin(sources):
    journal, broker, store = sources
    order, trade, stamp = entry(sources)
    broker.process_candle("BTCUSDT", dict(open=120, high=121, low=119, close=120, volume=100,
                                          timestamp=stamp.isoformat()))
    journal.close_trade(trade, exit_price=120, realised_r=2, result="WIN", close_reason="fixture target",
                        closed_at=(stamp + timedelta(seconds=1)).isoformat())
    imported(sources)
    view = smc_execution_links_view(store, "decision-1")
    assert broker.positions() == [] and len(broker.fills()) == 2
    assert len(view["entry_fills"]) == 1
    [closed] = view["closed_journal_links"]
    assert closed["order_id"] == order["id"] and closed["trade_id"] == trade
    assert closed["link_state"] == "EXPLICIT_ORDER_TRADE_IDS_OBSERVED"
    assert closed["broker_exit_verified"] is False
    assert view["exit_link_verified"] is view["full_lifecycle_verified"] is False
    before = [list(x.iterdump()) for x in (journal._db, broker._c)]
    restarted = GuardianStore(store.path)
    count = store.count()
    for _ in range(100):
        assert smc_execution_links_view(restarted, "decision-1")["closed_journal_links"] == view["closed_journal_links"]
    assert store.count() == count
    assert [list(x.iterdump()) for x in (journal._db, broker._c)] == before
