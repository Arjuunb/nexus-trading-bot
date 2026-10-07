"""Bounded source-side evidence for actual SMC paper exit fills.

Formatting is part of the existing broker transaction. Strict decoding is for
readers only: neither Guardian nor this projection decides whether to trade.
This is a snapshot at a fill, NOT a history of protection changes or journal
close certification. Legacy fills remain null and are never reconstructed.
"""
from __future__ import annotations

import json
from datetime import datetime

from execution.paper_fill_provenance import POSITION_FIELDS, _number, _text

MAX_BYTES = 8192
SCOPE = "SMC_PAPER_EXIT_FILL"
KINDS = {"POSITION_STOP_LOSS", "POSITION_TAKE_PROFIT", "POSITION_TRAILING_STOP",
         "ORDER_TRAILING_STOP", "ORDER_REDUCE_ONLY", "NETTING_FILL",
         "LEGACY_POSITION_REMEDIATION", "PAPER_LIQUIDATION"}
PROTECTION_FIELDS = {"stop_loss", "take_profit", "trailing_offset", "peak_price"}
ORDER_FIELDS = {"type", "limit_price", "stop_price", "trailing_offset"}
OBSERVATION_FIELDS = {"timestamp", "quote_event_id", "open", "high", "low", "close", "bid", "ask"}
FIELDS = {"schema_version", "scope", "account_id", "fill_id", "order_id", "symbol", "side",
          "quantity", "closed_quantity", "price", "raw_reference_price", "reduce_only",
          "persisted_order", "position", "protection", "order", "trigger_kind", "trigger_price",
          "effective_stop", "effective_peak", "fill_source", "observation"}


def encode_exit_fill(**fields):
    value = {"schema_version": 1, "scope": SCOPE, **fields}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Paper exit provenance exceeds bound")
    return raw


def observation(bar):
    """Retain only the input used for this fill, never a rolling window."""
    stamp = bar.get("timestamp")
    if isinstance(stamp, datetime):
        stamp = stamp.isoformat()
    # Some compatibility callers supply OHLCV without an identified candle.
    # An absent/non-text timestamp is unknown, not inferred from fill wall time.
    stamp = stamp if isinstance(stamp, str) else None
    return {key: (stamp if key == "timestamp" else bar.get(key)) for key in OBSERVATION_FIELDS}


def decode_exit_fill(raw):
    """Strict read contract; cannot certify source history or current risk."""
    if not isinstance(raw, str):
        raise ValueError("Invalid exit provenance payload")
    try:
        if len(raw.encode("utf-8")) > MAX_BYTES:
            raise ValueError("Exit provenance exceeds bound")
        value = json.loads(raw, object_pairs_hook=_unique_fields)
    except (UnicodeError, RecursionError, ValueError) as exc:
        raise ValueError("Invalid exit provenance JSON") from exc
    if (not isinstance(value, dict) or set(value) != FIELDS or
            type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["scope"] != SCOPE):
        raise ValueError("Invalid exit provenance contract")
    for key in ("account_id", "fill_id", "order_id", "symbol"):
        _text(value[key])
    if (value["side"] not in ("buy", "sell") or
            any(type(value[key]) is not bool for key in ("reduce_only", "persisted_order"))):
        raise ValueError("Invalid exit provenance flags")
    for key in ("quantity", "closed_quantity", "price", "raw_reference_price"):
        _number(value[key])
    pos = value["position"]
    if not isinstance(pos, dict) or set(pos) != POSITION_FIELDS or pos["side"] not in ("long", "short"):
        raise ValueError("Invalid exit position")
    for key in ("position_id", "entry_order_id", "entry_execution_key", "entry_timeframe"):
        if pos[key] is not None:
            _text(pos[key])
    _number(pos["size"])
    _number(pos["entry_price"])
    if ((pos["side"] == "long") == (value["side"] == "buy") or
            value["closed_quantity"] != min(value["quantity"], pos["size"]) or
            (value["reduce_only"] and value["quantity"] > pos["size"])):
        raise ValueError("Exit quantity/direction contradicts original position")
    for group, fields in (("protection", PROTECTION_FIELDS), ("order", ORDER_FIELDS),
                          ("observation", OBSERVATION_FIELDS)):
        item = value[group]
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("Invalid exit evidence projection")
        for key in fields - {"type", "timestamp", "quote_event_id"}:
            if item[key] is not None:
                _number(item[key])
    if value["order"]["type"] not in ("market", "limit", "stop", "stop_limit", "trailing_stop"):
        raise ValueError("Invalid exit order type")
    for key in ("trigger_price", "effective_stop", "effective_peak"):
        if value[key] is not None:
            _number(value[key])
    stamp = value["observation"]["timestamp"]
    if stamp is not None:
        _text(stamp)
        try:
            if datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("Unidentified exit observation timezone")
        except ValueError as exc:
            raise ValueError("Invalid exit observation time") from exc
    quote_id = value["observation"]["quote_event_id"]
    if quote_id is not None:
        _text(quote_id)
    kind, source = value["trigger_kind"], value["fill_source"]
    if not isinstance(kind, str) or kind not in KINDS or source not in ("CANDLE", "TICK", "MARK"):
        raise ValueError("Invalid exit trigger/source")
    if kind == "NETTING_FILL":
        if value["reduce_only"] or not value["persisted_order"] or source == "MARK":
            raise ValueError("Netting fill is not a protective exit")
    elif not value["reduce_only"]:
        raise ValueError("Exit trigger requires a reduce-only fill")
    if kind.startswith("POSITION_"):
        if value["persisted_order"] or source == "MARK" or value["trigger_price"] is None:
            raise ValueError("Invalid position protection trigger")
        if kind == "POSITION_TAKE_PROFIT":
            if value["trigger_price"] != value["protection"]["take_profit"]:
                raise ValueError("Target trigger disagrees with protection snapshot")
        elif value["trigger_price"] != value["effective_stop"]:
            raise ValueError("Stop trigger disagrees with effective stop")
        if kind == "POSITION_STOP_LOSS" and value["effective_stop"] != value["protection"]["stop_loss"]:
            raise ValueError("Static stop trigger disagrees with stored stop")
        if kind == "POSITION_TRAILING_STOP" and (
                source != "CANDLE" or value["protection"]["trailing_offset"] is None or
                value["effective_peak"] is None):
            raise ValueError("Unidentified trailing stop")
        if kind == "POSITION_TRAILING_STOP":
            delta = value["protection"]["trailing_offset"]
            stored = value["protection"]["stop_loss"]
            effective = value["effective_peak"] + (-delta if pos["side"] == "long" else delta)
            effective = (max(stored, effective) if pos["side"] == "long" else min(stored, effective)) if stored is not None else effective
            if value["effective_stop"] != effective or value["effective_stop"] == stored:
                raise ValueError("Trailing trigger disagrees with recorded inputs")
    if kind.startswith("ORDER_") and (not value["persisted_order"] or source == "MARK"):
        raise ValueError("Invalid explicit order exit")
    if kind == "ORDER_TRAILING_STOP" and (
            source != "CANDLE" or value["order"]["type"] != "trailing_stop" or value["trigger_price"] is None):
        raise ValueError("Invalid explicit trailing order")
    if kind in ("PAPER_LIQUIDATION", "LEGACY_POSITION_REMEDIATION") and (
            value["persisted_order"] or source != "MARK"):
        raise ValueError("Invalid synthetic mark exit")
    return value


def _unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Ambiguous duplicate exit evidence field")
        value[key] = item
    return value
