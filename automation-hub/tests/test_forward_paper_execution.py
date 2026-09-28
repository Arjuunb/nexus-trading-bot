from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from data.forward_market_data import ForwardMarketDataUnavailable, fetch_forward_bars, valid_closed_bars
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services.auto_engine import AutoStrategyEngine, EngineFeedError
from services.controls import TradingControl
from services.signal_pipeline import SignalPipeline
from services.trading_instances import TradingInstanceManager
from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


def _bar(at: datetime) -> Bar:
    return Bar(at, 100, 101, 99, 100, 1)


def _engine(*, cursor=None, checkpoint=None):
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, 1000)
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=1000)
    return AutoStrategyEngine(pipeline, paper, ledger, symbols=["BTCUSDT"], timeframe="5m",
                              live=True, initial_last_processed_candle=cursor,
                              candle_checkpoint=checkpoint, fetcher=lambda *_: ([], "live (test)"))


def test_forward_cursor_processes_each_new_closed_candle_once_and_persists():
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=15)
    saved, processed = [], []
    engine = _engine(cursor=start.isoformat(), checkpoint=saved.append)
    engine._process_bar = lambda _sym, bar, _strat: processed.append(bar.timestamp)  # type: ignore[method-assign]
    bars = [_bar(start), _bar(start + timedelta(minutes=5)), _bar(start + timedelta(minutes=10))]

    last = engine._ingest("BTCUSDT", object(), bars, start)
    assert processed == [start + timedelta(minutes=5), start + timedelta(minutes=10)]
    assert saved[-1] == (start + timedelta(minutes=10)).isoformat()
    assert engine.duplicate_candles_ignored == 1

    engine._ingest("BTCUSDT", object(), bars, last)
    assert len(processed) == 2
    assert engine.duplicate_candles_ignored == 4


def test_live_loop_passes_only_unseen_candles_to_ingest_contract():
    """Repeated provider windows are not counted as thousands of duplicates."""
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=15)
    engine = _engine(cursor=start.isoformat())
    bars = [_bar(start), _bar(start + timedelta(minutes=5))]
    unseen = [bar for bar in bars if bar.timestamp > start]
    engine._process_bar = lambda *_args: None  # type: ignore[method-assign]
    engine._ingest("BTCUSDT", object(), unseen, start)
    assert engine.duplicate_candles_ignored == 0


def test_closed_candle_filter_keeps_latest_closed_and_excludes_forming():
    now = datetime(2026, 8, 12, 12, 7, tzinfo=timezone.utc)
    closed = _bar(datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc))
    forming = _bar(datetime(2026, 8, 12, 12, 5, tzinfo=timezone.utc))
    assert valid_closed_bars([closed], 300, now=now) == [closed]
    assert valid_closed_bars([closed, forming], 300, now=now) == [closed]


def test_closed_candle_filter_sorts_deduplicates_and_rejects_malformed():
    now = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
    first = _bar(datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc))
    second = _bar(datetime(2026, 8, 12, 12, 5, tzinfo=timezone.utc))
    malformed = Bar(datetime(2026, 8, 12, 12, 10, tzinfo=timezone.utc), -1, 2, 1, 1, 1)
    assert valid_closed_bars([second, malformed, first, second], 300, now=now) == [first, second]


def test_cursor_bootstrap_requests_history_before_cursor_and_pages_to_live_edge():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    cursor = now - timedelta(minutes=1_200)
    all_bars = [_bar(cursor - timedelta(minutes=5 * 160) + timedelta(minutes=5 * i))
                for i in range(401)]
    calls = []

    def fetcher(_symbol, _timeframe, limit, since_ms=None):
        calls.append(since_ms)
        start = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc) if since_ms else all_bars[0].timestamp
        rows = [bar for bar in all_bars if bar.timestamp >= start][:min(limit, 200)]
        return rows, "live (test)"

    engine = _engine(cursor=cursor.isoformat())
    engine._fetcher = fetcher
    bars, _source = engine._fetch_bootstrap_history("BTCUSDT", 150, cursor)
    assert calls[0] < int(cursor.timestamp() * 1000)
    assert len(calls) >= 2
    assert len([bar for bar in bars if bar.timestamp <= cursor]) >= 150
    assert bars[-1].timestamp > cursor


def test_continuity_failure_blocks_processing_until_rest_backfill_repairs_gap():
    engine = _engine()
    start = datetime.now(timezone.utc) - timedelta(minutes=30)
    with pytest.raises(EngineFeedError, match="continuity failed"):
        engine._require_continuity([_bar(start), _bar(start + timedelta(minutes=10))], "5m")
    assert engine.missing_candles == 1


def test_multi_timeframe_context_is_causal_at_the_decision_close():
    engine = _engine()
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    decision = datetime(2026, 8, 8, 10, 5, tzinfo=timezone.utc)
    engine._multi_timeframe_context["BTCUSDT"] = {
        "5m": [_bar(decision)],
        "15m": [_bar(decision - timedelta(minutes=20)), _bar(decision)],
        "1h": [_bar(decision - timedelta(hours=2)), _bar(decision)],
        "4h": [_bar(decision - timedelta(hours=8)), _bar(decision)],
    }
    engine._apply_multi_timeframe_context(strategy, decision)
    # The bars opening at the decision time close in the future and are hidden.
    assert all(bar.timestamp < decision for tf in ("15m", "1h", "4h")
               for bar in strategy._context[tf])


def test_multi_timeframe_strategy_requires_its_five_minute_decision_clock():
    engine = _engine()
    engine.timeframe = "15m"
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    with pytest.raises(EngineFeedError, match="requires a 5m"):
        engine._refresh_multi_timeframe_context("BTCUSDT", strategy, entry_bars=[])


def test_adaptive_range_blocker_reaches_the_instance_without_an_order():
    engine = _engine()
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    start = datetime(2026, 9, 27, tzinfo=timezone.utc)
    context = {
        timeframe: [_bar(start + timedelta(minutes=minutes * index))
                    for index in range(70)]
        for timeframe, minutes in (("1h", 60), ("15m", 15), ("5m", 5))
    }
    context["4h"] = []
    strategy.set_timeframe_context(context)

    engine._process_bar("BTCUSDT", context["5m"][-1], strategy)

    assert engine.last_blocker == "GATE_REJECTED: REGIME_NOT_ALIGNED"
    assert engine.paper.open_position("BTCUSDT") is None


def test_stale_live_data_is_fail_closed_before_any_strategy_processing():
    engine = _engine()
    stale = _bar(datetime.now(timezone.utc) - timedelta(minutes=20))
    with pytest.raises(EngineFeedError, match="stale"):
        engine._record_market_snapshot("BTCUSDT", [stale])
    assert engine.market_data_status == "stale"


def test_freshness_starts_when_the_closed_candle_closes():
    """Provider OHLCV timestamps are candle-open times, not close times."""
    engine = _engine()
    # A 5m candle which opened 9m30s ago closed 4m30s ago.  It is still within
    # the 7m30s forward-paper freshness guard and must not be treated as stale.
    recently_closed = _bar(datetime.now(timezone.utc) - timedelta(minutes=9, seconds=30))
    engine._record_market_snapshot("BTCUSDT", [recently_closed])
    assert engine.market_data_status == "healthy"


def test_forward_fetcher_does_not_fall_back_when_provider_is_unavailable(monkeypatch):
    monkeypatch.setattr("data.live_data.fetch_ohlcv", lambda *args, **kwargs: None)
    monkeypatch.setattr("data.live_data.last_error", lambda *_args: "provider offline")
    with pytest.raises(ForwardMarketDataUnavailable, match="provider offline"):
        fetch_forward_bars("BTCUSDT", "5m", 200)


def test_instance_market_cursor_persists_across_manager_recreation():
    ledger = SqliteLedger(":memory:")
    first = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    instance = first.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                            strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                            capital_allocation=1000)
    first.store.save_market_state(instance.id, last_processed_candle_timestamp="2026-08-08T22:55:00+00:00",
                                  market_data_mode="paper_forward", market_data_status="healthy")
    restored = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    state = restored.store.market_state(instance.id)
    assert state["last_processed_candle_timestamp"] == "2026-08-08T22:55:00+00:00"
    assert state["market_data_mode"] == "paper_forward"


def test_lifecycle_aware_strategy_does_not_rescan_while_position_is_open():
    engine = _engine()

    class LifecycleStrategy:
        label = "Lifecycle test"

        def __init__(self):
            self.calls = 0
            self.state = "SCANNING"

        def on_bar(self, _bar):
            self.calls += 1

        def mark_position_managing(self, _reason=""):
            self.state = "MANAGING_POSITION"

        def decision_report(self):
            return {"state": self.state, "decision": "MANAGE", "reason": "open"}

    strategy = LifecycleStrategy()
    engine.paper.open(symbol="BTCUSDT", side="BUY", size=1, entry=100,
                      stop=90, target=120, alert_id="existing-position")
    engine._process_bar("BTCUSDT", _bar(datetime.now(timezone.utc)), strategy)
    assert strategy.calls == 0
    assert strategy.state == "MANAGING_POSITION"
