"""Pure source evidence formatting; no Guardian/runtime/strategy dependency.

Snapshots are taken by the broker around its existing position mutation. This
does not reconstruct legacy history or allocate a net position to entry lots.
"""
from __future__ import annotations

import json
import math

MAX_BYTES = 8192
SCOPE = "SMC_PAPER_FILL_POSITION_TRANSITION"
POSITION_FIELDS = {"position_id", "entry_order_id", "entry_execution_key", "entry_timeframe",
                   "side", "size", "entry_price"}
FIELDS = {"schema_version", "scope", "account_id", "fill_id", "order_id", "symbol", "side",
          "quantity", "price", "reduce_only", "persisted_order", "before", "after", "effect"}


def effect(before, after):
    if before is None:
        return "OPEN" if after is not None else "UNCHANGED"
    if after is None:
        return "CLOSE"
    if before["position_id"] != after["position_id"] or before["side"] != after["side"]:
        return "REVERSE"
    return ("INCREASE" if after["size"] > before["size"] else
            "REDUCE" if after["size"] < before["size"] else "UNCHANGED")


def encode_transition(**fields):
    value = {"schema_version": 1, "scope": SCOPE, **fields,
             "effect": effect(fields["before"], fields["after"])}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode()) > MAX_BYTES:
        raise ValueError("Paper fill provenance exceeds bound")
    return raw


def decode_transition(raw):
    """Strict read projection. Never invoke this to approve a trading action."""
    if not isinstance(raw, str) or len(raw.encode()) > MAX_BYTES:
        raise ValueError("Invalid fill provenance payload")
    try:
        value = json.loads(raw)
    except (RecursionError, ValueError) as exc:
        raise ValueError("Invalid fill provenance JSON") from exc
    if (not isinstance(value, dict) or set(value) != FIELDS or
            type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["scope"] != SCOPE):
        raise ValueError("Invalid fill provenance contract")
    for key in ("account_id", "fill_id", "order_id", "symbol"):
        _text(value[key])
    if value["side"] not in {"buy", "sell"} or any(type(value[k]) is not bool for k in ("reduce_only", "persisted_order")):
        raise ValueError("Invalid fill provenance flags")
    for key in ("quantity", "price"):
        _number(value[key])
    for key in ("before", "after"):
        pos = value[key]
        if pos is None:
            continue
        if not isinstance(pos, dict) or set(pos) != POSITION_FIELDS or pos["side"] not in {"long", "short"}:
            raise ValueError("Invalid fill position projection")
        for field in ("position_id", "entry_order_id", "entry_execution_key", "entry_timeframe"):
            if pos[field] is not None:
                _text(pos[field])
        _number(pos["size"])
        _number(pos["entry_price"])
    if value["effect"] != effect(value["before"], value["after"]):
        raise ValueError("Fill position effect disagrees with snapshots")
    return value


def _text(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("Invalid fill provenance identity")
    # JSON may contain escaped unpaired surrogates. Reject them before an API
    # response attempts UTF-8 encoding; never use this read validator to trade.
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Invalid fill provenance text encoding") from exc


def _number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Invalid fill provenance quantity or price")
