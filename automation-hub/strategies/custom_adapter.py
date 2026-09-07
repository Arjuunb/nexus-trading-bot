"""Run a user-built custom strategy spec inside the live (paper) engine.

Wraps a custom rule spec as a HubStrategy so the autonomous engine can paper-
trade it — the bridge from the Strategy Builder to paper trading. The pipeline
still applies all risk checks (market quality, sizing, exposure, drawdown halt).

When the spec enables it (``quality_filter``, on by default), the same TradeBrain
used in simulation scores each setup BEFORE it becomes a signal: blocked or
sub-threshold setups are suppressed (and logged via ``on_block`` if provided),
and the quality score scales the signal's confidence so the pipeline sizes good
setups larger than marginal ones. This makes paper trading take the same
fewer, higher-quality trades that simulation does.
"""
from __future__ import annotations

from typing import Callable, Optional

from bot.types import Bar, Signal, SignalType
from strategies.base_strategy import HubStrategy
from strategies.brain import TradeBrain, detect_reversal
from strategies.custom import WARMUP, _rule, _stop_distance, _target_distance, evaluate


class CustomStrategyAdapter(HubStrategy):
    name = "custom"
    label = "Custom"

    def __init__(self, symbol: str, spec: dict, *, max_history: int = 700,
                 brain: Optional[TradeBrain] = None, min_score: Optional[int] = None,
                 on_block: Optional[Callable[[dict], None]] = None, **params):
        target = spec.get("target") or {}
        params.setdefault("rr_target", float(target.get("rr", 1.5)) if target.get("type", "rr") == "rr" else 1.5)
        super().__init__(symbol, **params)
        self.spec = spec
        self.max_history = max_history
        self.label = f"Custom: {spec.get('name', 'Strategy')}"
        # Quality filter (on unless the spec disables it).
        self._use_brain = spec.get("quality_filter", True)
        self.brain = brain or (TradeBrain() if self._use_brain else None)
        self.min_score = int(spec.get("min_score", 60) if min_score is None else min_score)
        self.on_block = on_block
        self._reversal = detect_reversal(spec)
        # Multi-timeframe gate (on unless the spec disables it): never trade
        # against the higher-timeframe trend — the same gate replay uses.
        self._mtf_filter = spec.get("mtf_filter", True)

    def should_exit(self, bar: Bar, side: str) -> Optional[str]:
        """Live counterpart of the simulator's exit rules: close an open position
        when the spec's exit conditions fire. Called by the engine each bar with
        the current bar (self.bars does not yet include it at exit-check time)."""
        exit_cfg = self.spec.get("exit") or {}
        rules = exit_cfg.get("rules") or []
        if not rules and not exit_cfg.get("ai_exit"):
            return None
        window = self.bars if (self.bars and self.bars[-1] is bar) else [*self.bars, bar]
        i = len(window) - 1
        if i < WARMUP:
            return None
        if rules:
            hit, _ = evaluate({"op": exit_cfg.get("op", "OR"), "rules": rules}, window, i)
            if hit:
                return "indicator"
        if exit_cfg.get("ai_exit"):
            hit, _ = _rule({"type": "choch", "dir": "down" if side == "long" else "up"}, window, i)
            if hit:
                return "ai-exit"
        return None

    def generate(self, bar: Bar) -> Optional[Signal]:
        if len(self.bars) > self.max_history:
            del self.bars[:-self.max_history]
        if len(self.bars) < WARMUP:
            return None
        i = len(self.bars) - 1
        entry_tree = self.spec.get("entry") or {"op": "AND", "rules": []}
        matched, reasons = evaluate(entry_tree, self.bars, i)
        if not matched:
            return None

        side = self.spec.get("side", "long")
        entry = bar.close
        risk_abs = _stop_distance(self.spec.get("stop") or {}, entry, self.bars, i)
        if risk_abs <= 0:
            return None
        tgt = _target_distance(self.spec.get("target") or {}, risk_abs, entry)
        if side == "long":
            direction, stop, take = SignalType.LONG, entry - risk_abs, entry + tgt
        else:
            direction, stop, take = SignalType.SHORT, entry + risk_abs, entry - tgt

        reason = "; ".join(reasons) or "custom entry"
        confidence = 1.0

        # ---- trade-quality brain gate (same logic as simulation) ----
        if self.brain is not None:
            primary = self._native_mtf_evidence.get("primary") or {}
            primary_tf = primary.get("htf_timeframe")
            v = self.brain.evaluate(self.bars, i, side=side, entry=entry, stop=stop,
                                    target=take, reversal=self._reversal,
                                    native_htf_bars=self._native_mtf_context.get(primary_tf, ()),
                                    require_native_htf=True,
                                    allow_legacy_htf_resample=False)
            if not v.allowed or v.score < self.min_score:
                if self.on_block:
                    self.on_block({
                        "symbol": self.symbol, "side": side, "score": v.score,
                        "regime": v.regime, "htf_bias": v.htf_bias,
                        "reason": (v.blocks[0] if v.blocks else f"low quality score {v.score} < {self.min_score}"),
                        "timestamp": bar.timestamp.isoformat(),
                    })
                return None
            confidence = max(0.0, min(1.0, v.score / 100.0))
            reason = f"{reason} | score {v.score} ({v.grade}), {v.regime}, HTF {v.htf_bias}"

        # ---- multi-timeframe gate (never trade against the higher-timeframe trend) ----
        if self._mtf_filter and not self._reversal:
            from services.mtf_engine import htf_consensus
            primary = self._native_mtf_evidence.get("primary") or {}
            label = primary.get("htf_timeframe")
            bias = primary.get("htf_bias")
            if not label or bias not in {"BULLISH", "BEARISH", "NEUTRAL"}:
                if self.on_block:
                    self.on_block({
                        "symbol": self.symbol, "side": side, "score": confidence * 100,
                        "regime": "—", "htf_bias": "unavailable",
                        "reason": "native primary HTF context unavailable",
                        "timestamp": bar.timestamp.isoformat(),
                    })
                return None
            trends = {label: bias.title()}
            mtf = htf_consensus(trends, 1 if side == "long" else -1, tfs=(label,))
            if not mtf["allowed"]:
                if self.on_block:
                    self.on_block({
                        "symbol": self.symbol, "side": side, "score": confidence * 100,
                        "regime": "—", "htf_bias": mtf["reason"], "reason": mtf["reason"],
                        "timestamp": bar.timestamp.isoformat(),
                    })
                return None
            if mtf["aligned"]:
                reason = f"{reason} | {mtf['reason']}"

        sig = Signal(timestamp=bar.timestamp, symbol=self.symbol, type=direction,
                     entry=entry, stop_loss=stop, take_profit=take, reason=reason)
        sig.confidence = confidence
        sig.snapshot = {"mtf_evidence": dict(self._native_mtf_evidence)}
        return sig
