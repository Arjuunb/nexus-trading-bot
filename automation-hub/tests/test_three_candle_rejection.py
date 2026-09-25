"""3-Candle Rejection · EMA 9/33 (strategies/three_candle_rejection.py).

Each rule the owner specified is pinned to a hand-built case: the push,
rejection and confirmation candles at a level touched twice, the EMA 9/33
filter, the stop beyond the wick and the 2R target. Then the plumbing: warm-up
history loaded the way the engine loads it, the registry, the Visual Lab
adapter and a real forward-engine start.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.data.indicators import atr
from bot.types import Bar, SignalType
from services.strategy_factory import make_builtin_strategy
from strategies.three_candle_rejection import ThreeCandleRejectionStrategy

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
TF = timedelta(minutes=5)


def _bar(i: int, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(T0 + TF * i, o, h, low, c, 1.0)


def _history(*, level_touches: int = 2, trend: str = "up") -> tuple[list[Bar], int]:
    """A quiet base, a range whose swing highs at exactly 100 form the level,
    then either a steady rally (EMA 9 above 33) or a rally and a slow decline
    back towards the level (EMA 9 below 33)."""
    rows, i = [], 0
    for k in range(140):
        p = 96 + (k % 4) * 0.25
        rows.append(_bar(i, p, p + 0.3, p - 0.3, p + 0.1)); i += 1
    swing = [96, 97, 98, 99, 100, 99, 98, 97, 96]
    shape = swing + [96.5] + (swing if level_touches >= 2 else [96, 97, 98, 99, 98.5, 98, 97, 96.5])
    for p in shape:
        rows.append(_bar(i, p - 0.2, p, p - 0.6, p - 0.1)); i += 1
    price = 97.0
    while price < 110:
        rows.append(_bar(i, price, price + 0.35, price - 0.1, price + 0.3)); price += 0.3; i += 1
    if trend == "down":
        while price > 101.6:
            rows.append(_bar(i, price, price + 0.1, price - 0.35, price - 0.3)); price -= 0.3; i += 1
    return rows, i


def _long_pattern(i: int, *, open_: float = 110.0, confirm_close: float = 102.2) -> list[Bar]:
    return [
        _bar(i, open_, open_ + 0.2, 99.9, 100.4),              # 1 push into the level
        _bar(i + 1, 100.4, 101.0, 99.3, 100.8),                 # 2 wick below 100, close back above
        _bar(i + 2, 100.8, max(102.5, confirm_close), 100.6, confirm_close),  # 3 confirmation
    ]


def _mirror(rows: list[Bar], k: float = 200.0) -> list[Bar]:
    """Price reflected about k/2: every long setup becomes the short one."""
    return [Bar(r.timestamp, k - r.open, k - r.low, k - r.high, k - r.close, r.volume) for r in rows]


def _run(rows: list[Bar], strategy=None):
    strategy = strategy or ThreeCandleRejectionStrategy("BTCUSDT")
    signals = [s for s in (strategy.on_bar(b) for b in rows) if s is not None]
    return strategy, signals


# ─────────────────────────── the rules ───────────────────────────
def test_push_reject_confirm_at_a_two_touch_support_buys_with_the_wick_stop_and_2r():
    rows, i = _history()
    strategy, signals = _run(rows + _long_pattern(i))
    assert len(signals) == 1
    signal = signals[0]
    _, c2, c3 = _long_pattern(i)
    buffer = 0.1 * atr(strategy.bars, 14)
    assert signal.type == SignalType.LONG
    assert signal.timestamp == c3.timestamp                  # decided on candle 3's close
    assert signal.entry == c3.close
    assert signal.stop_loss == pytest.approx(min(c2.low, c3.low) - buffer)
    risk = signal.entry - signal.stop_loss
    assert signal.take_profit == pytest.approx(signal.entry + 2 * risk)
    assert "support 100" in signal.reason and "2 touches" in signal.reason
    report = strategy.decision_report()
    assert report["decision"] == "ENTER" and report["level"] == pytest.approx(100.0)


def test_the_mirror_image_sells_at_resistance():
    rows, i = _history()
    strategy, signals = _run(_mirror(rows + _long_pattern(i)))
    assert len(signals) == 1
    signal = signals[0]
    assert signal.type == SignalType.SHORT
    assert signal.entry == pytest.approx(200 - 102.2)
    assert signal.stop_loss > signal.entry
    assert signal.take_profit == pytest.approx(signal.entry - 2 * (signal.stop_loss - signal.entry))
    assert "resistance 100" in signal.reason


def test_a_rejection_against_the_ema_trend_is_filtered_and_says_so():
    rows, i = _history(trend="down")
    strategy, signals = _run(rows + _long_pattern(i, open_=101.6))
    assert signals == []
    report = strategy.decision_report()
    assert report["blocker_code"] == "EMA_TREND_NOT_ALIGNED"
    assert report["direction"] == "long" and "filtered" in report["reason"]


def test_without_the_confirmation_close_there_is_no_trade():
    rows, i = _history()
    strategy, signals = _run(rows + _long_pattern(i, confirm_close=100.9))  # not above 101.0
    assert signals == []
    assert strategy.decision_report()["blocker_code"] == "NO_CONFIRMATION"


def test_one_prior_touch_is_not_a_level_and_the_pattern_cannot_count_itself():
    rows, i = _history(level_touches=1)
    strategy, signals = _run(rows + _long_pattern(i))
    assert signals == []
    assert strategy.decision_report()["blocker_code"] == "NO_ELIGIBLE_ZONE"
    assert all(abs(level.price - 100) > 0.5 for level in strategy.levels)


def test_nothing_trades_before_the_declared_warm_up():
    rows, i = _history()
    strategy, signals = _run((rows + _long_pattern(i))[-150:])
    assert signals == []
    assert strategy.decision_report()["blocker_code"] == "WARMUP"


def test_history_loaded_the_way_the_engine_warms_up_gives_the_same_decision():
    """The forward engine appends warm-up candles straight to .bars without
    calling on_bar, so no rule may depend on state built bar by bar."""
    rows, i = _history()
    pattern = _long_pattern(i)
    warmed = ThreeCandleRejectionStrategy("BTCUSDT")
    warmed.bars.extend(rows)
    late = [s for s in (warmed.on_bar(b) for b in pattern) if s is not None]
    _, streamed = _run(rows + pattern)
    assert [(s.type, s.entry, s.stop_loss, s.take_profit) for s in late] == \
        [(s.type, s.entry, s.stop_loss, s.take_profit) for s in streamed]


def test_every_reported_blocker_is_explained_and_mapped_to_a_gate():
    from services.strategy_visual_registry import ADAPTERS, BLOCKER_EXPLANATIONS

    adapter = ADAPTERS["three_candle_rejection"]
    gated = set().union(*(gate.blockers for gate in adapter.setup_gates))
    codes = {"WARMUP", "NO_ELIGIBLE_ZONE", "NO_SUPPORT_REJECTION", "NO_RESISTANCE_REJECTION",
             "NO_CONFIRMATION", "EMA_TREND_NOT_ALIGNED"}
    rows, i = _history()
    strategy = ThreeCandleRejectionStrategy("BTCUSDT")
    for b in rows + _long_pattern(i):
        strategy.on_bar(b)
        code = strategy.decision_report()["blocker_code"]
        if code:
            codes.add(code)
    for code in codes:
        assert code in BLOCKER_EXPLANATIONS, code
        assert code in gated, code


# ─────────────────────────── the platform ───────────────────────────
def test_it_is_a_selectable_production_strategy_on_every_entry_timeframe():
    from services import strategy_registry as registry

    entry = registry.entry("three_candle_rejection")
    assert entry.lifecycle == registry.PRODUCTION and entry.version == "1.0.0"
    assert registry.selectable_for_new_instance("three_candle_rejection") == (True, "")
    assert set(entry.supported_timeframes) == {"1m", "5m", "15m", "1h", "4h"}
    assert entry.required_data == ("entry_candles",)
    built = make_builtin_strategy("three_candle_rejection", "ETHUSDT")
    assert isinstance(built, ThreeCandleRejectionStrategy) and built.symbol == "ETHUSDT"
    assert built.warmup_required >= entry.warmup_candles == 200


def test_the_visual_lab_draws_its_own_levels_emas_and_rejection():
    from services.strategy_visual_features import extract

    rows, i = _history()
    strategy, _ = _run(rows + _long_pattern(i))
    overlays = extract("three_candle_rejection", strategy)
    features = {o.feature for o in overlays}
    assert {"ema", "support", "rejection_candle"} <= features
    assert {o.id for o in overlays if o.feature == "ema"} == {"ema_fast", "ema_slow"}
    marker = next(o for o in overlays if o.feature == "rejection_candle")
    assert marker.payload["direction"] == "long" and marker.payload["price"] == pytest.approx(100.0)


def test_it_warms_up_and_runs_inside_the_forward_engine():
    import time

    from data.ledger import SqliteLedger
    from execution.paper_engine import PaperExecutionEngine
    from services.auto_engine import AutoStrategyEngine
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline

    newest = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=310)
    rows, _ = _history()
    rows = (rows * 2)[:420]
    shifted = [Bar(newest - TF * (len(rows) - 1 - index), r.open, r.high, r.low, r.close, r.volume)
               for index, r in enumerate(rows)]

    def resample(minutes: int) -> list[Bar]:
        size = minutes // 5
        out = []
        for start in range(0, len(shifted) - size + 1, size):
            chunk = shifted[start:start + size]
            out.append(Bar(chunk[0].timestamp, chunk[0].open, max(b.high for b in chunk),
                           min(b.low for b in chunk), chunk[-1].close, 1.0))
        return out

    def fetcher(_symbol, timeframe, _limit, **_kwargs):
        if timeframe == "5m":
            return list(shifted), "live (test hub)"
        return resample(60 if timeframe == "1h" else 240), "live (test hub)"

    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, 10_000)
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=10_000,
                              risk_per_trade_pct=0.01, exposure_limit_pct=0.5)
    engine = AutoStrategyEngine(
        pipeline, paper, ledger, symbols=["BTCUSDT"], timeframe="5m", live=True,
        strategy_factory=lambda symbol: make_builtin_strategy("three_candle_rejection", symbol),
        fetcher=fetcher, live_poll_s=600, entry_mode="market")
    assert engine.start() is True
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and engine.status().get("lifecycle_state") != "running":
            time.sleep(0.05)
        status = engine.status()
        assert status["lifecycle_state"] == "running", (status.get("lifecycle_state"), status.get("last_error"))
        assert status["warmup_bars"] >= 200
        assert paper.positions() == [], "a warm-up candle opened a position"
    finally:
        engine.stop()
