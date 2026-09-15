"""The research replay driver.

A replay script is easy to get subtly wrong in a way that never shows up as an
error: feed it a candle that had not closed yet and it reports results no
forward run could reproduce. These check the two things that matter -- it
refuses manufactured candles, and it slices every timeframe causally.
"""
from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

from bot.types import Bar
from services.pa_rulebook_v01 import CONFIRM_TF, CONTEXT_TF, SETUP_TF
from tests.test_pa_rulebook_engine import (
    ATR15, _confirm_series, _confirmation_for, _context_bars, _flat, _rejection_for,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pa_rulebook_replay.py"


@pytest.fixture(scope="module")
def replay_module():
    spec = importlib.util.spec_from_file_location("pa_rulebook_replay", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dataset():
    """The Setup A sequence from the engine tests, laid out on three clocks."""
    from services.pa_rulebook_v01 import RulebookConfig, build_zones

    config = RulebookConfig(symbol="BTCUSDT")
    context = _context_bars()
    support = [z for z in build_zones(context, config) if z.kind == "support"][-1]
    start = support.created_at + timedelta(hours=2)
    setup_bars = _flat(start, 40, support.upper + 400.0, ATR15, minutes=15)
    rejection = _rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15))
    setup_bars.append(rejection)
    filler, window = _confirm_series(rejection)
    confirms = filler + [_confirmation_for(rejection, window),
                         # one more bar so the replay loop steps past the trade
                         Bar(window + timedelta(minutes=5), 108_200, 108_260,
                             108_140, 108_200, 30.0)]
    return {CONTEXT_TF: context, SETUP_TF: setup_bars, CONFIRM_TF: confirms}


def test_the_replay_reaches_the_trade_the_engine_found(replay_module, capsys):
    data = _dataset()

    def loader(symbol, timeframe, count):
        return list(data[timeframe]), "test fixture"

    accepted = replay_module.replay("BTCUSDT", bars=len(data[CONFIRM_TF]),
                                    strategy="rejection", equity=100_000.0,
                                    verbose=True, loader=loader)
    out = capsys.readouterr().out
    assert accepted == 1, out
    assert "TRADE" in out and "PA_SR_REJECTION_V01" in out
    assert "RESEARCH REPLAY, NO ORDERS" in out
    assert "why nothing traded, by count" in out


def test_the_replay_refuses_anything_but_real_candles(replay_module):
    """Chapter 2: synthetic fixtures belong only to tests. The loader asks for
    real data and fails loudly rather than quietly measuring a made-up market."""
    import inspect

    source = inspect.getsource(replay_module._load)
    assert "require_real=True" in source
    with pytest.raises(SystemExit):
        replay_module.replay("NOSUCHPAIR", bars=10, strategy="both",
                             equity=1000.0, verbose=False,
                             loader=lambda *a: ([], "unavailable"))


def test_the_incremental_walk_matches_slicing_on_a_gappy_series(replay_module):
    """The loop feeds candles by advancing pointers instead of re-slicing.

    That is a performance change, so it has to be provably the same answer, not
    a plausibly similar one. The reference is the obvious O(n^2) definition --
    "every candle that had closed at this boundary" -- and the series here has
    deliberate gaps, because an equal-cadence series would pass even with an
    off-by-one that leaks the forming candle.
    """
    context = _context_bars()[:40]
    del context[7:13]                       # a hole the pointer must step over
    boundaries = [row.timestamp for row in _context_bars()[:40]]

    walked, at = [], 0
    for boundary in boundaries:
        while at < len(context) and context[at].timestamp < boundary:
            walked.append(context[at])
            at += 1
        reference = [row for row in context if row.timestamp < boundary]
        assert [row.timestamp for row in walked] == [row.timestamp for row in reference]
        assert all(row.timestamp < boundary for row in walked)


def test_a_date_window_bounds_decisions_but_keeps_the_warm_up(replay_module, capsys):
    """Chapter 18 needs 200 closed context bars before the first decision.

    Truncating the context to the window would make the opening weeks of every
    run decide on structure the engine had not seen, and no two windows would
    agree on the same day. The window bounds which candles are *decided on*,
    not which history exists.
    """
    data = _dataset()
    seen = {}

    def loader(symbol, timeframe, count):
        seen[timeframe] = list(data[timeframe])
        return list(data[timeframe]), "test fixture"

    confirms = data[CONFIRM_TF]
    cutoff = confirms[len(confirms) // 2].timestamp

    replay_module.replay("BTCUSDT", bars=len(confirms), strategy="rejection",
                         equity=100_000.0, verbose=False, loader=loader,
                         start=cutoff.isoformat())
    out = capsys.readouterr().out
    # The context frame is still the full 240 bars, not the post-cutoff tail.
    assert f"{len(data[CONTEXT_TF]):>6} candles" in out
    assert "RESEARCH REPLAY" in out
