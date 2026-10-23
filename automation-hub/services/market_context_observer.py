"""Freeze the market inputs actually observed before a strategy signal.

This adapter consumes batches the existing feed has already accepted. It has
no provider, database, order, or strategy mutation interface. A candle's open
and interval end identify it; only an actual observation proves availability.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import threading

from bot.data.resample import TF_SECONDS
from services.mtf_policy import policy_for


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market observations require an aware timestamp")
    return value.astimezone(timezone.utc)


def _hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _symbol(value):
    return str(value).upper().replace("/", "").replace("-", "").strip()


def _candle(bar, timeframe):
    duration = TF_SECONDS.get(timeframe)
    if duration is None:
        raise ValueError("unsupported observation timeframe")
    stamp = _utc(bar.timestamp)
    values = {name: float(getattr(bar, name)) for name in ("open", "high", "low", "close", "volume")}
    if (not all(math.isfinite(value) for value in values.values()) or
            min(values[name] for name in ("open", "high", "low", "close")) <= 0 or
            values["volume"] < 0 or
            values["high"] < max(values["open"], values["low"], values["close"]) or
            values["low"] > min(values["open"], values["high"], values["close"])):
        raise ValueError("invalid observed OHLCV candle")
    return {"timestamp": stamp.isoformat(), "open_time": stamp.isoformat(), **values,
            "close_timestamp": (stamp + timedelta(seconds=duration)).isoformat(), "is_closed": True}


def _provenance(source, exchange, market_type):
    exchange = str(exchange).strip() if exchange else None
    market_type = str(market_type).strip() if market_type else None
    # This label is emitted by the existing public USD-M adapter, rather than
    # guessed from a symbol suffix or the current deployment's exchange.
    contradicted = False
    if source == "live (binance_usdm_hub)":
        if exchange in (None, "binance_usdm") and market_type in (None, "perpetual"):
            exchange, market_type = "binance_usdm", "perpetual"
        else:
            contradicted = True
    known = (not contradicted and str(source).startswith("live") and exchange is not None and
             market_type in ("perpetual", "spot", "future", "futures"))
    return {"source": str(source or ""), "exchange": exchange, "market_type": market_type,
            "source_quality": "VERIFIED" if known else "UNKNOWN"}


class MarketContextObserver:
    """A bounded receipt registry; derived snapshots are detached JSON values.

    Content revisions latch a conflict until this registry is discarded. They
    never alter snapshots already returned or overwrite the first observation.
    Eviction loses proof and therefore produces UNKNOWN, not a guessed time.
    """

    def __init__(self, *, max_candles=1500, max_series=32):
        if (isinstance(max_candles, bool) or isinstance(max_series, bool) or
                not isinstance(max_candles, int) or not isinstance(max_series, int) or
                max_candles < 1 or max_series < 1):
            raise ValueError("observation bounds must be positive integers")
        self.max_candles, self.max_series = max_candles, max_series
        self._series = OrderedDict()
        self._lock = threading.RLock()

    def record_closed_batch(self, symbol, timeframe, bars, *, available_at, source,
                            exchange, market_type):
        observed = _utc(available_at)
        provenance = _provenance(source, exchange, market_type)
        # Bound both retained state and processing; feeds already bound their
        # batches and all earlier inputs remain explicitly unproven if evicted.
        prepared = []
        for bar in list(bars)[-self.max_candles:]:
            row = _candle(bar, timeframe)
            if _utc(row["close_timestamp"]) > observed:
                raise ValueError("forming candle cannot be an observed closed input")
            prepared.append((row, _hash(row)))
        key = (_symbol(symbol), timeframe)
        with self._lock:
            series = self._series.setdefault(key, {})
            self._series.move_to_end(key)
            for row, content_hash in prepared:
                stamp = row["open_time"]
                known = series.get(stamp)
                version = {**row, **provenance, "available_at": observed.isoformat(),
                           "observation_hash": content_hash}
                if known is None:
                    series[stamp] = {"original": version, "latest": version, "conflicted": False}
                elif known["original"]["observation_hash"] == content_hash:
                    # A clock regression cannot manufacture earlier receipt.
                    # The first call is the first observation, regardless of a
                    # later caller supplying a different clock value.
                    original = known["original"]
                    if (original["exchange"] is not None and provenance["exchange"] is not None and
                            (original["exchange"], original["market_type"]) !=
                            (provenance["exchange"], provenance["market_type"])):
                        known["conflicted"] = True
                else:
                    known["conflicted"] = True
                    if known["latest"]["observation_hash"] != content_hash:
                        known["latest"] = version
            for stamp in sorted(series)[:-self.max_candles]:
                del series[stamp]
            while len(self._series) > self.max_series:
                self._series.popitem(last=False)

    def _rows(self, symbol, timeframe, bars, decision_close, cutoff, *, replay, reasons):
        result = []
        series = self._series.get((_symbol(symbol), timeframe), {})
        for bar in list(bars)[-self.max_candles:]:
            row = _candle(bar, timeframe)
            if _utc(row["close_timestamp"]) > decision_close:
                continue
            content_hash = _hash(row)
            known = series.get(row["open_time"])
            match = next((version for version in (known["original"], known["latest"])
                          if version["observation_hash"] == content_hash), None) if known else None
            if match is None:
                row.update(available_at=None, source=None, exchange=None, market_type=None,
                           source_quality="UNKNOWN", observation_hash=content_hash)
                reasons.add("ORIGINAL_CANDLE_AVAILABILITY_UNPROVEN")
            else:
                row = copy.deepcopy(match)
                if known["conflicted"]:
                    row["source_quality"] = "UNKNOWN"
                    reasons.add("CONFLICTING_CANDLE_OBSERVATIONS")
                if _utc(row["available_at"]) > cutoff:
                    row["source_quality"] = "UNKNOWN"
                    reasons.add("CANDLE_OBSERVED_AFTER_SIGNAL")
                if row["source_quality"] == "UNKNOWN":
                    reasons.add("UNKNOWN_MARKET_DATA_PROVENANCE")
            if replay:
                row["source_quality"] = "UNKNOWN"
                reasons.add("HISTORICAL_AVAILABILITY_UNPROVEN")
            result.append(row)
        return result

    def freeze(self, symbol, strategy, *, entry_timeframe, signal_timestamp,
               signal_observed_at, source, execution_mode):
        from services.market_context_classifier import classifier_definition

        opened, observed = _utc(signal_timestamp), _utc(signal_observed_at)
        duration = TF_SECONDS.get(entry_timeframe)
        if duration is None:
            raise ValueError("unsupported entry timeframe")
        decision_close = opened + timedelta(seconds=duration)
        replay = execution_mode != "forward_paper"
        cutoff = decision_close if replay else observed
        if not replay and decision_close > observed:
            raise ValueError("signal observation precedes decision candle closure")
        try:
            higher_timeframe = policy_for(entry_timeframe)[0]
        except ValueError:
            higher_timeframe = None
        # Strategy context has already received native alignment. Access only
        # that detached input, never the engine's unaligned future feed cache.
        native = getattr(strategy, "_native_mtf_context", None)
        if native is None:
            native = getattr(strategy, "_context", {})
        native = native or {}
        reasons = set()
        with self._lock:
            entry = self._rows(symbol, entry_timeframe, getattr(strategy, "bars", ()) or (),
                decision_close, cutoff, replay=replay, reasons=reasons)
            higher = self._rows(symbol, higher_timeframe, native.get(higher_timeframe, ()),
                decision_close, cutoff, replay=replay, reasons=reasons) if higher_timeframe else []
        if not entry:
            reasons.add("ENTRY_CANDLES_UNAVAILABLE")
        if not higher:
            reasons.add("NATIVE_HTF_UNAVAILABLE")
        exchanges = {row.get("exchange") for row in entry + higher if row.get("exchange")}
        markets = {row.get("market_type") for row in entry + higher if row.get("market_type")}
        snapshot = {
            "symbol": _symbol(symbol), "execution_mode": execution_mode,
            "entry_timeframe": entry_timeframe, "higher_timeframe": higher_timeframe,
            "signal_timestamp": cutoff.isoformat(), "signal_candle_timestamp": opened.isoformat(),
            "signal_observed_at": observed.isoformat(),
            "decision_candle_close_timestamp": decision_close.isoformat(),
            "market_data_source": str(source or ""),
            "exchange": next(iter(exchanges)) if len(exchanges) == 1 else None,
            "market_type": next(iter(markets)) if len(markets) == 1 else None,
            "market_data_timestamp": max((row["available_at"] for row in entry if row["available_at"]), default=None),
            "last_closed_candle_timestamp": max((row["close_timestamp"] for row in entry), default=None),
            "entry_candles": entry, "higher_candles": higher,
            "classifier_definition": copy.deepcopy(classifier_definition()),
            "evidence_quality": "UNKNOWN" if reasons else "COMPLETE",
            "quality_reasons": sorted(reasons),
            "availability_basis": "ACTUAL_FEED_OBSERVATION" if not replay else "HISTORICAL_AVAILABILITY_UNPROVEN",
        }
        snapshot["original_input_hash"] = _hash(snapshot)
        return snapshot
