"""Canonical built-in strategy construction for every execution entry point."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def make_builtin_strategy(key: str, symbol: str, *,
                          adaptive: Callable[[str], Any] | None = None):
    """Construct exactly the requested strategy; unknown keys fail closed."""
    if key == "adaptive":
        if adaptive is None:
            raise ValueError("adaptive strategy requires an allocator factory")
        return adaptive(symbol)
    if key == "ema":
        from strategies.ema_strategy import EMAStrategy
        return EMAStrategy(symbol)
    if key == "supertrend":
        from strategies.supertrend_strategy import SupertrendStrategy
        return SupertrendStrategy(symbol)
    if key == "donchian":
        from strategies.donchian_strategy import DonchianStrategy
        return DonchianStrategy(symbol)
    if key == "ensemble":
        from strategies.ensemble_strategy import ConfirmationEnsemble
        return ConfirmationEnsemble(symbol)
    if key == "smc":
        from strategies.smc_strategy import SMCStrategy
        return SMCStrategy(symbol)
    if key == "liquidity_sweep":
        from strategies.liquidity_sweep_strategy import LiquiditySweepStrategy
        return LiquiditySweepStrategy(symbol)
    if key == "adaptive_trend_pullback":
        from strategies.adaptive_trend_pullback import (
            AdaptiveTrendPullbackConfig,
            AdaptiveTrendPullbackStrategy,
        )
        return AdaptiveTrendPullbackStrategy(
            symbol, config=AdaptiveTrendPullbackConfig.from_env())
    if key == "brain":
        from strategies.brain_strategy import DecisionBrain
        return DecisionBrain(symbol)
    if key == "price_action_rejection":
        from strategies.price_action_rejection import PriceActionRejectionStrategy
        return PriceActionRejectionStrategy(symbol)
    if key == "price_action_flip_retest":
        from strategies.price_action_rejection import PriceActionFlipRetestStrategy
        return PriceActionFlipRetestStrategy(symbol)
    if key == "pa_rulebook":
        from strategies.pa_rulebook_strategy import PriceActionRulebookStrategy
        return PriceActionRulebookStrategy(symbol)
    raise ValueError(f"unknown built-in strategy '{key}'")
