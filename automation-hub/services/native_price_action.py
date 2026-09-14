"""Deterministic native price-action research engine.

The engine consumes chronological *closed* OHLC candles and has no broker,
exchange-order, webhook, or live-trading dependency.  Volume is retained for
chart provenance only and is deliberately absent from every decision rule.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Iterable, Literal

from bot.types import Bar

RESEARCH_ID = "PRICE_ACTION_NATIVE_V1_RESEARCH"
STRATEGY_VERSION = "1.1.0"
EXECUTION_ALLOWED = False
STRATEGIES = (
    "PA1_SR_REJECTION",
    "PA2_TREND_PULLBACK",
    "PA3_FLIP_RETEST",
    "PA4_FALSE_BREAK_REVERSAL",
)
TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


class SetupPhase(str, Enum):
    WATCHING_LOCATION = "WATCHING_LOCATION"
    LOCATION_REACHED = "LOCATION_REACHED"
    REJECTION_DETECTED = "REJECTION_DETECTED"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    ORDER_PENDING = "ORDER_PENDING"
    ENTERED = "ENTERED"
    STOPPED = "STOPPED"
    TARGET_HIT = "TARGET_HIT"
    CANCELLED = "CANCELLED"
    INVALIDATED = "INVALIDATED"
    DATA_PAUSED = "DATA_PAUSED"
    LIQUIDATED_PAPER = "LIQUIDATED_PAPER"
    CLOSED_OTHER = "CLOSED_OTHER"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class PriceActionConfig:
    symbol: str
    timeframe: str = "5m"
    swing_left: int = 3
    swing_right: int = 3
    zone_atr_fraction: float = 0.18
    zone_min_bps: float = 6.0
    zone_expiry_bars: int = 300
    event_memory_bars: int = 8
    confusion_candles: int = 3
    setup_expiry_bars: int = 12
    entry_expiry_bars: int = 3
    rr_ratio: float = 2.5
    wick_body_ratio: float = 1.5
    commission_bps: float = 4.0
    spread_bps: float = 2.0
    slippage_bps: float = 3.0
    entry_model: str = "confirmation"
    stop_model: str = "rejection_extreme"
    equal_inside_boundaries: bool = True
    first_touch_only: bool = False
    trigger_filter: str = "generic_rejection"
    zone_timeframe_scope: str = "same_timeframe"
    execution_allowed: bool = False


@dataclass(frozen=True)
class ConfirmedSwing:
    id: str
    kind: Literal["high", "low"]
    price: float
    occurred_at: datetime
    confirmed_at: datetime
    occurred_index: int
    confirmed_index: int
    label: Literal["HH", "HL", "LH", "LL", "H", "L"]


@dataclass
class PriceZone:
    id: str
    role: Literal["support", "resistance"]
    original_role: Literal["support", "resistance"]
    low: float
    high: float
    created_at: datetime
    confirmed_at: datetime
    source_swing_ids: list[str]
    touch_count: int = 0
    #: Distinct visits to the zone: price arrives, leaves, and comes back.
    #: touch_count counts every bar whose range overlaps the zone, so a single
    #: approach-reject-confirm sequence already registers several touches --
    #: and the bars that formed the confirming swing overlap it before the zone
    #: is even active. Measured across every zone of a structured run, the
    #: minimum touch_count was 2 and the maximum 13; it was never 1. Any rule
    #: phrased as "the first interaction with this level" therefore has to key
    #: on visits, not on touches.
    visit_count: int = 0
    #: Whether the previous bar was inside the zone, so a re-entry can be told
    #: apart from simply remaining there.
    inside_zone: bool = False
    last_touch_at: datetime | None = None
    active: bool = True
    flipped: bool = False
    flipped_at: datetime | None = None
    invalidated_at: datetime | None = None
    wick_touches: int = 0
    body_interactions: int = 0
    closing_violations: int = 0
    successful_reactions: int = 0
    expiration_reason: str | None = None
    timeframe_scope: str = "same_timeframe"


@dataclass(frozen=True)
class PriceActionEvent:
    id: str
    event_type: str
    direction: Literal["bullish", "bearish", "neutral"]
    level: float
    occurred_at: datetime
    confirmed_at: datetime
    bar_index: int
    zone_id: str | None
    pattern: str | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SetupTransition:
    id: str
    setup_id: str
    from_phase: str
    to_phase: str
    timestamp: datetime
    reason: str
    object_id: str | None = None
    candle_identity: str | None = None
    market_data_health: str = "HISTORICAL_RECONCILED"
    reason_code: str = "STATE_TRANSITION"
    zone_id: str | None = None
    order_id: str | None = None
    position_id: str | None = None
    strategy_version: str = STRATEGY_VERSION
    configuration_fingerprint: str | None = None
    relevant_prices: dict = field(default_factory=dict)


@dataclass
class PriceActionSetup:
    id: str
    strategy_id: str
    direction: Literal["bullish", "bearish"]
    phase: SetupPhase
    created_at: datetime
    created_index: int
    zone_id: str | None
    trigger_event_id: str | None
    expires_index: int
    reasons: list[str]
    missing_conditions: list[str]
    invalidation_reason: str | None = None
    transitions: list[SetupTransition] = field(default_factory=list)
    pattern_metadata: list[dict] = field(default_factory=list)
    context_snapshot: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProposedTrade:
    id: str
    setup_id: str
    strategy_id: str
    direction: Literal["bullish", "bearish"]
    entry_model: str
    entry: float
    stop: float
    target: float
    risk_distance: float
    rr_ratio: float
    signal_at: datetime
    signal_index: int
    valid_until_index: int
    trigger_high: float
    trigger_low: float
    status: Literal["SIGNAL_ONLY"] = "SIGNAL_ONLY"
    execution_allowed: bool = False
    paper_execution_allowed: bool = True


@dataclass
class ResearchTrade:
    """Normalized research execution; it is not a broker order."""
    id: str
    proposal_id: str
    setup_id: str
    strategy_id: str
    direction: Literal["bullish", "bearish"]
    status: Literal["PENDING", "OPEN", "WON", "LOST", "EXPIRED", "CANCELLED", "INVALIDATED", "DATA_PAUSED"]
    requested_entry: float
    stop: float
    target: float
    created_at: datetime
    valid_until_index: int
    created_index: int = 0
    filled_at: datetime | None = None
    filled_index: int | None = None
    raw_fill_price: float | None = None
    fill_price: float | None = None
    closed_at: datetime | None = None
    closed_index: int | None = None
    exit_price: float | None = None
    raw_exit_price: float | None = None
    gross_r: float | None = None
    costs_r: float | None = None
    net_r: float | None = None
    outcome: str | None = None
    reason: str | None = None
    execution_model: str = "conservative_ohlc_adverse_first"
    entry_model: str = "confirmation"
    stop_model: str = "rejection_extreme"
    config_snapshot: dict = field(default_factory=dict)
    intrabar_ambiguous: bool = False
    maximum_favourable_excursion_r: float | None = None
    maximum_adverse_excursion_r: float | None = None
    bars_to_entry: int | None = None
    bars_in_trade: int | None = None
    excursion_model: str = "completed_ohlc_conservative"
    live_execution_allowed: bool = False


@dataclass(frozen=True)
class StrategyTrace:
    strategy_id: str
    direction: Literal["bullish", "bearish"]
    state: str
    conditions: tuple[dict, ...]
    missing_conditions: tuple[str, ...]
    supporting_object_ids: tuple[str, ...]
    setup_id: str | None
    next_required_event: str


@dataclass(frozen=True)
class PriceActionSnapshot:
    id: str
    symbol: str
    timeframe: str
    candle_open: datetime
    candle_close: datetime
    structure_bias: Literal["bullish", "bearish", "neutral"]
    latest_high_label: str | None
    latest_low_label: str | None
    pattern: str | None
    patterns: tuple[dict, ...]
    event_ids: tuple[str, ...]
    active_zone_ids: tuple[str, ...]
    setup_ids: tuple[str, ...]
    proposal_ids: tuple[str, ...]
    strategy_traces: tuple[StrategyTrace, ...]


def _id(kind: str, *parts: object) -> str:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]
    return f"pa-{kind}-{digest}"


def _atr(bars: list[Bar], length: int = 14) -> float:
    rows = bars[-length:]
    if not rows:
        return 0.0
    prior = rows[0].close
    values: list[float] = []
    for row in rows:
        values.append(max(row.high - row.low, abs(row.high - prior), abs(row.low - prior)))
        prior = row.close
    return sum(values) / len(values)


class NativePriceActionEngine:
    """Closed-candle, idempotent PA1–PA4 research state machine."""

    def __init__(self, config: PriceActionConfig):
        if config.execution_allowed or EXECUTION_ALLOWED:
            raise ValueError("native Price Action research cannot enable live execution")
        if config.timeframe not in TF_SECONDS:
            raise ValueError(f"unsupported timeframe '{config.timeframe}'")
        if min(config.swing_left, config.swing_right) < 1:
            raise ValueError("swing confirmation windows must be positive")
        if config.zone_timeframe_scope not in {"same_timeframe", "higher_timeframe"}:
            raise ValueError("zone_timeframe_scope must be same_timeframe or higher_timeframe")
        if config.entry_model not in {"confirmation", "close", "retracement_50"}:
            raise ValueError("unsupported Price Action entry model")
        if config.stop_model not in {"rejection_extreme", "pattern", "structural_zone"}:
            raise ValueError("unsupported Price Action stop model")
        if config.trigger_filter not in {"generic_rejection", "pin_bar_only"}:
            raise ValueError("trigger_filter must be generic_rejection or pin_bar_only")
        if not 0 <= config.confusion_candles <= 3:
            raise ValueError("confusion_candles must be between zero and three")
        self.config = config
        self.configuration_fingerprint = hashlib.sha256(
            json.dumps(asdict(config), sort_keys=True, default=str).encode()).hexdigest()
        self._market_data_health = "HISTORICAL_RECONCILED"
        self.bars: list[Bar] = []
        self.processed: set[datetime] = set()
        self.swings: dict[str, ConfirmedSwing] = {}
        self.zones: dict[str, PriceZone] = {}
        self.events: dict[str, PriceActionEvent] = {}
        self.setups: dict[str, PriceActionSetup] = {}
        self.proposals: dict[str, ProposedTrade] = {}
        self.research_trades: dict[str, ResearchTrade] = {}
        self.snapshots: dict[datetime, PriceActionSnapshot] = {}
        self._zone_snapshots: dict[datetime, tuple[dict, ...]] = {}
        self.latest_snapshot: PriceActionSnapshot | None = None
        self._last_high: ConfirmedSwing | None = None
        self._last_low: ConfirmedSwing | None = None
        self._breakouts: dict[str, PriceActionEvent] = {}
        self._htf_engine: NativePriceActionEngine | None = None
        self._native_mtf_context: dict[str, list[Bar]] = {}
        self._mtf_evidence: dict = {}
        self._htf_processed: set[datetime] = set()

    def set_native_mtf_context(self, context: dict[str, list[Bar]]) -> None:
        """Supply provider-native policy candles; no LTF aggregation is allowed."""
        from services.mtf_policy import native_timeframes
        expected = native_timeframes(self.config.timeframe)
        self._native_mtf_context = {timeframe: list(context.get(timeframe, ()))
                                    for timeframe in expected}

    def process_closed_bar(self, bar: Bar, *,
                           market_data_health: str = "HISTORICAL_RECONCILED") -> PriceActionSnapshot:
        if bar.timestamp in self.processed:
            if self.latest_snapshot is None:
                raise RuntimeError("duplicate candle before first snapshot")
            return self.snapshots[bar.timestamp]
        if self.bars and bar.timestamp <= self.bars[-1].timestamp:
            raise ValueError("candles must be unique and chronological")
        if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
            raise ValueError("invalid OHLC candle")

        self._market_data_health = str(market_data_health or "UNKNOWN")
        self.processed.add(bar.timestamp)
        self.bars.append(bar)
        index = len(self.bars) - 1
        self._advance_higher_timeframe(bar)
        self._confirm_swings(index)
        self._expire_zones(index)
        self._expire_setups(index)
        self._advance_research_trades(bar, index)
        bar_events = self._detect_events(bar, index)
        traces, setup_ids, proposal_ids = self._evaluate_strategies(bar, index, bar_events)
        snapshot = PriceActionSnapshot(
            id=_id("snapshot", self.config.symbol, self.config.timeframe, bar.timestamp),
            symbol=self.config.symbol,
            timeframe=self.config.timeframe,
            candle_open=bar.timestamp,
            candle_close=bar.timestamp + timedelta(seconds=TF_SECONDS[self.config.timeframe]),
            structure_bias=self.structure_bias,
            latest_high_label=self._last_high.label if self._last_high else None,
            latest_low_label=self._last_low.label if self._last_low else None,
            pattern=self._pattern(bar, index),
            patterns=tuple(self._patterns(bar, index)),
            event_ids=tuple(row.id for row in bar_events),
            active_zone_ids=tuple(row.id for row in self.zones.values() if row.active),
            setup_ids=tuple(setup_ids),
            proposal_ids=tuple(proposal_ids),
            strategy_traces=tuple(traces),
        )
        self.snapshots[bar.timestamp] = snapshot
        # Zones are mutable (touch, flip, invalidation).  Preserve their exact
        # per-candle projection so replaying an older cursor cannot reveal a
        # role flip or invalidation that happened later.
        self._zone_snapshots[bar.timestamp] = tuple(asdict(row) for row in self.zones.values())
        self.latest_snapshot = snapshot
        return snapshot

    def ingest_closed_bars(self, bars: Iterable[Bar], *,
                           market_data_health: str = "HISTORICAL_RECONCILED") -> list[PriceActionSnapshot]:
        return [self.process_closed_bar(row, market_data_health=market_data_health) for row in bars]

    @property
    def structure_bias(self) -> Literal["bullish", "bearish", "neutral"]:
        if self._last_high and self._last_low:
            if self._last_high.label == "HH" and self._last_low.label == "HL":
                return "bullish"
            if self._last_high.label == "LH" and self._last_low.label == "LL":
                return "bearish"
        return "neutral"

    def _confirm_swings(self, index: int) -> None:
        right = self.config.swing_right
        candidate_index = index - right
        if candidate_index < self.config.swing_left:
            return
        candidate = self.bars[candidate_index]
        left = self.bars[candidate_index - self.config.swing_left:candidate_index]
        after = self.bars[candidate_index + 1:index + 1]
        confirmation_time = self.bars[index].timestamp + timedelta(seconds=TF_SECONDS[self.config.timeframe])
        if candidate.high > max(row.high for row in left + after):
            label = "H" if self._last_high is None else "HH" if candidate.high > self._last_high.price else "LH"
            swing = ConfirmedSwing(
                _id("swing", "high", candidate.timestamp, candidate.high), "high", candidate.high,
                candidate.timestamp, confirmation_time, candidate_index, index, label,
            )
            self.swings[swing.id] = swing
            self._last_high = swing
            self._upsert_zone(swing, candidate, "resistance")
        if candidate.low < min(row.low for row in left + after):
            label = "L" if self._last_low is None else "HL" if candidate.low > self._last_low.price else "LL"
            swing = ConfirmedSwing(
                _id("swing", "low", candidate.timestamp, candidate.low), "low", candidate.low,
                candidate.timestamp, confirmation_time, candidate_index, index, label,
            )
            self.swings[swing.id] = swing
            self._last_low = swing
            self._upsert_zone(swing, candidate, "support")

    def _advance_higher_timeframe(self, bar: Bar) -> None:
        from services.mtf_policy import (
            candle_close, closed_native_bars, evidence_at, policy_for,
        )
        decision_time = candle_close(bar, self.config.timeframe)
        self._mtf_evidence = evidence_at(
            self.config.symbol, self.config.timeframe,
            self._native_mtf_context, decision_time,
        )
        if self.config.zone_timeframe_scope != "higher_timeframe":
            return
        label, _secondary = policy_for(self.config.timeframe)
        rows = closed_native_bars(
            self._native_mtf_context.get(label, ()), label, decision_time,
        )
        if not rows:
            return
        if self._htf_engine is None:
            self._htf_engine = NativePriceActionEngine(replace(
                self.config, symbol=self.config.symbol, timeframe=label,
                zone_timeframe_scope="same_timeframe"))
        for complete in rows:
            if complete.timestamp in self._htf_processed:
                continue
            self._htf_engine.process_closed_bar(complete)
            self._htf_processed.add(complete.timestamp)
        for source in self._htf_engine.zones.values():
            if source.id not in self.zones:
                payload = asdict(source)
                payload["timeframe_scope"] = f"higher_timeframe:{label}"
                self.zones[source.id] = PriceZone(**payload)

    def _upsert_zone(self, swing: ConfirmedSwing, source: Bar, role: Literal["support", "resistance"]) -> None:
        if self.config.zone_timeframe_scope == "higher_timeframe":
            return
        width = max(_atr(self.bars) * self.config.zone_atr_fraction,
                    swing.price * self.config.zone_min_bps / 10_000)
        if role == "support":
            low, high = swing.price, min(max(source.open, source.close), swing.price + width)
            high = max(high, low + width * .35)
        else:
            high, low = swing.price, max(min(source.open, source.close), swing.price - width)
            low = min(low, high - width * .35)
        for zone in reversed(list(self.zones.values())):
            if zone.active and zone.role == role and not (high < zone.low - width or low > zone.high + width):
                zone.low = min(zone.low, low)
                zone.high = max(zone.high, high)
                zone.source_swing_ids.append(swing.id)
                return
        zone = PriceZone(
            id=_id("zone", role, swing.occurred_at, round(low, 10), round(high, 10)),
            role=role, original_role=role, low=low, high=high, created_at=swing.occurred_at,
            confirmed_at=swing.confirmed_at, source_swing_ids=[swing.id],
        )
        self.zones[zone.id] = zone

    def _expire_zones(self, index: int) -> None:
        occurred_index = {s.id: s.occurred_index for s in self.swings.values()}
        for zone in self.zones.values():
            origins = [occurred_index[sid] for sid in zone.source_swing_ids if sid in occurred_index]
            if origins:
                origin = min(origins)
            else:
                # Higher-timeframe zones retain the source engine's swing IDs,
                # which deliberately are not copied into the base-timeframe
                # engine.  Anchor their age to the first base candle at or
                # after confirmation so they obey the same deterministic age
                # policy instead of silently living forever.
                origin = next(
                    (bar_index for bar_index, row in enumerate(self.bars)
                     if row.timestamp >= zone.confirmed_at),
                    index,
                )
            if zone.active and index - origin > self.config.zone_expiry_bars:
                zone.active = False
                zone.invalidated_at = self.bars[index].timestamp
                zone.expiration_reason = "maximum_age"

    def _expire_setups(self, index: int) -> None:
        terminal = {SetupPhase.STOPPED, SetupPhase.TARGET_HIT, SetupPhase.CANCELLED,
                    SetupPhase.EXPIRED, SetupPhase.INVALIDATED, SetupPhase.DATA_PAUSED,
                    SetupPhase.LIQUIDATED_PAPER, SetupPhase.CLOSED_OTHER}
        for setup in self.setups.values():
            if setup.phase in terminal:
                continue
            zone = self.zones.get(setup.zone_id or "")
            if zone is not None and not zone.active and setup.phase != SetupPhase.ENTERED:
                previous = setup.phase
                setup.phase = SetupPhase.INVALIDATED
                setup.invalidation_reason = zone.expiration_reason or "zone invalidated"
                setup.transitions.append(SetupTransition(
                    _id("transition", setup.id, "INVALIDATED", self.bars[index].timestamp), setup.id,
                    previous.value, SetupPhase.INVALIDATED.value, self.bars[index].timestamp,
                    setup.invalidation_reason, zone.id,
                    candle_identity=self.bars[index].timestamp.isoformat(),
                    market_data_health=self._market_data_health,
                    reason_code="ZONE_TERMINAL", zone_id=zone.id,
                    configuration_fingerprint=self.configuration_fingerprint))
            elif index > setup.expires_index and setup.phase == SetupPhase.WAITING_FOR_CONFIRMATION:
                previous = setup.phase
                setup.phase = SetupPhase.EXPIRED
                setup.transitions.append(SetupTransition(
                    _id("transition", setup.id, "EXPIRED", self.bars[index].timestamp), setup.id,
                    previous.value, SetupPhase.EXPIRED.value, self.bars[index].timestamp,
                    "setup confirmation window expired", setup.trigger_event_id,
                    candle_identity=self.bars[index].timestamp.isoformat(),
                    market_data_health=self._market_data_health,
                    reason_code="SETUP_WINDOW_EXPIRED", zone_id=setup.zone_id,
                    configuration_fingerprint=self.configuration_fingerprint))

    def _pattern(self, bar: Bar, index: int) -> str | None:
        rows = self._patterns(bar, index)
        return rows[0]["name"] if rows else None

    def _pin_direction(self, bar: Bar) -> str | None:
        body = max(abs(bar.close - bar.open), (bar.high - bar.low) * .02)
        upper = bar.high - max(bar.open, bar.close)
        lower = min(bar.open, bar.close) - bar.low
        if lower >= body * self.config.wick_body_ratio and upper <= body:
            return "bullish"
        if upper >= body * self.config.wick_body_ratio and lower <= body:
            return "bearish"
        return None

    def _patterns(self, bar: Bar, index: int) -> list[dict]:
        """Return named candle features as metadata; never as a PA1 gate."""
        out: list[dict] = []
        body = max(abs(bar.close - bar.open), (bar.high - bar.low) * .02)
        upper = bar.high - max(bar.open, bar.close)
        lower = min(bar.open, bar.close) - bar.low
        if lower >= body * self.config.wick_body_ratio and upper <= body:
            out.append({"name": "bullish_pin_bar", "direction": "bullish", "mother_at": None})
        if upper >= body * self.config.wick_body_ratio and lower <= body:
            out.append({"name": "bearish_pin_bar", "direction": "bearish", "mother_at": None})
        if index:
            prior = self.bars[index - 1]
            inside = (bar.high <= prior.high and bar.low >= prior.low) if self.config.equal_inside_boundaries else \
                     (bar.high < prior.high and bar.low > prior.low)
            if inside:
                out.append({"name": "inside_bar", "direction": "neutral",
                            "mother_at": prior.timestamp, "mother_high": prior.high, "mother_low": prior.low})
                out.append({"name": "mother_bar_reference", "direction": "neutral",
                            "mother_at": prior.timestamp})
                if any(row["name"].endswith("pin_bar") for row in out):
                    direction = next(row["direction"] for row in out if row["name"].endswith("pin_bar"))
                    out.append({"name": "inside_pin_bar", "direction": direction, "mother_at": prior.timestamp})
                prior_pin = self._pin_direction(prior)
                if prior_pin:
                    out.append({"name": "pin_plus_inside_bar", "direction": prior_pin,
                                "mother_at": prior.timestamp})
            if bar.high > prior.high and bar.low < prior.low:
                out.append({"name": "outside_bar", "direction": "neutral", "mother_at": prior.timestamp})
            if bar.close > bar.open and prior.close < prior.open and bar.close >= prior.open and bar.open <= prior.close:
                out.append({"name": "bullish_engulfing", "direction": "bullish", "mother_at": prior.timestamp})
            if bar.close < bar.open and prior.close > prior.open and bar.open >= prior.close and bar.close <= prior.open:
                out.append({"name": "bearish_engulfing", "direction": "bearish", "mother_at": prior.timestamp})
            current_pin = self._pin_direction(bar)
            prior_pin = self._pin_direction(prior)
            tolerance = max(_atr(self.bars) * .25, bar.close * .0002)
            if current_pin and prior_pin and current_pin == prior_pin:
                same_level = abs((bar.low if current_pin == "bullish" else bar.high) -
                                 (prior.low if prior_pin == "bullish" else prior.high)) <= tolerance
                if same_level:
                    out.append({"name": "double_pin_bar", "direction": current_pin,
                                "mother_at": prior.timestamp})
        # Fakey: one or more inside bars followed by a failed mother-range break.
        if index >= 2:
            for mother_index in range(max(0, index - 4), index - 1):
                mother = self.bars[mother_index]
                inside_rows = self.bars[mother_index + 1:index]
                if not inside_rows or not all(row.high <= mother.high and row.low >= mother.low for row in inside_rows):
                    continue
                bullish = bar.low < mother.low and bar.close >= mother.low and bar.close <= mother.high
                bearish = bar.high > mother.high and bar.close <= mother.high and bar.close >= mother.low
                if bullish or bearish:
                    direction = "bullish" if bullish else "bearish"
                    pin = any(row["name"] == f"{direction}_pin_bar" for row in out)
                    variant = "one_bar" if len(inside_rows) == 1 else "multi_inside"
                    out.append({"name": f"{direction}_{variant}_{'pin_' if pin else ''}fakey",
                                "direction": direction, "mother_at": mother.timestamp,
                                "inside_count": len(inside_rows)})
                    break
        return out

    @staticmethod
    def _dominance(bar: Bar, direction: Literal["bullish", "bearish"]) -> bool:
        """Closed-candle dominance used by the frozen default entry model."""
        span = bar.high - bar.low
        if span <= 0 or abs(bar.close - bar.open) / span < .5:
            return False
        if direction == "bullish":
            return bar.close > bar.open and bar.close >= bar.low + span * .7
        return bar.close < bar.open and bar.close <= bar.high - span * .7

    def _event(self, event_type: str, direction: Literal["bullish", "bearish", "neutral"], level: float,
               bar: Bar, index: int, zone: PriceZone | None, *reasons: str) -> PriceActionEvent:
        row = PriceActionEvent(
            id=_id("event", event_type, direction, bar.timestamp, zone.id if zone else level),
            event_type=event_type, direction=direction, level=level, occurred_at=bar.timestamp,
            confirmed_at=bar.timestamp + timedelta(seconds=TF_SECONDS[self.config.timeframe]),
            bar_index=index, zone_id=zone.id if zone else None, pattern=self._pattern(bar, index),
            reasons=tuple(reasons),
        )
        self.events[row.id] = row
        return row

    def _detect_events(self, bar: Bar, index: int) -> list[PriceActionEvent]:
        prior = self.bars[index - 1] if index else None
        out: list[PriceActionEvent] = []
        for zone in list(self.zones.values()):
            # A zone confirmed by this candle only becomes an eligible input
            # after that candle closes; it cannot explain its own intrabar path.
            if not zone.active or zone.confirmed_at > bar.timestamp:
                continue
            touched = bar.low <= zone.high and bar.high >= zone.low
            if touched and not zone.inside_zone:
                # Price has arrived from outside: a new visit, not a further
                # bar of the visit already in progress.
                zone.visit_count += 1
            zone.inside_zone = touched
            if touched:
                zone.touch_count += 1
                zone.last_touch_at = bar.timestamp
                zone.wick_touches += int(bar.low < zone.low or bar.high > zone.high)
                zone.body_interactions += int(min(bar.open, bar.close) <= zone.high and max(bar.open, bar.close) >= zone.low)
            midpoint = (zone.low + zone.high) / 2
            if zone.role == "support":
                broke = bar.close < zone.low and (prior is None or prior.close >= zone.low)
                rejected = touched and bar.close > zone.high and bar.close >= bar.open
                direction: Literal["bullish", "bearish"] = "bullish"
            else:
                broke = bar.close > zone.high and (prior is None or prior.close <= zone.high)
                rejected = touched and bar.close < zone.low and bar.close <= bar.open
                direction = "bearish"
            if rejected:
                zone.successful_reactions += 1
                out.append(self._event("zone_rejection", direction, midpoint, bar, index, zone,
                                       f"closed away from {zone.role} after trading inside the zone"))
            breakout = self._breakouts.get(zone.id)
            if breakout and index > breakout.bar_index:
                age = index - breakout.bar_index
                failed = ((zone.role == "support" and bar.close < zone.low)
                          or (zone.role == "resistance" and bar.close > zone.high))
                if age <= self.config.event_memory_bars and failed:
                    reversal: Literal["bullish", "bearish"] = "bearish" if breakout.direction == "bullish" else "bullish"
                    out.append(self._event("false_break_reclaim", reversal, midpoint, bar, index, zone,
                                           "price closed back through the broken zone within the false-break window"))
                    zone.role = zone.original_role
                    zone.flipped = False
                    zone.flipped_at = None
                    self._breakouts.pop(zone.id, None)
                    continue
                if age <= self.config.event_memory_bars and touched:
                    expected: Literal["bullish", "bearish"] = "bullish" if zone.role == "support" else "bearish"
                    held = bar.close > zone.high if zone.role == "support" else bar.close < zone.low
                    if held:
                        out.append(self._event("flip_retest", expected, midpoint, bar, index, zone,
                                               f"flipped {zone.role} held on closed-candle retest"))
                    continue
                if age > self.config.event_memory_bars:
                    self._breakouts.pop(zone.id, None)
            if broke:
                zone.closing_violations += 1
                breakout_direction: Literal["bullish", "bearish"] = "bearish" if zone.role == "support" else "bullish"
                event = self._event("confirmed_breakout", breakout_direction, midpoint, bar, index, zone,
                                    "closed beyond the complete zone boundary")
                out.append(event)
                self._breakouts[zone.id] = event
                zone.role = "resistance" if zone.role == "support" else "support"
                zone.flipped = True
                zone.flipped_at = bar.timestamp
                out.append(self._event("role_flip", breakout_direction, midpoint, bar, index, zone,
                                       f"zone now acts as {zone.role}"))
                continue
        return out

    @staticmethod
    def _condition(key: str, passed: bool, detail: str, object_id: str | None = None) -> dict:
        return {"key": key, "status": "PASS" if passed else "MISSING", "detail": detail, "object_id": object_id}

    def _evaluate_strategies(self, bar: Bar, index: int, events: list[PriceActionEvent]) -> tuple[list[StrategyTrace], list[str], list[str]]:
        traces: list[StrategyTrace] = []
        setup_ids: list[str] = []
        proposal_ids: list[str] = []
        bias = self.structure_bias
        for strategy in STRATEGIES:
            for direction in ("bullish", "bearish"):
                matching = [row for row in self.events.values()
                            if row.direction == direction and
                            0 <= index - row.bar_index <= self.config.confusion_candles]
                if strategy == "PA1_SR_REJECTION":
                    trigger = next((row for row in reversed(matching) if row.event_type == "zone_rejection"), None)
                    zone = self.zones.get(trigger.zone_id or "") if trigger else None
                    directional_zone = bool(zone and (
                        (direction == "bullish" and zone.role == "support") or
                        (direction == "bearish" and zone.role == "resistance")))
                    conditions = [
                        self._condition("zone", directional_zone, "confirmed directional support/resistance zone is present", zone.id if zone else None),
                        self._condition("rejection", trigger is not None, "closed-candle rejection is required", trigger.id if trigger else None),
                    ]
                    next_event = "Wait for a directional closed-candle rejection at a confirmed zone"
                elif strategy == "PA2_TREND_PULLBACK":
                    trigger = next((row for row in reversed(matching) if row.event_type == "zone_rejection"), None)
                    zone = self.zones.get(trigger.zone_id or "") if trigger else None
                    trend_ok = bias == direction
                    directional_zone = bool(zone and (
                        (direction == "bullish" and zone.role == "support") or
                        (direction == "bearish" and zone.role == "resistance")))
                    conditions = [
                        self._condition("trend", trend_ok, f"confirmed HH/HL or LH/LL structure must be {direction}"),
                        self._condition("pullback_zone", directional_zone, "pullback must reach a directional confirmed zone", zone.id if zone else None),
                        self._condition("pullback_rejection", trigger is not None, "pullback must reject on a closed candle", trigger.id if trigger else None),
                    ]
                    next_event = "Wait for a closed-candle pullback rejection aligned with confirmed structure"
                elif strategy == "PA3_FLIP_RETEST":
                    trigger = next((row for row in reversed(matching) if row.event_type == "flip_retest"), None)
                    zone = self.zones.get(trigger.zone_id or "") if trigger else None
                    conditions = [
                        self._condition("role_flip", bool(zone and zone.flipped), "a confirmed support/resistance role flip is required", zone.id if zone else None),
                        self._condition("retest", trigger is not None, "the flipped zone must hold on a later closed candle", trigger.id if trigger else None),
                    ]
                    next_event = "Wait for a closed-candle retest of a confirmed role flip"
                else:
                    trigger = next((row for row in reversed(matching) if row.event_type == "false_break_reclaim"), None)
                    zone = self.zones.get(trigger.zone_id or "") if trigger else None
                    conditions = [
                        self._condition("false_break", trigger is not None, "breakout must fail and reclaim within the configured window", trigger.id if trigger else None),
                        self._condition("reversal_close", trigger is not None, "reversal is confirmed only by the closing price", trigger.id if trigger else None),
                    ]
                    next_event = "Wait for a failed breakout and closed-candle reclaim"
                trigger_patterns = []
                if trigger is not None:
                    trigger_patterns = self._patterns(self.bars[trigger.bar_index], trigger.bar_index)
                if self.config.trigger_filter == "pin_bar_only" and strategy in {
                        "PA1_SR_REJECTION", "PA2_TREND_PULLBACK"}:
                    pin = next((row for row in trigger_patterns
                                if row["name"] in {"bullish_pin_bar", "bearish_pin_bar"}
                                and row["direction"] == direction), None)
                    conditions.append(self._condition(
                        "pin_bar_only", pin is not None,
                        "isolated experiment requires the generic rejection candle to classify as a directional pin bar",
                    ))
                if self.config.first_touch_only:
                    # visit_count, not touch_count: the latter counts bars
                    # overlapping the zone and is never below 2 by the time a
                    # setup can exist, which made this switch unsatisfiable and
                    # silently produced zero proposals whenever it was enabled.
                    conditions.append(self._condition(
                        "first_touch_only", bool(zone and zone.visit_count <= 1),
                        "experiment accepts only the first distinct visit to the zone",
                        zone.id if zone else None))
                missing = [row["key"] for row in conditions if row["status"] != "PASS"]
                supporting = tuple(row["object_id"] for row in conditions if row.get("object_id"))
                setup_id = None
                if trigger is not None and not missing:
                    setup_id = _id("setup", strategy, direction, trigger.id)
                    setup = self.setups.get(setup_id)
                    if setup is None:
                        setup = PriceActionSetup(
                            id=setup_id, strategy_id=strategy, direction=direction,
                            phase=SetupPhase.WAITING_FOR_CONFIRMATION,
                            created_at=trigger.occurred_at, created_index=trigger.bar_index,
                            zone_id=trigger.zone_id, trigger_event_id=trigger.id,
                            expires_index=trigger.bar_index + self.config.setup_expiry_bars,
                            reasons=[row["detail"] for row in conditions], missing_conditions=list(missing),
                            pattern_metadata=trigger_patterns,
                            context_snapshot={
                                "structure_state": bias,
                                "relevant_swings": [
                                    asdict(row) for row in self.swings.values()
                                    if row.id in (zone.source_swing_ids if zone else [])
                                ],
                                "zone": asdict(zone) if zone else None,
                                "zone_age": (
                                    trigger.bar_index - next(
                                        (bar_index for bar_index, row in enumerate(self.bars)
                                         if zone and row.timestamp >= zone.confirmed_at),
                                        trigger.bar_index,
                                    ) if zone else None
                                ),
                                "trigger_event": asdict(trigger),
                                "market_data_health": self._market_data_health,
                                "decision_candle": trigger.occurred_at.isoformat(),
                            },
                        )
                        chain = (
                            (SetupPhase.WATCHING_LOCATION, SetupPhase.LOCATION_REACHED,
                             "price reached a confirmed structural zone", setup.zone_id),
                            (SetupPhase.LOCATION_REACHED, SetupPhase.REJECTION_DETECTED,
                             "closed-candle rejection or reclaim was detected", trigger.id),
                            (SetupPhase.REJECTION_DETECTED, SetupPhase.WAITING_FOR_CONFIRMATION,
                             "reaction is closed; confirmation must occur on a later candle", trigger.id),
                        )
                        for previous, current, reason, object_id in chain:
                            setup.transitions.append(SetupTransition(
                                _id("transition", setup_id, current.value, trigger.occurred_at), setup_id,
                                previous.value, current.value, trigger.occurred_at, reason, object_id,
                                candle_identity=trigger.occurred_at.isoformat(), zone_id=setup.zone_id,
                                market_data_health=self._market_data_health,
                                reason_code=current.value,
                                configuration_fingerprint=self.configuration_fingerprint))
                        self.setups[setup_id] = setup
                    setup_ids.append(setup_id)
                    existing = next((row for row in self.proposals.values() if row.setup_id == setup_id), None)
                    proposal = existing or self._propose(setup, trigger, self.bars[trigger.bar_index])
                    if proposal is not None and proposal.id not in self.proposals:
                        self.proposals[proposal.id] = proposal
                        trade = ResearchTrade(
                            id=_id("research-trade", proposal.id), proposal_id=proposal.id,
                            setup_id=proposal.setup_id, strategy_id=proposal.strategy_id,
                            direction=proposal.direction, status="PENDING",
                            requested_entry=proposal.entry, stop=proposal.stop, target=proposal.target,
                            created_at=proposal.signal_at, created_index=proposal.signal_index,
                            valid_until_index=proposal.valid_until_index,
                            entry_model=proposal.entry_model, stop_model=self.config.stop_model,
                            config_snapshot=asdict(self.config),
                        )
                        self.research_trades[trade.id] = trade
                        setup.transitions.append(SetupTransition(
                            _id("transition", setup_id, "ORDER_PENDING", trigger.occurred_at), setup.id,
                            SetupPhase.WAITING_FOR_CONFIRMATION.value, SetupPhase.ORDER_PENDING.value,
                            trigger.occurred_at,
                            "confirmation stop staged beyond the completed reaction boundary", trade.id,
                            candle_identity=trigger.occurred_at.isoformat(), zone_id=setup.zone_id,
                            order_id=trade.id, market_data_health=self._market_data_health,
                            reason_code="CONFIRMATION_ORDER_STAGED",
                            configuration_fingerprint=self.configuration_fingerprint,
                            relevant_prices={"entry": proposal.entry, "stop": proposal.stop,
                                             "target": proposal.target}))
                        setup.phase = SetupPhase.ORDER_PENDING
                    if proposal is not None:
                        proposal_ids.append(proposal.id)
                traces.append(StrategyTrace(
                    strategy_id=strategy, direction=direction,
                    state="ORDER_PENDING" if setup_id else "WATCHING",
                    conditions=tuple(conditions), missing_conditions=tuple(missing),
                    supporting_object_ids=supporting, setup_id=setup_id,
                    next_required_event=("Wait for a later candle to cross the confirmation stop; paper only"
                                         if setup_id else next_event),
                ))
        return traces, setup_ids, proposal_ids

    def _propose(self, setup: PriceActionSetup, trigger: PriceActionEvent, bar: Bar) -> ProposedTrade:
        zone = self.zones.get(trigger.zone_id or "")
        pad = max(_atr(self.bars) * .08, bar.close * 2 / 10_000)
        pattern_boundary = None
        if self.config.stop_model == "pattern":
            mother_at = next((row.get("mother_at") for row in self._patterns(bar, len(self.bars) - 1)
                              if row.get("mother_at")), None)
            mother = next((row for row in self.bars if row.timestamp == mother_at), None)
            if mother:
                pattern_boundary = mother.low if setup.direction == "bullish" else mother.high
        if setup.direction == "bullish":
            boundary = (zone.low if self.config.stop_model == "structural_zone" and zone else
                        pattern_boundary if pattern_boundary is not None else bar.low)
            stop = boundary - pad
            entry = (bar.close if self.config.entry_model == "close" else
                     (bar.high + bar.low) / 2 if self.config.entry_model == "retracement_50" else bar.high + pad)
            risk = entry - stop
            target = entry + risk * self.config.rr_ratio
        else:
            boundary = (zone.high if self.config.stop_model == "structural_zone" and zone else
                        pattern_boundary if pattern_boundary is not None else bar.high)
            stop = boundary + pad
            entry = (bar.close if self.config.entry_model == "close" else
                     (bar.high + bar.low) / 2 if self.config.entry_model == "retracement_50" else bar.low - pad)
            risk = stop - entry
            target = entry - risk * self.config.rr_ratio
        return ProposedTrade(
            id=_id("proposal", setup.id, bar.timestamp, round(entry, 10)), setup_id=setup.id,
            strategy_id=setup.strategy_id, direction=setup.direction,
            entry_model=self.config.entry_model, entry=entry, stop=stop, target=target,
            risk_distance=risk, rr_ratio=self.config.rr_ratio, signal_at=bar.timestamp,
            signal_index=len(self.bars) - 1,
            valid_until_index=len(self.bars) - 1 + self.config.entry_expiry_bars,
            trigger_high=bar.high, trigger_low=bar.low,
        )

    def _advance_research_trades(self, bar: Bar, index: int) -> None:
        """Advance normalized research orders with conservative OHLC rules."""
        slip = (self.config.slippage_bps + self.config.spread_bps / 2) / 10_000
        fee = self.config.commission_bps / 10_000
        for trade in self.research_trades.values():
            just_filled = False
            if trade.status == "PENDING":
                setup = self.setups.get(trade.setup_id)
                zone = self.zones.get(setup.zone_id or "") if setup else None
                if setup and setup.phase in {
                        SetupPhase.INVALIDATED, SetupPhase.CANCELLED, SetupPhase.DATA_PAUSED}:
                    trade.status = setup.phase.value
                    trade.closed_at = bar.timestamp
                    trade.outcome = "UNFILLED"
                    trade.reason = setup.invalidation_reason or "setup became terminal before activation"
                    continue
                if index > trade.valid_until_index:
                    trade.status = "EXPIRED"
                    trade.closed_at = bar.timestamp
                    trade.outcome = "UNFILLED"
                    trade.reason = "confirmation order expired after three completed candles"
                    setup = self.setups.get(trade.setup_id)
                    if setup:
                        setup.phase = SetupPhase.EXPIRED
                        setup.transitions.append(SetupTransition(
                            _id("transition", setup.id, "EXPIRED", bar.timestamp), setup.id,
                            SetupPhase.ORDER_PENDING.value, SetupPhase.EXPIRED.value, bar.timestamp,
                            trade.reason, trade.id, candle_identity=bar.timestamp.isoformat(),
                            zone_id=setup.zone_id, order_id=trade.id,
                            market_data_health=self._market_data_health,
                            reason_code="PENDING_DURATION_EXPIRED",
                            configuration_fingerprint=self.configuration_fingerprint))
                    continue
                # The stop is the saved structural invalidation boundary.  If
                # both entry and invalidation occur inside one OHLC bar, their
                # sequence is unknowable and the adverse invalidation is
                # applied first; an unfilled stop order is cancelled, not
                # invented as a fill-and-loss.
                stop_breached = (bar.low <= trade.stop if trade.direction == "bullish"
                                 else bar.high >= trade.stop)
                if stop_breached:
                    trade.status = "INVALIDATED"
                    trade.closed_at = bar.timestamp
                    trade.outcome = "UNFILLED"
                    trade.reason = "structural invalidation touched before a provable confirmation fill"
                    if setup:
                        previous = setup.phase
                        setup.phase = SetupPhase.INVALIDATED
                        setup.invalidation_reason = trade.reason
                        setup.transitions.append(SetupTransition(
                            _id("transition", setup.id, "INVALIDATED", bar.timestamp), setup.id,
                            previous.value, SetupPhase.INVALIDATED.value, bar.timestamp,
                            trade.reason, trade.id, candle_identity=bar.timestamp.isoformat(),
                            zone_id=zone.id if zone else setup.zone_id, order_id=trade.id,
                            market_data_health=self._market_data_health,
                            reason_code="INVALIDATION_BEFORE_ENTRY",
                            configuration_fingerprint=self.configuration_fingerprint,
                            relevant_prices={"entry": trade.requested_entry, "stop": trade.stop}))
                    continue
                if trade.entry_model == "retracement_50":
                    touched = bar.low <= trade.requested_entry if trade.direction == "bullish" else bar.high >= trade.requested_entry
                else:
                    touched = bar.high >= trade.requested_entry if trade.direction == "bullish" else bar.low <= trade.requested_entry
                if not touched:
                    continue
                if trade.entry_model == "retracement_50":
                    raw = (min(bar.open, trade.requested_entry) if trade.direction == "bullish"
                           else max(bar.open, trade.requested_entry))
                else:
                    raw = (max(bar.open, trade.requested_entry) if trade.direction == "bullish"
                           else min(bar.open, trade.requested_entry))
                trade.raw_fill_price = raw
                trade.fill_price = raw * (1 + slip if trade.direction == "bullish" else 1 - slip)
                risk_from_fill = abs(trade.fill_price - trade.stop)
                trade.target = trade.fill_price + risk_from_fill * self.config.rr_ratio * \
                    (1 if trade.direction == "bullish" else -1)
                trade.filled_at = bar.timestamp
                trade.filled_index = index
                trade.bars_to_entry = max(0, index - trade.created_index)
                trade.maximum_favourable_excursion_r = 0.0
                trade.maximum_adverse_excursion_r = 0.0
                trade.status = "OPEN"
                just_filled = True
                setup = self.setups.get(trade.setup_id)
                if setup:
                    setup.phase = SetupPhase.ENTERED
                    setup.transitions.append(SetupTransition(
                        _id("transition", setup.id, "ENTERED", bar.timestamp), setup.id,
                        SetupPhase.ORDER_PENDING.value, SetupPhase.ENTERED.value, bar.timestamp,
                        "confirmation order filled using the first available candle price", trade.id,
                        candle_identity=bar.timestamp.isoformat(), zone_id=setup.zone_id,
                        order_id=trade.id, position_id=trade.id,
                        market_data_health=self._market_data_health,
                        reason_code="REALISTIC_PAPER_FILL",
                        configuration_fingerprint=self.configuration_fingerprint,
                        relevant_prices={"requested_entry": trade.requested_entry,
                                         "fill": trade.fill_price, "stop": trade.stop,
                                         "target": trade.target}))
            if trade.status != "OPEN" or trade.fill_price is None:
                continue
            stop_hit = bar.low <= trade.stop if trade.direction == "bullish" else bar.high >= trade.stop
            target_hit = bar.high >= trade.target if trade.direction == "bullish" else bar.low <= trade.target
            if not stop_hit and not target_hit:
                # The order of prices inside the activation candle is unknown,
                # so its extremes are not attributed to a newly filled trade.
                # Every later completed candle was fully observed while the
                # position was open and may safely update excursion evidence.
                if not just_filled:
                    risk = abs(trade.fill_price - trade.stop)
                    favourable = (bar.high - trade.fill_price if trade.direction == "bullish"
                                  else trade.fill_price - bar.low)
                    adverse = (trade.fill_price - bar.low if trade.direction == "bullish"
                               else bar.high - trade.fill_price)
                    trade.maximum_favourable_excursion_r = max(
                        float(trade.maximum_favourable_excursion_r or 0), favourable / risk)
                    trade.maximum_adverse_excursion_r = max(
                        float(trade.maximum_adverse_excursion_r or 0), adverse / risk)
                filled_index = trade.filled_index if trade.filled_index is not None else index
                trade.bars_in_trade = max(0, index - filled_index)
                continue
            # If both extremes occurred in one candle their order is unknowable;
            # the adverse stop is intentionally applied first.
            stopped = stop_hit
            if stopped:
                raw_exit = (min(bar.open, trade.stop) if trade.direction == "bullish"
                            else max(bar.open, trade.stop))
                outcome = "STOP_FIRST" if target_hit else "STOP"
            else:
                raw_exit = trade.target
                outcome = "TARGET"
            trade.raw_exit_price = raw_exit
            trade.exit_price = raw_exit * (1 - slip if trade.direction == "bullish" else 1 + slip)
            risk = abs(trade.fill_price - trade.stop)
            signed = (trade.exit_price - trade.fill_price if trade.direction == "bullish"
                      else trade.fill_price - trade.exit_price)
            trade.gross_r = signed / risk
            trade.costs_r = fee * (trade.fill_price + trade.exit_price) / risk
            trade.net_r = trade.gross_r - trade.costs_r
            trade.closed_at = bar.timestamp
            trade.closed_index = index
            filled_index = trade.filled_index if trade.filled_index is not None else index
            trade.bars_in_trade = max(0, index - filled_index)
            # On an exit candle only the executed exit is sequence-proven.
            # Do not claim an OHLC extreme that may have occurred after exit.
            if trade.gross_r is not None:
                trade.maximum_favourable_excursion_r = max(
                    float(trade.maximum_favourable_excursion_r or 0), trade.gross_r, 0)
                trade.maximum_adverse_excursion_r = max(
                    float(trade.maximum_adverse_excursion_r or 0), -trade.gross_r, 0)
            trade.outcome = outcome
            trade.intrabar_ambiguous = bool(stop_hit and target_hit)
            trade.reason = ("stop and target both touched; adverse stop applied first"
                            if stop_hit and target_hit else
                            "protective stop reached" if stop_hit else "fixed 2.5R target reached")
            trade.status = "LOST" if stop_hit else "WON"
            setup = self.setups.get(trade.setup_id)
            if setup:
                terminal = SetupPhase.STOPPED if stop_hit else SetupPhase.TARGET_HIT
                setup.transitions.append(SetupTransition(
                    _id("transition", setup.id, terminal.value, bar.timestamp), setup.id,
                    SetupPhase.ENTERED.value, terminal.value, bar.timestamp, trade.reason or outcome, trade.id,
                    candle_identity=bar.timestamp.isoformat(), zone_id=setup.zone_id,
                    order_id=trade.id, position_id=trade.id,
                    market_data_health=self._market_data_health,
                    reason_code="ADVERSE_STOP_FIRST" if trade.intrabar_ambiguous else terminal.value,
                    configuration_fingerprint=self.configuration_fingerprint,
                    relevant_prices={"fill": trade.fill_price, "exit": trade.exit_price,
                                     "stop": trade.stop, "target": trade.target}))
                setup.phase = terminal

    def visual_state(self, *, candle_at: datetime | None = None, candle_window: int = 500) -> dict:
        from services.mtf_policy import display_contract
        snapshot = self.latest_snapshot if candle_at is None else self.snapshots.get(candle_at)
        if snapshot is None and candle_at is not None:
            eligible = [stamp for stamp in self.snapshots if stamp <= candle_at]
            snapshot = self.snapshots[max(eligible)] if eligible else None
        candle_cutoff = snapshot.candle_open if snapshot else (self.bars[-1].timestamp if self.bars else None)
        knowledge_cutoff = snapshot.candle_close if snapshot else candle_cutoff
        eligible_bars = [row for row in self.bars if candle_cutoff is None or row.timestamp <= candle_cutoff]
        absolute_last_bar_index = len(eligible_bars) - 1
        bars = eligible_bars[-max(1, min(candle_window, 3000)):]
        start = bars[0].timestamp if bars else None
        swings = [row for row in self.swings.values() if knowledge_cutoff is None or row.confirmed_at <= knowledge_cutoff]
        historical_zones = self._zone_snapshots.get(snapshot.candle_open, ()) if snapshot else ()
        zone_payload = list(historical_zones) if historical_zones else [asdict(row) for row in self.zones.values()]
        events = [row for row in self.events.values() if knowledge_cutoff is None or row.confirmed_at <= knowledge_cutoff]
        setups = [row for row in self.setups.values() if candle_cutoff is None or row.created_at <= candle_cutoff]
        proposals = [row for row in self.proposals.values() if candle_cutoff is None or row.signal_at <= candle_cutoff]
        research_trades = [row for row in self.research_trades.values()
                           if candle_cutoff is None or row.created_at <= candle_cutoff]
        completed = [row for row in research_trades if row.status in {"WON", "LOST"}]
        config_payload = asdict(self.config)
        config_id = hashlib.sha256(json.dumps(config_payload, sort_keys=True, default=str).encode()).hexdigest()
        dataset_id = hashlib.sha256("|".join(
            f"{row.timestamp.isoformat()}:{row.open}:{row.high}:{row.low}:{row.close}:{row.volume}"
            for row in self.bars).encode()).hexdigest()
        engine_id = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        strategy_metrics = {}
        for strategy_id in STRATEGIES:
            strategy_trades = [row for row in research_trades if row.strategy_id == strategy_id]
            rows = [row for row in strategy_trades if row.status in {"WON", "LOST"}]
            strategy_metrics[strategy_id] = {
                "closed": len(rows), "wins": sum(row.status == "WON" for row in rows),
                "losses": sum(row.status == "LOST" for row in rows),
                "unfilled": sum(row.status in {"EXPIRED", "CANCELLED", "INVALIDATED", "DATA_PAUSED"}
                                for row in strategy_trades),
                "gross_r": sum(float(row.gross_r or 0) for row in rows),
                "net_r": sum(float(row.net_r or 0) for row in rows),
                "costs_r": sum(float(row.costs_r or 0) for row in rows),
            }
        cancelled_setups = sum(
            row.phase in {SetupPhase.CANCELLED, SetupPhase.INVALIDATED} for row in setups)
        return {
            "research_id": RESEARCH_ID,
            "strategy_version": STRATEGY_VERSION,
            "research_only": True,
            "execution_allowed": False,
            "paper_execution_allowed": True,
            "signal_inputs": ["open", "high", "low", "close"],
            "volume_signal_input": False,
            "symbol": self.config.symbol,
            "timeframe": self.config.timeframe,
            "mtf_evidence": dict(self._mtf_evidence),
            "mtf_policy": display_contract(self.config.timeframe, self._mtf_evidence),
            # Proposal expiry is expressed in the engine's absolute bar-index
            # coordinate system.  Keep that coordinate independent of the
            # bounded candle slice returned for rendering.
            "absolute_last_bar_index": absolute_last_bar_index,
            "candles": [asdict(row) for row in bars],
            "swings": [asdict(row) for row in swings if start is None or row.confirmed_at >= start],
            "zones": [row for row in zone_payload if knowledge_cutoff is None or row["confirmed_at"] <= knowledge_cutoff],
            "events": [asdict(row) for row in events if start is None or row.occurred_at >= start],
            "setups": [asdict(row) for row in setups],
            "rejected_setups": [
                {"strategy_id": trace.strategy_id, "direction": trace.direction,
                 "state": trace.state, "missing_conditions": list(trace.missing_conditions),
                 "next_required_event": trace.next_required_event}
                for trace in (snapshot.strategy_traces if snapshot else ()) if trace.missing_conditions
            ],
            "proposals": [asdict(row) for row in proposals],
            "orders": [asdict(row) for row in research_trades if row.status in {"PENDING", "OPEN"}],
            "trades": [asdict(row) for row in research_trades if row.status in {
                "WON", "LOST", "EXPIRED", "CANCELLED", "INVALIDATED", "DATA_PAUSED"}],
            "metrics": {
                "closed": len(completed),
                "wins": sum(row.status == "WON" for row in completed),
                "losses": sum(row.status == "LOST" for row in completed),
                "unfilled": sum(row.status in {"EXPIRED", "CANCELLED", "INVALIDATED", "DATA_PAUSED"}
                                for row in research_trades),
                "cancelled": cancelled_setups,
                "rejected": 0,
                "gross_r": sum(float(row.gross_r or 0) for row in completed),
                "net_r": sum(float(row.net_r or 0) for row in completed),
                "costs_r": sum(float(row.costs_r or 0) for row in completed),
                "by_strategy": strategy_metrics,
            },
            "metrics_scope": {
                "scope": "AGGREGATE_PA1_PA4",
                "account": "RESEARCH_ENGINE_NORMALIZED",
                "session_id": None,
                "mode": "ENGINE_WINDOW",
                "symbol": self.config.symbol,
                "timeframe": self.config.timeframe,
                "strategies": list(STRATEGIES),
                "strategy_variant": {
                    "entry_model": self.config.entry_model,
                    "stop_model": self.config.stop_model,
                    "trigger_filter": self.config.trigger_filter,
                },
                "dataset_start": self.bars[0].timestamp.isoformat() if self.bars else None,
                "dataset_end": self.bars[-1].timestamp.isoformat() if self.bars else None,
                "closed_candles": len(self.bars),
                "configuration_id": config_id,
                "dataset_fingerprint": dataset_id,
                "engine_fingerprint": engine_id,
                "strategy_version": STRATEGY_VERSION,
                "experiment_id": None,
                "cost_model": {
                    "commission_bps": self.config.commission_bps,
                    "spread_bps": self.config.spread_bps,
                    "slippage_bps": self.config.slippage_bps,
                    "spread_and_slippage_embedded_in_gross_r": True,
                    "commission_reported_as_costs_r": True,
                    "funding_coverage": "NOT_APPLIED_TO_VISUAL_ENGINE_METRICS",
                },
                "duplicate_trade_ids": 0,
            },
            "snapshot": asdict(self.latest_snapshot) if self.latest_snapshot else None,
            "selected_snapshot": asdict(snapshot) if snapshot else None,
            "snapshot_ledger": [asdict(row) for stamp, row in self.snapshots.items() if start is None or stamp >= start],
            "strategies": list(STRATEGIES),
            "data_boundary": {
                "closed_candles_only": True,
                "forming_candle_is_display_only": True,
                "future_candles_visible": False,
                "real_order_path": False,
            },
        }


def build_engine(symbol: str, timeframe: str, bars: Iterable[Bar]) -> NativePriceActionEngine:
    engine = NativePriceActionEngine(PriceActionConfig(symbol=symbol.upper(), timeframe=timeframe))
    engine.ingest_closed_bars(bars)
    return engine
