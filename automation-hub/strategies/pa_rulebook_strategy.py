"""The Nexus Price Action rulebook v0.1, as a Trading Instance strategy.

A thin adapter over ``services/pa_rulebook_v01.py``. Every level this emits is
a number that pure engine computed: the adapter feeds it closed candles on the
three timeframes the specification names and converts an accepted plan into a
Signal. It contains no entry condition, no threshold and no sizing of its own,
because the rulebook's whole premise is that "the same pure strategy engine"
runs in backtest and forward execution -- an adapter that made its own
decisions would mean the thing being researched is not the thing being run.

Two catalog entries, one per strategy identity, each running the engine with
only its own setup enabled. That is the document's instruction rather than a
packaging convenience: "Initially run A and B in independent books to measure
each without arbitration effects. A combined-book study is a separate
experiment with an explicit shared-risk policy." Two instances therefore never
arbitrate against each other, and their statistics stay separable.

Research only, and it stays that way until there is evidence. The rulebook's
own status line is "a research hypothesis, not a proven edge. Every threshold
is an initial engineering choice. No backtest or forward experiment supports
it", and it requires that "The design must not route exchange orders". The
registry entries are RESEARCH_ONLY and the supported market is forward paper.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from bot.types import Bar, Signal, SignalType
from services.pa_rulebook_v01 import (
    CONFIRM_TF,
    CONTEXT_TF,
    FLIP_RETEST_ID,
    RULEBOOK_VERSION,
    SETUP_TF,
    SR_REJECTION_ID,
    CostModel,
    Decision,
    PriceActionRulebookEngine,
    RulebookConfig,
)
from strategies.base_strategy import HubStrategy

DECISION_TIMEFRAME = CONFIRM_TF          # 5m: the confirmation clock

#: Chapter 18: "warmup / ATR -- 200 closed bars per timeframe / Wilder 14".
#: Declared per timeframe because the runtime sizes each independent fetch from
#: this mapping; the engine's regime classifier refuses to leave UNKNOWN below
#: it, so a smaller history produces a strategy that runs and never trades.
WARMUP_CANDLES = 200
MINIMUM_BARS = {CONTEXT_TF: WARMUP_CANDLES, SETUP_TF: WARMUP_CANDLES,
                CONFIRM_TF: WARMUP_CANDLES}


@dataclass(frozen=True)
class RulebookInstanceConfig:
    """What the instance runtime needs, kept out of the pure engine.

    ``services/pa_rulebook_v01.py`` deliberately knows nothing about fetch
    limits or account equity. This carries them alongside the pure config so
    the runtime can read ``config.minimum_bars`` without the strategy module
    growing a field that has no meaning in a backtest.
    """
    rulebook: RulebookConfig
    minimum_bars: Mapping[str, int]
    costs: CostModel


def _float_env(name: str, fallback: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw.strip()) if raw.strip() else fallback
    except ValueError:
        return fallback


def _instance_config(symbol: str) -> RulebookInstanceConfig:
    """Read HUB_PA_RB_* overrides, defaulting to the document's own values.

    Chapter 18 requires that omitted defaults be persisted explicitly "so a
    software upgrade cannot change behaviour invisibly", which is why every
    value here resolves to a named RulebookConfig field rather than a literal.
    The fee rates are configuration with evidence attached, never an assumed
    universal exchange fee -- the rulebook's 5bps is an illustration and says so.
    """
    rulebook = RulebookConfig(
        symbol=symbol,
        tick_size=_float_env("HUB_PA_RB_TICK_SIZE", RulebookConfig.tick_size),
        step_size=_float_env("HUB_PA_RB_STEP_SIZE", RulebookConfig.step_size),
    )
    rulebook.validate()
    costs = CostModel(
        entry_fee_rate=_float_env("HUB_PA_RB_ENTRY_FEE", CostModel.entry_fee_rate),
        exit_fee_rate=_float_env("HUB_PA_RB_EXIT_FEE", CostModel.exit_fee_rate),
        per_unit_allowance=_float_env("HUB_PA_RB_ALLOWANCE", CostModel.per_unit_allowance),
    )
    return RulebookInstanceConfig(
        rulebook=rulebook, minimum_bars=dict(MINIMUM_BARS), costs=costs)


class PriceActionRulebookStrategy(HubStrategy):
    """Base adapter. Subclasses bind one rulebook strategy identity."""

    rulebook_strategy_id = SR_REJECTION_ID
    strategy_version = RULEBOOK_VERSION
    decision_timeframe = DECISION_TIMEFRAME
    # All three are mandatory. Unlike the indicator strategies there is no
    # bias-only frame here: chapter 3 makes 1H the regime authority and 15M the
    # setup clock, and the engine cannot raise a setup without either.
    required_timeframes = (CONFIRM_TF, SETUP_TF, CONTEXT_TF)
    warmup_required = WARMUP_CANDLES

    def __init__(self, symbol: str, *,
                 config: RulebookInstanceConfig | None = None, **params):
        super().__init__(symbol, **params)
        self.config = config or _instance_config(symbol)
        # Deliberately not on ``config``: that namespace is the engine's
        # parameter contract, and the registry's warm-up audit reads every bare
        # number in it as a candle lookback. This is an account amount, and it
        # only scales the evidence quantity the engine reports -- the instance's
        # own risk engine sizes the order. The net-RR gate that decides the
        # trade does not depend on it at all.
        self.sizing_equity = _float_env("HUB_PA_RB_EQUITY", 10_000.0)
        self._engine = PriceActionRulebookEngine(
            self.config.rulebook, self.config.costs,
            strategies=(self.rulebook_strategy_id,))
        self._context: dict[str, list[Bar]] = {}
        self._last_context_close: Optional[object] = None
        self._last_setup_close: Optional[object] = None
        self._emitted: set = set()
        self.last_reason = "Awaiting multi-timeframe context"

    # ---------------------------------------------------------------- context

    def set_timeframe_context(self, context: Mapping[str, Sequence[Bar]]) -> None:
        """Take the runtime's causal candles; never resample here.

        Chapter 2 forbids the shortcut this would otherwise invite: a 1H series
        rebuilt from the 5M stream is not an independently validated series,
        and the rulebook requires the forward path accept "only validated
        real-provider data". Each frame arrives already trimmed at the decision
        boundary, so these are the only candles this decision may see.
        """
        self._context = {timeframe: list(rows) for timeframe, rows in context.items()}
        self.bars = list(self._context.get(self.decision_timeframe, ()))

    # ----------------------------------------------------------------- decide

    def on_bar(self, bar: Bar) -> Optional[Signal]:
        """Drive the engine in chapter 17's order: context, setup, confirm."""
        context = self._context.get(CONTEXT_TF, ())
        setup_bars = self._context.get(SETUP_TF, ())
        confirm_bars = self._context.get(CONFIRM_TF, ())
        if len(context) < WARMUP_CANDLES or len(setup_bars) < 2 or not confirm_bars:
            self.last_reason = (
                f"warming up: {len(context)}/{WARMUP_CANDLES} {CONTEXT_TF}, "
                f"{len(setup_bars)} {SETUP_TF}, {len(confirm_bars)} {CONFIRM_TF}")
            return None

        # A 1H or 15M candle only closes every 12th or 3rd decision candle. The
        # engine must see each closed candle exactly once: replaying one would
        # re-arm a retest window or re-spend a confirmation slot.
        if context[-1].timestamp != self._last_context_close:
            self._last_context_close = context[-1].timestamp
            self._engine.update_context(context)
        if setup_bars[-1].timestamp != self._last_setup_close:
            self._last_setup_close = setup_bars[-1].timestamp
            self._engine.on_setup_close(setup_bars)

        decision = self._engine.on_confirm_close(
            confirm_bars, equity=self.sizing_equity,
            # Freshness and provenance are proven upstream: the runtime's
            # forward fetcher refuses any non-live source and the market
            # snapshot rejects a stale candle before on_bar is reached. Passing
            # a blocker here would double-count a gate that already ran.
            entry_blocked=None)
        return self.generate(bar, decision=decision)

    def generate(self, bar: Bar, *, decision: Decision | None = None) -> Optional[Signal]:
        if decision is None:
            self.last_reason = "no closed-candle decision for this bar"
            return None
        plan = decision.plan
        if plan is None or not plan.accepted:
            state = decision.state.value if decision.state else "WATCHING"
            blocker = decision.blocker.value if decision.blocker else "no candidate"
            self.last_reason = f"{state}: {blocker} (regime {decision.regime.value})"
            return None
        if decision.setup.id in self._emitted:
            self.last_reason = "plan already emitted for this setup"
            return None
        self._emitted.add(decision.setup.id)
        return self._as_signal(bar, decision)

    def _as_signal(self, bar: Bar, decision: Decision) -> Signal:
        """Carry the engine's own numbers through unchanged.

        Not self._bracket(): that re-derives a stop from ATR, which would throw
        away the structural stop, the target taken from a pre-existing opposing
        zone and the net-RR gate those two feed. Those levels are the strategy.
        """
        plan, setup = decision.plan, decision.setup
        direction = SignalType.LONG if plan.direction == "long" else SignalType.SHORT
        self.last_reason = (
            f"{plan.strategy_id} {plan.direction} at {plan.entry_bound:.8f}, "
            f"stop {plan.stop:.8f}, target {plan.target:.8f} "
            f"({plan.net_rr:.2f}R net of costs)")
        signal = Signal(
            timestamp=bar.timestamp, symbol=self.symbol, type=direction,
            entry=plan.entry_bound, stop_loss=plan.stop,
            take_profit=plan.target, reason=self.last_reason,
        )
        # Provenance for the trade journal. Without it a paper trade lands with
        # no way back to the zone and the candles that caused it -- which is
        # the one thing worth knowing when deciding whether to trust the setup.
        signal.snapshot = {
            "mtf_evidence": dict(self._native_mtf_evidence),
            "research_id": plan.strategy_id,
            "rulebook_version": RULEBOOK_VERSION,
            "setup_id": setup.id,
            "zone_id": setup.zone.id,
            "zone_bounds": [setup.zone.lower, setup.zone.upper],
            "regime": decision.regime.value,
            "setup_atr15": setup.setup_atr,
            "net_rr": plan.net_rr,
            "costs_loss": plan.costs_loss,
            "costs_win": plan.costs_win,
            "stop_distance_atr": plan.stop_distance_atr,
            "planned_quantity": plan.quantity,
            "planned_loss": plan.planned_loss,
            "rejection_open_time": setup.rejection.timestamp.isoformat(),
            "confirmation_open_time": setup.confirmation.timestamp.isoformat(),
        }
        return signal


class PriceActionRulebookRejectionStrategy(PriceActionRulebookStrategy):
    """Setup A: a trend-aligned pullback rejects an existing zone."""

    name = "pa_rulebook_sr_rejection"
    label = "PA Rulebook S/R Rejection (v0.1 research)"
    rulebook_strategy_id = SR_REJECTION_ID


class PriceActionRulebookFlipRetestStrategy(PriceActionRulebookStrategy):
    """Setup B: a trend-aligned breakout is followed by a retest of the flip."""

    name = "pa_rulebook_flip_retest"
    label = "PA Rulebook Flip Retest (v0.1 research)"
    rulebook_strategy_id = FLIP_RETEST_ID
