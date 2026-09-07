"""Causal market context for PA/SMC shadow research.

This module is observational.  It has no broker, account, risk, or execution
imports and cannot alter the armed Price Action or SMC strategies.

Session windows are expressed in Europe/London wall time so ``zoneinfo``
applies UK daylight-saving transitions:

* ASIA: 00:00 <= local time < 07:00
* LONDON: 07:00 <= local time < 13:00
* LONDON_NY_OVERLAP: 13:00 <= local time < 16:00
* NEW_YORK: 16:00 <= local time < 21:00
* OUT_OF_SESSION: all other times

Higher-timeframe evidence is accepted only from authoritative, explicitly
identified Binance USD-M 1h/4h candles.  It is never resampled from the
decision timeframe.  A lookup at decision time T can return only a candle
whose close timestamp is <= T.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from bot.types import Bar
from services.mtf_policy import evidence_for


LONDON = ZoneInfo("Europe/London")
SESSION_WINDOWS = (
    ("ASIA", 0, 7),
    ("LONDON", 7, 13),
    ("LONDON_NY_OVERLAP", 13, 16),
    ("NEW_YORK", 16, 21),
)
HTF_SECONDS = {"1h": 3_600, "4h": 14_400}


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def session_tag(at: datetime) -> str:
    """Return the deterministic London-wall-clock attribution for ``at``."""
    hour = utc(at).astimezone(LONDON).hour
    return next((name for name, start, end in SESSION_WINDOWS
                 if start <= hour < end), "OUT_OF_SESSION")


def stable_hash(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, default=str, separators=(",", ":")
    ).encode()).hexdigest()


@dataclass
class LiquidityReference:
    id: str
    type: str
    price: float
    created_at: str
    source_timeframe: str
    source_candle_id: str
    side: str
    first_touch: str | None = None
    touches: int = 0
    freshness: str = "FRESH"
    invalidated_at: str | None = None
    age_bars: int = 0
    atr_width: float | None = None
    departure_strength_atr: float | None = None
    displacement: bool | None = None
    structure_break: bool | None = None
    distance_to_opposing_liquidity: float | None = None

    def payload(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class HTFEvidence:
    htf_timeframe: str
    htf_candle_id: str
    htf_bias: str
    htf_close_timestamp: str
    structure: str
    htf_open_timestamp: str
    source: str = "Binance USD-M public closed candle"


class CausalHTFContext:
    """Closed 1h/4h evidence, indexed independently of decision bars."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], list[tuple[datetime, Bar, str]]] = {}

    def ingest(self, symbol: str, timeframe: str, bar: Bar, candle_id: str) -> None:
        if timeframe not in HTF_SECONDS:
            raise ValueError("research HTF accepts only explicit closed 1h/4h candles")
        if not candle_id:
            raise ValueError("authoritative HTF candle_id is required")
        opened = utc(bar.timestamp)
        closed = opened + timedelta(seconds=HTF_SECONDS[timeframe])
        key = (symbol.upper().replace("/", ""), timeframe)
        rows = self._rows.setdefault(key, [])
        if any(existing_id == candle_id for _, _, existing_id in rows):
            return
        rows.append((closed, bar, candle_id))
        rows.sort(key=lambda row: (row[0], row[2]))
        timestamps = [utc(row[1].timestamp) for row in rows]
        if len(timestamps) != len(set(timestamps)):
            rows.remove((closed, bar, candle_id))
            raise ValueError("conflicting HTF candle identity for one open timestamp")

    def at(self, symbol: str, decision_time: datetime) -> dict[str, dict | None]:
        decision = utc(decision_time)
        result: dict[str, dict | None] = {}
        normalized = symbol.upper().replace("/", "")
        for timeframe in HTF_SECONDS:
            stored = self._rows.get((normalized, timeframe), [])
            eligible = [item for item in stored if item[0] <= decision]
            row = evidence_for(
                normalized, timeframe, [item[1] for item in stored], decision_time,
            )
            if row is None or not eligible:
                result[timeframe] = None
                continue
            structure = {"BULLISH": "HIGHER_CLOSE", "BEARISH": "LOWER_CLOSE"}.get(
                row["htf_bias"], "UNCHANGED_CLOSE")
            payload = asdict(HTFEvidence(
                htf_timeframe=timeframe,
                # Research ingestion may carry a venue event identifier. Keep
                # that authoritative identity while sharing selection/bias
                # semantics with the production MTF policy.
                htf_candle_id=eligible[-1][2],
                htf_bias=row["htf_bias"],
                htf_close_timestamp=row["htf_close_timestamp"],
                structure=structure,
                htf_open_timestamp=row["htf_open_timestamp"],
            ))
            # Compatibility aliases for existing frozen shadow datasets. New
            # consumers persist the explicit htf_* contract above.
            payload.update({
                "candle_id": payload["htf_candle_id"],
                "bias": payload["htf_bias"],
                "open_timestamp": payload["htf_open_timestamp"],
                "close_timestamp": payload["htf_close_timestamp"],
            })
            result[timeframe] = payload
        return result


class NamedLiquidityBook:
    """Collect named liquidity facts without merging their origins.

    ``equal_tolerance_atr`` defaults to ``None``: equal-high/low discovery is
    disabled until an operator explicitly freezes a threshold in research
    configuration.  Confirmed major swings reuse the existing PA default of
    three completed candles on each side.
    """

    def __init__(self, *, swing_left: int = 3, swing_right: int = 3,
                 equal_tolerance_atr: float | None = None) -> None:
        if swing_left < 1 or swing_right < 1:
            raise ValueError("confirmed swing widths must be positive")
        if equal_tolerance_atr is not None and equal_tolerance_atr <= 0:
            raise ValueError("equal-liquidity ATR tolerance must be positive")
        self.swing_left = int(swing_left)
        self.swing_right = int(swing_right)
        self.equal_tolerance_atr = equal_tolerance_atr
        self._bars: dict[tuple[str, str], list[tuple[Bar, str]]] = {}
        self._references: dict[tuple[str, str], dict[str, LiquidityReference]] = {}
        self._periods: dict[tuple[str, str, str], dict] = {}

    @property
    def config_hash(self) -> str:
        return stable_hash({
            "swing_left": self.swing_left,
            "swing_right": self.swing_right,
            "equal_tolerance_atr": self.equal_tolerance_atr,
            "session_windows": SESSION_WINDOWS,
        })

    @staticmethod
    def _add(refs: dict[str, LiquidityReference], *, kind: str, price: float,
             created_at: datetime, timeframe: str, candle_id: str,
             side: str) -> LiquidityReference:
        ident = "liq-" + stable_hash({
            "type": kind, "price": float(price), "created_at": utc(created_at).isoformat(),
            "timeframe": timeframe, "candle_id": candle_id,
        })[:24]
        row = refs.setdefault(ident, LiquidityReference(
            id=ident, type=kind, price=float(price),
            created_at=utc(created_at).isoformat(), source_timeframe=timeframe,
            source_candle_id=candle_id, side=side,
        ))
        return row

    def add_zone(self, symbol: str, timeframe: str, *, price: float, role: str,
                 created_at: datetime, source_candle_id: str, flipped: bool = False,
                 atr_width: float | None = None,
                 departure_strength_atr: float | None = None,
                 displacement: bool | None = None,
                 structure_break: bool | None = None,
                 distance_to_opposing_liquidity: float | None = None) -> None:
        kind = (("FLIPPED_RESISTANCE" if role == "support" else "FLIPPED_SUPPORT")
                if flipped else ("SUPPORT_ZONE" if role == "support" else "RESISTANCE_ZONE"))
        key = (symbol.upper().replace("/", ""), timeframe)
        row = self._add(
            self._references.setdefault(key, {}), kind=kind, price=price,
            created_at=created_at, timeframe=timeframe,
            candle_id=source_candle_id,
            side="LOW" if role == "support" else "HIGH",
        )
        row.atr_width = atr_width
        row.departure_strength_atr = departure_strength_atr
        row.displacement = displacement
        row.structure_break = structure_break
        row.distance_to_opposing_liquidity = distance_to_opposing_liquidity

    @staticmethod
    def _atr(rows: Iterable[Bar], length: int = 14) -> float | None:
        bars = list(rows)[-length - 1:]
        if len(bars) < 2:
            return None
        values = [max(bar.high - bar.low, abs(bar.high - previous.close),
                      abs(bar.low - previous.close))
                  for previous, bar in zip(bars, bars[1:])]
        return sum(values) / len(values) if values else None

    def _roll_period(self, key: tuple[str, str], period_type: str, period_key: object,
                     bar: Bar, candle_id: str, high_type: str, low_type: str) -> None:
        state_key = (*key, period_type)
        current = self._periods.get(state_key)
        if current and current["period_key"] != period_key:
            refs = self._references.setdefault(key, {})
            # Use the completed period's own labels and the exact candles that
            # established each extreme. The next session/day must never rename
            # prior evidence or inherit the first candle as false provenance.
            self._add(
                refs, kind=current["high_type"], price=current["high"],
                created_at=current["high_at"], timeframe=key[1],
                candle_id=current["high_candle_id"], side="HIGH",
            )
            self._add(
                refs, kind=current["low_type"], price=current["low"],
                created_at=current["low_at"], timeframe=key[1],
                candle_id=current["low_candle_id"], side="LOW",
            )
            current = None
        if current is None:
            self._periods[state_key] = {
                "period_key": period_key,
                "high": bar.high, "low": bar.low,
                "high_candle_id": candle_id, "low_candle_id": candle_id,
                "high_at": utc(bar.timestamp), "low_at": utc(bar.timestamp),
                "high_type": high_type, "low_type": low_type,
            }
        else:
            if bar.high > current["high"]:
                current.update(high=bar.high, high_candle_id=candle_id,
                               high_at=utc(bar.timestamp))
            if bar.low < current["low"]:
                current.update(low=bar.low, low_candle_id=candle_id,
                               low_at=utc(bar.timestamp))

    def ingest(self, symbol: str, timeframe: str, bar: Bar, candle_id: str) -> list[dict]:
        if not candle_id:
            raise ValueError("source candle_id is required for liquidity lineage")
        key = (symbol.upper().replace("/", ""), timeframe)
        rows = self._bars.setdefault(key, [])
        if any(existing_id == candle_id for _, existing_id in rows):
            return self.snapshot(*key)
        if rows and utc(bar.timestamp) <= utc(rows[-1][0].timestamp):
            raise ValueError("liquidity source candles must be chronological")
        rows.append((bar, candle_id))
        local = utc(bar.timestamp).astimezone(LONDON)
        self._roll_period(key, "day", local.date(), bar, candle_id,
                          "PREVIOUS_DAY_HIGH", "PREVIOUS_DAY_LOW")
        iso = local.isocalendar()
        self._roll_period(key, "week", (iso.year, iso.week), bar, candle_id,
                          "PREVIOUS_WEEK_HIGH", "PREVIOUS_WEEK_LOW")
        tag = session_tag(bar.timestamp)
        self._roll_period(key, "session", (local.date(), tag), bar, candle_id,
                          f"{tag}_HIGH", f"{tag}_LOW")

        bars = [row for row, _ in rows]
        candidate_index = len(bars) - self.swing_right - 1
        if candidate_index >= self.swing_left:
            candidate = bars[candidate_index]
            left = bars[candidate_index - self.swing_left:candidate_index]
            right = bars[candidate_index + 1:candidate_index + 1 + self.swing_right]
            refs = self._references.setdefault(key, {})
            source_id = rows[candidate_index][1]
            if all(candidate.high > item.high for item in (*left, *right)):
                self._add(refs, kind="MAJOR_SWING_HIGH", price=candidate.high,
                          created_at=candidate.timestamp, timeframe=timeframe,
                          candle_id=source_id, side="HIGH")
            if all(candidate.low < item.low for item in (*left, *right)):
                self._add(refs, kind="MAJOR_SWING_LOW", price=candidate.low,
                          created_at=candidate.timestamp, timeframe=timeframe,
                          candle_id=source_id, side="LOW")

        if self.equal_tolerance_atr is not None:
            atr = self._atr(bars)
            prior = bars[-2] if len(bars) > 1 else None
            refs = self._references.setdefault(key, {})
            if prior and atr:
                tolerance = atr * self.equal_tolerance_atr
                if abs(prior.high - bar.high) <= tolerance:
                    self._add(refs, kind="EQUAL_HIGHS", price=max(prior.high, bar.high),
                              created_at=bar.timestamp, timeframe=timeframe,
                              candle_id=candle_id, side="HIGH")
                if abs(prior.low - bar.low) <= tolerance:
                    self._add(refs, kind="EQUAL_LOWS", price=min(prior.low, bar.low),
                              created_at=bar.timestamp, timeframe=timeframe,
                              candle_id=candle_id, side="LOW")

        for reference in self._references.setdefault(key, {}).values():
            reference.age_bars += 1
            touched = (bar.high >= reference.price if reference.side == "HIGH"
                       else bar.low <= reference.price)
            if touched and candle_id != reference.source_candle_id:
                reference.touches += 1
                reference.first_touch = reference.first_touch or utc(bar.timestamp).isoformat()
            invalidated = (bar.close > reference.price if reference.side == "HIGH"
                           else bar.close < reference.price)
            if invalidated and reference.invalidated_at is None:
                reference.invalidated_at = utc(bar.timestamp).isoformat()
            reference.freshness = ("INVALIDATED" if reference.invalidated_at else
                                   "FRESH" if reference.touches <= 1 else "TOUCHED")
        return self.snapshot(*key)

    def snapshot(self, symbol: str, timeframe: str) -> list[dict]:
        key = (symbol.upper().replace("/", ""), timeframe)
        return [row.payload() for row in sorted(
            self._references.get(key, {}).values(),
            key=lambda item: (item.created_at, item.type, item.id),
        )]
