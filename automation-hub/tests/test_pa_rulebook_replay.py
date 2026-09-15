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


def test_each_timeframe_is_sliced_strictly_before_the_decision_boundary(replay_module):
    """The classic replay bug is an off-by-one that leaks the forming candle."""
    rows = _context_bars()[:5]
    boundary = rows[3].timestamp
    sliced = replay_module._slice_upto(rows, boundary)
    assert [row.timestamp for row in sliced] == [row.timestamp for row in rows[:3]]
    assert all(row.timestamp < boundary for row in sliced)
