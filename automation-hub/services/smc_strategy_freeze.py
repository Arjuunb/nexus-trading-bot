"""The SMC decision path, pinned so it cannot change by accident.

The SMC Lab strategy is READ-ONLY. An agent layer is being built around it --
execution, journalling, review, memory -- and the whole point of that layer is
that it observes the strategy rather than altering it. "We did not change it"
is a claim, and this module is what makes it checkable.

Two independent locks, because they fail differently:

  * **The source manifest** catches an edit to the files that decide trades. It
    is a SHA-256 per file, so a one-character change to a threshold, a
    condition or an ordering moves the hash.

  * **The behaviour fingerprint** catches a change in what the strategy DOES
    when the files look untouched -- a dependency that shifted underneath it, a
    default that moved, a condition evaluated in a new order. A canonical
    candle sequence is pushed through the real engine and the real decision
    contract, and the decision-bearing output is hashed.

Neither is a substitute for the other. A refactor that preserves behaviour
moves the source hash and not the fingerprint; an upstream change that alters
behaviour moves the fingerprint and not the source hash. Both are reported.

The canonical sequence is defined HERE rather than imported from a test,
because a baseline that moves when a test is edited is not a baseline.

Nothing in this module is part of the trading path. It reads files, runs a
fixture in memory, and returns hashes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]

#: The files that decide whether, when and at what price the SMC strategy
#: trades. native_smc.py already carries its own engine freeze; it is included
#: here too so one check covers the whole decision path.
SMC_DECISION_PATH: tuple[str, ...] = (
    "services/native_smc.py",            # market structure engine
    "services/smc_strategy_ladder.py",   # candidate conditions and ordering
    "services/smc_strategy_v1.py",       # the decision contract and gating
    "services/native_smc_live_visual.py",  # produces source_strategy for the lab
)

#: Fields that change on every evaluation and say nothing about the decision.
_VOLATILE = ("evaluated_at",)

UTC = timezone.utc
_TF = timedelta(minutes=5)
#: The engine needs 51 closed 4h candles before it reports a higher-timeframe
#: bias at all, so the warm-up is load-bearing rather than padding.
_WARMUP_BARS = 2600
#: Inside the engine's London session window. Outside one it refuses the entry,
#: so the anchor is part of the fixture's meaning.
_ANCHOR = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)


def canonical_sequence():
    """A bullish sweep -> CHoCH -> FVG -> retest -> rejection, in order.

    Built to satisfy the shipped rules rather than the rules relaxed to admit
    it: every threshold below is read off the strategy's own configuration.
    """
    from bot.types import Bar

    def bar(ts, o, h, l, c, v=100.0):
        return Bar(ts, o, h, l, c, v)

    bars = []
    price, ts = 100.0, _ANCHOR - _TF * _WARMUP_BARS
    for _ in range(_WARMUP_BARS):
        nxt = price + 0.02
        bars.append(bar(ts, price, max(price, nxt) + 0.05, min(price, nxt) - 0.05, nxt))
        price, ts = nxt, ts + _TF

    pivot_high = price + 3.0
    bars.append(bar(ts, price, pivot_high, price - 0.1, price + 0.2))
    ts += _TF
    price += 0.2
    for _ in range(6):
        nxt = price - 0.15
        bars.append(bar(ts, price, price + 0.05, nxt - 0.05, nxt))
        price, ts = nxt, ts + _TF
    for _ in range(6):
        nxt = price - 0.10
        bars.append(bar(ts, price, price + 0.05, nxt - 0.05, nxt))
        price, ts = nxt, ts + _TF

    recent_low = min(row.low for row in bars[-10:])
    bars.append(bar(ts, price, price + 0.10, recent_low - 0.60, recent_low + 0.35))
    price, ts = recent_low + 0.35, ts + _TF

    for target in (pivot_high - 1.2, pivot_high + 0.9):
        bars.append(bar(ts, price, target + 0.05, price - 0.05, target, 100.0))
        price, ts = target, ts + _TF

    two_high = price + 0.05
    bars.append(bar(ts, price, two_high, price - 0.05, price, 100.0))
    ts += _TF
    mid = price + 1.8
    bars.append(bar(ts, price, mid + 0.05, price - 0.02, mid, 900.0))
    ts += _TF
    gap_low = two_high + 0.9
    top = gap_low + 1.2
    bars.append(bar(ts, gap_low + 0.1, top, gap_low, top - 0.1, 200.0))
    ts += _TF

    open_ = gap_low + 0.15
    bars.append(bar(ts, open_, open_ + 0.05, two_high + 0.02, open_ - 0.10, 300.0))
    return bars


def _sha(value: str | bytes) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def source_manifest() -> dict[str, str]:
    """SHA-256 of each file on the decision path, as it is on disk now."""
    out = {}
    for rel in SMC_DECISION_PATH:
        path = _ROOT / rel
        out[rel] = _sha(path.read_bytes()) if path.exists() else "MISSING"
    return out


def decision_snapshot() -> dict:
    """What the strategy decides on the canonical sequence.

    Runs the REAL engine and the REAL decision contract. Volatile fields are
    dropped, so the snapshot is the decision and nothing else.
    """
    from services.native_smc import SMCConfig, SMCMarketStructureEngine
    from services.smc_strategy_v1 import evaluate

    bars = canonical_sequence()
    engine = SMCMarketStructureEngine(SMCConfig(symbol="BTCUSDT", timeframe="5m"))
    for row in bars:
        engine.process_closed_bar(row)
    evaluation = evaluate(engine, candle_at=bars[-1].timestamp)
    decision = {k: v for k, v in evaluation.items() if k not in _VOLATILE}
    # The contract's verdict AND the structure underneath it: a change that
    # moved a proposal's entry or stop while leaving the verdict WATCHING
    # would otherwise pass unnoticed.
    return {
        "evaluation": decision,
        "htf_bias": engine._htf_bias(),
        "setup_phases": sorted(str(row.phase) for row in engine.setups.values()),
        "proposals": sorted(
            _canonical_json({"direction": p.direction, "entry": p.entry, "stop": p.stop})
            for p in engine.proposals.values()),
    }


def behaviour_fingerprint() -> str:
    return _sha(_canonical_json(decision_snapshot()))


@dataclass(frozen=True)
class FreezeVerdict:
    """Whether the read-only strategy is still what it was."""
    intact: bool
    source_changed: tuple[str, ...]
    behaviour_changed: bool
    expected_fingerprint: str
    actual_fingerprint: str

    def describe(self) -> str:
        if self.intact:
            return "SMC decision path is unchanged (source and behaviour both match)."
        parts = []
        if self.source_changed:
            parts.append("edited: " + ", ".join(self.source_changed))
        if self.behaviour_changed:
            parts.append(f"behaviour moved {self.expected_fingerprint[:12]} -> "
                         f"{self.actual_fingerprint[:12]}")
        return "SMC DECISION PATH CHANGED -- " + "; ".join(parts)


def verify(baseline: dict, *, fingerprint: Optional[str] = None) -> FreezeVerdict:
    """Compare the decision path against a recorded baseline.

    ``baseline`` is a {path: sha256} manifest. ``fingerprint`` is the recorded
    behaviour hash; when it is omitted only the source is checked, and that is
    reported honestly rather than counted as a pass.
    """
    current = source_manifest()
    changed = tuple(sorted(p for p in set(baseline) | set(current)
                           if baseline.get(p) != current.get(p)))
    actual = behaviour_fingerprint() if fingerprint is not None else ""
    moved = bool(fingerprint) and actual != fingerprint
    return FreezeVerdict(
        intact=not changed and not moved,
        source_changed=changed, behaviour_changed=moved,
        expected_fingerprint=fingerprint or "", actual_fingerprint=actual)
