from __future__ import annotations

import math
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar, SignalType
from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy
from strategies.adaptive_trend_pullback.config import AdaptiveTrendPullbackConfig
from strategies.adaptive_trend_pullback.models import MarketRegime, SetupState, StageAssessment
from strategies.adaptive_trend_pullback.regime_engine import MarketRegimeEngine
from strategies.builtin_versions import BUILTIN_STRATEGY_VERSIONS


_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def _trend(timeframe: str, direction: int, count: int = 90, *, volatility: float = 1.0) -> list[Bar]:
    bars = []
    for index in range(count):
        centre = 200 + direction * index * 0.45 + math.sin(index / 3) * 1.3
        opened = centre - direction * 0.18
        closed = centre + direction * 0.18
        bars.append(Bar(
            _START + timedelta(seconds=_SECONDS[timeframe] * index),
            opened, max(opened, closed) + volatility, min(opened, closed) - volatility,
            closed, 100 + index,
        ))
    return bars


def _context(direction: int) -> dict[str, list[Bar]]:
    regime = _trend("4h", direction)
    trend = _trend("1h", direction)
    pullback = _trend("15m", direction)
    # Corrective final 15M sequence around the rising/falling EMA rather than
    # chasing the trend's extreme.
    anchor = pullback[-5].close
    for offset, index in enumerate(range(len(pullback) - 4, len(pullback))):
        close = anchor - direction * 0.08 * offset
        pullback[index] = Bar(pullback[index].timestamp, close + direction * 0.05,
                              close + 0.7, close - 0.7, close, 90)
    entry = _trend("5m", direction)
    consolidation = entry[-9:-1]
    boundary = max(bar.high for bar in consolidation) if direction > 0 else min(bar.low for bar in consolidation)
    close = boundary + direction * 1.0
    open_price = boundary - direction * 0.5
    entry[-1] = Bar(entry[-1].timestamp, open_price,
                    max(open_price, close) + 0.2, min(open_price, close) - 0.2,
                    close, 500)
    return {"4h": regime, "1h": trend, "15m": pullback, "5m": entry}


def test_regime_engine_classifies_bull_bear_and_high_volatility():
    engine = MarketRegimeEngine(AdaptiveTrendPullbackConfig())
    assert engine.assess(_trend("4h", 1)).regime == MarketRegime.BULL_TREND
    assert engine.assess(_trend("4h", -1)).regime == MarketRegime.BEAR_TREND
    explosive = _trend("4h", 1, volatility=20)
    assert engine.assess(explosive).regime == MarketRegime.HIGH_VOLATILITY


def test_missing_or_ranging_context_is_an_explicit_no_trade():
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    assert strategy.on_bar(_trend("5m", 1, 1)[0]) is None
    report = strategy.decision_report()
    assert report["state"] == "BLOCKED"
    assert "Insufficient" in report["reason"]


def test_long_and_short_use_real_stage_logic_structure_stop_and_symmetric_targets():
    for direction, expected in ((1, SignalType.LONG), (-1, SignalType.SHORT)):
        strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
        context = _context(direction)
        strategy.set_timeframe_context(context)
        signal = strategy.on_bar(context["5m"][-1])
        assert signal is not None and signal.type == expected
        assert signal.confidence >= 0.75
        assert signal.take_profit > signal.entry > signal.stop_loss if direction > 0 else signal.take_profit < signal.entry < signal.stop_loss
        assert abs(signal.take_profit - signal.entry) / abs(signal.entry - signal.stop_loss) >= 2.0
        assert strategy.lifecycle_state == SetupState.ORDER_PENDING
        assert signal.snapshot["timeframe_closes"].keys() == {"4h", "1h", "15m", "5m"}


def test_quality_threshold_is_a_hard_gate(monkeypatch):
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT", config=AdaptiveTrendPullbackConfig(quality_minimum=99))
    context = _context(1)
    strategy.set_timeframe_context(context)
    monkeypatch.setattr(strategy.trend_engine, "assess", lambda *_: StageAssessment(True, 70, "BULLISH", swing_low=90))
    monkeypatch.setattr(strategy.pullback_detector, "assess", lambda *_: StageAssessment(True, 70, "VALID", swing_low=90, location="EMA"))
    monkeypatch.setattr(strategy.confirmation_engine, "assess", lambda *_: StageAssessment(True, 70, "CONFIRMED"))
    assert strategy.on_bar(context["5m"][-1]) is None
    assert strategy.decision_report()["decision"] == "REJECT"


def test_position_lifecycle_is_explicit_and_reported():
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    strategy.mark_position_open()
    assert strategy.decision_report()["state"] == "POSITION_OPEN"
    strategy.mark_position_managing()
    assert strategy.decision_report()["state"] == "MANAGING_POSITION"
    strategy.mark_position_closed("take-profit completed")
    report = strategy.decision_report()
    assert report["state"] == "POSITION_CLOSED"
    assert report["reason"] == "take-profit completed"


def test_configuration_is_namespaced_validated_and_environment_driven(monkeypatch):
    monkeypatch.setenv("HUB_ATP_QUALITY_MINIMUM", "82")
    monkeypatch.setenv("HUB_ATP_TARGET_RR", "3")
    config = AdaptiveTrendPullbackConfig.from_env()
    assert config.quality_minimum == 82
    assert config.target_rr == 3

    with __import__("pytest").raises(ValueError, match="minimum_rr"):
        AdaptiveTrendPullbackConfig(minimum_rr=1.5)


def test_versioned_fixture_fingerprint_is_deterministic():
    rows = []
    for direction in (1, -1):
        strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
        context = _context(direction)
        strategy.set_timeframe_context(context)
        signal = strategy.on_bar(context["5m"][-1])
        assert signal is not None
        rows.append({
            "type": signal.type.value,
            "entry": round(signal.entry, 8),
            "stop_loss": round(signal.stop_loss, 8),
            "take_profit": round(signal.take_profit, 8),
            "confidence": round(signal.confidence, 8),
            "regime": signal.regime,
            "quality": signal.brain_score,
            "timeframe_closes": signal.snapshot["timeframe_closes"],
        })
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    metadata = BUILTIN_STRATEGY_VERSIONS["adaptive_trend_pullback"]
    assert metadata.fixture_signal_count == len(rows)
    assert digest == metadata.fixture_signal_sha256


def test_research_resolver_exposes_the_same_versioned_strategy():
    from services.strategy_presets import REGISTRY, resolve
    descriptor = resolve("Adaptive MTF Trend Pullback", "BTCUSDT", "5m", {})
    assert descriptor == {
        "kind": "builtin", "key": "adaptive_trend_pullback",
        "label": "Adaptive MTF Trend Pullback",
    }
    registry = next(row for row in REGISTRY if row["id"] == "adaptive_trend_pullback")
    assert registry["version"] == "1.0.0"
    assert registry["timeframes"] == ["5m"]


@pytest.mark.parametrize("direction", (1, -1))
@pytest.mark.parametrize("optional_context", ("missing", "empty"))
def test_optional_four_hour_context_does_not_change_valid_entry(direction, optional_context):
    context = _context(direction)
    baseline = AdaptiveTrendPullbackStrategy("BTCUSDT")
    baseline.set_timeframe_context(context)
    expected = baseline.on_bar(context["5m"][-1])
    assert expected is not None

    if optional_context == "missing":
        context.pop("4h")
    else:
        context["4h"] = []
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    strategy.set_timeframe_context(context)
    actual = strategy.on_bar(context["5m"][-1])

    assert actual is not None
    # The optional clock contributes telemetry only. All trading outputs and
    # evidence from the required clocks retain the valid-context baseline.
    for attribute in ("type", "entry", "stop_loss", "take_profit", "confidence", "reason", "checklist"):
        assert getattr(actual, attribute) == getattr(expected, attribute)
    expected.snapshot["timeframe_closes"].pop("4h")
    assert actual.snapshot == expected.snapshot
    assert set(actual.snapshot["timeframe_closes"]) == {"1h", "15m", "5m"}


@pytest.mark.parametrize("timeframe", ("1h", "15m", "5m"))
def test_missing_required_context_still_blocks_entries(timeframe):
    context = _context(1)
    decision_bar = context["5m"][-1]
    context.pop(timeframe)
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    strategy.set_timeframe_context(context)

    assert strategy.on_bar(decision_bar) is None
    assert strategy.decision_report()["state"] == "BLOCKED"
    assert timeframe in strategy.decision_report()["reason"]


def _current_native_context() -> dict[str, list[Bar]]:
    """Keep stage-fixture prices with independently closed native clocks."""
    now = datetime.now(timezone.utc)
    context = _context(1)
    for timeframe, rows in context.items():
        duration = _SECONDS[timeframe]
        latest_close = datetime.fromtimestamp(
            int(now.timestamp()) // duration * duration, timezone.utc)
        shift = latest_close - (rows[-1].timestamp + timedelta(seconds=duration))
        context[timeframe] = [replace(bar, timestamp=bar.timestamp + shift) for bar in rows]
    return context


def _native_engine(context):
    from data.ledger import SqliteLedger
    from execution.paper_engine import PaperExecutionEngine
    from services.auto_engine import AutoStrategyEngine
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline

    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, starting_balance=10_000)
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=10_000)
    return AutoStrategyEngine(
        pipeline, paper, ledger, symbols=["BTCUSDT"], timeframe="5m",
        strategy_factory=AdaptiveTrendPullbackStrategy, live=True,
        fetcher=lambda _symbol, timeframe, _limit: (context.get(timeframe, []), "live (test fixture)"),
    )


@pytest.mark.parametrize("optional_context", ("missing", "empty", "stale", "valid"))
def test_forward_native_boundary_handles_optional_context_without_an_entry_gate(optional_context):
    context = _current_native_context()
    if optional_context == "missing":
        context.pop("4h")
    elif optional_context == "empty":
        context["4h"] = []
    elif optional_context == "stale":
        context["4h"] = [replace(bar, timestamp=bar.timestamp - timedelta(hours=12))
                         for bar in context["4h"]]
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    engine = _native_engine(context)
    decision_bar = context["5m"][-1]

    engine._refresh_multi_timeframe_context("BTCUSDT", strategy, entry_bars=context["5m"])
    engine._apply_multi_timeframe_context(strategy, decision_bar.timestamp)
    signal = strategy.on_bar(decision_bar)

    assert signal is not None
    assert signal.type == SignalType.LONG
    assert signal.snapshot["mtf_evidence"]["primary"] is not None
    if optional_context == "valid":
        assert signal.snapshot["mtf_evidence"]["secondary"] is not None
        assert "4h" in signal.snapshot["timeframe_closes"]
    else:
        assert signal.snapshot["mtf_evidence"]["secondary"] is None
        assert "4h" not in signal.snapshot["timeframe_closes"]
        assert strategy._context["4h"] == []


@pytest.mark.parametrize("timeframe", ("1h", "15m"))
@pytest.mark.parametrize("required_context", ("missing", "stale"))
def test_forward_native_boundary_keeps_required_context_fail_closed(timeframe, required_context):
    from services.auto_engine import EngineFeedError

    context = _current_native_context()
    if required_context == "missing":
        context.pop(timeframe)
    else:
        context[timeframe] = [replace(bar, timestamp=bar.timestamp - timedelta(hours=4))
                              for bar in context[timeframe]]
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    engine = _native_engine(context)

    with pytest.raises(EngineFeedError, match=(f"{timeframe} returned" if required_context == "missing"
                                             else f"{timeframe} context stale")):
        engine._refresh_multi_timeframe_context("BTCUSDT", strategy, entry_bars=context["5m"])
    assert strategy.lifecycle_state == SetupState.SCANNING
    assert engine.ledger.get_positions() == []
