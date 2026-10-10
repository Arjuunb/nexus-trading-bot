"""Original market observations survive deferral without inventing history."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import sys

import pytest

from bot.types import Bar
from services.market_context_observer import MarketContextObserver


UTC = timezone.utc
OPEN = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
SOURCE = "live (binance_usdm_hub)"


@pytest.fixture(autouse=True)
def frozen_definition(monkeypatch, request):
    if "actual_classifier" in request.fixturenames:
        return
    # The transport adapter has no dependency on classifier computation.
    definition = {"classifier_id": "market_context", "classifier_version": "1.0.0",
                  "parameter_hash": "immutable", "parameters": {"atr_period": 14}}
    monkeypatch.setitem(sys.modules, "services.market_context_classifier", SimpleNamespace(
        classifier_definition=lambda: definition))


@pytest.fixture
def actual_classifier():
    from services.market_context_classifier import classify_frozen_context
    return classify_frozen_context


def bar(stamp=OPEN, close=101):
    return Bar(stamp, 100, max(102, close), 99, close, 10)


def record(observer, rows, timeframe="5m", at=None, **kwargs):
    observer.record_closed_batch("BTCUSDT", timeframe, rows,
        available_at=at or OPEN + timedelta(minutes=5, seconds=1),
        source=SOURCE, exchange="binance_usdm", market_type="perpetual", **kwargs)


def freeze(observer, rows, *, native=None, observed=None, mode="forward_paper"):
    return observer.freeze("BTCUSDT", SimpleNamespace(bars=rows, _native_mtf_context=native or {}),
        entry_timeframe="5m", signal_timestamp=OPEN,
        signal_observed_at=observed or OPEN + timedelta(minutes=5, seconds=2),
        source=SOURCE, execution_mode=mode)


def test_initial_receipt_is_retained_when_identical_history_is_fetched_again():
    observer = MarketContextObserver()
    record(observer, [bar()])
    record(observer, [bar()], at=OPEN + timedelta(minutes=6))
    snapshot = freeze(observer, [bar()])
    candle = snapshot["entry_candles"][0]
    assert candle["available_at"] == (OPEN + timedelta(minutes=5, seconds=1)).isoformat()
    assert snapshot["signal_timestamp"] == (OPEN + timedelta(minutes=5, seconds=2)).isoformat()
    assert snapshot["signal_candle_timestamp"] == OPEN.isoformat()
    assert snapshot["decision_candle_close_timestamp"] == (OPEN + timedelta(minutes=5)).isoformat()
    assert snapshot["exchange"] == "binance_usdm"
    assert snapshot["market_type"] == "perpetual"
    assert snapshot["classifier_definition"]["parameters"] == {"atr_period": 14}
    assert len(snapshot["original_input_hash"]) == 64
    json.dumps(snapshot, allow_nan=False)


def test_market_snapshots_are_detached_and_changed_history_is_conflicted():
    observer = MarketContextObserver()
    record(observer, [bar()])
    original = freeze(observer, [bar()])
    record(observer, [bar(close=100.5)], at=OPEN + timedelta(minutes=6))
    amended = freeze(observer, [bar(close=100.5)], observed=OPEN + timedelta(minutes=6, seconds=1))
    old_again = freeze(observer, [bar()], observed=OPEN + timedelta(minutes=6, seconds=1))
    assert original["entry_candles"][0]["close"] == 101
    assert original["entry_candles"][0]["source_quality"] != "UNKNOWN"
    assert amended["entry_candles"][0]["source_quality"] == "UNKNOWN"
    assert old_again["entry_candles"][0]["source_quality"] == "UNKNOWN"
    assert "CONFLICTING_CANDLE_OBSERVATIONS" in amended["quality_reasons"]
    amended["classifier_definition"]["parameters"]["atr_period"] = 3
    assert original["classifier_definition"]["parameters"]["atr_period"] == 14


def test_higher_timeframe_uses_native_aligned_inputs_and_drops_future_candles():
    observer = MarketContextObserver()
    entry = [bar(), bar(OPEN + timedelta(minutes=5), 110)]
    higher = [bar(OPEN - timedelta(hours=1)), bar(OPEN)]
    record(observer, entry, at=OPEN + timedelta(minutes=10, seconds=1))
    record(observer, higher, timeframe="1h", at=OPEN + timedelta(hours=1, seconds=1))
    snapshot = freeze(observer, entry, native={"1h": higher},
        observed=OPEN + timedelta(minutes=10, seconds=2))
    assert len(snapshot["entry_candles"]) == 1
    assert len(snapshot["higher_candles"]) == 1
    assert snapshot["higher_timeframe"] == "1h"
    assert snapshot["higher_candles"][0]["open_time"] == (OPEN - timedelta(hours=1)).isoformat()


def test_posthoc_replay_and_missing_receipts_do_not_prove_historical_availability():
    observer = MarketContextObserver()
    unknown = freeze(observer, [bar()])
    assert unknown["entry_candles"][0]["available_at"] is None
    assert unknown["entry_candles"][0]["source_quality"] == "UNKNOWN"
    record(observer, [bar()])
    replay = freeze(observer, [bar()], mode="replay")
    assert replay["signal_timestamp"] == (OPEN + timedelta(minutes=5)).isoformat()
    assert replay["entry_candles"][0]["source_quality"] == "UNKNOWN"
    assert "HISTORICAL_AVAILABILITY_UNPROVEN" in replay["quality_reasons"]


def test_registry_is_bounded_and_evicted_inputs_remain_explicitly_unknown():
    observer = MarketContextObserver(max_candles=2, max_series=1)
    rows = [bar(OPEN + timedelta(minutes=5 * n)) for n in range(3)]
    record(observer, rows, at=OPEN + timedelta(minutes=16))
    snapshot = observer.freeze("BTCUSDT", SimpleNamespace(bars=rows, _native_mtf_context={}),
        entry_timeframe="5m", signal_timestamp=rows[-1].timestamp,
        signal_observed_at=OPEN + timedelta(minutes=16, seconds=1), source=SOURCE,
        execution_mode="forward_paper")
    assert len(snapshot["entry_candles"]) == 2
    record(observer, [bar(OPEN - timedelta(hours=1))], timeframe="1h")
    assert freeze(observer, [bar()])["entry_candles"][0]["available_at"] is None


def test_unknown_provider_does_not_inherit_a_binance_futures_label():
    observer = MarketContextObserver()
    observer.record_closed_batch("BTCUSDT", "5m", [bar()],
        available_at=OPEN + timedelta(minutes=5, seconds=1), source="local real cache",
        exchange=None, market_type=None)
    snapshot = freeze(observer, [bar()])
    assert snapshot["exchange"] is None
    assert snapshot["market_type"] is None
    assert snapshot["entry_candles"][0]["source_quality"] == "UNKNOWN"


def test_same_prices_from_a_different_market_do_not_share_receipt_proof():
    observer = MarketContextObserver()
    record(observer, [bar()])
    observer.record_closed_batch("BTCUSDT", "5m", [bar()],
        available_at=OPEN + timedelta(minutes=5, seconds=1), source="live (ccxt:binance)",
        exchange="binance", market_type="spot")
    snapshot = freeze(observer, [bar()])
    assert snapshot["entry_candles"][0]["source_quality"] == "UNKNOWN"
    assert "CONFLICTING_CANDLE_OBSERVATIONS" in snapshot["quality_reasons"]


def test_later_receipt_with_an_earlier_clock_does_not_rewrite_the_first_receipt():
    observer = MarketContextObserver()
    record(observer, [bar()], at=OPEN + timedelta(minutes=5, seconds=1))
    record(observer, [bar()], at=OPEN + timedelta(minutes=5, milliseconds=500))
    assert freeze(observer, [bar()])["entry_candles"][0]["available_at"] == (
        OPEN + timedelta(minutes=5, seconds=1)).isoformat()


def histories():
    def series(count, step, final):
        return [Bar(final - step * (count - index - 1), 100 + index,
                    102 + index, 99 + index, 101 + index, 10 + index)
                for index in range(count)]
    return series(150, timedelta(minutes=5), OPEN), series(80, timedelta(hours=1), OPEN - timedelta(hours=1))


def test_real_classifier_accepts_observed_native_history_and_rejects_a_revision(actual_classifier):
    observer = MarketContextObserver()
    entry, higher = histories()
    record(observer, entry)
    record(observer, higher, timeframe="1h")
    snapshot = freeze(observer, entry, native={"1h": higher})
    result = actual_classifier(snapshot, classification_timestamp=OPEN + timedelta(days=1))
    assert result["context_quality"] == "VALID"
    assert result["trend_regime"] != "UNKNOWN"
    assert result["volatility_regime"] != "UNKNOWN"
    amended = list(entry)
    last = entry[-1]
    amended[-1] = Bar(last.timestamp, last.open, last.high, last.low, last.close - .5, last.volume)
    record(observer, amended, at=OPEN + timedelta(minutes=6))
    conflicted = freeze(observer, amended, native={"1h": higher}, observed=OPEN + timedelta(minutes=6, seconds=1))
    blocked = actual_classifier(conflicted, classification_timestamp=OPEN + timedelta(days=1))
    assert blocked["context_quality"] == "UNKNOWN"
    assert blocked["trend_regime"] == blocked["volatility_regime"] == "UNKNOWN"


def test_real_classifier_receives_no_future_entry_or_higher_candle_values(actual_classifier):
    observer = MarketContextObserver()
    entry, higher = histories()
    record(observer, entry)
    record(observer, higher, timeframe="1h")
    future_entry = bar(OPEN + timedelta(minutes=5), 1000)
    future_higher = bar(OPEN, 1000)
    before = freeze(observer, entry + [future_entry], native={"1h": higher + [future_higher]})
    result = actual_classifier(before, classification_timestamp=OPEN + timedelta(days=1))
    future_entry.close = 2000
    future_entry.high = 3000
    future_higher.close = 2000
    future_higher.high = 3000
    after = freeze(observer, entry + [future_entry], native={"1h": higher + [future_higher]})
    assert after == before
    assert actual_classifier(after, classification_timestamp=OPEN + timedelta(days=1)) == result


@pytest.mark.parametrize("at", [OPEN, OPEN.replace(tzinfo=None), "2026-01-15T10:05:01"])
def test_unclosed_or_timezone_unknown_observations_are_rejected(at):
    observer = MarketContextObserver()
    with pytest.raises(ValueError):
        record(observer, [bar()], at=at)
