"""Authoritative native multi-timeframe policy for every paper engine.

Provider candle timestamps are candle *opens*.  A decision at ``T`` may use
only a native higher-timeframe candle whose ``open + duration <= T``.  This
module never resamples entry candles and is deliberately independent of any
strategy, account, broker, or UI code.

``ENTRY_HTF`` is the single source of truth.  The primary timeframe is an
entry gate; the secondary timeframe is context/bias only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping, Sequence

from bot.types import Bar


ENTRY_HTF = MappingProxyType({
    "1m": ("15m", "1h"),
    "5m": ("1h", "4h"),
    "15m": ("1h", "4h"),
    "1h": ("4h", "1d"),
    "4h": ("1d", None),
})

TIMEFRAME_SECONDS = MappingProxyType({
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3_600,
    "4h": 14_400,
    "1d": 86_400,
})


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def policy_for(entry_timeframe: str) -> tuple[str, str | None]:
    """Return primary/secondary native clocks or reject unsupported entries."""
    key = str(entry_timeframe or "").strip().lower()
    try:
        return ENTRY_HTF[key]
    except KeyError as exc:
        raise ValueError(
            f"entry timeframe '{entry_timeframe}' has no native MTF policy; "
            f"supported entries: {', '.join(ENTRY_HTF)}"
        ) from exc


def native_timeframes(entry_timeframe: str) -> tuple[str, ...]:
    primary, secondary = policy_for(entry_timeframe)
    return tuple(tf for tf in (primary, secondary) if tf is not None)


def candle_close(bar: Bar, timeframe: str) -> datetime:
    try:
        duration = TIMEFRAME_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported native timeframe '{timeframe}'") from exc
    return utc(bar.timestamp) + timedelta(seconds=duration)


def canonical_candle_id(symbol: str, timeframe: str, bar: Bar) -> str:
    normalized = str(symbol or "").upper().replace("/", "").replace("-", "").strip()
    return f"BINANCE_USDM:{normalized}:{timeframe}:{int(utc(bar.timestamp).timestamp() * 1000)}"


@dataclass(frozen=True)
class NativeHTFEvidence:
    htf_timeframe: str
    htf_candle_id: str
    htf_close_timestamp: str
    htf_bias: str
    htf_open_timestamp: str
    source: str = "Binance USD-M native closed kline"


def _validated(rows: Sequence[Bar], timeframe: str) -> list[Bar]:
    ordered = sorted(rows, key=lambda row: utc(row.timestamp))
    stamps = [utc(row.timestamp) for row in ordered]
    if len(stamps) != len(set(stamps)):
        raise ValueError(f"duplicate native {timeframe} candle timestamps")
    return ordered


def closed_native_bars(
    rows: Sequence[Bar], timeframe: str, decision_time: datetime,
) -> list[Bar]:
    """Return only native candles known closed at the decision boundary."""
    decision = utc(decision_time)
    return [row for row in _validated(rows, timeframe)
            if candle_close(row, timeframe) <= decision]


def evidence_for(
    symbol: str, timeframe: str, rows: Sequence[Bar], decision_time: datetime,
) -> dict | None:
    eligible = closed_native_bars(rows, timeframe, decision_time)
    if not eligible:
        return None
    selected = eligible[-1]
    previous = eligible[-2] if len(eligible) > 1 else None
    if previous is None or float(selected.close) == float(previous.close):
        bias = "NEUTRAL"
    elif float(selected.close) > float(previous.close):
        bias = "BULLISH"
    else:
        bias = "BEARISH"
    return asdict(NativeHTFEvidence(
        htf_timeframe=timeframe,
        htf_candle_id=canonical_candle_id(symbol, timeframe, selected),
        htf_close_timestamp=candle_close(selected, timeframe).isoformat(),
        htf_bias=bias,
        htf_open_timestamp=utc(selected.timestamp).isoformat(),
    ))


def evidence_at(
    symbol: str,
    entry_timeframe: str,
    context: Mapping[str, Sequence[Bar]],
    decision_time: datetime,
) -> dict:
    """Select the primary gate and secondary bias evidence for one decision."""
    primary, secondary = policy_for(entry_timeframe)
    return {
        "entry_timeframe": entry_timeframe,
        "available_entry_timeframes": list(ENTRY_HTF),
        "primary": evidence_for(symbol, primary, context.get(primary, ()), decision_time),
        "secondary": (evidence_for(symbol, secondary, context.get(secondary, ()), decision_time)
                      if secondary else None),
    }


def material_evidence(evidence: Mapping | None) -> dict:
    """Return the transient-free native HTF identity saved with a decision."""
    supplied = evidence or {}
    result = {"entry_timeframe": supplied.get("entry_timeframe")}
    fields = ("htf_timeframe", "htf_candle_id", "htf_close_timestamp", "htf_bias")
    for role in ("primary", "secondary"):
        row = supplied.get(role)
        result[role] = ({field: row.get(field) for field in fields} if row else None)
    return result


def display_contract(entry_timeframe: str, evidence: Mapping | None = None) -> dict:
    """Backend-owned UI wording; clients must not invent a separate MTF map."""
    primary, secondary = policy_for(entry_timeframe)
    supplied = evidence or {}
    primary_row = supplied.get("primary") or {}
    secondary_row = supplied.get("secondary") or {}
    return {
        "entry_timeframe": entry_timeframe,
        "available_entry_timeframes": list(ENTRY_HTF),
        "primary_timeframe": primary,
        "secondary_timeframe": secondary,
        "label": (
            f"Entry {entry_timeframe} · HTF {primary} "
            f"{'closed' if primary_row else 'waiting'}"
            + (f" · Bias {secondary} {'closed' if secondary_row else 'waiting'}"
               if secondary else "")
        ),
        "evidence": supplied,
    }
