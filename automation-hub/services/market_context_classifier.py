"""Versioned, point-in-time research classification; no execution dependencies.

Bar timestamps are candle opens. Closure and observed publication are distinct:
an elapsed interval alone does not prove that a candle was available to a signal.
The returned input material is detached and bounded for immutable reproduction.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
import hashlib
import math
from pathlib import Path
import re
from zoneinfo import TZPATH, ZoneInfo

from bot.data.indicators import atr, ema, true_range
from bot.data.resample import TF_SECONDS
from bot.types import Bar
from services.strategy_identity import configuration_fingerprint
from strategies.adaptive_trend_pullback.indicators import adx


CLASSIFIER_ID = "market_context_classifier"
CLASSIFIER_VERSION = "1.0.0"
_SESSION_PRIORITY = ("NEW_YORK", "LONDON", "ASIA")
_CONVENTIONS = {
    "candle_timestamp": "UTC_OPEN_EXCLUSIVE_CLOSE",
    "availability": "EXPLICIT_CLOSED_AND_OBSERVED_AT_OR_BEFORE_SIGNAL",
    "ema": "FIRST_CLOSE_SEED",
    "atr": "SIMPLE_MOVING_TRUE_RANGE",
    "adx": "EXISTING_WILDER_ADX",
    "percentile": "PRIOR_ATR_TO_CLOSE_MIDRANK_EXCLUDING_CURRENT",
    "history": "LATEST_FIXED_REQUIRED_WINDOW",
    "calendar": "CONFIGURED_LOCAL_WEEKDAY_START_DAY_HALF_OPEN",
    "structure": "EXPERIMENTAL_UNKNOWN",
}


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp requires an explicit timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class SessionDefinition:
    name: str
    timezone: str
    start: str
    end: str
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

    def __post_init__(self):
        if self.name not in _SESSION_PRIORITY:
            raise ValueError("unsupported canonical session name")
        ZoneInfo(self.timezone)
        for label in (self.start, self.end):
            if not re.fullmatch(r"\d{2}:\d{2}", label):
                raise ValueError("session boundaries must be HH:MM")
            time.fromisoformat(label)
        if self.start == self.end:
            raise ValueError("session boundaries must define a nonempty interval")
        days = tuple(self.weekdays)
        if not days or len(set(days)) != len(days) or any(type(day) is not int or not 0 <= day <= 6 for day in days):
            raise ValueError("session weekdays must be distinct integers from 0 to 6")
        object.__setattr__(self, "weekdays", tuple(sorted(days)))


DEFAULT_SESSIONS = (
    SessionDefinition("ASIA", "Asia/Tokyo", "09:00", "17:00"),
    SessionDefinition("LONDON", "Europe/London", "08:00", "17:00"),
    SessionDefinition("NEW_YORK", "America/New_York", "08:00", "17:00"),
)


@lru_cache(maxsize=64)
def _timezone_rule_hash(name: str) -> str:
    """Bind definitions to actual TZif bytes, not just mutable zone names."""
    ZoneInfo(name)  # Reject invalid/absolute/traversal zone keys before reading.
    for root in TZPATH:
        target = Path(root).joinpath(*name.split("/"))
        if target.is_file():
            return hashlib.sha256(target.read_bytes()).hexdigest()
    try:
        from importlib.resources import files
        data = files("tzdata.zoneinfo").joinpath(*name.split("/")).read_bytes()
    except (ImportError, FileNotFoundError, ModuleNotFoundError) as exc:
        raise ValueError("timezone rules cannot be fingerprinted") from exc
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ClassifierParameters:
    ema_period: int = 20
    ema_slope_lag: int = 3
    adx_period: int = 14
    atr_period: int = 14
    volatility_window: int = 100
    htf_ema_period: int = 50
    htf_slope_lag: int = 1
    range_adx: float = 20.0
    strong_adx: float = 25.0
    directional_slope: float = .05
    strong_slope: float = .2
    volatility_low: float = 25.0
    volatility_high: float = 75.0
    volatility_extreme: float = 95.0
    publication_delay_seconds: float = 0.0
    stale_after_intervals: float = 1.5

    def __post_init__(self):
        for name in ("ema_period", "ema_slope_lag", "adx_period", "atr_period", "volatility_window", "htf_ema_period", "htf_slope_lag"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 10000:
                raise ValueError(f"{name} must be an integer from 1 to 10000")
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not 0 <= self.range_adx <= self.strong_adx <= 100:
            raise ValueError("ADX thresholds must be ordered from 0 to 100")
        if not 0 < self.directional_slope <= self.strong_slope:
            raise ValueError("slope thresholds must be positive and ordered")
        if not 0 < self.volatility_low < self.volatility_high < self.volatility_extreme < 100:
            raise ValueError("volatility percentiles must be strictly ordered from 0 to 100")
        if self.publication_delay_seconds < 0 or self.stale_after_intervals <= 0:
            raise ValueError("publication delay must be nonnegative and stale interval positive")

    @property
    def entry_history(self) -> int:
        return max(self.ema_period + self.ema_slope_lag, 2 * self.adx_period + 1,
                   self.atr_period + self.volatility_window + 1)

    @property
    def higher_history(self) -> int:
        return max(self.htf_ema_period + self.htf_slope_lag, self.atr_period + 1)


@dataclass(frozen=True)
class CandleObservation:
    bar: Bar
    available_at: datetime | str | None = None
    is_closed: bool | None = None
    close_timestamp: datetime | str | None = None
    source_quality: str | None = None


def classifier_definition(parameters: ClassifierParameters | None = None,
                          sessions: Sequence[SessionDefinition] | None = None, *,
                          classifier_version: str = CLASSIFIER_VERSION) -> dict:
    parameters = parameters or ClassifierParameters()
    sessions = tuple(DEFAULT_SESSIONS if sessions is None else sessions)
    if not sessions or len({row.name for row in sessions}) != len(sessions):
        raise ValueError("session definitions must have distinct names")
    if not re.fullmatch(r"1\.\d+\.\d+", classifier_version):
        raise ValueError("unsupported classifier algorithm version")
    material = {**asdict(parameters), "sessions": [asdict(row) for row in sorted(sessions, key=lambda row: row.name)],
                "session_priority": list(_SESSION_PRIORITY), "conventions": dict(_CONVENTIONS),
                "timezone_rules": {name: _timezone_rule_hash(name) for name in sorted({row.timezone for row in sessions})}}
    # Canonical round-trip gives JSON lists and detached material to the store.
    from json import loads
    from services.strategy_identity import canonical_configuration_json
    material = loads(canonical_configuration_json(material))
    return {"classifier_id": CLASSIFIER_ID, "classifier_version": classifier_version,
            "parameter_hash": configuration_fingerprint(material), "parameters": material}


def _definition_configuration(definition: Mapping) -> tuple[ClassifierParameters, tuple[SessionDefinition, ...]]:
    if definition.get("classifier_id") != CLASSIFIER_ID:
        raise ValueError("unsupported classifier identity")
    material = definition.get("parameters")
    if not isinstance(material, Mapping) or configuration_fingerprint(material) != definition.get("parameter_hash"):
        raise ValueError("classifier parameter hash mismatch")
    numeric = dict(material)
    session_rows = numeric.pop("sessions", None)
    stored_timezone_rules = numeric.pop("timezone_rules", None)
    if numeric.pop("conventions", None) != _CONVENTIONS or numeric.pop("session_priority", None) != list(_SESSION_PRIORITY):
        raise ValueError("unsupported classifier conventions")
    parameters = ClassifierParameters(**numeric)
    sessions = tuple(SessionDefinition(**row) for row in session_rows)
    current_timezone_rules = {name: _timezone_rule_hash(name) for name in sorted({row.timezone for row in sessions})}
    if stored_timezone_rules != current_timezone_rules:
        raise ValueError("timezone rule hash mismatch; original rules or explicit new version required")
    expected = classifier_definition(parameters, sessions, classifier_version=definition.get("classifier_version", ""))
    if expected != dict(definition):
        raise ValueError("classifier definition is not canonical")
    return parameters, sessions


def classify_session(timestamp: datetime | str, *, sessions: Sequence[SessionDefinition] | None = None) -> dict:
    at = _utc(timestamp)
    active = set()
    for session in DEFAULT_SESSIONS if sessions is None else sessions:
        local = at.astimezone(ZoneInfo(session.timezone))
        start, end = time.fromisoformat(session.start), time.fromisoformat(session.end)
        clock = local.timetz().replace(tzinfo=None)
        if start < end:
            matched, weekday = start <= clock < end, local.weekday()
        else:
            matched = clock >= start or clock < end
            weekday = (local - timedelta(days=1)).weekday() if clock < end else local.weekday()
        if matched and weekday in session.weekdays:
            active.add(session.name)
    if {"LONDON", "NEW_YORK"} <= active:
        canonical = "LONDON_NEW_YORK_OVERLAP"
    else:
        canonical = next((name for name in _SESSION_PRIORITY if name in active), "OFF_SESSION")
    return {"session": canonical, "active_sessions": sorted(active)}


def classify_trend(*, adx_value: float, normalized_slope: float, price_bias: int,
                   htf_direction: int, parameters: ClassifierParameters | None = None) -> str:
    cfg = parameters or ClassifierParameters()
    if not all(math.isfinite(value) for value in (adx_value, normalized_slope)) or not 0 <= adx_value <= 100:
        return "UNKNOWN"
    if adx_value < cfg.range_adx or abs(normalized_slope) < cfg.directional_slope:
        return "RANGE"
    direction = 1 if normalized_slope > 0 else -1
    if price_bias != direction:
        return "RANGE"
    strong = (adx_value >= cfg.strong_adx and abs(normalized_slope) >= cfg.strong_slope
              and htf_direction == direction)
    return ("STRONG_" if strong else "") + ("BULL" if direction == 1 else "BEAR")


def classify_volatility(percentile: float | None, *, parameters: ClassifierParameters | None = None) -> str:
    cfg = parameters or ClassifierParameters()
    if percentile is None or not math.isfinite(percentile) or not 0 <= percentile <= 100:
        return "UNKNOWN"
    if percentile < cfg.volatility_low:
        return "LOW"
    if percentile < cfg.volatility_high:
        return "NORMAL"
    return "HIGH" if percentile <= cfg.volatility_extreme else "EXTREME"


def _observation(value) -> CandleObservation:
    if isinstance(value, CandleObservation):
        return value
    if isinstance(value, Bar):
        return CandleObservation(value)
    if not isinstance(value, Mapping):
        raise ValueError("invalid candle observation")
    bar = Bar(value.get("timestamp", value.get("open_time")), *(value.get(field) for field in ("open", "high", "low", "close", "volume")))
    return CandleObservation(bar, value.get("available_at"), value.get("is_closed"), value.get("close_timestamp"), value.get("source_quality"))


def _material(observation: CandleObservation) -> dict:
    result = {}
    for name in ("timestamp", "open", "high", "low", "close", "volume"):
        value = getattr(observation.bar, name)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, float) and not math.isfinite(value):
            value = repr(value)
        result[name] = value
    for name in ("available_at", "is_closed", "close_timestamp"):
        value = getattr(observation, name)
        result[name] = value.isoformat() if isinstance(value, datetime) else value
    if observation.source_quality is not None:
        result["source_quality"] = observation.source_quality
    return result


def _select(rows: Sequence, timeframe: str, cutoff: datetime, history: int,
            cfg: ClassifierParameters, role: str) -> tuple[list[CandleObservation], list[dict], list[str]]:
    duration = TF_SECONDS.get(timeframe)
    if not duration:
        return [], [], [f"{role}:unsupported_timeframe"]
    candidates, invalid = [], []
    for supplied in rows:
        try:
            row = _observation(supplied)
            opened = _utc(row.bar.timestamp)
            close = opened + timedelta(seconds=duration)
            # Exclude future/in-progress/unpublished candles BEFORE OHLCV reads.
            if close + timedelta(seconds=cfg.publication_delay_seconds) > cutoff:
                continue
            available = _utc(row.available_at) if row.available_at is not None else None
            if available is not None and available > cutoff:
                continue
            candidates.append((opened, close, available, row))
        except (TypeError, ValueError, OverflowError, AttributeError):
            invalid.append(f"{role}:invalid_timestamp")
    candidates.sort(key=lambda item: item[0])
    # Bound by distinct opens so repeated delivery does not evict history.
    keep = set(sorted({item[0] for item in candidates})[-history:])
    candidates = [item for item in candidates if item[0] in keep]
    selected, material, errors = {}, {}, list(invalid)
    for opened, close, available, row in candidates:
        try:
            canonical = _material(row)
            canonical["timestamp"] = opened.isoformat()
            canonical["close_timestamp"] = close.isoformat()
            canonical["available_at"] = available.isoformat() if available else None
            # Preserve original unknown metadata in input material for replay.
            material.setdefault(opened, canonical)
            if row.is_closed is not True or available is None:
                raise ValueError("unknown_availability")
            if row.source_quality is not None and row.source_quality != "VERIFIED":
                raise ValueError("unknown_source_quality")
            if available < close:
                raise ValueError("publication_before_close")
            if row.close_timestamp is not None:
                reported = _utc(row.close_timestamp)
                if not close - timedelta(milliseconds=1) <= reported <= close:
                    raise ValueError("close_boundary_mismatch")
            if opened.timestamp() % duration != 0:
                raise ValueError("unaligned_open")
            values = [float(getattr(row.bar, field)) for field in ("open", "high", "low", "close", "volume")]
            o, high, low, c, volume = values
            if not all(math.isfinite(value) for value in values) or min(o, high, low, c) <= 0 or volume < 0 or high < max(o, low, c) or low > min(o, high, c):
                raise ValueError("invalid_ohlcv")
            validated = CandleObservation(Bar(opened, *values), available, True, close, row.source_quality)
            previous = selected.get(opened)
            if previous is not None:
                prior_values = [getattr(previous.bar, field) for field in ("open", "high", "low", "close", "volume")]
                if values != prior_values:
                    raise ValueError("conflicting_duplicate")
                # Earliest actual observation is reproducible across retries.
                if previous.available_at <= available:
                    continue
            selected[opened] = validated
            material[opened] = _material(validated)
        except (TypeError, ValueError, OverflowError) as exc:
            code = str(exc)
            known = {"unknown_availability", "unknown_source_quality", "publication_before_close",
                     "close_boundary_mismatch", "unaligned_open", "invalid_ohlcv", "conflicting_duplicate"}
            errors.append(f"{role}:{code if code in known else 'invalid_candle'}")
    return [selected[key] for key in sorted(selected)], [material[key] for key in sorted(material)], sorted(set(errors))


def classify_market_context(entry_candles: Sequence, *, signal_timestamp: datetime | str,
                            entry_timeframe: str, higher_timeframe: str,
                            higher_candles: Sequence = (), market_data_source: str | None,
                            classification_timestamp: datetime | str,
                            parameters: ClassifierParameters | None = None,
                            sessions: Sequence[SessionDefinition] | None = None,
                            classifier_version: str = CLASSIFIER_VERSION,
                            input_quality_reasons: Sequence[str] = ()) -> dict:
    cfg = parameters or ClassifierParameters()
    cutoff, calculated = _utc(signal_timestamp), _utc(classification_timestamp)
    definition = classifier_definition(cfg, sessions, classifier_version=classifier_version)
    result = {"signal_timestamp": cutoff.isoformat(), "classification_timestamp": calculated.isoformat(),
              "entry_timeframe": entry_timeframe, "higher_timeframe": higher_timeframe,
              "market_data_source": market_data_source, **{key: definition[key] for key in ("classifier_id", "classifier_version", "parameter_hash")},
              **classify_session(cutoff, sessions=sessions), "trend_regime": "UNKNOWN",
              "volatility_regime": "UNKNOWN", "structure_regime": "UNKNOWN", "trend_strength": None,
              "atr_value": None, "atr_percentile": None, "adx_value": None,
              "ema_value": None, "ema_normalized_slope": None, "higher_timeframe_direction": None,
              "market_data_timestamp": None, "last_closed_candle_timestamp": None,
              "higher_timeframe_market_data_timestamp": None, "higher_timeframe_last_closed_candle_timestamp": None}
    entry, entry_material, errors = _select(entry_candles, entry_timeframe, cutoff, cfg.entry_history, cfg, "entry")
    higher, higher_material, higher_errors = _select(higher_candles, higher_timeframe, cutoff, cfg.higher_history, cfg, "higher")
    errors.extend(higher_errors)
    errors.extend(input_quality_reasons)
    frozen = {"classifier_definition": definition, "signal_timestamp": cutoff.isoformat(),
              "entry_timeframe": entry_timeframe, "higher_timeframe": higher_timeframe,
              "market_data_source": market_data_source, "entry_candles": entry_material, "higher_candles": higher_material,
              "input_quality_reasons": sorted(set(errors))}
    result.update(classification_input=frozen, input_data_hash=configuration_fingerprint(frozen),
                  entry_candle_count=len(entry), higher_timeframe_candle_count=len(higher))
    for prefix, rows in (("", entry), ("higher_timeframe_", higher)):
        if rows:
            result[prefix + "market_data_timestamp"] = rows[-1].available_at.isoformat()
            result[prefix + "last_closed_candle_timestamp"] = rows[-1].close_timestamp.isoformat()
    quality = "VALID"
    if errors or not market_data_source:
        quality = "UNKNOWN"
        if not market_data_source:
            errors.append("unknown_market_data_source")
    elif not entry:
        quality, errors = "INSUFFICIENT_HISTORY", ["entry:no_available_closed_candles"]
    elif not higher:
        quality, errors = "MISSING_HTF", ["higher:no_available_closed_candles"]
    else:
        for role, rows, timeframe in (("entry", entry, entry_timeframe), ("higher", higher, higher_timeframe)):
            duration = TF_SECONDS[timeframe]
            if (cutoff - rows[-1].close_timestamp).total_seconds() > duration * cfg.stale_after_intervals:
                quality, errors = "STALE_DATA", [f"{role}:last_closed_candle_stale"]
                break
            if any((current.bar.timestamp - previous.bar.timestamp).total_seconds() != duration for previous, current in zip(rows, rows[1:])):
                quality, errors = "GAPPED_CANDLES", [f"{role}:noncontiguous_candles"]
                break
        if quality == "VALID" and (len(entry) < cfg.entry_history or len(higher) < cfg.higher_history):
            quality, errors = "INSUFFICIENT_HISTORY", [f"required_history:entry={cfg.entry_history},higher={cfg.higher_history}"]
    if quality == "VALID":
        bars, htf_bars = [row.bar for row in entry], [row.bar for row in higher]
        a = atr(bars, cfg.atr_period)
        result["atr_value"] = a if math.isfinite(a) else None
        if not math.isfinite(a):
            quality, errors = "UNKNOWN", ["entry:nonfinite_atr"]
        elif a <= 0:
            quality, errors = "UNKNOWN", ["entry:zero_atr"]
        else:
            averages = ema([bar.close for bar in bars], cfg.ema_period)
            slope = (averages[-1] - averages[-1 - cfg.ema_slope_lag]) / (cfg.ema_slope_lag * a)
            strength = adx(bars, cfg.adx_period)
            htf_averages = ema([bar.close for bar in htf_bars], cfg.htf_ema_period)
            htf_slope = htf_averages[-1] - htf_averages[-1 - cfg.htf_slope_lag]
            htf_bias = htf_bars[-1].close - htf_averages[-1]
            htf_direction = 1 if htf_slope > 0 and htf_bias > 0 else -1 if htf_slope < 0 and htf_bias < 0 else 0
            price_bias = 1 if bars[-1].close > averages[-1] else -1 if bars[-1].close < averages[-1] else 0
            # O(n) rolling simple ATR uses the existing true-range definition.
            ranges = [true_range(previous.close, current) for previous, current in zip(bars, bars[1:])]
            rolling = sum(ranges[:cfg.atr_period])
            historical = []
            for index in range(cfg.atr_period, len(bars) - 1):
                if index > cfg.atr_period:
                    rolling += ranges[index - 1] - ranges[index - 1 - cfg.atr_period]
                historical.append((rolling / cfg.atr_period) / bars[index].close)
            reference = historical[-cfg.volatility_window:]
            current = a / bars[-1].close
            if not all(math.isfinite(value) for value in (*averages, slope, strength, *htf_averages, htf_slope, htf_bias, *reference, current)):
                result.update(context_quality="UNKNOWN", quality_reasons=["nonfinite_indicator"])
                return result
            percentile = 100 * (sum(sample < current for sample in reference) + .5 * sum(sample == current for sample in reference)) / len(reference)
            result.update(trend_strength=strength, adx_value=strength, ema_value=averages[-1],
                          ema_normalized_slope=slope, higher_timeframe_direction=htf_direction,
                          atr_percentile=percentile, trend_regime=classify_trend(adx_value=strength,
                          normalized_slope=slope, price_bias=price_bias, htf_direction=htf_direction, parameters=cfg),
                          volatility_regime=classify_volatility(percentile, parameters=cfg))
    result.update(context_quality=quality, quality_reasons=sorted(set(errors)))
    return result


def classify_frozen_context(input: Mapping, *, classification_timestamp: datetime | str) -> dict:
    """Replay original immutable parameters and original observed candle facts."""
    definition = input.get("classifier_definition")
    if not isinstance(definition, Mapping):
        raise ValueError("original classifier definition is required")
    parameters, sessions = _definition_configuration(definition)
    # Keep provenance failures from the original observer even if an offending
    # older row falls outside the bounded indicator window. Missing series are
    # assessed by their specific MISSING_HTF/INSUFFICIENT_HISTORY gates below.
    reasons = set(input.get("input_quality_reasons", ()))
    reasons.update(reason for reason in input.get("quality_reasons", ())
                   if reason not in {"ENTRY_CANDLES_UNAVAILABLE", "NATIVE_HTF_UNAVAILABLE"})
    if input.get("evidence_quality") == "UNKNOWN" and not input.get("quality_reasons"):
        reasons.add("ORIGINAL_INPUT_EVIDENCE_UNKNOWN")
    return classify_market_context(input.get("entry_candles", ()), signal_timestamp=input["signal_timestamp"],
                                   entry_timeframe=input["entry_timeframe"], higher_timeframe=input["higher_timeframe"],
                                   higher_candles=input.get("higher_candles", ()), market_data_source=input.get("market_data_source"),
                                   classification_timestamp=classification_timestamp, parameters=parameters, sessions=sessions,
                                   classifier_version=definition["classifier_version"], input_quality_reasons=sorted(reasons))
