from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from data.historical import canonical_candle_manifest, slice_candles, _finalized_rows


def _bars(count=4):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [Bar(start + timedelta(minutes=5 * i), 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i)
            for i in range(count)]


def test_half_open_slice_excludes_end_and_reports_exact_coverage():
    bars = _bars()
    result = slice_candles(bars, timeframe="5m", start=bars[1].timestamp, end=bars[3].timestamp)
    assert [bar.timestamp for bar in result["evaluation"]] == [bars[1].timestamp, bars[2].timestamp]
    assert result["coverage"]["expected_candles"] == 2
    assert result["coverage"]["evaluation_candles"] == 2
    assert result["coverage"]["missing_candles"] == 0


def test_slice_fails_closed_for_missing_critical_candle():
    bars = _bars()
    with pytest.raises(ValueError, match="missing critical candles"):
        slice_candles(bars[:2] + bars[3:], timeframe="5m", start=bars[0].timestamp, end=bars[4 - 1].timestamp + timedelta(minutes=5))


def test_canonical_manifest_is_deterministic_and_changes_when_candle_changes():
    first = canonical_candle_manifest(_bars(), symbol="XRPUSDT", timeframe="5m", provider="binance")
    second = canonical_candle_manifest(_bars(), symbol="XRPUSDT", timeframe="5m", provider="binance")
    changed = _bars(); changed[1] = Bar(changed[1].timestamp, 100, 999, 99, 100.5, 11)
    third = canonical_candle_manifest(changed, symbol="XRPUSDT", timeframe="5m", provider="binance")
    assert first["content_hash"] == second["content_hash"]
    assert first["dataset_id"] == second["dataset_id"]
    assert first["content_hash"] != third["content_hash"]


def test_invalid_ohlcv_is_rejected():
    bars = _bars()
    bars[1] = Bar(bars[1].timestamp, 100, 99, 98, 98.5, 10)
    with pytest.raises(ValueError, match="invalid OHLCV"):
        canonical_candle_manifest(bars, symbol="XRPUSDT", timeframe="5m", provider="binance")


def test_historical_store_rejects_conflicting_duplicate(tmp_path):
    from data.historical import HistoricalStore
    store = HistoricalStore(str(tmp_path / "market.sqlite"))
    row = (1735689600000, 100, 101, 99, 100.5, 10)
    store.upsert("XRPUSDT", "5m", [row])
    with pytest.raises(ValueError, match="conflicting duplicate"):
        store.upsert("XRPUSDT", "5m", [(row[0], 100, 999, 99, 100.5, 10)], strict=True)


def test_provider_rows_with_future_close_time_are_not_finalized():
    rows = [[1735689600000, "100", "101", "99", "100.5", "10", 2000],
            [1735689900000, "100", "101", "99", "100.5", "10", 9000]]
    assert len(_finalized_rows(rows, now_ms=5000)) == 1
