from __future__ import annotations

import math
import hashlib
import json
from datetime import datetime, timedelta, timezone

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
        components = strategy.decision_report()["components"]
        assert "1h_regime_alignment" in components
        assert "4h_regime_alignment" not in components


def test_missing_optional_4h_bias_does_not_crash_an_eligible_5m_signal():
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    context = _context(1)
    context["4h"] = []  # A missing secondary bias is not an entry veto.
    strategy.set_timeframe_context(context)

    signal = strategy.on_bar(context["5m"][-1])

    assert signal is not None
    assert set(signal.snapshot["timeframe_closes"]) == {"1h", "15m", "5m"}
    assert "4H bias NEUTRAL" in signal.reason


def test_adaptive_blocker_codes_name_the_stage_without_changing_signals(monkeypatch):
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    context = _context(1)
    context["1h"] = [Bar(bar.timestamp, 100, 101, 99, 100, 100)
                     for bar in context["1h"]]
    strategy.set_timeframe_context(context)
    assert strategy.on_bar(context["5m"][-1]) is None
    assert strategy.decision_report()["blocker_code"] == "REGIME_NOT_ALIGNED"

    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    context = _context(1)
    strategy.set_timeframe_context(context)
    monkeypatch.setattr(strategy.pullback_detector, "assess",
                        lambda *_: StageAssessment(False, 0, "INVALID",
                                                   failed=("not at pullback location",)))
    assert strategy.on_bar(context["5m"][-1]) is None
    assert strategy.decision_report()["blocker_code"] == "PULLBACK_NOT_READY"
    assert "not at pullback location" in strategy.decision_report()["reason"]

    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    strategy.set_timeframe_context(context)
    monkeypatch.setattr(strategy.confirmation_engine, "assess",
                        lambda *_: StageAssessment(False, 0, "NONE"))
    assert strategy.on_bar(context["5m"][-1]) is None
    assert strategy.decision_report()["blocker_code"] == "CONFIRMATION_NOT_READY"


def test_quality_threshold_is_a_hard_gate(monkeypatch):
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT", config=AdaptiveTrendPullbackConfig(quality_minimum=99))
    context = _context(1)
    strategy.set_timeframe_context(context)
    monkeypatch.setattr(strategy.trend_engine, "assess", lambda *_: StageAssessment(True, 70, "BULLISH", swing_low=90))
    monkeypatch.setattr(strategy.pullback_detector, "assess", lambda *_: StageAssessment(True, 70, "VALID", swing_low=90, location="EMA"))
    monkeypatch.setattr(strategy.confirmation_engine, "assess", lambda *_: StageAssessment(True, 70, "CONFIRMED"))
    assert strategy.on_bar(context["5m"][-1]) is None
    assert strategy.decision_report()["decision"] == "REJECT"
    assert strategy.decision_report()["blocker_code"] == "QUALITY_TOO_LOW"
    assert strategy.decision_report()["reason"].startswith("Quality ")


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
