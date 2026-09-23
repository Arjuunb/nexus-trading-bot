"""The Price Action lab's engine, as a Trading Instance strategy.

The lab and the instances were running different alpha. Both read the same
Binance USD-M hub and both fill through a paper broker, but the lab decides
with ``NativePriceActionEngine`` — confirmed swings, support/resistance zones
with role flips, rejection triggers and a rejection-extreme stop — while an
instance could only pick from the eight indicator strategies in the catalog.
There was no way to run the lab's setup autonomously, or to stand it next to
SMC on equal terms.

This adapter wraps that engine rather than reimplementing it. The engine module
is hash-frozen in ``data/pr6_real_paper_freeze.json`` precisely so its entry
conditions, stop model and target R cannot drift, and nothing here touches it:
every price this strategy emits is a number the engine computed.

Two rules carry over from the lab, and both matter more than they look:

* Only a proposal attested by the snapshot of *this* closed candle may become
  an order. The engine keeps its whole proposal history for research, and the
  lab is explicit that replayed bootstrap history must never be converted into
  a new paper order. Here that means the bars before the first decision bar are
  ingested as warm-up and everything they proposed is recorded as history.
* Warm-up builds indicator and zone state only. It can never trade.

The engine evaluates four setups on every candle and may propose more than one.
An instance casts a single vote per candle, so each catalog entry binds one
setup: ``PA1_SR_REJECTION`` (rejection at a confirmed zone) and
``PA3_FLIP_RETEST`` (a flipped zone retested), which are the two variants the
shadow research observatory already ranks.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional, Sequence

from bot.types import Bar, Signal, SignalType
from services.mtf_policy import native_timeframes
from services.native_price_action import (
    STRATEGIES,
    STRATEGY_VERSION,
    NativePriceActionEngine,
    PriceActionConfig,
    ProposedTrade,
)
from strategies.base_strategy import HubStrategy

# The engine's tuned defaults are 5m, which is also the clock the lab defaults
# to and the only one the shadow observatory measures. Binding the instance to
# it keeps a comparison between them meaningful; the catalog advertises the same
# restriction so the creation form offers nothing else.
DECISION_TIMEFRAME = "5m"

# Enough history for swings to confirm and for zones to have been touched and
# aged. Below a few hundred candles the engine is technically running but has
# no structure to reject from, which would look like a strategy that never
# fires rather than one that is still warming. Declared as warmup_required
# because PriceActionConfig is frozen and carries no minimum_bars field; the
# higher timeframes keep the policy's own two-candle minimum.
WARMUP_CANDLES = 400

_OVERRIDABLE = (set(PriceActionConfig.__dataclass_fields__)
                - {"symbol", "timeframe", "execution_allowed"})


def _config_from_env(symbol: str) -> PriceActionConfig:
    """Read HUB_PA_* overrides for the engine's own dataclass fields.

    The lab stores its knobs on the session row, which an autonomous worker
    cannot see. Env is how an instance gets the same values, and the names are
    the engine's own field names so there is no second vocabulary to keep in
    sync. Anything unset keeps the engine default, so an untouched deployment
    runs exactly the alpha the frozen module describes.
    """
    defaults = PriceActionConfig(symbol=symbol, timeframe=DECISION_TIMEFRAME)
    overrides: dict[str, object] = {}
    for field in sorted(_OVERRIDABLE):
        raw = os.environ.get("HUB_PA_" + field.upper())
        if raw is None or not raw.strip():
            continue
        current = getattr(defaults, field)
        text = raw.strip()
        if isinstance(current, bool):
            overrides[field] = text.lower() in ("1", "true", "yes", "on")
        elif isinstance(current, int) and not isinstance(current, bool):
            overrides[field] = int(text)
        elif isinstance(current, float):
            overrides[field] = float(text)
        else:
            overrides[field] = text
    return PriceActionConfig(symbol=symbol, timeframe=DECISION_TIMEFRAME, **overrides)


class PriceActionRejectionStrategy(HubStrategy):
    """One Price Action setup from the lab's engine, as an instance strategy."""

    name = "price_action_rejection"
    label = "Price Action S/R Rejection"
    pa_strategy_id = "PA1_SR_REJECTION"
    strategy_version = STRATEGY_VERSION
    decision_timeframe = DECISION_TIMEFRAME
    # 1h is the policy primary and is already a mandatory gate upstream. 4h is
    # deliberately left out: the MTF policy makes the secondary bias-only, and
    # naming it here would promote it to a gate that can block entries, which
    # the lab does not do.
    required_timeframes = (DECISION_TIMEFRAME, "1h")
    warmup_required = WARMUP_CANDLES

    def __init__(self, symbol: str, *, config: PriceActionConfig | None = None, **params):
        super().__init__(symbol, **params)
        if self.pa_strategy_id not in STRATEGIES:
            raise ValueError(
                f"unknown Price Action setup '{self.pa_strategy_id}'; "
                f"the engine evaluates {', '.join(STRATEGIES)}"
            )
        self.config = config or _config_from_env(symbol)
        if self.config.timeframe != self.decision_timeframe:
            raise ValueError(
                f"{self.label} is tuned for {self.decision_timeframe}, "
                f"not {self.config.timeframe}"
            )
        self._policy_timeframes = native_timeframes(self.decision_timeframe)
        self._context: dict[str, list[Bar]] = {}
        self._engine: NativePriceActionEngine | None = None
        # Proposals the engine made while catching up on history. They are real
        # research output and must never become an order.
        self._history_proposals: set[str] = set()
        self._emitted: set[str] = set()
        self._last_snapshot = None
        self.last_reason = "Awaiting multi-timeframe context"

    # ---------------------------------------------------------------- context

    def set_timeframe_context(self, context: Mapping[str, Sequence[Bar]]) -> None:
        """Take the engine-supplied causal candles; never resample here.

        Called once per closed candle, immediately before on_bar. The engine
        has already trimmed every series at the decision boundary, so these are
        the only candles this decision is allowed to see.
        """
        self._context = {timeframe: list(rows) for timeframe, rows in context.items()}
        # Warm-up progress is reported from self.bars, so keep it the canonical
        # entry stream the way the other MTF strategy does.
        self.bars = list(self._context.get(self.decision_timeframe, ()))

    def _mtf(self) -> dict[str, list[Bar]]:
        return {timeframe: list(self._context.get(timeframe, ()))
                for timeframe in self._policy_timeframes}

    def _catch_up(self, engine: NativePriceActionEngine, bar: Bar) -> None:
        """Ingest every candle strictly before this decision bar, as history."""
        seen = engine.bars[-1].timestamp if engine.bars else None
        history = [row for row in self._context.get(self.decision_timeframe, ())
                   if row.timestamp < bar.timestamp
                   and (seen is None or row.timestamp > seen)]
        if not history:
            return
        engine.set_native_mtf_context(self._mtf())
        engine.ingest_closed_bars(history, market_data_health="LIVE_BOOTSTRAP_RECONCILED")
        self._history_proposals |= set(engine.proposals)

    # ----------------------------------------------------------------- decide

    def on_bar(self, bar: Bar) -> Optional[Signal]:
        if self._engine is None:
            self._engine = NativePriceActionEngine(self.config)
        engine = self._engine
        self._catch_up(engine, bar)
        if engine.bars and bar.timestamp <= engine.bars[-1].timestamp:
            # Already decided on this candle. The engine would hand back its
            # cached snapshot, whose proposals are accounted for either way.
            self.last_reason = "candle already evaluated"
            return None
        # Refreshed per candle, exactly as the lab does before processing.
        engine.set_native_mtf_context(self._mtf())
        snapshot = engine.process_closed_bar(bar, market_data_health="LIVE_RECONCILED")
        self._last_snapshot = snapshot
        self.bars = list(engine.bars)
        return self.generate(bar, snapshot=snapshot)

    def generate(self, bar: Bar, *, snapshot=None) -> Optional[Signal]:
        if snapshot is None or self._engine is None:
            # A direct caller that did not go through on_bar has given the
            # engine nothing to decide on; saying so beats inventing a trade.
            self.last_reason = "no closed-candle snapshot for this bar"
            return None
        attested = [self._engine.proposals[pid] for pid in snapshot.proposal_ids
                    if pid in self._engine.proposals]
        candidates = [row for row in attested
                      if row.strategy_id == self.pa_strategy_id
                      and row.paper_execution_allowed
                      and row.id not in self._history_proposals
                      and row.id not in self._emitted]
        if not candidates:
            self.last_reason = (
                f"no {self.pa_strategy_id} proposal on this candle "
                f"(bias {self._engine.structure_bias})"
            )
            return None
        directions = {row.direction for row in candidates}
        if len(directions) > 1:
            # The setup fired both ways on one candle. Picking a side here would
            # be this adapter inventing a rule the engine does not have.
            self.last_reason = "contradictory long and short proposals on one candle"
            return None
        proposal = max(candidates, key=lambda row: (row.rr_ratio, row.id))
        self._emitted.add(proposal.id)
        return self._as_signal(bar, proposal)

    #: The engine's own condition keys, mapped to the runtime's blocker
    #: vocabulary. Taken from NativePriceActionEngine._condition rather than
    #: invented here, so a condition the engine renames fails the mapping test
    #: instead of silently becoming "no setup".
    _BLOCKER_BY_CONDITION = {
        "zone": "NO_ELIGIBLE_ZONE",
        "pullback_zone": "NO_ELIGIBLE_ZONE",
        "rejection": "REJECTION_FAILED",
        "pullback_rejection": "REJECTION_FAILED",
        "trend": "TREND_NOT_ALIGNED",
        "role_flip": "NO_ROLE_FLIP",
        "retest": "RETEST_NOT_HELD",
        "false_break": "NO_FALSE_BREAK",
        "reversal_close": "NO_REVERSAL_CLOSE",
        "pin_bar_only": "PIN_BAR_REQUIRED",
        "first_touch_only": "NOT_FIRST_TOUCH",
    }

    def decision_report(self) -> dict:
        """Why this candle did not produce a signal, in the engine's own terms.

        The engine already evaluates every condition and records which ones are
        unmet, in order, on each closed candle. None of that reached the
        runtime: this strategy had no decision_report, so every refusal it made
        arrived at the dashboard as "GATE_REJECTED: NO_SETUP" -- the same six
        words whether price never reached a zone or reached one and failed the
        rejection test.

        The first unmet condition is the blocker, because the engine lists them
        in the order it requires them, so the earliest one is what actually
        stopped the setup. The rest are reported as still-unmet rather than
        promoted, since a condition never reached has not failed.
        """
        snapshot = self._last_snapshot
        if snapshot is None:
            return {"decision": "WAIT", "reason": self.last_reason,
                    "blocker_code": "WARMUP", "state": None}
        traces = [trace for trace in getattr(snapshot, "strategy_traces", ())
                  if trace.strategy_id == self.pa_strategy_id]
        if not traces:
            return {"decision": "WAIT", "reason": self.last_reason,
                    "blocker_code": "NO_SETUP", "state": None}
        # The trace closest to firing is the informative one: a strategy
        # evaluates both directions and the one with fewer unmet conditions is
        # the setup actually developing.
        trace = min(traces, key=lambda row: len(row.missing_conditions))
        missing = list(trace.missing_conditions)
        passed = [row["key"] for row in trace.conditions if row["status"] == "PASS"]
        code = (self._BLOCKER_BY_CONDITION.get(missing[0], "NO_SETUP")
                if missing else None)
        return {
            "decision": "ENTER" if not missing else "WAIT",
            "reason": self.last_reason,
            "blocker_code": code,
            "state": trace.state,
            "direction": trace.direction,
            "passed_conditions": passed,
            "missing_conditions": missing,
            "next_required_event": trace.next_required_event,
            "setup_id": trace.setup_id,
        }

    def _as_signal(self, bar: Bar, proposal: ProposedTrade) -> Signal:
        """Carry the engine's own numbers through unchanged.

        Not self._signal(): that brackets with ATR, which would discard the
        rejection-extreme stop and the target R the engine derived from the
        zone. The whole point of running the lab's engine is its levels.
        """
        direction = (SignalType.LONG if proposal.direction == "bullish"
                     else SignalType.SHORT)
        self.last_reason = (
            f"{proposal.strategy_id} {proposal.direction} {proposal.entry_model} "
            f"at {proposal.entry:.8f}, stop {proposal.stop:.8f} "
            f"({proposal.rr_ratio:.2f}R)"
        )
        signal = Signal(
            timestamp=bar.timestamp, symbol=self.symbol, type=direction,
            entry=proposal.entry, stop_loss=proposal.stop,
            take_profit=proposal.target, reason=self.last_reason,
        )
        # Provenance for the trade journal. services/auto_engine.py copies
        # ``signal.snapshot`` into the pipeline payload, so without this a
        # Price Action paper trade lands in the journal with no way back to the
        # proposal that caused it -- which is the very thing the registry means
        # when it says a paper record must be attributable. Metadata only: it
        # names what the engine already decided and moves no level.
        # Which level, and what rejected off it. Carrying only setup_id would
        # leave a journal entry that says a trade happened without saying which
        # support or resistance caused it -- the one thing worth knowing when
        # deciding whether to trust the setup.
        setup = (self._engine.setups.get(proposal.setup_id)
                 if self._engine is not None else None)
        signal.snapshot = {
            "mtf_evidence": dict(self._native_mtf_evidence),
            "research_id": proposal.strategy_id,
            "strategy_version": STRATEGY_VERSION,
            "setup_id": proposal.setup_id,
            "proposal_id": proposal.id,
            "entry_model": proposal.entry_model,
            "rr_ratio": proposal.rr_ratio,
            "risk_distance": proposal.risk_distance,
            "zone_id": getattr(setup, "zone_id", None),
            "trigger_event_id": getattr(setup, "trigger_event_id", None),
            "reasons": list(getattr(setup, "reasons", ()) or ()),
        }
        return signal


class PriceActionFlipRetestStrategy(PriceActionRejectionStrategy):
    """The flipped-zone retest, the observatory's other measured PA variant."""

    name = "price_action_flip_retest"
    label = "Price Action Flip Retest"
    pa_strategy_id = "PA3_FLIP_RETEST"
