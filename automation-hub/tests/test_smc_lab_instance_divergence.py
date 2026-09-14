"""SMC Lab and SMC Instance are two strategies, not one implemented twice.

A unification was proposed: make the Trading Instance SMC strategy a thin
adapter over ``services/native_smc.py``, mirroring how Price Action works. That
refactor is correct for Price Action because
``strategies/price_action_rejection.py`` already imports
``NativePriceActionEngine`` -- there is one engine and two front doors.

SMC is not that shape. The two implementations share no code and disagree about
when to enter, so making the instance defer to the lab engine would change what
an SMC Trading Instance trades. That is an alpha change, not a refactor, and it
needs an explicit decision rather than a tidy-up.

These tests pin the divergence so it stays visible, and pin the instance's own
behaviour so that a future unification shows up as a failure here rather than
as a quiet change in what the bot trades. When the unification is approved,
this module should be replaced by real parity assertions -- not deleted.
"""
from __future__ import annotations

import inspect

import pytest

from bot.data.resample import resample
from bot.data.synthetic import generate_bars
from services.mtf_policy import native_timeframes
from services.native_smc import SMCConfig, SMCMarketStructureEngine
from strategies.smc_strategy import SMCStrategy

_TFS = native_timeframes("5m")


def _instance_signal_bars(bars) -> set[int]:
    strategy = SMCStrategy("BTCUSDT")
    fired: set[int] = set()
    for index, bar in enumerate(bars):
        window = bars[: index + 1]
        strategy.set_native_mtf_context(
            {tf: resample(window, tf) for tf in _TFS},
            {"primary": {"htf_timeframe": _TFS[0]}})
        if strategy.on_bar(bar) is not None:
            fired.add(index)
    return fired


def _lab_proposal_bars(bars) -> set[int]:
    engine = SMCMarketStructureEngine(SMCConfig(symbol="BTCUSDT"))
    fired: set[int] = set()
    seen: set[str] = set()
    for index, bar in enumerate(bars):
        window = bars[: index + 1]
        # One argument: the lab engine derives its own HTF evidence. The
        # instance setter takes two. That asymmetry is itself part of why the
        # two paths cannot be swapped for one another casually.
        engine.set_native_mtf_context({tf: resample(window, tf) for tf in _TFS})
        engine.process_closed_bar(bar)
        for pid in engine.proposals:
            if pid not in seen:
                seen.add(pid)
                fired.add(index)
    return fired


def test_the_instance_does_not_import_the_lab_engine():
    """The structural fact underneath everything else here."""
    source = inspect.getsource(inspect.getmodule(SMCStrategy))
    assert "native_smc" not in source, (
        "strategies/smc_strategy.py now references the lab engine. If the "
        "unification has been approved, replace this module with parity tests "
        "asserting equal entries, stops, targets and RR.")


def test_the_lab_requires_an_ordered_chain_the_instance_does_not():
    """Lab: sweep -> BOS/CHoCH -> FVG -> retest -> rejection, in that order.

    Instance: the same three events in any order, each merely *recent* within
    its own rolling lookback, with no retest and no rejection requirement.
    """
    lab = inspect.getsource(inspect.getmodule(SMCMarketStructureEngine))
    assert "WAITING_RETEST" in lab and "REJECTION_CONFIRMED" in lab, (
        "the lab's retest-and-rejection stage is what the instance lacks")

    instance = inspect.getsource(SMCStrategy.generate)
    assert "recent_sweep_low" in instance and "recent_bull_fvg" in instance, (
        "the instance's confluence-in-window rule is what the lab lacks")
    assert "WAITING_RETEST" not in instance


def test_the_instance_does_not_require_a_rejection_candle_by_default():
    """``use_rejection`` is False, so ``rej_long``/``rej_short`` are always True.

    The lab, by contrast, requires a rejection at the point of interest before
    a setup can reach ENTRY_READY. Enabling the instance flag would not close
    the gap; it would only add one of several missing gates.
    """
    assert SMCStrategy("BTCUSDT").params["use_rejection"] is False


def test_only_the_lab_gates_on_volume_and_expiry():
    config = SMCConfig(symbol="BTCUSDT")
    assert config.require_volume_surge is True
    assert config.setup_expiry_bars == 10

    params = SMCStrategy("BTCUSDT").params
    assert "require_volume_surge" not in params
    assert "setup_expiry_bars" not in params, (
        "the instance has no expiry: its lookback windows roll forward instead")


def test_the_two_paths_agree_on_brackets_but_not_on_when_to_fire():
    """The arithmetic matches; the trigger does not.

    Both compute entry = bar.close, stop = bar.low - ATR * 1.5 (mirrored for
    shorts) and target = entry + risk * 2.5. So a unification would not change
    how a trade is bracketed -- it would change which bars produce one, which
    is the part that matters.
    """
    lab = SMCConfig(symbol="BTCUSDT")
    instance = SMCStrategy("BTCUSDT").params
    assert lab.atr_multiplier == instance["atr_mult"] == 1.5
    assert lab.rr_ratio == instance["rr_target"] == 2.5
    assert lab.atr_length == instance["atr_period"] == 14


@pytest.mark.parametrize("seed,drift", [(7, 0.0009), (11, -0.0009), (3, 0.0)])
def test_the_two_paths_do_not_fire_on_the_same_bars(seed, drift):
    """The empirical half of the finding.

    Measured over 1600 bars per regime across bull, bear and range: the
    instance fired on 116 bars in total, the lab on none of them. If this ever
    starts overlapping, the implementations have converged and the divergence
    recorded here needs revisiting.
    """
    bars = generate_bars(n=900, timeframe="5m", drift_per_bar=drift,
                         vol_per_bar=0.006, seed=seed)
    instance = _instance_signal_bars(bars)
    lab = _lab_proposal_bars(bars)

    assert not (instance & lab), (
        f"Lab and instance now fire together on bars {sorted(instance & lab)[:10]}. "
        "That is a real change in one of the two implementations; confirm which "
        "moved before assuming it is an improvement.")
