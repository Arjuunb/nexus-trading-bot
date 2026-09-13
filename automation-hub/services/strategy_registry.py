"""Authoritative registry of the strategies a Trading Instance may run.

The dashboard used to receive whatever ``webhook_api._STRATEGY_CATALOG``
happened to list, and that list was assembled by hand: it omitted Donchian's
pinned version (so the selector offered ``unversioned`` for a strategy that
has a reproducible one), and it made no distinction between a strategy with an
immutable version plus dedicated tests and one that has neither.  A trading
platform cannot attribute a paper record to "EMA Crossover" alone, so exposing
an unversioned strategy in the creation screen promises reproducibility the
repository cannot deliver.

This module is that single source of truth.  Every entry declares what it
supports and what evidence backs it, and ``lifecycle`` decides whether it may
be selected for a NEW Trading Instance:

``PRODUCTION``
    Implemented, signal-generating, immutably versioned, dedicated tests, and
    proven to run end-to-end as a forward-paper instance.  Offered in the UI.
``RESEARCH_ONLY``
    Implemented and working, but missing the reproducibility or coverage
    evidence a production selection requires.  Kept importable for research,
    backtests and existing instances; never offered for a new instance.
``DEPRECATED``
    Superseded.  Existing instances keep running; no new selections.
``DISABLED``
    Must not run at all.

Nothing here changes strategy logic.  Demotion is a *packaging* decision and
is reversed by supplying the missing evidence — a pinned entry in
``strategies.builtin_versions`` and a dedicated test module — not by editing
alpha.  Instances created before a demotion are deliberately grandfathered:
``selectable_for_new_instance`` gates creation only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from services.mtf_policy import ENTRY_HTF

PRODUCTION = "PRODUCTION"
RESEARCH_ONLY = "RESEARCH_ONLY"
DEPRECATED = "DEPRECATED"
DISABLED = "DISABLED"

LIFECYCLES = (PRODUCTION, RESEARCH_ONLY, DEPRECATED, DISABLED)

#: Every entry timeframe the native MTF policy can serve.
ALL_ENTRY_TIMEFRAMES: tuple[str, ...] = tuple(ENTRY_HTF)

#: Trading Instances are forward-paper on Binance USD-M perpetuals only.
FORWARD_PAPER_MARKET = "binance_usdm:perpetual"


@dataclass(frozen=True)
class StrategyEntry:
    strategy_id: str
    display_name: str
    lifecycle: str
    description: str
    supported_markets: tuple[str, ...]
    supported_timeframes: tuple[str, ...]
    required_data: tuple[str, ...]
    #: The entry-timeframe closed candles this strategy needs before its first
    #: decision can be trusted, derived from its own longest lookback. Declared
    #: rather than left to the engine's generic default so "READY" means a
    #: stated requirement was met, not that a shared constant happened to be
    #: large enough. The engine warms up to at least this many.
    warmup_candles: int = 150
    #: The longest indicator lookback behind that number, so the figure can be
    #: checked against the strategy rather than taken on trust.
    warmup_basis: str = "engine default"
    #: Why this entry is not PRODUCTION.  Empty for production strategies.
    lifecycle_reason: str = ""
    #: Test modules that specifically exercise this strategy.
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def version(self) -> str:
        from strategies.builtin_versions import builtin_strategy_version
        from services.native_price_action import STRATEGY_VERSION as PA_VERSION
        if self.strategy_id.startswith("price_action_"):
            return PA_VERSION
        return builtin_strategy_version(self.strategy_id)

    def public(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "key": self.strategy_id,              # legacy field name
            "label": self.display_name,
            "display_name": self.display_name,
            "status": self.lifecycle,
            "lifecycle": self.lifecycle,
            "lifecycle_reason": self.lifecycle_reason,
            "desc": self.description,
            "version": self.version,
            "supported_markets": list(self.supported_markets),
            "supported_timeframes": list(self.supported_timeframes),
            "required_data": list(self.required_data),
            "warmup_candles": self.warmup_candles,
            "warmup_basis": self.warmup_basis,
            "evidence": list(self.evidence),
        }


_ENTRIES: tuple[StrategyEntry, ...] = (
    StrategyEntry(
        strategy_id="brain", display_name="Decision Brain", lifecycle=PRODUCTION,
        description=("Multi-factor trend: EMA trend + filter, momentum, RSI, regime; "
                     "conviction-weighted sizing"),
        supported_markets=(FORWARD_PAPER_MARKET,),
        supported_timeframes=ALL_ENTRY_TIMEFRAMES,
        required_data=("entry_candles", "native_primary_htf", "native_secondary_htf"),
        warmup_candles=150, warmup_basis="trend EMA 50, slow EMA 26, RSI 14, ATR 14",
        evidence=("tests/test_builtin_strategy_versions.py", "tests/test_brain_mtf.py",
                  "tests/test_brain_profitability.py"),
    ),
    StrategyEntry(
        strategy_id="supertrend", display_name="Supertrend", lifecycle=PRODUCTION,
        description="ATR trend-following indicator",
        supported_markets=(FORWARD_PAPER_MARKET,),
        supported_timeframes=ALL_ENTRY_TIMEFRAMES,
        required_data=("entry_candles",),
        warmup_candles=150, warmup_basis="Supertrend period 10, ATR 14",
        evidence=("tests/test_builtin_strategy_versions.py",),
    ),
    StrategyEntry(
        strategy_id="donchian", display_name="Donchian Breakout", lifecycle=PRODUCTION,
        description="Classic Turtle channel breakout",
        supported_markets=(FORWARD_PAPER_MARKET,),
        supported_timeframes=ALL_ENTRY_TIMEFRAMES,
        required_data=("entry_candles",),
        warmup_candles=150, warmup_basis="Donchian channel 30, ATR 14",
        evidence=("tests/test_builtin_strategy_versions.py",),
    ),
    StrategyEntry(
        strategy_id="adaptive_trend_pullback", display_name="Adaptive MTF Trend Pullback",
        lifecycle=PRODUCTION,
        description="5m entry + native 1h regime gate + native 4h bias + 15m pullback context",
        supported_markets=(FORWARD_PAPER_MARKET,),
        supported_timeframes=("5m",),
        required_data=("entry_candles", "native_primary_htf", "native_secondary_htf"),
        warmup_candles=150,
        warmup_basis="slow EMA 50, structure lookback 30, ADX 14",
        evidence=("tests/test_adaptive_trend_pullback.py",
                  "tests/test_builtin_strategy_versions.py"),
    ),
    StrategyEntry(
        strategy_id="price_action_rejection", display_name="Price Action S/R Rejection",
        lifecycle=PRODUCTION,
        description=("Price Action lab engine: confirmed S/R zone + closed-candle rejection, "
                     "rejection-extreme stop, native 1h gate and 4h bias"),
        supported_markets=(FORWARD_PAPER_MARKET,),
        supported_timeframes=("5m",),
        required_data=("entry_candles", "native_primary_htf", "native_secondary_htf"),
        warmup_candles=400,
        warmup_basis="S/R zone confirmation window; declared by the engine itself",
        evidence=("tests/test_price_action_instance_strategy.py",
                  "tests/test_native_price_action.py"),
    ),
    StrategyEntry(
        strategy_id="price_action_flip_retest", display_name="Price Action Flip Retest",
        lifecycle=PRODUCTION,
        description=("Price Action lab engine: flipped zone retested after a break, "
                     "rejection-extreme stop, native 1h gate and 4h bias"),
        supported_markets=(FORWARD_PAPER_MARKET,),
        supported_timeframes=("5m",),
        required_data=("entry_candles", "native_primary_htf", "native_secondary_htf"),
        warmup_candles=400,
        warmup_basis="flip zone history; declared by the engine itself",
        evidence=("tests/test_price_action_instance_strategy.py",
                  "tests/test_price_action_continuation.py"),
    ),
    # ---------------------------------------------------------------- research
    # These four are implemented and do produce signals (measured: SMC 55,
    # Liquidity Sweep 72, EMA 55, Ensemble 30 over the bundled 1h BTCUSDT
    # series with native 4h context).  What they lack is the reproducibility
    # evidence a production selection requires, so they stay available to
    # research, backtests and already-created instances but are not offered
    # when creating a new one.
    StrategyEntry(
        strategy_id="smc", display_name="Supply/Demand", lifecycle=RESEARCH_ONLY,
        description=("SMC supply/demand zones: liquidity sweep + CHoCH/BOS + FVG with "
                     "higher-timeframe bias"),
        supported_markets=(FORWARD_PAPER_MARKET,),
        supported_timeframes=ALL_ENTRY_TIMEFRAMES,
        required_data=("entry_candles", "native_primary_htf"),
        warmup_candles=150, warmup_basis="internal warmup 120, pivot/sweep lookbacks",
        lifecycle_reason=("No immutable version in strategies.builtin_versions, so a paper "
                          "record cannot be attributed to a reproducible build. The "
                          "supported SMC research path is the SMC Strategy Lab."),
        evidence=("tests/test_smc_strategy.py",),
    ),
    StrategyEntry(
        strategy_id="liquidity_sweep", display_name="Liquidity Sweep", lifecycle=RESEARCH_ONLY,
        description="Stop-hunt wick beyond a prior range, candle reclaim, ATR-defined invalidation",
        supported_markets=(FORWARD_PAPER_MARKET,),
        supported_timeframes=ALL_ENTRY_TIMEFRAMES,
        required_data=("entry_candles",),
        warmup_candles=150, warmup_basis="sweep lookback 20, internal warmup 30, ATR 14",
        lifecycle_reason=("No immutable version in strategies.builtin_versions; add a pinned "
                          "entry with a signal fixture to promote."),
        evidence=("tests/test_liquidity_sweep_strategy.py",),
    ),
    StrategyEntry(
        strategy_id="ema", display_name="EMA Crossover", lifecycle=RESEARCH_ONLY,
        description="Simple fast/slow EMA cross",
        supported_markets=(FORWARD_PAPER_MARKET,),
        supported_timeframes=ALL_ENTRY_TIMEFRAMES,
        required_data=("entry_candles",),
        warmup_candles=150, warmup_basis="slow EMA 26, ATR 14",
        lifecycle_reason=("No immutable version and no test module exercises EMAStrategy "
                          "directly; it is a baseline, not a production alpha."),
    ),
    StrategyEntry(
        strategy_id="ensemble", display_name="Confirmation Ensemble", lifecycle=RESEARCH_ONLY,
        description="Trades only when 2 of 3 agree (EMA + Supertrend + Donchian)",
        supported_markets=(FORWARD_PAPER_MARKET,),
        supported_timeframes=ALL_ENTRY_TIMEFRAMES,
        required_data=("entry_candles",),
        warmup_candles=150,
        warmup_basis="widest member lookback: Donchian channel 30, slow EMA 26",
        lifecycle_reason=("No immutable version and only an incidental backtest reference; "
                          "one of its three members (EMA) is itself research-only."),
    ),
)

REGISTRY: Mapping[str, StrategyEntry] = MappingProxyType(
    {entry.strategy_id: entry for entry in _ENTRIES})


def entry(strategy_id: str) -> StrategyEntry | None:
    return REGISTRY.get(str(strategy_id or ""))


def all_entries() -> list[StrategyEntry]:
    return list(_ENTRIES)


def production_entries() -> list[StrategyEntry]:
    return [item for item in _ENTRIES if item.lifecycle == PRODUCTION]


def selectable_for_new_instance(strategy_id: str) -> tuple[bool, str]:
    """May a NEW Trading Instance be created with this strategy?

    Existing instances are never evaluated here — a demotion must not stop a
    worker that is already running and holding paper positions.
    """
    row = entry(strategy_id)
    if row is None:
        return False, f"Unknown strategy '{strategy_id}'"
    if row.lifecycle == PRODUCTION:
        return True, ""
    return False, (
        f"{row.display_name} is {row.lifecycle} and cannot be selected for a new "
        f"Trading Instance. {row.lifecycle_reason}".strip())


def catalog_rows() -> list[dict]:
    """Legacy ``_STRATEGY_CATALOG`` shape, derived rather than hand-maintained."""
    return [
        {"key": item.strategy_id, "label": item.display_name, "desc": item.description,
         "version": item.version, "supported_timeframes": list(item.supported_timeframes),
         "status": item.lifecycle}
        for item in _ENTRIES
    ]
