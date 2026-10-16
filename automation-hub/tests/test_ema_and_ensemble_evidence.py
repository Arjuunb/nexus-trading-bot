"""Evidence for the two strategies that had none.

Both `ema` and `ensemble` carried empty ``evidence`` tuples in the strategy
registry: they construct and emit plausible signals, but nothing pinned what
they actually decide. Both stay RESEARCH_ONLY -- these tests describe existing
behaviour so it cannot drift unnoticed, and are not a promotion case.

Nothing here tunes a parameter. Where a test names a number it is the shipped
default, asserted so that changing it is a deliberate act with a failing test
attached rather than a quiet edit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar, SignalType
from services.strategy_registry import REGISTRY
from strategies.ema_strategy import EMAStrategy
from strategies.ensemble_strategy import ConfirmationEnsemble

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(index: int, close: float, *, high: float | None = None,
         low: float | None = None, volume: float = 100.0) -> Bar:
    span = abs(close) * 0.002 or 0.02
    return Bar(timestamp=START + timedelta(minutes=5 * index),
               open=close, high=high if high is not None else close + span,
               low=low if low is not None else close - span,
               close=close, volume=volume)


def _feed(strategy, closes):
    """Drive a strategy over a close series and collect its signals."""
    out = []
    for index, close in enumerate(closes):
        signal = strategy.on_bar(_bar(index, close))
        if signal is not None:
            out.append((index, signal))
    return out


# --------------------------------------------------------------------- EMA

def test_ema_is_a_fast_slow_cross_on_its_shipped_periods():
    strategy = EMAStrategy("BTCUSDT")
    assert strategy.params["fast"] == 12
    assert strategy.params["slow"] == 26
    assert strategy.params["rr_target"] == 2.0


def test_ema_emits_long_when_fast_crosses_up_and_short_when_it_crosses_down():
    """Flat, then up, then down: a long cross, then a short cross.

    The flat lead-in is required, not decorative. Both averages seed from the
    same price, so on a series that only ever rises the fast average is already
    above the slow one by the time the warm-up completes -- the separation is
    established while the strategy is not yet looking, and no crossing bar is
    ever observed. Holding them equal first makes both crosses visible.
    """
    closes = [100.0] * 40                                   # equal averages
    closes += [100.0 + i * 1.6 for i in range(1, 60)]       # fast crosses up
    closes += [closes[-1] - i * 2.2 for i in range(1, 80)]  # fast crosses back down
    signals = _feed(EMAStrategy("BTCUSDT"), closes)

    kinds = [s.type for _, s in signals]
    assert SignalType.LONG in kinds, "a sustained rally must produce a bullish cross"
    assert SignalType.SHORT in kinds, "the reversal must produce a bearish cross"
    assert kinds.index(SignalType.LONG) < kinds.index(SignalType.SHORT)


def test_ema_is_silent_until_it_has_slow_plus_two_bars():
    """The warm-up is a real gate, not an incidental side effect."""
    strategy = EMAStrategy("BTCUSDT")
    slow = strategy.params["slow"]
    closes = [100.0 + i * 1.5 for i in range(slow + 1)]
    assert _feed(strategy, closes) == [], (
        f"EMA must not decide before it holds {slow} + 2 bars")


def test_ema_emits_nothing_on_a_flat_series():
    """No cross can occur when the two averages never separate."""
    assert _feed(EMAStrategy("BTCUSDT"), [100.0] * 120) == []


def test_ema_brackets_are_ordered_around_the_entry():
    closes = [100.0] * 40
    closes += [100.0 + i * 1.6 for i in range(1, 60)]
    closes += [closes[-1] - i * 2.2 for i in range(1, 80)]
    for _, signal in _feed(EMAStrategy("BTCUSDT"), closes):
        if signal.type is SignalType.LONG:
            assert signal.stop_loss < signal.entry < signal.take_profit
        else:
            assert signal.take_profit < signal.entry < signal.stop_loss


# ---------------------------------------------------------------- ENSEMBLE

def test_ensemble_requires_a_majority_of_its_three_votes():
    strategy = ConfirmationEnsemble("BTCUSDT")
    assert strategy.params["min_votes"] == 2, (
        "the ensemble is a best-of-three; changing this changes what it is")
    assert strategy.params["channel"] == 30
    assert strategy.params["st_period"] == 10


def test_ensemble_waits_for_the_longest_of_its_three_lookbacks():
    strategy = ConfirmationEnsemble("BTCUSDT")
    need = max(strategy.params["slow"], strategy.params["st_period"],
               strategy.params["channel"]) + 2
    closes = [100.0 + i * 1.2 for i in range(need - 1)]
    assert _feed(strategy, closes) == [], (
        f"the ensemble must hold {need} bars before voting")


def test_ensemble_takes_a_side_in_a_sustained_trend():
    """With EMA, Supertrend and Donchian all trending, the vote is decisive."""
    rising = [100.0 + i * 1.4 for i in range(200)]
    signals = _feed(ConfirmationEnsemble("BTCUSDT"), rising)
    assert signals, "a clean uptrend must carry at least two of three votes"
    assert signals[0][1].type is SignalType.LONG

    falling = [400.0 - i * 1.4 for i in range(200)]
    signals = _feed(ConfirmationEnsemble("BTCUSDT"), falling)
    assert signals, "a clean downtrend must carry at least two of three votes"
    assert signals[0][1].type is SignalType.SHORT


def test_ensemble_does_not_repeat_a_side_it_already_holds():
    """A vote that stays long must not re-emit long on every later bar."""
    rising = [100.0 + i * 1.4 for i in range(200)]
    signals = _feed(ConfirmationEnsemble("BTCUSDT"), rising)
    longs = [s for _, s in signals if s.type is SignalType.LONG]
    assert len(longs) < 20, (
        f"{len(longs)} long signals over 200 trending bars suggests the "
        "desired-state latch stopped suppressing repeats")


def test_ensemble_brackets_are_ordered_around_the_entry():
    rising = [100.0 + i * 1.4 for i in range(200)]
    for _, signal in _feed(ConfirmationEnsemble("BTCUSDT"), rising):
        if signal.type is SignalType.LONG:
            assert signal.stop_loss < signal.entry < signal.take_profit
        else:
            assert signal.take_profit < signal.entry < signal.stop_loss


# --------------------------------------------------------------- LIFECYCLE

@pytest.mark.parametrize("key", ["ema", "ensemble"])
def test_both_stay_research_only_and_now_carry_evidence(key):
    entry = REGISTRY[key]
    record = entry if isinstance(entry, dict) else vars(entry)
    assert record["lifecycle"] == "RESEARCH_ONLY", (
        "Adding evidence is not a promotion. Production requires the "
        "repository's own approval process, not a passing test file.")
    assert record.get("evidence"), (
        "this file is that evidence; the registry must point at it")
    assert any("ema_and_ensemble" in str(path) for path in record["evidence"])
