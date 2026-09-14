"""Smart Money Concepts (SMC) as a Trading Instance strategy.

A thin adapter over ``services.native_smc.SMCMarketStructureEngine`` -- the
same engine the SMC Strategy Lab runs -- mirroring how
``price_action_rejection`` adapts ``NativePriceActionEngine``.

Until this rewrite there were two unrelated SMC implementations. This module
carried a self-contained confluence model (a liquidity sweep, a structure shift
and a fair-value gap each merely *recent* within its own rolling window, in any
order, with no retest, no rejection requirement, no session filter, no expiry,
no invalidation and no volume gate), while the lab ran a sequential state
machine. Measured over 1600 candles in each of four regimes they never agreed:
this module fired on 116 bars, the lab on none of them.

The repository already settled which is authoritative. The strategy registry
records this path as RESEARCH_ONLY because it has "no immutable version in
strategies.builtin_versions, so a paper record cannot be attributed to a
reproducible build. The supported SMC research path is the SMC Strategy Lab.",
and ``selectable_for_new_instance("smc")`` is False -- no Trading Instance can
be created on it. ``services/native_smc.py`` is a frozen alpha source; this
module was not. So the confluence model was a second, unvalidated
implementation of a strategy the repository had already located elsewhere.

Bracket arithmetic is unchanged by the rewrite: both computed
``entry = bar.close``, ``stop = bar.low - ATR * 1.5`` (mirrored for shorts) and
``target = entry + risk * 2.5`` on ATR 14. What changes is when a trade is
produced -- now only when the lab's engine reaches ENTRY_READY through its own
sweep -> structure shift -> point of interest -> retest -> rejection sequence.

Nothing here recomputes a level. Entry, stop, target and R:R are the engine's
own numbers, carried through untouched.

On execution: this path trades on paper only. The engine's ProposedTrade now
carries the same two permissions the native Price Action engine has --
``execution_allowed`` (False, and the engine's constructor raises outright if
it is ever set, so no live order can be routed from here) and
``paper_execution_allowed`` (True), which is what ``generate`` gates on. That
second flag was added as an approved, non-alpha delta: the state-machine hash
and the engine's decision output are byte-identical across it, so the frozen
visual attestation still binds. See data/native_smc_engine_freeze_manifest.json
and tests/test_native_execution_chain.py.

Reaching a Trading Instance is a separate question from being allowed to trade.
The registry still lists this strategy RESEARCH_ONLY for want of an immutable
build version, so ``selectable_for_new_instance("smc")`` remains False and the
supported SMC surface is the Strategy Lab.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Mapping, Optional, Sequence

from bot.types import Bar, Signal, SignalType
from services.mtf_policy import native_timeframes
from services.native_smc import (
    NATIVE_SMC_ID,
    ProposedTrade,
    SMCConfig,
    SMCMarketStructureEngine,
)
from strategies.base_strategy import HubStrategy

DECISION_TIMEFRAME = "5m"
#: Seconds per decision candle, for the engine's history ingestion.
_TIMEFRAME_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900,
                      "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


class SMCStrategy(HubStrategy):
    """The lab's SMC engine, exposed as an instance strategy."""

    name = "smc"
    label = "SMC (Smart Money)"
    research_id = NATIVE_SMC_ID
    decision_timeframe = DECISION_TIMEFRAME
    # The policy primary only. The secondary is bias-only in the MTF policy, and
    # naming it here would promote it to a gate that can block entries -- which
    # the lab does not do. Same reasoning as the Price Action adapter.
    required_timeframes = (DECISION_TIMEFRAME, "1h")
    supported_regimes = ()  # the engine's own sequence gates it

    def __init__(self, symbol: str, *, timeframe: str = DECISION_TIMEFRAME,
                 config: SMCConfig | None = None, rr_target: float | None = None,
                 **params):
        super().__init__(symbol, **params)
        # ``rr_target`` is the name the confluence model took, and backtest.py
        # still passes it (``--rr``). It reaches the engine as its own
        # ``rr_ratio`` rather than being absorbed into params and ignored, which
        # would have quietly served 2.5 to anyone asking for 3. Not supplying it
        # leaves the engine's shipped default alone.
        if config is not None and rr_target is not None:
            raise ValueError(
                "pass rr_target or a full SMCConfig, not both: an explicit "
                "config already carries its own rr_ratio")
        # The engine is configured for whichever entry timeframe it is given.
        # The confluence model this replaced was timeframe-agnostic, so the
        # adapter takes a timeframe even though the registry offers the
        # instance path 5m alone (see the smc entry in services/strategy_registry).
        self.config = config or SMCConfig(
            symbol=symbol, timeframe=timeframe,
            **({"rr_ratio": float(rr_target)} if rr_target is not None else {}))
        self.decision_timeframe = self.config.timeframe
        if self.decision_timeframe not in _TIMEFRAME_SECONDS:
            raise ValueError(
                f"{self.label} cannot run on {self.decision_timeframe}; "
                f"known timeframes are {', '.join(_TIMEFRAME_SECONDS)}")
        self.required_timeframes = (self.decision_timeframe,
                                    native_timeframes(self.decision_timeframe)[0])
        self._policy_timeframes = native_timeframes(self.decision_timeframe)
        self._context: dict[str, list[Bar]] = {}
        self._engine: SMCMarketStructureEngine | None = None
        # Proposals the engine made while catching up on history. Real research
        # output, but they must never become an order.
        self._history_proposals: set[str] = set()
        self._emitted: set[str] = set()
        self._stamp_cache: list = []
        self._native_attached = False
        self.last_reason = "Awaiting multi-timeframe context"

    # ---------------------------------------------------------------- context

    def set_timeframe_context(self, context: Mapping[str, Sequence[Bar]]) -> None:
        """Take the engine-supplied causal candles; never resample here.

        Called once per closed candle immediately before on_bar. Every series
        has already been trimmed at the decision boundary upstream, so these
        are the only candles this decision may see.
        """
        self._context = {timeframe: list(rows) for timeframe, rows in context.items()}
        self.bars = list(self._context.get(self.decision_timeframe, ()))
        self._native_attached = True   # this caller owns a market-data hub

    def set_native_mtf_context(self, context: Mapping[str, Sequence[Bar]],
                               evidence: Mapping) -> None:
        """Keep the two-argument strategy contract the runtime calls.

        The engine's own setter takes the context alone and derives its
        evidence itself, so the evidence is recorded here for reporting and
        deliberately not forwarded.
        """
        super().set_native_mtf_context(context, evidence)
        for timeframe, rows in context.items():
            self._context.setdefault(timeframe, list(rows))
        self._native_attached = True   # this caller owns a market-data hub

    def _mtf(self) -> dict[str, list[Bar]]:
        return {timeframe: list(self._context.get(timeframe, ()))
                for timeframe in self._policy_timeframes}

    def _attach_mtf(self, engine: SMCMarketStructureEngine) -> None:
        """Hand the engine native higher-timeframe candles, if this caller has them.

        Calling the engine's setter is not free of consequence: it latches
        ``_native_mtf_enabled`` on permanently, which retires the engine's own
        internal higher-timeframe bucket -- the documented fallback "for frozen
        historical tests that do not own a market-data hub". Passing an empty
        context is therefore not a no-op. It leaves the engine with no
        higher-timeframe series at all, a bias of 0 on every candle, and so no
        setup can ever form: before this, services/replay.py drove SMC over
        2400 candles and produced exactly zero setups, where the engine on its
        own bucket produces 164.

        The latch is set by the two context setters the runtime calls, not by
        whether a context happens to be populated yet. That distinction is the
        point. A live instance whose higher-timeframe series is still filling
        has a market-data hub and must wait for it -- dropping to the internal
        bucket there would aggregate higher-timeframe candles from the entry
        stream, which ``HubStrategy.set_native_mtf_context`` forbids outright
        ("must never manufacture replacement HTF candles from their entry
        stream"). Only a caller that never supplies a context at all -- replay,
        backtest.py, the frozen historical tests -- gets the fallback, which is
        exactly who it was written for.
        """
        if self._native_attached:
            engine.set_native_mtf_context(self._mtf())

    def _catch_up(self, engine: SMCMarketStructureEngine, bar: Bar) -> None:
        """Ingest every candle strictly before this decision bar, as history."""
        seen = engine.bars[-1].timestamp if engine.bars else None
        history = [row for row in self._context.get(self.decision_timeframe, ())
                   if row.timestamp < bar.timestamp
                   and (seen is None or row.timestamp > seen)]
        if not history:
            return
        self._attach_mtf(engine)
        engine.ingest_authoritative_closed_bars(
            history, timeframe_seconds=_TIMEFRAME_SECONDS[self.decision_timeframe])
        self._history_proposals |= set(engine.proposals)

    # ----------------------------------------------------------------- decide

    def on_bar(self, bar: Bar) -> Optional[Signal]:
        if self._engine is None:
            self._engine = SMCMarketStructureEngine(self.config)
        engine = self._engine
        self._catch_up(engine, bar)
        if engine.bars and bar.timestamp <= engine.bars[-1].timestamp:
            # Already decided on this candle; its proposals are accounted for.
            self.last_reason = "candle already evaluated"
            return None
        self._attach_mtf(engine)
        engine.process_closed_bar(bar)
        self.bars = list(engine.bars)
        return self.generate(bar)

    def generate(self, bar: Bar) -> Optional[Signal]:
        engine = self._engine
        if engine is None:
            self.last_reason = "no engine: on_bar has not run for this candle"
            return None

        candidates = [row for pid, row in engine.proposals.items()
                      if pid not in self._history_proposals
                      and pid not in self._emitted]
        if not candidates:
            self.last_reason = self._waiting_reason(engine)
            return None

        directions = {row.direction for row in candidates}
        if len(directions) > 1:
            # Both sides proposed on one candle. Choosing here would be this
            # adapter inventing a rule the engine does not have.
            self.last_reason = "contradictory long and short proposals on one candle"
            return None

        proposal = max(candidates, key=lambda row: (row.rr_ratio, row.id))
        self._emitted.add(proposal.id)
        # Paper, not live. ``paper_execution_allowed`` is the permission that
        # lets a proposal be simulated; ``execution_allowed`` stays False and
        # the engine's constructor refuses to start if it is ever set, so no
        # path from here reaches a real exchange. Same gate the Price Action
        # adapter uses, for the same reason.
        if not getattr(proposal, "paper_execution_allowed", False):
            self.last_reason = (
                f"engine proposal {proposal.id} is research-only "
                f"({getattr(proposal, 'risk_status', None) or 'paper execution not allowed'})")
            return None
        return self._as_signal(bar, proposal)

    def _waiting_reason(self, engine: SMCMarketStructureEngine) -> str:
        """Name the stage the newest live setup is waiting on.

        The engine already publishes the next event each setup needs, so the
        blocker is reported rather than guessed at.
        """
        live = [row for row in engine.setups.values()
                if row.phase not in ("INVALIDATED", "EXPIRED")
                and getattr(row.phase, "value", row.phase) not in ("INVALIDATED", "EXPIRED")]
        if not live:
            return "no live SMC setup on this candle"
        newest = max(live, key=lambda row: row.created_index)
        try:
            return f"{getattr(newest.phase, 'value', newest.phase)}: {engine.next_required_event(newest)}"
        except Exception as exc:  # noqa: BLE001 -- reporting must not mask the decision
            return f"setup in {getattr(newest.phase, 'value', newest.phase)} ({type(exc).__name__})"

    # ------------------------------------------------- replay compatibility

    # services/replay.py draws liquidity sweeps, structure shifts and fair
    # value gaps on the chart by reading these names off the strategy
    # (`isinstance(strat, SMCStrategy)` at replay.py:566). The confluence model
    # kept them as plain bar indices. They are derived from the engine's own
    # objects now, so the replay view shows what the engine actually found
    # rather than a second opinion. This is a read-only seam: nothing here
    # participates in a decision.

    _NEVER = -10 ** 9

    def _event_index(self, kind: str, direction: str) -> int:
        """Bar index of the newest engine event of this kind and direction."""
        engine = self._engine
        if engine is None:
            return self._NEVER
        best = self._NEVER
        for event in engine.events.values():
            if getattr(event, "direction", None) != direction:
                continue
            if kind == "sweep" and not str(event.id).startswith("sweep"):
                continue
            if kind == "structure" and str(event.id).startswith("sweep"):
                continue
            index = getattr(event, "bar_index", None)
            if index is None:
                # StructureEvent carries timestamps rather than an index.
                index = self._index_of(getattr(event, "confirmed_at", None)
                                       or getattr(event, "occurred_at", None))
            if index is not None and index > best:
                best = index
        return best

    def _index_of(self, stamp) -> int | None:
        """Index of the last candle at or before ``stamp``.

        Bisected over a cached timestamp list rather than scanned: replay reads
        six of these properties on every candle and each walks every engine
        event, so a linear scan here is cubic over a long window.
        """
        engine = self._engine
        if engine is None or stamp is None:
            return None
        if len(self._stamp_cache) != len(engine.bars):
            self._stamp_cache = [row.timestamp for row in engine.bars]
        position = bisect_right(self._stamp_cache, stamp)
        return position - 1 if position else None

    @property
    def _last_sweep_low(self) -> int:
        return self._event_index("sweep", "bullish")

    @property
    def _last_sweep_high(self) -> int:
        return self._event_index("sweep", "bearish")

    @property
    def _last_bull_struct(self) -> int:
        return self._event_index("structure", "bullish")

    @property
    def _last_bear_struct(self) -> int:
        return self._event_index("structure", "bearish")

    def _last_fvg(self, direction: str) -> int:
        engine = self._engine
        if engine is None:
            return self._NEVER
        best = self._NEVER
        for gap in engine.fvgs.values():
            if gap.direction != direction:
                continue
            index = self._index_of(gap.created_at)
            if index is not None and index > best:
                best = index
        return best

    @property
    def _last_bull_fvg(self) -> int:
        return self._last_fvg("bullish")

    @property
    def _last_bear_fvg(self) -> int:
        return self._last_fvg("bearish")

    @property
    def _swing_high(self) -> float | None:
        engine = self._engine
        pivot = getattr(engine, "swing_high", None) if engine else None
        return getattr(pivot, "price", None)

    @property
    def _swing_low(self) -> float | None:
        engine = self._engine
        pivot = getattr(engine, "swing_low", None) if engine else None
        return getattr(pivot, "price", None)

    def _as_signal(self, bar: Bar, proposal: ProposedTrade) -> Signal:
        """Carry the engine's own numbers through unchanged.

        Not self._signal(): that brackets with ATR, which would discard the
        engine's stop and the target it derived from its own risk distance.
        Running the lab's engine is pointless if its levels are recomputed.
        """
        direction = (SignalType.LONG if proposal.direction == "bullish"
                     else SignalType.SHORT)
        self.last_reason = (
            f"{NATIVE_SMC_ID} {proposal.direction} setup {proposal.setup_id} "
            f"at {proposal.entry:.8f}, stop {proposal.stop:.8f} "
            f"({proposal.rr_ratio:.2f}R)")
        signal = Signal(
            timestamp=bar.timestamp, symbol=self.symbol, type=direction,
            entry=proposal.entry, stop_loss=proposal.stop,
            take_profit=proposal.target, reason=self.last_reason,
        )
        signal.snapshot = {
            "mtf_evidence": dict(self._native_mtf_evidence),
            "research_id": NATIVE_SMC_ID,
            "setup_id": proposal.setup_id,
            "proposal_id": proposal.id,
            "snapshot_id": proposal.snapshot_id,
            "rr_ratio": proposal.rr_ratio,
            "risk_distance": proposal.risk_distance,
        }
        return signal
