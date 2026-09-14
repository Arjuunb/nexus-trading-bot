"""The SMC Trading Instance strategy must be the lab's engine, not a copy.

Until the unification there were two unrelated SMC implementations: a
self-contained confluence model in ``strategies/smc_strategy.py`` and the
sequential state machine in ``services/native_smc.py`` that the SMC Strategy
Lab runs. Over 1600 candles in each of four regimes they never once agreed --
the instance fired on 116 bars, the lab on none of them.

The instance is now a thin adapter over the lab engine, mirroring
``price_action_rejection``. These tests hold that shape: that there is exactly
one SMC implementation, that the adapter never recomputes a level, and that
what it emits is the engine's own proposal carried through untouched.

They are deliberately not satisfied by silence. A comparison over market data
where neither path proposes anything would pass while proving nothing, so the
decisive tests inject a proposal into the engine and assert the adapter
reproduces it exactly.
"""
from __future__ import annotations

import inspect

import pytest

from bot.data.resample import resample
from bot.data.synthetic import generate_bars
from bot.types import SignalType
from services.mtf_policy import native_timeframes
from services.native_smc import (
    NATIVE_SMC_ID,
    ProposedTrade,
    SMCConfig,
    SMCMarketStructureEngine,
)
from strategies.smc_strategy import SMCStrategy

_TFS = native_timeframes("5m")


def _drive(strategy: SMCStrategy, bars) -> list:
    """Feed the adapter the way AutoStrategyEngine does."""
    emitted = []
    for index, bar in enumerate(bars):
        window = bars[: index + 1]
        context = {tf: resample(window, tf) for tf in _TFS}
        context["5m"] = window
        strategy.set_timeframe_context(context)
        signal = strategy.on_bar(bar)
        if signal is not None:
            emitted.append(signal)
    return emitted


def _proposal(**overrides) -> ProposedTrade:
    fields = dict(id="prop-1", setup_id="setup-1", direction="bullish",
                  entry=123.45, stop=118.20, target=136.50, risk_distance=5.25,
                  rr_ratio=2.5, snapshot_id="snap-1", execution_allowed=True)
    fields.update(overrides)
    return ProposedTrade(**fields)


@pytest.fixture()
def primed():
    """An adapter warmed on real candles, with its engine reachable."""
    bars = generate_bars(n=300, timeframe="5m", seed=3)
    strategy = SMCStrategy("BTCUSDT")
    _drive(strategy, bars[:-1])
    assert strategy._engine is not None, "the adapter must build its engine"
    return strategy, bars


# ------------------------------------------------------------- one engine

def test_there_is_one_smc_implementation():
    source = inspect.getsource(inspect.getmodule(SMCStrategy))
    assert "native_smc" in source, (
        "the instance strategy must consume the lab engine, not reimplement it")


def test_the_adapter_does_not_reimplement_market_structure():
    """The confluence model's internals must not come back."""
    source = inspect.getsource(inspect.getmodule(SMCStrategy))
    for gone in ("_update_pivots", "_update_structure", "_update_sweep",
                 "_update_fvg", "recent_sweep_low", "recent_bull_fvg"):
        assert gone not in source, (
            f"{gone} is a second implementation of something the engine owns")


def test_the_adapter_computes_no_levels_of_its_own():
    """No ATR, no arithmetic on price: the engine's numbers or nothing."""
    source = inspect.getsource(inspect.getmodule(SMCStrategy))
    assert "from bot.data.indicators import atr" not in source
    assert "atr(" not in source, (
        "bracketing here would discard the stop and target the engine derived")


# --------------------------------------------------- proposal pass-through

def test_an_engine_proposal_reaches_the_signal_untouched(primed):
    strategy, bars = primed
    proposal = _proposal()
    strategy._engine.proposals[proposal.id] = proposal

    signal = strategy.generate(bars[-1])

    assert signal is not None, "an executable engine proposal must produce a signal"
    assert signal.entry == proposal.entry
    assert signal.stop_loss == proposal.stop
    assert signal.take_profit == proposal.target
    assert signal.type is SignalType.LONG


def test_a_bearish_proposal_maps_to_a_short_without_mirroring_levels(primed):
    strategy, bars = primed
    proposal = _proposal(id="prop-2", direction="bearish",
                         entry=100.0, stop=105.0, target=87.5)
    strategy._engine.proposals[proposal.id] = proposal

    signal = strategy.generate(bars[-1])

    assert signal is not None
    assert signal.type is SignalType.SHORT
    assert (signal.entry, signal.stop_loss, signal.take_profit) == (100.0, 105.0, 87.5)


def test_the_signal_carries_the_engine_provenance(primed):
    strategy, bars = primed
    proposal = _proposal()
    strategy._engine.proposals[proposal.id] = proposal

    snapshot = strategy.generate(bars[-1]).snapshot

    assert snapshot["research_id"] == NATIVE_SMC_ID
    assert snapshot["setup_id"] == proposal.setup_id
    assert snapshot["proposal_id"] == proposal.id
    assert snapshot["snapshot_id"] == proposal.snapshot_id
    assert snapshot["rr_ratio"] == proposal.rr_ratio


# ----------------------------------------------------------------- gating

def test_a_research_only_proposal_is_refused_with_a_reason(primed):
    strategy, bars = primed
    proposal = _proposal(id="prop-3", execution_allowed=False)
    strategy._engine.proposals[proposal.id] = proposal

    assert strategy.generate(bars[-1]) is None
    assert "research-only" in strategy.last_reason


def test_a_proposal_is_emitted_once(primed):
    strategy, bars = primed
    proposal = _proposal(id="prop-4")
    strategy._engine.proposals[proposal.id] = proposal

    assert strategy.generate(bars[-1]) is not None
    assert strategy.generate(bars[-1]) is None, (
        "re-emitting one proposal would duplicate the order it produces")


def test_contradictory_proposals_on_one_candle_are_refused(primed):
    strategy, bars = primed
    strategy._engine.proposals["a"] = _proposal(id="a", direction="bullish")
    strategy._engine.proposals["b"] = _proposal(id="b", direction="bearish")

    assert strategy.generate(bars[-1]) is None
    assert "contradictory" in strategy.last_reason


def test_history_proposals_never_become_orders():
    """Catching up on history is research, not a trade."""
    bars = generate_bars(n=300, timeframe="5m", seed=5)
    strategy = SMCStrategy("BTCUSDT")
    _drive(strategy, bars[:-1])
    strategy._history_proposals.add("historic")
    strategy._engine.proposals["historic"] = _proposal(id="historic")

    assert strategy.generate(bars[-1]) is None


# ------------------------------------------------------------- no drift

@pytest.mark.parametrize("seed,drift", [(7, 0.0009), (11, -0.0009), (3, 0.0)])
def test_the_adapter_never_emits_what_the_engine_did_not_propose(seed, drift):
    """Whatever the adapter emits must trace to an identical lab proposal."""
    bars = generate_bars(n=700, timeframe="5m", drift_per_bar=drift,
                         vol_per_bar=0.006, seed=seed)

    lab = SMCMarketStructureEngine(SMCConfig(symbol="BTCUSDT"))
    lab_proposals: dict[str, tuple] = {}
    for index, bar in enumerate(bars):
        window = bars[: index + 1]
        lab.set_native_mtf_context({tf: resample(window, tf) for tf in _TFS})
        lab.process_closed_bar(bar)
        for pid, row in lab.proposals.items():
            lab_proposals.setdefault(pid, (row.entry, row.stop, row.target, row.direction))

    for signal in _drive(SMCStrategy("BTCUSDT"), bars):
        pid = (signal.snapshot or {}).get("proposal_id")
        assert pid in lab_proposals, (
            f"the adapter emitted proposal {pid}, which the lab engine never made")
        entry, stop, target, direction = lab_proposals[pid]
        assert (signal.entry, signal.stop_loss, signal.take_profit) == (entry, stop, target)
        assert signal.type is (SignalType.LONG if direction == "bullish" else SignalType.SHORT)


def test_the_waiting_reason_names_the_stage_rather_than_guessing():
    """'Active but no orders' must be answerable from the strategy itself."""
    bars = generate_bars(n=400, timeframe="5m", seed=9)
    strategy = SMCStrategy("BTCUSDT")
    _drive(strategy, bars)
    assert strategy.last_reason, "the adapter must always say why it did not trade"
    assert strategy.last_reason != "Awaiting multi-timeframe context", (
        "after a full run the reason must reflect the engine's own state")


# ------------------------------------------- higher-timeframe context routing

def test_a_context_less_caller_still_lets_the_engine_form_setups():
    """Driving the adapter bar by bar must not starve the engine.

    The engine's ``set_native_mtf_context`` latches ``_native_mtf_enabled`` on
    for good, retiring the internal higher-timeframe bucket it falls back to.
    Calling it with an empty context is therefore worse than not calling it:
    the engine is left with no higher-timeframe series at all, its bias is 0 on
    every candle and no setup can ever open. services/replay.py drives
    strategies exactly this way, and this is what it used to get -- zero setups
    over 2400 candles, which read on the chart as "SMC never finds anything".
    """
    bars = generate_bars(n=1200, timeframe="15m", seed=1)
    strategy = SMCStrategy("BTCUSDT")
    for bar in bars:
        strategy.on_bar(bar)

    engine = strategy._engine
    assert engine._native_mtf_enabled is False, (
        "a caller that never supplied a context must not be switched to the "
        "native path, which would leave the engine with no HTF candles at all")
    assert engine.htf_closed, "the engine's own HTF bucket must have filled"
    assert engine._htf_bias() != 0, "with HTF candles the bias must resolve"
    assert engine.setups, (
        "the engine formed no setup at all -- it is being starved of "
        "higher-timeframe context, not declining to trade")


def test_a_caller_with_a_hub_never_drops_to_the_internal_bucket():
    """A live instance must not aggregate HTF candles from its entry stream.

    HubStrategy.set_native_mtf_context is explicit that a strategy "must never
    manufacture replacement HTF candles from their entry stream". The internal
    bucket does precisely that, so supplying a context -- even one whose higher
    timeframes have not filled yet -- must pin the engine to the native path
    and keep it there.
    """
    bars = generate_bars(n=300, timeframe="5m", seed=3)
    strategy = SMCStrategy("BTCUSDT")
    strategy.set_timeframe_context({"5m": bars[:1], _TFS[0]: [], _TFS[1]: []})
    strategy.on_bar(bars[0])

    engine = strategy._engine
    assert engine._native_mtf_enabled is True, (
        "an empty HTF series is a hub that has not filled yet, not a caller "
        "without one; falling back here would resample HTF from entry candles")
    assert not engine.htf_closed, "the internal bucket must stay unused"
