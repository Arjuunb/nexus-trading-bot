"""A journal pass writes only what changed.

The recorder re-projects every trade and every decision on each pass, every
30 seconds. Each of those used to be a write and a commit, even when nothing
had changed: a disk sync per record. On the production VPS that was several
thousand syncs a pass, and a pass took five and a half minutes, holding a core
and the journal database the whole time. Guardian, which read the recorder's
status through that database, waited behind it and reported its own
heartbeat as lost.

These tests count the rows SQLite actually changes (``total_changes``).
"""
from __future__ import annotations

from data.trade_record_store import TradeRecordStore
from services.guardian import sources
from services.journal_labs import PALabProjector
from services.native_price_action import NativePriceActionEngine, PriceActionConfig
from tests.test_journal_pa_decisions import _account, _candles, _fill, _proposal_state, _trade_it


def _changes(store: TradeRecordStore) -> int:
    return store._c.total_changes


def _decision(**extra) -> dict:
    return {"decision_key": "PA_LAB:decision:s1:p1", "record_source": "PA_LAB",
            "record_origin": "LAB_PAPER", "decision_type": "SIGNAL_ONLY", "status": "SIGNAL_ONLY", "reason": "signals only",
            "evidence": {"proposal": {"entry": 105, "stop": 100}}, **extra}


def test_an_unchanged_decision_is_not_written_again(tmp_path):
    store = TradeRecordStore(str(tmp_path / "tr.db"))
    store.upsert_decision(_decision(decided_at="2026-09-30T10:00:00+00:00"))
    first = store.query_decisions()[0]
    before = _changes(store)
    store.upsert_decision(_decision(decided_at="2026-09-30T10:00:00+00:00"))
    assert _changes(store) == before
    assert store.query_decisions()[0]["updated_at"] == first["updated_at"]

    store.upsert_decision(_decision(decided_at="2026-09-30T10:00:00+00:00",
                                    reason="paused: feed unreliable"))   # a real change is written
    assert _changes(store) == before + 1
    assert store.query_decisions()[0]["reason"] == "paused: feed unreliable"


def test_a_decision_keeps_the_time_it_was_first_recorded_with(tmp_path):
    """A decision whose source gives no time used to take the time of every pass."""
    store = TradeRecordStore(str(tmp_path / "tr.db"))
    store.upsert_decision(_decision())
    first = store.query_decisions()[0]["decided_at"]
    assert first
    before = _changes(store)
    store.upsert_decision(_decision())
    assert store.query_decisions()[0]["decided_at"] == first
    assert _changes(store) == before


def test_a_missing_value_never_erases_a_recorded_one(tmp_path):
    store = TradeRecordStore(str(tmp_path / "tr.db"))
    store.upsert_decision(_decision(decided_at="2026-09-30T10:00:00+00:00", symbol="BTCUSDT"))
    before = _changes(store)
    store.upsert_decision(_decision(decided_at="2026-09-30T10:00:00+00:00", symbol=None))
    assert store.query_decisions()[0]["symbol"] == "BTCUSDT"
    assert _changes(store) == before                 # COALESCE would keep it: nothing to write


def test_a_second_pass_over_an_unchanged_lab_writes_nothing(tmp_path):
    account = _account(tmp_path, "automatic")
    engine = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m"))
    bars = _candles()
    engine.ingest_closed_bars(bars[:150])
    for bar in bars[150:]:                           # the real engine: setups waiting to confirm
        engine.process_closed_bar(bar, market_data_health="SYNCHRONIZED")
        account.record_evaluation(engine.visual_state(candle_window=3000), bar,
                                  {"state": "SYNCHRONIZED"})
    state = _proposal_state()                        # and one trade the lab placed and filled
    _fill(account, state, _trade_it(account, state))

    store = TradeRecordStore(str(tmp_path / "tr.db"))
    PALabProjector(account).project(store)
    decisions, trades = store.count_decisions(), len(store.query_trades())
    assert decisions >= 5 and trades == 1
    before = _changes(store)
    PALabProjector(account).project(store)           # the next pass: nothing new happened
    assert _changes(store) == before
    assert store.count_decisions() == decisions and len(store.query_trades()) == trades


def test_an_unchanged_trade_timeline_is_not_rewritten(tmp_path):
    store = TradeRecordStore(str(tmp_path / "tr.db"))
    record = {"execution_key": "MAIN:i1:s1:a1", "trade_id": "t1", "record_source": "MAIN",
              "record_origin": "FORWARD_PAPER", "data_completeness": "COMPLETE",
              "symbol": "BTCUSDT", "status": "OPEN"}
    events = [{"stage": "SIGNAL", "at": "2026-09-30T10:00:00+00:00"},
              {"stage": "ENTRY_FILLED", "at": "2026-09-30T10:05:00+00:00", "detail": "filled"}]
    store.upsert_trade(record, events=events)
    before = _changes(store)
    assert store.upsert_trade(record, events=events)["action"] == "unchanged"
    assert _changes(store) == before
    store.upsert_trade(record, events=[*events, {"stage": "EXIT_FILLED",
                                                 "at": "2026-09-30T11:00:00+00:00"}])
    assert _changes(store) == before + 1             # only the new stage


def test_guardian_reads_the_recorder_without_touching_the_journal_database():
    class Recorder:                                  # a pass is running and holds the database
        running, passes, last_error = True, 41, None
        last_report = {"at": "2026-09-30T23:03:57+00:00", "seconds": 1.2, "ledgers": [
            {"source": "MAIN", "skipped": None}]}

        def status(self):
            raise AssertionError("Guardian must not wait on the journal database")

    row = sources.journal_row(Recorder())
    assert row == {"running": True, "passes": 41, "last_error": None,
                   "last_pass_at": "2026-09-30T23:03:57+00:00", "pass_seconds": 1.2,
                   "skipped": []}
