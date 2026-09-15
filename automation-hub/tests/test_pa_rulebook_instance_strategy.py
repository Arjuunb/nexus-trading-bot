"""The rulebook engine as a Trading Instance strategy.

tests/test_pa_rulebook_engine.py proves the engine reaches a trade. This proves
the adapter does not lose it on the way out, and -- the part that actually goes
wrong in adapters -- does not quietly substitute its own levels for the ones the
engine computed. The adapter's only job is to be transparent.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from bot.types import Bar, SignalType
from services import strategy_registry
from services.pa_rulebook_v01 import (
    FLIP_RETEST_ID, RULEBOOK_VERSION, SR_REJECTION_ID, RulebookConfig,
)
from services.strategy_factory import make_builtin_strategy
from strategies.pa_rulebook_strategy import (
    CONTEXT_TF,
    SETUP_TF,
    WARMUP_CANDLES,
    PriceActionRulebookStrategy,
)
from tests.test_pa_rulebook_engine import (
    ATR15,
    _confirm_series,
    _confirmation_for,
    _context_bars,
    _flat,
    _rejection_for,
)

CONFIRM_TF = "5m"


@pytest.fixture
def config():
    return RulebookConfig(symbol="BTCUSDT", tick_size=0.1, step_size=0.001)


@pytest.fixture
def series(config):
    """The three causal frames a rulebook decision needs, as the runtime feeds them."""
    from services.pa_rulebook_v01 import build_zones

    context = _context_bars()
    support = [z for z in build_zones(context, config) if z.kind == "support"][-1]
    start = support.created_at + timedelta(hours=2)
    setup_bars = _flat(start, 40, support.upper + 400.0, ATR15, minutes=15)
    rejection = _rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15))
    filler, window = _confirm_series(rejection)
    confirmation = _confirmation_for(rejection, window)
    return {"context": context, "support": support, "setup_bars": setup_bars,
            "rejection": rejection, "filler": filler, "confirmation": confirmation}


def _feed(strategy, *, context, setup_bars, confirm_bars):
    strategy.set_timeframe_context({CONTEXT_TF: context, SETUP_TF: setup_bars,
                                    CONFIRM_TF: confirm_bars})
    return strategy.on_bar(confirm_bars[-1])


def test_the_adapter_emits_the_engines_own_levels(series):
    """A signal whose stop came from ATR would be a different strategy.

    The rulebook's stop is structural -- ``min(rejection.low, zone.lower)``
    minus a buffer -- and its target is the lower bound of a pre-existing
    opposing zone. Both feed the net-RR gate that decided the trade was worth
    taking, so re-deriving either downstream would invalidate the decision that
    produced the signal in the first place.
    """
    strategy = PriceActionRulebookStrategy("BTCUSDT")

    quiet = _feed(strategy, context=series["context"], setup_bars=series["setup_bars"],
                  confirm_bars=series["filler"])
    assert quiet is None, strategy.last_reason

    setup_bars = series["setup_bars"] + [series["rejection"]]
    confirm_bars = series["filler"] + [series["confirmation"]]
    signal = _feed(strategy, context=series["context"], setup_bars=setup_bars,
                   confirm_bars=confirm_bars)

    assert signal is not None, strategy.last_reason
    assert signal.type is SignalType.LONG
    assert signal.symbol == "BTCUSDT"

    plan = strategy._engine.history[-1]
    assert signal.stop_loss == pytest.approx(
        min(float(series["rejection"].low), series["support"].lower) - 0.10 * ATR15,
        abs=0.1)
    assert signal.entry == pytest.approx(float(series["confirmation"].close), abs=0.1)
    assert signal.take_profit > signal.entry > signal.stop_loss
    assert plan.zone.id == series["support"].id


def test_the_signal_carries_the_evidence_back_to_the_zone(series):
    """A paper trade with no way back to the level that caused it is not
    research, it is an anecdote."""
    strategy = PriceActionRulebookStrategy("BTCUSDT")
    _feed(strategy, context=series["context"], setup_bars=series["setup_bars"],
          confirm_bars=series["filler"])
    signal = _feed(strategy, context=series["context"],
                   setup_bars=series["setup_bars"] + [series["rejection"]],
                   confirm_bars=series["filler"] + [series["confirmation"]])

    snapshot = signal.snapshot
    assert snapshot["research_id"] == SR_REJECTION_ID
    assert snapshot["zone_id"] == series["support"].id
    assert snapshot["regime"] == "BULL"
    assert snapshot["net_rr"] >= 2.5
    assert snapshot["zone_bounds"] == [series["support"].lower, series["support"].upper]
    assert snapshot["rejection_open_time"] == series["rejection"].timestamp.isoformat()
    assert snapshot["confirmation_open_time"] == series["confirmation"].timestamp.isoformat()
    assert snapshot["stop_distance_atr"] == pytest.approx(
        abs(signal.entry - signal.stop_loss) / ATR15, abs=1e-6)


def test_the_same_setup_is_never_emitted_twice(series):
    """The runtime may re-present a candle. One setup is one vote."""
    strategy = PriceActionRulebookStrategy("BTCUSDT")
    _feed(strategy, context=series["context"], setup_bars=series["setup_bars"],
          confirm_bars=series["filler"])
    setup_bars = series["setup_bars"] + [series["rejection"]]
    confirm_bars = series["filler"] + [series["confirmation"]]

    first = _feed(strategy, context=series["context"], setup_bars=setup_bars,
                  confirm_bars=confirm_bars)
    again = _feed(strategy, context=series["context"], setup_bars=setup_bars,
                  confirm_bars=confirm_bars)
    assert first is not None and again is None


def test_short_history_warms_up_instead_of_trading(series):
    """Chapter 18 requires 200 closed bars per timeframe before any decision.

    Below it the regime classifier returns UNKNOWN, so the failure mode without
    this guard is not a wrong trade -- it is a strategy that looks alive and
    silently never fires, which is much harder to notice.
    """
    strategy = PriceActionRulebookStrategy("BTCUSDT")
    signal = _feed(strategy, context=series["context"][:WARMUP_CANDLES - 1],
                   setup_bars=series["setup_bars"],
                   confirm_bars=series["filler"])
    assert signal is None
    assert "warming up" in strategy.last_reason


def test_one_strategy_runs_the_whole_rulebook_under_arbitration():
    """Both identities in one engine, which is what makes chapter 9 apply.

    Two catalog entries would put two engines on the same symbol with nothing
    arbitrating between them -- each would raise its own setup and each would
    think it held the one pending slot the rulebook allows per symbol.
    """
    strategy = PriceActionRulebookStrategy("BTCUSDT")
    assert strategy._engine.strategies == (SR_REJECTION_ID, FLIP_RETEST_ID)
    assert strategy.required_timeframes == (CONFIRM_TF, SETUP_TF, CONTEXT_TF)


@pytest.mark.parametrize("choice,expected", [
    ("rejection", (SR_REJECTION_ID,)),
    ("flip", (FLIP_RETEST_ID,)),
    ("both", (SR_REJECTION_ID, FLIP_RETEST_ID)),
])
def test_a_single_setup_can_still_be_measured_on_its_own(monkeypatch, choice, expected):
    """Chapter 9: "Initially run A and B in independent books to measure each
    without arbitration effects."

    That research mode survives the consolidation as a configuration of one
    strategy rather than as a second catalog entry.
    """
    monkeypatch.setenv("HUB_PA_RB_SETUPS", choice)
    assert PriceActionRulebookStrategy("BTCUSDT")._engine.strategies == expected


def test_an_unknown_setup_selection_fails_closed(monkeypatch):
    """A typo must not silently run something other than what was asked for."""
    monkeypatch.setenv("HUB_PA_RB_SETUPS", "rejektion")
    with pytest.raises(ValueError, match="HUB_PA_RB_SETUPS"):
        PriceActionRulebookStrategy("BTCUSDT")


def test_the_catalog_offers_it_for_a_new_instance_with_a_real_version():
    """PRODUCTION here is a claim about reproducibility, not profitability.

    builtin_versions is explicit that a pinned version "does *not* assert a
    profitable historical run". This engine is versioned, hash-attested and
    pure, so a paper record it makes can be reproduced exactly -- and the
    description states the edge is unproven, which is the separate claim.
    """
    strategy = make_builtin_strategy("pa_rulebook", "BTCUSDT")
    assert isinstance(strategy, PriceActionRulebookStrategy)

    entry = strategy_registry.entry("pa_rulebook")
    assert entry is not None and entry.lifecycle == strategy_registry.PRODUCTION
    assert entry.version != "unversioned"
    assert entry.version == RULEBOOK_VERSION, "a paper record must name the engine that made it"
    assert entry.supported_markets == (strategy_registry.FORWARD_PAPER_MARKET,)

    selectable, reason = strategy_registry.selectable_for_new_instance("pa_rulebook")
    assert selectable is True, reason
    assert entry.strategy_id in {e.strategy_id for e in strategy_registry.production_entries()}
    assert "no backtest" in entry.description.lower()


def test_the_split_entries_are_gone():
    """The consolidation has to remove them, not shadow them."""
    for retired in ("pa_rulebook_sr_rejection", "pa_rulebook_flip_retest"):
        assert strategy_registry.entry(retired) is None
        with pytest.raises(ValueError):
            make_builtin_strategy(retired, "BTCUSDT")
