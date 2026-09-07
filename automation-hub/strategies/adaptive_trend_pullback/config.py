"""Immutable configuration for the adaptive trend-pullback strategy."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class AdaptiveTrendPullbackConfig:
    version: str = "1.0.0"
    # For a 5m entry the shared MTF policy makes 1h the gate and 4h bias-only.
    regime_timeframe: str = "1h"
    trend_timeframe: str = "1h"
    pullback_timeframe: str = "15m"
    entry_timeframe: str = "5m"
    fast_ema: int = 20
    slow_ema: int = 50
    adx_period: int = 14
    adx_min: float = 20.0
    regime_confidence_min: float = 70.0
    trend_confidence_min: float = 70.0
    quality_minimum: float = 75.0
    minimum_rr: float = 2.0
    target_rr: float = 2.5
    atr_period: int = 14
    stop_atr_buffer: float = 0.25
    maximum_atr_pct: float = 0.04
    maximum_atr_expansion: float = 2.0
    maximum_extension_atr: float = 3.0
    ema_separation_min_atr: float = 0.10
    structure_lookback: int = 30
    pullback_lookback: int = 20
    confirmation_lookback: int = 8
    abnormal_volume_multiple: float = 2.5
    confirmation_volume_multiple: float = 1.15
    rejection_wick_body_ratio: float = 1.5
    corrective_move_atr: float = 0.5
    pullback_zone_atr: float = 0.80
    target_method: str = "fixed_rr"
    minimum_bars: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({
        "4h": 70, "1h": 70, "15m": 70, "5m": 70,
    }))

    def __post_init__(self) -> None:
        if not 0 <= self.quality_minimum <= 100:
            raise ValueError("quality_minimum must be between 0 and 100")
        if self.minimum_rr < 2.0 or self.target_rr < self.minimum_rr:
            raise ValueError("target_rr must be at least minimum_rr, and minimum_rr must be >= 2")
        if self.fast_ema >= self.slow_ema:
            raise ValueError("fast_ema must be less than slow_ema")
        if self.stop_atr_buffer < 0:
            raise ValueError("stop_atr_buffer must be non-negative")
        if self.target_method not in ("fixed_rr", "structure"):
            raise ValueError("target_method must be fixed_rr or structure")

    @classmethod
    def from_env(cls) -> "AdaptiveTrendPullbackConfig":
        """Production overrides with one namespaced, validated source."""
        def number(name: str, default, cast=float):
            raw = os.environ.get(f"HUB_ATP_{name}")
            return default if raw in (None, "") else cast(raw)
        return cls(
            fast_ema=number("FAST_EMA", cls.fast_ema, int),
            slow_ema=number("SLOW_EMA", cls.slow_ema, int),
            adx_period=number("ADX_PERIOD", cls.adx_period, int),
            adx_min=number("ADX_MIN", cls.adx_min),
            regime_confidence_min=number("REGIME_CONFIDENCE_MIN", cls.regime_confidence_min),
            trend_confidence_min=number("TREND_CONFIDENCE_MIN", cls.trend_confidence_min),
            quality_minimum=number("QUALITY_MINIMUM", cls.quality_minimum),
            minimum_rr=number("MINIMUM_RR", cls.minimum_rr),
            target_rr=number("TARGET_RR", cls.target_rr),
            atr_period=number("ATR_PERIOD", cls.atr_period, int),
            stop_atr_buffer=number("STOP_ATR_BUFFER", cls.stop_atr_buffer),
            maximum_atr_pct=number("MAXIMUM_ATR_PCT", cls.maximum_atr_pct),
            maximum_atr_expansion=number("MAXIMUM_ATR_EXPANSION", cls.maximum_atr_expansion),
            maximum_extension_atr=number("MAXIMUM_EXTENSION_ATR", cls.maximum_extension_atr),
            ema_separation_min_atr=number("EMA_SEPARATION_MIN_ATR", cls.ema_separation_min_atr),
            structure_lookback=number("STRUCTURE_LOOKBACK", cls.structure_lookback, int),
            pullback_lookback=number("PULLBACK_LOOKBACK", cls.pullback_lookback, int),
            confirmation_lookback=number("CONFIRMATION_LOOKBACK", cls.confirmation_lookback, int),
            abnormal_volume_multiple=number("ABNORMAL_VOLUME_MULTIPLE", cls.abnormal_volume_multiple),
            confirmation_volume_multiple=number("CONFIRMATION_VOLUME_MULTIPLE", cls.confirmation_volume_multiple),
            rejection_wick_body_ratio=number("REJECTION_WICK_BODY_RATIO", cls.rejection_wick_body_ratio),
            corrective_move_atr=number("CORRECTIVE_MOVE_ATR", cls.corrective_move_atr),
            pullback_zone_atr=number("PULLBACK_ZONE_ATR", cls.pullback_zone_atr),
            target_method=os.environ.get("HUB_ATP_TARGET_METHOD", cls.target_method).strip().lower(),
        )
