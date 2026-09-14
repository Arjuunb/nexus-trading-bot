"""Native, deterministic SMC market-model engine — research only.

It consumes closed :class:`bot.types.Bar` candles, has no TradingView or
webhook dependency, and deliberately contains no order-placement capability.
The output objects are the single source for a future chart and decision layer.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterable, Literal

from bot.data.indicators import atr, ema
from bot.types import Bar

EXECUTION_ALLOWED = False
NATIVE_SMC_ID = "SMC_NATIVE_V1_RESEARCH"


class SetupPhase(str, Enum):
    IDLE = "IDLE"; CONTEXT_VALID = "CONTEXT_VALID"; LIQUIDITY_SWEPT = "LIQUIDITY_SWEPT"
    STRUCTURE_SHIFT_CONFIRMED = "STRUCTURE_SHIFT_CONFIRMED"; POI_CREATED = "POI_CREATED"
    WAITING_RETEST = "WAITING_RETEST"; REJECTION_CONFIRMED = "REJECTION_CONFIRMED"
    ENTRY_READY = "ENTRY_READY"; INVALIDATED = "INVALIDATED"; EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class SMCConfig:
    symbol: str
    timeframe: str = "5m"
    htf_minutes: int = 240
    internal_pivot_length: int = 5
    swing_pivot_length: int = 50
    sweep_lookback: int = 10
    atr_length: int = 14
    atr_multiplier: float = 1.5
    rr_ratio: float = 2.5
    wick_multiplier: float = 2.0
    structure_break_atr_mult: float = .3
    poi_atr_buffer_mult: float = .8
    setup_expiry_bars: int = 10
    require_volume_surge: bool = True
    execution_allowed: bool = False


@dataclass(frozen=True)
class PivotPoint:
    id: str; kind: str; price: float; occurred_at: datetime; confirmed_at: datetime; index: int; scope: str = "internal"


@dataclass(frozen=True)
class StructureEvent:
    id: str; symbol: str; timeframe: str; scope: str; event_type: str; direction: str
    level: float; occurred_at: datetime; confirmed_at: datetime; source_pivot_id: str; break_price: float


@dataclass(frozen=True)
class LiquiditySweep:
    id: str; symbol: str; timeframe: str; direction: str; level: float; timestamp: datetime; bar_index: int


@dataclass
class FairValueGap:
    id: str; direction: str; top: float; bottom: float; created_at: datetime; origin: tuple[datetime, datetime, datetime]
    active: bool = True; mitigated: bool = False; mitigation_at: datetime | None = None


@dataclass
class OrderBlock:
    id: str; direction: str; high: float; low: float; source_pivot_id: str; source_structure_id: str
    created_at: datetime; active: bool = True; mitigated: bool = False; mitigation_at: datetime | None = None


@dataclass(frozen=True)
class DealingRange:
    high: float; low: float; equilibrium: float; area: str


@dataclass(frozen=True)
class PriceAction:
    bullish_rejection: bool; bearish_rejection: bool; body: float; upper_wick: float; lower_wick: float


@dataclass(frozen=True)
class SetupTransition:
    id: str; setup_id: str; from_phase: str; to_phase: str; timestamp: datetime; reason: str; object_id: str | None = None


@dataclass
class SMCSetup:
    id: str; direction: str; created_at: datetime; created_index: int; phase: SetupPhase
    sweep_id: str | None = None; structure_id: str | None = None; poi_id: str | None = None
    first_touch_at: datetime | None = None; invalidation_reason: str | None = None
    transitions: list[SetupTransition] = field(default_factory=list)


@dataclass(frozen=True)
class ProposedTrade:
    id: str; setup_id: str; direction: str; entry: float; stop: float; target: float; risk_distance: float
    rr_ratio: float; snapshot_id: str; risk_percent: float | None = None; position_size: float | None = None
    execution_allowed: bool = False; risk_status: str = "PENDING_RISK_ENGINE"
    # Paper and live are separate permissions, as they already are for the
    # native Price Action engine. execution_allowed stays False and is still
    # refused outright by the constructor above; this one lets a proposal be
    # simulated against the paper broker. Approved delta, recorded in
    # data/native_smc_engine_freeze_manifest.json -- it changes who may act on
    # a proposal, never when one is made: the state machine, its thresholds and
    # its output are untouched, and state_machine_hash is unchanged.
    paper_execution_allowed: bool = True


@dataclass(frozen=True)
class ChartObject:
    id: str; source_id: str; kind: str; direction: str; start_at: datetime; top: float | None = None; bottom: float | None = None


@dataclass(frozen=True)
class SMCMarketSnapshot:
    id: str; symbol: str; timeframe: str; candle_open: datetime; candle_close: datetime
    htf_bias: int; htf_ema: float | None; htf_completed_at: datetime | None; swing_bias: int; internal_bias: int
    dealing_range: DealingRange; session: str; price_action: PriceAction; active_setup_id: str | None
    setup_phase: str | None; next_required_event: str; latest_sweep_id: str | None
    event_ids: tuple[str, ...]; active_fvg_ids: tuple[str, ...]; active_ob_ids: tuple[str, ...]
    proposal_ids: tuple[str, ...] = ()


ReviewClassification = Literal["CORRECT", "INCORRECT", "AMBIGUOUS"]


@dataclass(frozen=True)
class VisualReview:
    """Human evaluation evidence; it can never alter native engine rules."""
    id: str; research_id: str; symbol: str; timeframe: str; object_id: str; component: str
    classification: ReviewClassification; expected_structure: str | None; actual_structure: str | None
    reason: str | None; screenshot_timestamp: str | None; created_at: datetime
    notes: str | None = None; visible_range_start: str | None = None
    visible_range_end: str | None = None; selected_candle_timestamp: str | None = None


class VisualReviewLedger:
    """Small append-safe research ledger stored outside the repository."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get(
            "HUB_SMC_VISUAL_REVIEW_PATH", "/var/lib/tradexa/smc_visual_reviews.json"
        ))

    def records(self) -> list[VisualReview]:
        if not self.path.exists():
            return []
        try:
            rows = json.loads(self.path.read_text())
            return [VisualReview(**{**row, "created_at": datetime.fromisoformat(row["created_at"])}) for row in rows]
        except (OSError, ValueError, TypeError, KeyError):
            # An unreadable review file is evidence unavailable, never a reason
            # to manufacture a passing verification score.
            return []

    def append(self, review: VisualReview) -> VisualReview:
        rows = [asdict(row) for row in self.records()]
        rows.append(asdict(review))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(rows, default=lambda value: value.isoformat(), sort_keys=True, indent=2) + "\n")
        os.replace(temp, self.path)
        return review


def _stable_id(kind: str, *values: object) -> str:
    raw = "|".join(str(value) for value in values).encode()
    return f"{kind}-{hashlib.sha256(raw).hexdigest()[:16]}"


class SMCMarketStructureEngine:
    """Single-writer closed-bar SMC state machine with idempotent processing."""

    def __init__(self, config: SMCConfig):
        if config.execution_allowed or EXECUTION_ALLOWED:
            raise ValueError("Native SMC research execution is permanently disabled in this stage")
        self.config = config; self.bars: list[Bar] = []; self.processed: set[datetime] = set()
        self.events: dict[str, StructureEvent | LiquiditySweep] = {}; self.fvgs: dict[str, FairValueGap] = {}; self.obs: dict[str, OrderBlock] = {}
        self.pivots: dict[str, PivotPoint] = {}; self.swing_bias = 0; self.internal_bias = 0
        self.broken_pivots: set[str] = set()
        self.swing_high: PivotPoint | None = None; self.swing_low: PivotPoint | None = None
        self.internal_high: PivotPoint | None = None; self.internal_low: PivotPoint | None = None
        self.protected_swing_high: PivotPoint | None = None; self.protected_swing_low: PivotPoint | None = None
        self.protected_internal_high: PivotPoint | None = None; self.protected_internal_low: PivotPoint | None = None
        self.htf_closed: list[Bar] = []; self._htf_bucket: list[Bar] = []; self._htf_key = None
        self._native_mtf_context: dict[str, list[Bar]] = {}
        self._native_mtf_enabled = False
        self.setups: dict[str, SMCSetup] = {}; self.transitions: list[SetupTransition] = []; self.proposals: dict[str, ProposedTrade] = {}
        self._ready_for_plan: list[SMCSetup] = []
        self.latest_snapshot: SMCMarketSnapshot | None = None
        # Historical snapshots are immutable inspection evidence. They are not
        # read by any entry, risk, or execution decision.
        self.snapshots: dict[datetime, SMCMarketSnapshot] = {}

    def set_native_mtf_context(self, context: dict[str, list[Bar]]) -> None:
        """Attach provider-native HTF candles before decision-bar ingestion.

        This is the production path. The legacy internal bucket remains only
        for frozen historical tests that do not own a market-data hub.
        """
        from services.mtf_policy import native_timeframes

        required = native_timeframes(self.config.timeframe)
        self._native_mtf_context = {tf: list(context.get(tf, ())) for tf in required}
        self._native_mtf_enabled = True

    def _native_evidence(self) -> dict:
        if not self._native_mtf_enabled or not self.bars:
            return {}
        from services.mtf_policy import candle_close, evidence_at

        return evidence_at(
            self.config.symbol, self.config.timeframe, self._native_mtf_context,
            candle_close(self.bars[-1], self.config.timeframe),
        )

    def process_closed_bar(self, bar: Bar) -> SMCMarketSnapshot:
        if bar.timestamp in self.processed:
            if self.latest_snapshot is None: raise RuntimeError("duplicate candle before snapshot")
            return self.latest_snapshot
        if self.bars and bar.timestamp <= self.bars[-1].timestamp:
            raise ValueError("candles must be unique and chronological")
        self.processed.add(bar.timestamp); self.bars.append(bar); index = len(self.bars) - 1
        self._advance_htf(bar); self._mitigate_zones(bar); self._confirm_pivots(bar, index)
        events = self._break_structure(bar, index); sweep = self._detect_sweep(bar, index); fvg = self._detect_fvg(bar, index)
        action = self._price_action(bar); dealing = self._dealing_range(bar.close)
        self._advance_setups(bar, index, sweep, events, fvg, action, dealing)
        snapshot = self._snapshot(bar, events, sweep, fvg, dealing, action)
        self.latest_snapshot = snapshot
        proposal_ids: list[str] = []
        for setup in self._ready_for_plan:
            proposal = self._propose(setup, bar, snapshot.id)
            if proposal is not None:
                proposal_ids.append(proposal.id)
        self._ready_for_plan.clear()
        if proposal_ids:
            snapshot = replace(snapshot, proposal_ids=tuple(proposal_ids))
            self.latest_snapshot = snapshot
        self.snapshots[bar.timestamp] = snapshot
        return snapshot

    def ingest_authoritative_closed_bars(self, bars: Iterable[Bar], *, timeframe_seconds: int,
                                         now: datetime | None = None) -> list[SMCMarketSnapshot]:
        """Feed only provider-validated closed candles from the existing adapter.

        This method performs no fetch and has no historical/synthetic fallback;
        workers must call it only after ``data.forward_market_data`` validation.
        """
        from data.forward_market_data import valid_closed_bars
        return [self.process_closed_bar(row) for row in valid_closed_bars(bars, timeframe_seconds, now=now)]

    def _advance_htf(self, bar: Bar) -> None:
        if self._native_mtf_enabled:
            return
        minutes = self.config.htf_minutes; key = int(bar.timestamp.timestamp() // (minutes * 60))
        if self._htf_key is None: self._htf_key = key
        if key != self._htf_key:
            rows = self._htf_bucket
            if rows:
                self.htf_closed.append(Bar(rows[0].timestamp, rows[0].open, max(x.high for x in rows), min(x.low for x in rows), rows[-1].close, sum(x.volume for x in rows)))
            self._htf_bucket, self._htf_key = [], key
        self._htf_bucket.append(bar)

    def _htf_bias(self) -> int:
        if self._native_mtf_enabled:
            named = ((self._native_evidence().get("primary") or {}).get("htf_bias"))
            return 1 if named == "BULLISH" else -1 if named == "BEARISH" else 0
        # Current forming HTF bucket is excluded: equivalent to completed-candle context.
        if len(self.htf_closed) < 51: return 0
        closes = [row.close for row in self.htf_closed]
        return 1 if closes[-1] > ema(closes, 50)[-1] else -1

    def _htf_ema(self) -> float | None:
        """Return an EMA built only from completed higher-timeframe bars."""
        if self._native_mtf_enabled and self.bars:
            from services.mtf_policy import candle_close, closed_native_bars, policy_for

            primary, _secondary = policy_for(self.config.timeframe)
            rows = closed_native_bars(
                self._native_mtf_context.get(primary, ()), primary,
                candle_close(self.bars[-1], self.config.timeframe),
            )
            if len(rows) < 51:
                return None
            return float(ema([row.close for row in rows], 50)[-1])
        if len(self.htf_closed) < 51:
            return None
        return float(ema([row.close for row in self.htf_closed], 50)[-1])

    def _confirm_pivots(self, now: Bar, index: int) -> None:
        for scope, length in (("internal", self.config.internal_pivot_length), ("swing", self.config.swing_pivot_length)):
            if index < length: continue
            candidate = self.bars[index - length]; after = self.bars[index - length + 1:index + 1]
            # Native baseline retains the reference's right-confirmation timing.
            if candidate.high > max(x.high for x in after): self._record_pivot(scope, "high", candidate, index - length, now)
            if candidate.low < min(x.low for x in after): self._record_pivot(scope, "low", candidate, index - length, now)

    def _record_pivot(self, scope: str, kind: str, bar: Bar, index: int, now: Bar) -> None:
        price = bar.high if kind == "high" else bar.low; ident = _stable_id("pivot", scope, kind, bar.timestamp, price)
        pivot = PivotPoint(ident, kind, price, bar.timestamp, now.timestamp, index, scope); self.pivots[ident] = pivot
        setattr(self, f"{scope}_{kind}", pivot)
        protected = f"protected_{scope}_{kind}"
        if getattr(self, protected) is None: setattr(self, protected, pivot)

    def _break_structure(self, bar: Bar, index: int) -> list[StructureEvent]:
        result: list[StructureEvent] = []; current_atr = atr(self.bars, self.config.atr_length)
        if current_atr <= 0: return result
        buffer = current_atr * self.config.structure_break_atr_mult
        for scope in ("internal", "swing"):
            bias = getattr(self, f"{scope}_bias"); high = getattr(self, f"{scope}_high"); low = getattr(self, f"{scope}_low")
            ph = getattr(self, f"protected_{scope}_high"); pl = getattr(self, f"protected_{scope}_low")
            candidate: tuple[str, str, PivotPoint] | None = None
            if ph and ph.id not in self.broken_pivots and bias != 1 and bar.close > ph.price + buffer: candidate = ("CHOCH", "bullish", ph)
            elif high and high.id not in self.broken_pivots and bias == 1 and bar.close > high.price + buffer: candidate = ("BOS", "bullish", high)
            elif pl and pl.id not in self.broken_pivots and bias != -1 and bar.close < pl.price - buffer: candidate = ("CHOCH", "bearish", pl)
            elif low and low.id not in self.broken_pivots and bias == -1 and bar.close < low.price - buffer: candidate = ("BOS", "bearish", low)
            if candidate is None: continue
            kind, direction, pivot = candidate; ident = _stable_id("structure", scope, kind, direction, pivot.id, bar.timestamp)
            event = StructureEvent(ident, self.config.symbol, self.config.timeframe, scope, kind, direction, pivot.price, pivot.occurred_at, bar.timestamp, pivot.id, bar.close)
            self.events[ident] = event; self.broken_pivots.add(pivot.id); result.append(event); setattr(self, f"{scope}_bias", 1 if direction == "bullish" else -1)
            if direction == "bullish" and getattr(self, f"{scope}_low"): setattr(self, f"protected_{scope}_low", getattr(self, f"{scope}_low"))
            if direction == "bearish" and getattr(self, f"{scope}_high"): setattr(self, f"protected_{scope}_high", getattr(self, f"{scope}_high"))
            self._create_ob(scope, direction, pivot, event, bar)
        return result

    def _create_ob(self, scope: str, direction: str, pivot: PivotPoint, event: StructureEvent, bar: Bar) -> None:
        rows = self.bars[pivot.index:-1]
        if not rows: return
        source = min(rows, key=lambda x: x.low) if direction == "bullish" else max(rows, key=lambda x: x.high)
        ident = _stable_id("ob", scope, direction, source.timestamp, event.id)
        self.obs.setdefault(ident, OrderBlock(ident, direction, source.high, source.low, pivot.id, event.id, bar.timestamp))

    def _detect_sweep(self, bar: Bar, index: int) -> LiquiditySweep | None:
        n = self.config.sweep_lookback
        if len(self.bars) <= n: return None
        prior = self.bars[-n-1:-1]; high, low = max(x.high for x in prior), min(x.low for x in prior)
        direction = "bearish" if bar.high > high and bar.close < high else "bullish" if bar.low < low and bar.close > low else None
        if direction is None: return None
        level = high if direction == "bearish" else low; ident = _stable_id("sweep", direction, level, bar.timestamp)
        event = LiquiditySweep(ident, self.config.symbol, self.config.timeframe, direction, level, bar.timestamp, index); self.events[ident] = event
        return event

    def _detect_fvg(self, bar: Bar, index: int) -> FairValueGap | None:
        if len(self.bars) < 22: return None
        prior, two = self.bars[-2], self.bars[-3]; volumes = [x.volume for x in self.bars[:-1]]
        surge = not self.config.require_volume_surge or prior.volume > sum(volumes[-21:-1]) / 20 * 1.2
        direction = "bullish" if bar.low > two.high and prior.close > two.high and surge else "bearish" if bar.high < two.low and prior.close < two.low and surge else None
        if direction is None: return None
        top, bottom = (bar.low, two.high) if direction == "bullish" else (two.low, bar.high)
        ident = _stable_id("fvg", direction, two.timestamp, prior.timestamp, bar.timestamp, top, bottom)
        gap = FairValueGap(ident, direction, top, bottom, bar.timestamp, (two.timestamp, prior.timestamp, bar.timestamp)); self.fvgs[ident] = gap
        return gap

    def _mitigate_zones(self, bar: Bar) -> None:
        for gap in self.fvgs.values():
            if gap.active and ((gap.direction == "bullish" and bar.low < gap.bottom) or (gap.direction == "bearish" and bar.high > gap.top)):
                gap.active = False; gap.mitigated = True; gap.mitigation_at = bar.timestamp
        for ob in self.obs.values():
            if ob.active and ((ob.direction == "bullish" and bar.low < ob.low) or (ob.direction == "bearish" and bar.high > ob.high)):
                ob.active = False; ob.mitigated = True; ob.mitigation_at = bar.timestamp

    @staticmethod
    def _price_action(bar: Bar) -> PriceAction:
        body = abs(bar.close - bar.open); lower = min(bar.close, bar.open) - bar.low; upper = bar.high - max(bar.close, bar.open)
        return PriceAction(lower >= body * 2 and upper <= body, upper >= body * 2 and lower <= body, body, upper, lower)

    def _dealing_range(self, close: float) -> DealingRange:
        high = max((x.high for x in self.bars), default=close); low = min((x.low for x in self.bars), default=close); eq = (high + low) / 2
        band = abs(high - low) * .10; area = "equilibrium" if abs(close - eq) <= band else "premium" if close > eq else "discount"
        return DealingRange(high, low, eq, area)

    def _transition(self, setup: SMCSetup, phase: SetupPhase, bar: Bar, reason: str, object_id: str | None = None) -> None:
        if setup.phase == phase: return
        transition = SetupTransition(_stable_id("transition", setup.id, phase.value, bar.timestamp), setup.id, setup.phase.value, phase.value, bar.timestamp, reason, object_id)
        setup.phase = phase; setup.transitions.append(transition); self.transitions.append(transition)

    def _advance_setups(self, bar: Bar, index: int, sweep: LiquiditySweep | None, structures: Iterable[StructureEvent], fvg: FairValueGap | None, action: PriceAction, dealing: DealingRange) -> None:
        htf = self._htf_bias()
        for setup in self.setups.values():
            if setup.phase in (SetupPhase.ENTRY_READY, SetupPhase.INVALIDATED, SetupPhase.EXPIRED): continue
            if index - setup.created_index > self.config.setup_expiry_bars: self._transition(setup, SetupPhase.EXPIRED, bar, "setup expiry")
            elif htf and ((setup.direction == "bullish" and htf < 0) or (setup.direction == "bearish" and htf > 0)): self._transition(setup, SetupPhase.INVALIDATED, bar, "HTF bias changed")
        if sweep and ((sweep.direction == "bullish" and htf == 1) or (sweep.direction == "bearish" and htf == -1)):
            sid = _stable_id("setup", self.config.symbol, self.config.timeframe, sweep.direction, sweep.timestamp)
            setup = self.setups.setdefault(sid, SMCSetup(sid, sweep.direction, bar.timestamp, index, SetupPhase.IDLE, sweep_id=sweep.id)); self._transition(setup, SetupPhase.LIQUIDITY_SWEPT, bar, "liquidity swept", sweep.id)
        for setup in self.setups.values():
            if setup.phase == SetupPhase.LIQUIDITY_SWEPT:
                event = next((e for e in structures if e.direction == setup.direction and e.event_type in ("CHOCH", "BOS")), None)
                if event: setup.structure_id = event.id; self._transition(setup, SetupPhase.STRUCTURE_SHIFT_CONFIRMED, bar, f"{event.scope} {event.event_type}", event.id)
                elif any(e.direction != setup.direction for e in structures): self._transition(setup, SetupPhase.INVALIDATED, bar, "opposite structure event")
            if setup.phase == SetupPhase.STRUCTURE_SHIFT_CONFIRMED and fvg and fvg.direction == setup.direction:
                setup.poi_id = fvg.id; self._transition(setup, SetupPhase.POI_CREATED, bar, "ordered FVG created", fvg.id); self._transition(setup, SetupPhase.WAITING_RETEST, bar, "awaiting POI retest", fvg.id)
            if setup.phase == SetupPhase.WAITING_RETEST and setup.poi_id:
                poi = self.fvgs.get(setup.poi_id); touched = poi and poi.active and bar.timestamp > poi.created_at and bar.low <= poi.top and bar.high >= poi.bottom
                if touched:
                    setup.first_touch_at = setup.first_touch_at or bar.timestamp
                    rejected = action.bullish_rejection if setup.direction == "bullish" else action.bearish_rejection
                    if rejected and self._session_name(bar) != "inactive":
                        self._transition(setup, SetupPhase.REJECTION_CONFIRMED, bar, "POI retest and rejection", poi.id)
                        self._transition(setup, SetupPhase.ENTRY_READY, bar, "research proposal only", poi.id)
                        self._ready_for_plan.append(setup)

    def _propose(self, setup: SMCSetup, bar: Bar, snapshot_id: str) -> ProposedTrade | None:
        current_atr = atr(self.bars, self.config.atr_length)
        if current_atr <= 0: return None
        stop = bar.low - current_atr * self.config.atr_multiplier if setup.direction == "bullish" else bar.high + current_atr * self.config.atr_multiplier
        risk = abs(bar.close - stop); target = bar.close + risk * self.config.rr_ratio if setup.direction == "bullish" else bar.close - risk * self.config.rr_ratio
        ident = _stable_id("proposal", setup.id, bar.timestamp)
        proposal = ProposedTrade(ident, setup.id, setup.direction, bar.close, stop, target, risk, self.config.rr_ratio, snapshot_id)
        return self.proposals.setdefault(ident, proposal)

    def _session_name(self, bar: Bar) -> str:
        try:
            from zoneinfo import ZoneInfo
            hour = bar.timestamp.astimezone(ZoneInfo("Europe/London")).hour
        except Exception: hour = bar.timestamp.hour
        return "London" if 7 <= hour < 11 else "New York" if 13 <= hour < 16 else "inactive"

    def _snapshot(self, bar: Bar, structures: list[StructureEvent], sweep: LiquiditySweep | None, fvg: FairValueGap | None, dealing: DealingRange, action: PriceAction) -> SMCMarketSnapshot:
        ids = [e.id for e in structures] + ([sweep.id] if sweep else []) + ([fvg.id] if fvg else [])
        active_setup = next((x for x in self.setups.values() if x.phase not in (SetupPhase.INVALIDATED, SetupPhase.EXPIRED, SetupPhase.ENTRY_READY)), None)
        ident = _stable_id("snapshot", self.config.symbol, self.config.timeframe, bar.timestamp, json.dumps(ids))
        latest_sweep = sweep.id if sweep else None
        native_primary = (self._native_evidence().get("primary") or {})
        native_close = native_primary.get("htf_close_timestamp")
        htf_completed_at = (datetime.fromisoformat(native_close)
                            if native_close else
                            self.htf_closed[-1].timestamp if self.htf_closed else None)
        return SMCMarketSnapshot(
            ident, self.config.symbol, self.config.timeframe, bar.timestamp, bar.timestamp,
            self._htf_bias(), self._htf_ema(), htf_completed_at,
            self.swing_bias, self.internal_bias, dealing, self._session_name(bar), action,
            active_setup.id if active_setup else None,
            active_setup.phase.value if active_setup else None,
            self.next_required_event(active_setup), latest_sweep, tuple(ids),
            tuple(x.id for x in self.fvgs.values() if x.active),
            tuple(x.id for x in self.obs.values() if x.active),
        )

    def chart_objects(self) -> list[ChartObject]:
        rows = [ChartObject(x.id, x.id, "fvg", x.direction, x.created_at, x.top, x.bottom) for x in self.fvgs.values() if x.active]
        rows += [ChartObject(x.id, x.id, "order_block", x.direction, x.created_at, x.high, x.low) for x in self.obs.values() if x.active]
        rows += [ChartObject(x.id, x.id, f"{x.scope}_pivot_{x.kind}", "neutral", x.occurred_at, x.price, x.price) for x in self.pivots.values()]
        for event in self.events.values():
            if isinstance(event, StructureEvent):
                rows.append(ChartObject(event.id, event.id, event.event_type.lower(), event.direction, event.confirmed_at, event.level, event.level))
            else:
                rows.append(ChartObject(event.id, event.id, "liquidity_sweep", event.direction, event.timestamp, event.level, event.level))
        return rows

    def _pivot_strength(self, pivot: PivotPoint) -> str:
        protected = (self.protected_swing_high, self.protected_swing_low,
                     self.protected_internal_high, self.protected_internal_low)
        return "strong" if any(row and row.id == pivot.id for row in protected) else "weak"

    def next_required_event(self, setup: SMCSetup | None) -> str:
        if setup is None:
            return "Wait for confirmed HTF bias and liquidity sweep"
        labels = {
            SetupPhase.IDLE: "Wait liquidity sweep",
            SetupPhase.CONTEXT_VALID: "Wait liquidity sweep",
            SetupPhase.LIQUIDITY_SWEPT: f"Wait {setup.direction} CHoCH or BOS",
            SetupPhase.STRUCTURE_SHIFT_CONFIRMED: f"Wait {setup.direction} FVG",
            SetupPhase.POI_CREATED: "Wait POI retest",
            SetupPhase.WAITING_RETEST: "Wait POI retest and rejection",
            SetupPhase.REJECTION_CONFIRMED: "Research proposal is being recorded",
            SetupPhase.ENTRY_READY: "Proposal recorded — execution remains disabled",
            SetupPhase.INVALIDATED: "Setup invalidated; wait for a new sequence",
            SetupPhase.EXPIRED: "Setup expired; wait for a new sequence",
        }
        return labels[setup.phase]

    def visual_state(self, *, candle_at: datetime | None = None, candle_window: int = 400) -> dict:
        """The chart contract: raw engine objects plus presentation-neutral facts."""
        if candle_window < 20 or candle_window > 1_000:
            raise ValueError("candle_window must be between 20 and 1000")
        chart_bars = self.bars
        if candle_at is not None and self.bars:
            nearest = min(range(len(self.bars)), key=lambda index: abs(self.bars[index].timestamp - candle_at))
            start = max(0, nearest - candle_window // 2)
            chart_bars = self.bars[start:start + candle_window]
        else:
            chart_bars = self.bars[-candle_window:]
        state = self.public_state()
        state["pivots"] = [
            {**asdict(row), "strength": self._pivot_strength(row)} for row in self.pivots.values()
        ]
        state["fair_value_gaps"] = [asdict(row) for row in self.fvgs.values()]
        state["order_blocks"] = [asdict(row) for row in self.obs.values()]
        state["setups"] = [
            {**asdict(row), "next_required_event": self.next_required_event(row)} for row in self.setups.values()
        ]
        state["snapshot_ledger"] = [
            asdict(self.snapshots[row.timestamp]) for row in chart_bars if row.timestamp in self.snapshots
        ]
        state["selected_snapshot"] = (
            asdict(self.snapshots.get(candle_at)) if candle_at in self.snapshots else None
        )
        state["candles"] = [
            {"timestamp": row.timestamp, "open": row.open, "high": row.high, "low": row.low, "close": row.close, "volume": row.volume}
            for row in chart_bars
        ]
        return state

    def known_object_ids(self) -> set[str]:
        return set(self.pivots) | set(self.events) | set(self.fvgs) | set(self.obs) | set(self.setups) | set(self.proposals)

    def public_state(self) -> dict:
        return {"research_id": NATIVE_SMC_ID, "execution_allowed": False, "paper_execution_allowed": True, "config": asdict(self.config), "snapshot": asdict(self.latest_snapshot) if self.latest_snapshot else None, "setups": [asdict(x) for x in self.setups.values()], "events": [asdict(x) for x in self.events.values()], "proposals": [asdict(x) for x in self.proposals.values()], "chart_objects": [asdict(x) for x in self.chart_objects()]}

    def checkpoint(self) -> dict:
        """Durable, JSON-safe recovery payload; no order or authority is stored."""
        return {"version": 1, "config": asdict(self.config), "closed_bars": [
            {"timestamp": x.timestamp.isoformat(), "open": x.open, "high": x.high, "low": x.low, "close": x.close, "volume": x.volume}
            for x in self.bars]}

    @classmethod
    def restore_checkpoint(cls, payload: dict) -> "SMCMarketStructureEngine":
        if payload.get("version") != 1: raise ValueError("unsupported native SMC checkpoint")
        engine = cls(SMCConfig(**payload["config"]))
        for row in payload.get("closed_bars", []):
            engine.process_closed_bar(Bar(datetime.fromisoformat(row["timestamp"]), row["open"], row["high"], row["low"], row["close"], row["volume"]))
        return engine


_ENGINES: dict[tuple[str, str], SMCMarketStructureEngine] = {}


def _verified_checkpoint(symbol: str, timeframe: str) -> SMCMarketStructureEngine | None:
    """Load an operator-provided visual-review checkpoint without creating a worker.

    The path is opt-in and intentionally external to the source tree.  A
    checkpoint is accepted only for its declared symbol/timeframe and with
    execution permanently disabled by the model constructor.
    """
    path_value = os.environ.get("HUB_SMC_VISUAL_CHECKPOINT_PATH")
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_file():
        raise ValueError("HUB_SMC_VISUAL_CHECKPOINT_PATH does not point to a readable checkpoint")
    try:
        payload = json.loads(path.read_text())
        config = payload["config"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("HUB_SMC_VISUAL_CHECKPOINT_PATH is not a valid native SMC checkpoint") from exc
    if config.get("symbol", "").upper() != symbol or config.get("timeframe") != timeframe:
        return None
    return SMCMarketStructureEngine.restore_checkpoint(payload)


def research_engine(symbol: str = "BTCUSDT", timeframe: str = "5m") -> SMCMarketStructureEngine:
    """Return a non-running research model. Market workers must feed it explicitly."""
    key = (symbol.upper(), timeframe)
    if key not in _ENGINES:
        _ENGINES[key] = _verified_checkpoint(*key) or SMCMarketStructureEngine(SMCConfig(symbol=key[0], timeframe=timeframe))
    return _ENGINES[key]
