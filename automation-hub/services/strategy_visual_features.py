"""Turn a running strategy's own state into chart overlays.

Every object this produces is read out of the live strategy the instance run
loop is driving, or computed by the very function that strategy calls. Nothing
here re-derives a zone, a pivot or a break of structure: the Price Action and
SMC engines already hold theirs as state, and the indicator strategies compute
theirs from ``bot.data.indicators``, so the honest thing is to read the first
and call the second.

That distinction is the whole design. An overlay produced from a parallel
implementation looks identical on screen and is worthless the moment the two
disagree, because nothing on the chart tells you which one the bot obeyed.

Every overlay carries provenance -- which strategy, which module, which field
it came from -- so a shape on the chart can always be traced back to the code
that produced it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def _iso(value) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (
        str(value) if value is not None else None)


@dataclass
class Overlay:
    """One drawable object, with the evidence that produced it."""
    kind: str                      # "zone" | "level" | "line" | "marker" | "box"
    feature: str                   # the Feature vocabulary value
    id: str
    provenance: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"kind": self.kind, "feature": self.feature, "id": self.id,
                "provenance": self.provenance, **self.payload}


class FeatureUnavailable(RuntimeError):
    """The runtime does not expose this strategy's features (yet).

    Raised rather than returning an empty overlay set, because "this strategy
    draws nothing" and "we cannot see what it drew" look identical on a chart
    and mean opposite things.
    """


def _engine_of(strategy) -> Any:
    engine = getattr(strategy, "_engine", None)
    if engine is None:
        raise FeatureUnavailable(
            "the strategy has not built its engine yet (no closed candle "
            "has been processed since the worker started)")
    return engine


# ------------------------------------------------------------- price action

def _price_action_overlays(strategy, strategy_id: str) -> list[Overlay]:
    """Zones, swings and rejection events, read from NativePriceActionEngine."""
    engine = _engine_of(strategy)
    module = "services.native_price_action"
    out: list[Overlay] = []

    for zone in getattr(engine, "zones", {}).values():
        out.append(Overlay(
            kind="zone",
            feature="support" if zone.role == "support" else "resistance",
            id=zone.id,
            provenance={"strategy_id": strategy_id, "module": module,
                        "field": "engine.zones", "object": "PriceZone"},
            payload={
                "role": zone.role, "original_role": zone.original_role,
                "flipped": zone.role != zone.original_role,
                "lower": float(zone.low), "upper": float(zone.high),
                "created_at": _iso(zone.created_at),
                "confirmed_at": _iso(zone.confirmed_at),
                "touch_count": getattr(zone, "touch_count", 0),
                "active": bool(getattr(zone, "active", True)),
                "status": ("active" if getattr(zone, "active", True) else "invalidated"),
                "invalidated_at": _iso(getattr(zone, "invalidated_at", None)),
                "expiration_reason": getattr(zone, "expiration_reason", None),
                "source_swing_ids": list(getattr(zone, "source_swing_ids", []) or []),
                "label": ("Flip " if zone.role != zone.original_role else "") + zone.role.title(),
            }))

    for swing in getattr(engine, "swings", {}).values():
        out.append(Overlay(
            kind="marker", feature="swing_high_low", id=swing.id,
            provenance={"strategy_id": strategy_id, "module": module,
                        "field": "engine.swings", "object": "ConfirmedSwing"},
            payload={"kind_label": swing.label, "price": float(swing.price),
                     "occurred_at": _iso(swing.occurred_at),
                     "confirmed_at": _iso(swing.confirmed_at),
                     "direction": "high" if swing.kind == "high" else "low"}))

    for event in getattr(engine, "events", {}).values():
        out.append(Overlay(
            kind="marker", feature="rejection_candle", id=event.id,
            provenance={"strategy_id": strategy_id, "module": module,
                        "field": "engine.events", "object": "PriceActionEvent"},
            payload={"event_type": event.event_type, "direction": event.direction,
                     "price": float(event.level), "occurred_at": _iso(event.occurred_at),
                     "confirmed_at": _iso(event.confirmed_at),
                     "zone_id": event.zone_id, "pattern": getattr(event, "pattern", None),
                     "reasons": list(getattr(event, "reasons", ()) or ())}))
    return out


# --------------------------------------------------------------------- smc

def _smc_overlays(strategy, strategy_id: str) -> list[Overlay]:
    """Order blocks, fair value gaps, pivots, structure breaks and sweeps."""
    engine = _engine_of(strategy)
    module = "services.native_smc"
    out: list[Overlay] = []

    for block in getattr(engine, "obs", {}).values():
        bullish = str(block.direction).lower() in ("bullish", "long", "up")
        out.append(Overlay(
            kind="zone", feature="demand" if bullish else "supply", id=block.id,
            provenance={"strategy_id": strategy_id, "module": module,
                        "field": "engine.obs", "object": "OrderBlock"},
            payload={"direction": block.direction,
                     "lower": float(block.low), "upper": float(block.high),
                     "created_at": _iso(block.created_at),
                     "active": bool(block.active), "mitigated": bool(block.mitigated),
                     "status": ("mitigated" if block.mitigated
                                else "active" if block.active else "invalidated"),
                     "mitigation_at": _iso(block.mitigation_at),
                     "source_pivot_id": block.source_pivot_id,
                     "source_structure_id": block.source_structure_id,
                     "label": "Demand" if bullish else "Supply"}))

    for gap in getattr(engine, "fvgs", {}).values():
        out.append(Overlay(
            kind="box", feature="fvg", id=gap.id,
            provenance={"strategy_id": strategy_id, "module": module,
                        "field": "engine.fvgs", "object": "FairValueGap"},
            payload={"direction": gap.direction,
                     "lower": float(gap.bottom), "upper": float(gap.top),
                     "created_at": _iso(gap.created_at),
                     "origin": [_iso(t) for t in (gap.origin or ())],
                     "active": bool(gap.active), "mitigated": bool(gap.mitigated),
                     "status": ("filled" if gap.mitigated
                                else "unfilled" if gap.active else "invalidated"),
                     "mitigation_at": _iso(gap.mitigation_at),
                     "label": f"FVG {gap.direction}"}))

    for pivot in getattr(engine, "pivots", {}).values():
        out.append(Overlay(
            kind="marker", feature="swing_high_low", id=pivot.id,
            provenance={"strategy_id": strategy_id, "module": module,
                        "field": "engine.pivots", "object": "PivotPoint"},
            payload={"kind_label": pivot.kind, "price": float(pivot.price),
                     "occurred_at": _iso(pivot.occurred_at),
                     "confirmed_at": _iso(pivot.confirmed_at),
                     "scope": getattr(pivot, "scope", "internal"),
                     "direction": "high" if pivot.kind in ("high", "swing_high") else "low"}))

    for event in getattr(engine, "events", {}).values():
        # The same dict holds structure breaks and sweeps; they are told apart
        # by shape rather than by a flag, because that is how the engine stores
        # them and guessing from the id would be a second source of truth.
        event_type = str(getattr(event, "event_type", "") or "")
        is_sweep = not event_type and hasattr(event, "bar_index")
        if is_sweep:
            out.append(Overlay(
                kind="marker", feature="liquidity_sweep", id=event.id,
                provenance={"strategy_id": strategy_id, "module": module,
                            "field": "engine.events", "object": "LiquiditySweep"},
                payload={"direction": event.direction, "price": float(event.level),
                         "occurred_at": _iso(event.timestamp),
                         "bar_index": event.bar_index,
                         "label": f"Sweep {event.direction}"}))
            continue
        feature = "choch" if "choch" in event_type.lower() else "bos"
        out.append(Overlay(
            kind="marker", feature=feature, id=event.id,
            provenance={"strategy_id": strategy_id, "module": module,
                        "field": "engine.events", "object": "StructureEvent"},
            payload={"event_type": event_type, "direction": event.direction,
                     "price": float(event.level),
                     "occurred_at": _iso(event.occurred_at),
                     "confirmed_at": _iso(event.confirmed_at),
                     "scope": getattr(event, "scope", ""),
                     "break_price": float(getattr(event, "break_price", event.level)),
                     "source_pivot_id": getattr(event, "source_pivot_id", None),
                     "label": f"{event_type.upper()} {event.direction}"}))

    bias = getattr(engine, "swing_bias", 0)
    out.append(Overlay(
        kind="context", feature="htf_bias", id="swing_bias",
        provenance={"strategy_id": strategy_id, "module": module,
                    "field": "engine.swing_bias", "object": "int"},
        payload={"bias": "bullish" if bias > 0 else "bearish" if bias < 0 else "neutral",
                 "internal_bias": getattr(engine, "internal_bias", 0)}))
    return out


# ----------------------------------------------------------- pa rulebook

def _rulebook_overlays(strategy, strategy_id: str) -> list[Overlay]:
    engine = getattr(strategy, "_engine", None)
    if engine is None:
        raise FeatureUnavailable("the rulebook engine has not been built yet")
    module = "services.pa_rulebook_v01"
    out: list[Overlay] = []
    consumed = set(getattr(engine, "consumed_zone_ids", ()) or ())
    for zone in getattr(engine, "zones", []) or []:
        out.append(Overlay(
            kind="zone",
            feature="support" if zone.kind == "support" else "resistance",
            id=zone.id,
            provenance={"strategy_id": strategy_id, "module": module,
                        "field": "engine.zones", "object": "Zone"},
            payload={"role": zone.kind, "lower": float(zone.lower),
                     "upper": float(zone.upper), "created_at": _iso(zone.created_at),
                     "origin": zone.origin, "retired": bool(zone.retired),
                     "consumed": zone.id in consumed,
                     "status": ("retired" if zone.retired else
                                "consumed" if zone.id in consumed else "active"),
                     "creation_atr": getattr(zone, "creation_atr", None),
                     "label": zone.kind.title()}))
    evidence = getattr(engine, "regime_evidence", None)
    if evidence:
        out.append(Overlay(
            kind="context", feature="regime", id="regime",
            provenance={"strategy_id": strategy_id, "module": module,
                        "field": "engine.regime_evidence", "object": "dict"},
            payload={"evidence": dict(evidence)}))
    return out


# ------------------------------------------------------- indicator strategies

def _series_overlay(strategy_id: str, feature: str, overlay_id: str, label: str,
                    bars, values, field_name: str) -> Overlay:
    """A line, sampled at the exact bars the strategy itself indexed."""
    points = [{"t": _iso(bar.timestamp), "v": float(value)}
              for bar, value in zip(bars[-len(values):], values)
              if value is not None]
    return Overlay(
        kind="line", feature=feature, id=overlay_id,
        provenance={"strategy_id": strategy_id, "module": "bot.data.indicators",
                    "field": field_name,
                    "note": "computed by the same function the strategy calls, "
                            "over the same bars, with the instance's own parameters"},
        payload={"label": label, "points": points})


def _indicator_overlays(strategy, strategy_id: str) -> list[Overlay]:
    """Lines from the authoritative indicator functions, never a re-implementation.

    The strategies below keep no feature state -- they compute inside
    ``generate`` and discard. So rather than inventing a parallel EMA in the
    browser, the same ``bot.data.indicators`` function is called here over the
    strategy's own ``self.bars`` with the strategy's own ``self.params``.
    """
    from bot.data.indicators import ema

    bars = list(getattr(strategy, "bars", []) or [])
    params = dict(getattr(strategy, "params", {}) or {})
    if len(bars) < 5:
        raise FeatureUnavailable(
            f"the strategy has only {len(bars)} bars; nothing to draw yet")
    closes = [float(bar.close) for bar in bars]
    out: list[Overlay] = []

    if strategy_id in ("ema", "ensemble"):
        for name, key in (("fast", "fast"), ("slow", "slow")):
            period = params.get(key)
            if period:
                out.append(_series_overlay(
                    strategy_id, "ema", f"ema_{name}", f"EMA {period}",
                    bars, ema(closes, int(period)), f"ema(closes, params['{key}'])"))

    if strategy_id in ("donchian", "ensemble"):
        channel = int(params.get("channel") or 0)
        if channel and len(bars) > channel + 1:
            highs, lows, stamps = [], [], []
            for index in range(channel + 1, len(bars) + 1):
                window = bars[index - channel - 1:index - 1]
                highs.append(max(b.high for b in window))
                lows.append(min(b.low for b in window))
                stamps.append(bars[index - 1].timestamp)
            provenance = {"strategy_id": strategy_id,
                          "module": "strategies.donchian_strategy",
                          "field": "max(prior.high) / min(prior.low)",
                          "note": ("the same window the strategy slices: the "
                                   f"{channel} bars BEFORE the current one, so the "
                                   "current bar's own extreme is excluded")}
            for overlay_id, label, values in (("donchian_high", f"Donchian high {channel}", highs),
                                              ("donchian_low", f"Donchian low {channel}", lows)):
                out.append(Overlay(
                    kind="line", feature="donchian_channel", id=overlay_id,
                    provenance=provenance,
                    payload={"label": label,
                             "points": [{"t": _iso(t), "v": float(v)}
                                        for t, v in zip(stamps, values)]}))

    if not out:
        raise FeatureUnavailable(
            f"'{strategy_id}' keeps no feature state and has no registered "
            "authoritative series; nothing may be drawn for it")
    return out


_EXTRACTORS = {
    "price_action_rejection": _price_action_overlays,
    "price_action_flip_retest": _price_action_overlays,
    "smc": _smc_overlays,
    "pa_rulebook": _rulebook_overlays,
    "ema": _indicator_overlays,
    "donchian": _indicator_overlays,
    "ensemble": _indicator_overlays,
}


def supported() -> tuple[str, ...]:
    return tuple(sorted(_EXTRACTORS))


def extract(strategy_id: str, strategy) -> list[Overlay]:
    """Overlays for a running strategy, or a refusal naming the reason.

    A strategy with no extractor is refused rather than drawn empty. Silence
    here would read as "this strategy sees nothing", which is a claim about the
    market rather than about the plumbing.
    """
    extractor = _EXTRACTORS.get(strategy_id)
    if extractor is None:
        raise FeatureUnavailable(
            f"no runtime feature extractor for '{strategy_id}'. Its engine does "
            "not publish overlay state, and the Visual Lab will not invent it.")
    return extractor(strategy, strategy_id)
