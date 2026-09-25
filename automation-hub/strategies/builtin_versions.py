"""Immutable provenance for source-controlled built-in strategies.

Custom strategies already keep editable version snapshots.  The two established
built-ins below are code-defined, so a label such as ``Supertrend`` alone is
not sufficient to reproduce a paper record.  This registry makes the preserved
production baseline explicit and pairs it with a deterministic signal fixture
in ``tests/test_builtin_strategy_versions.py``.

Changing either strategy's observable behaviour requires a new semantic version
and a new fixture fingerprint.  Do not edit an existing version in place.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class BuiltinStrategyVersion:
    key: str
    version: str
    label: str
    source_ref: str
    source_blob: str
    defaults: tuple[tuple[str, float | int], ...]
    fixture: str
    fixture_signal_count: int
    fixture_signal_sha256: str

    def public(self) -> dict:
        """Return safe, API-ready provenance without a mutable object."""
        row = asdict(self)
        row["defaults"] = dict(self.defaults)
        return row


# ``source_ref`` records the earliest preserved production baseline.  It does
# *not* assert a profitable historical run; there is no such run in Git or the
# checked-in ledger.  ``source_blob`` is the Git blob at that baseline and is
# useful when matching an exported trade journal to the source that produced it.
BUILTIN_STRATEGY_VERSIONS: Mapping[str, BuiltinStrategyVersion] = MappingProxyType({
    "brain": BuiltinStrategyVersion(
        key="brain",
        version="1.0.0",
        label="Decision Brain",
        source_ref="v1.0.0-production (5d0782c)",
        source_blob="8906671d8cd46688f4ba5d86d7cea55966eaee4f",
        defaults=(("fast", 12), ("slow", 26), ("trend", 50), ("rsi_period", 14),
                  ("conviction_threshold", 0.56), ("rr_target", 3.0)),
        fixture="BTCUSDT bundled 1h sample, 2,000 bars, causal signal stream",
        fixture_signal_count=56,
        fixture_signal_sha256="8714cd53d8e29f9e96801a3f64b5c925dbf332db19bccef18145d47f04c27339",
    ),
    "supertrend": BuiltinStrategyVersion(
        key="supertrend",
        version="1.0.0",
        label="Supertrend",
        source_ref="v1.0.0-production (5d0782c)",
        source_blob="b0dda664b3f03f20ea0af982f9999c7e10e6ea64",
        defaults=(("period", 10), ("mult", 3.0), ("atr_period", 14),
                  ("atr_mult", 1.5), ("rr_target", 2.5)),
        fixture="BTCUSDT bundled 1h sample, 2,000 bars, causal signal stream",
        fixture_signal_count=22,
        fixture_signal_sha256="f811f8fd7c7d67ce8bc7cbb3719760f12801566960c284dd4deaff7863c68b98",
    ),
    "donchian": BuiltinStrategyVersion(
        key="donchian",
        version="1.0.0",
        label="Donchian Breakout",
        source_ref="v1.0.0-production (5d0782c)",
        source_blob="6c95cb696986e4edaa4194db1dd6f864d2e58a88",
        defaults=(("channel", 30), ("atr_period", 14),
                  ("atr_mult", 1.5), ("rr_target", 2.5)),
        fixture="BTCUSDT bundled 1h sample, 2,000 bars, causal signal stream",
        fixture_signal_count=23,
        fixture_signal_sha256="9370aedf34c90cc96a5f3de3cd5fbd6ac57927863178cace87e83e70ee57faad",
    ),
    "adaptive_trend_pullback": BuiltinStrategyVersion(
        key="adaptive_trend_pullback",
        version="1.0.0",
        label="Adaptive MTF Trend Pullback",
        source_ref="source-controlled production candidate",
        source_blob="strategy-package-v1.0.0",
        defaults=(("fast_ema", 20), ("slow_ema", 50), ("adx_period", 14),
                  ("adx_min", 20), ("regime_confidence_min", 70),
                  ("quality_minimum", 75), ("minimum_rr", 2.0),
                  ("target_rr", 2.5), ("stop_atr_buffer", 0.25)),
        fixture="Deterministic long/short/range/high-volatility multi-timeframe fixtures",
        fixture_signal_count=2,
        fixture_signal_sha256="1b2043dd98cf75f32b5ce24d74833794074feae5763612e3dea782ee51a0e701",
    ),
    "three_candle_rejection": BuiltinStrategyVersion(
        key="three_candle_rejection",
        version="1.0.0",
        label="3-Candle Rejection · EMA 9/33",
        # The owner's own setup, specified by them: push, reject, confirm at a
        # swing level touched twice or more, EMA 9/33 trend filter, stop beyond
        # the rejection wick, 2R target. New code, not an existing baseline.
        source_ref="source-controlled owner specification",
        source_blob="strategies/three_candle_rejection.py@1.0.0",
        defaults=(("ema_fast", 9), ("ema_slow", 33), ("pivot", 3),
                  ("level_lookback", 150), ("min_touches", 2),
                  ("level_tolerance_atr_mult", 0.25), ("stop_buffer_atr_mult", 0.1),
                  ("atr_period", 14), ("rr_target", 2.0)),
        fixture="BTCUSDT bundled 1h sample, 2,000 bars, causal signal stream",
        fixture_signal_count=14,
        fixture_signal_sha256="dc15ef634f53a4d5d579b5782cabd28e4481abcb9ec7cf5bf6d25cfa56fcb1c8",
    ),
})


def builtin_strategy_version(key: str) -> str:
    """The immutable version label for a known built-in strategy.

    Unknown/legacy strategies deliberately remain labelled ``unversioned`` so
    callers cannot imply reproducibility that the repository cannot provide.
    """
    entry = BUILTIN_STRATEGY_VERSIONS.get(key)
    return entry.version if entry else "unversioned"


def builtin_strategy_metadata(key: str) -> dict:
    entry = BUILTIN_STRATEGY_VERSIONS.get(key)
    return entry.public() if entry else {"key": key, "version": "unversioned"}
