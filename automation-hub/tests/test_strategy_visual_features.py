"""Overlays must carry the runtime's own numbers, not a recomputation.

The failure this guards against is subtle and expensive: a chart that draws a
zone one tick away from where the strategy put it looks right, and is worthless
the moment you use it to decide whether the bot behaved. So each test asserts
the overlay's geometry equals the engine's stored value exactly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.strategy_visual_features import (
    FeatureUnavailable, extract, supported,
)

NOW = datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc)


class _Stub:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Strategy:
    def __init__(self, engine=None, bars=(), params=None):
        self._engine = engine
        self.bars = list(bars)
        self.params = dict(params or {})


def _bars(count=80, start=100.0):
    from bot.types import Bar
    return [Bar(NOW + timedelta(minutes=5 * i), start + i, start + i + 1.0,
                start + i - 1.0, start + i + 0.5, 10.0) for i in range(count)]


# ------------------------------------------------------------- price action

def test_a_price_action_zone_is_drawn_at_the_runtime_bounds():
    """Acceptance 5: zones appear at exact bounds."""
    zone = _Stub(id="z-1", role="resistance", original_role="support",
                 low=108_240.5, high=108_910.25, created_at=NOW,
                 confirmed_at=NOW + timedelta(minutes=15), touch_count=2,
                 active=True, source_swing_ids=["s-1"])
    engine = _Stub(zones={"z-1": zone}, swings={}, events={})

    overlays = extract("price_action_rejection", _Strategy(engine))
    drawn = next(o for o in overlays if o.id == "z-1")

    assert drawn.kind == "zone"
    assert drawn.feature == "resistance"
    assert drawn.payload["lower"] == 108_240.5
    assert drawn.payload["upper"] == 108_910.25
    assert drawn.payload["flipped"] is True          # role differs from original
    assert drawn.payload["label"] == "Flip Resistance"
    assert drawn.payload["touch_count"] == 2
    assert drawn.provenance["module"] == "services.native_price_action"
    assert drawn.provenance["field"] == "engine.zones"


def test_an_invalidated_zone_is_marked_not_dropped():
    """Requirement 4: never silently remove historical evidence."""
    zone = _Stub(id="z-dead", role="support", original_role="support",
                 low=100.0, high=101.0, created_at=NOW, confirmed_at=NOW,
                 touch_count=3, active=False, source_swing_ids=[],
                 invalidated_at=NOW + timedelta(hours=1),
                 expiration_reason="closed through")
    overlays = extract("price_action_rejection",
                       _Strategy(_Stub(zones={"z-dead": zone}, swings={}, events={})))
    drawn = overlays[0]
    assert drawn.payload["status"] == "invalidated"
    assert drawn.payload["invalidated_at"] is not None
    assert drawn.payload["expiration_reason"] == "closed through"


def test_a_swing_carries_its_confirmation_candle():
    """Acceptance 7's principle: attach to the candle that confirmed it."""
    swing = _Stub(id="s-1", kind="high", price=109_400.0, label="HH",
                  occurred_at=NOW, confirmed_at=NOW + timedelta(minutes=30),
                  occurred_index=10, confirmed_index=12)
    overlays = extract("price_action_rejection",
                       _Strategy(_Stub(zones={}, swings={"s-1": swing}, events={})))
    drawn = overlays[0]
    assert drawn.feature == "swing_high_low"
    assert drawn.payload["price"] == 109_400.0
    assert drawn.payload["kind_label"] == "HH"
    assert drawn.payload["occurred_at"] == NOW.isoformat()
    assert drawn.payload["confirmed_at"] == (NOW + timedelta(minutes=30)).isoformat()


# --------------------------------------------------------------------- smc

def test_order_blocks_become_supply_and_demand_rectangles():
    bull = _Stub(id="ob-1", direction="bullish", high=101.0, low=99.0,
                 source_pivot_id="p1", source_structure_id="e1", created_at=NOW,
                 active=True, mitigated=False, mitigation_at=None)
    bear = _Stub(id="ob-2", direction="bearish", high=120.0, low=118.5,
                 source_pivot_id="p2", source_structure_id="e2", created_at=NOW,
                 active=False, mitigated=True, mitigation_at=NOW)
    engine = _Stub(obs={"ob-1": bull, "ob-2": bear}, fvgs={}, pivots={},
                   events={}, swing_bias=1, internal_bias=0)

    overlays = {o.id: o for o in extract("smc", _Strategy(engine))}
    assert overlays["ob-1"].feature == "demand"
    assert overlays["ob-1"].payload["lower"] == 99.0
    assert overlays["ob-1"].payload["upper"] == 101.0
    assert overlays["ob-2"].feature == "supply"
    assert overlays["ob-2"].payload["status"] == "mitigated"
    assert overlays["ob-1"].provenance["field"] == "engine.obs"


def test_fvg_bounds_match_the_runtime_evidence():
    """Acceptance 8."""
    gap = _Stub(id="fvg-1", direction="bullish", top=105.25, bottom=104.75,
                created_at=NOW, origin=(NOW, NOW, NOW), active=True,
                mitigated=False, mitigation_at=None)
    engine = _Stub(obs={}, fvgs={"fvg-1": gap}, pivots={}, events={},
                   swing_bias=0, internal_bias=0)
    drawn = next(o for o in extract("smc", _Strategy(engine)) if o.id == "fvg-1")
    assert drawn.kind == "box" and drawn.feature == "fvg"
    assert drawn.payload["lower"] == 104.75
    assert drawn.payload["upper"] == 105.25
    assert drawn.payload["status"] == "unfilled"
    assert len(drawn.payload["origin"]) == 3


def test_structure_events_attach_to_their_source_candle():
    """Acceptance 7: BOS/CHoCH on the exact candle that confirmed them."""
    bos = _Stub(id="e-1", symbol="BTCUSDT", timeframe="5m", scope="swing",
                event_type="bos", direction="bullish", level=110.0,
                occurred_at=NOW, confirmed_at=NOW + timedelta(minutes=5),
                source_pivot_id="p-1", break_price=110.5)
    choch = _Stub(id="e-2", symbol="BTCUSDT", timeframe="5m", scope="internal",
                  event_type="choch", direction="bearish", level=99.0,
                  occurred_at=NOW, confirmed_at=NOW, source_pivot_id="p-2",
                  break_price=98.5)
    engine = _Stub(obs={}, fvgs={}, pivots={}, events={"e-1": bos, "e-2": choch},
                   swing_bias=0, internal_bias=0)

    overlays = {o.id: o for o in extract("smc", _Strategy(engine))}
    assert overlays["e-1"].feature == "bos"
    assert overlays["e-1"].payload["confirmed_at"] == (NOW + timedelta(minutes=5)).isoformat()
    assert overlays["e-1"].payload["break_price"] == 110.5
    assert overlays["e-2"].feature == "choch"
    assert overlays["e-2"].payload["direction"] == "bearish"


def test_a_liquidity_sweep_is_told_apart_from_a_structure_break():
    """They share one dict in the engine and are distinguished by shape, not by
    a guess at the id -- guessing would be a second source of truth."""
    sweep = _Stub(id="sw-1", symbol="BTCUSDT", timeframe="5m", direction="bullish",
                  level=98.0, timestamp=NOW, bar_index=42)
    engine = _Stub(obs={}, fvgs={}, pivots={}, events={"sw-1": sweep},
                   swing_bias=0, internal_bias=0)
    drawn = next(o for o in extract("smc", _Strategy(engine)) if o.id == "sw-1")
    assert drawn.feature == "liquidity_sweep"
    assert drawn.payload["bar_index"] == 42
    assert drawn.payload["price"] == 98.0


# -------------------------------------------------------------- indicators

def test_ema_points_come_from_the_authoritative_function():
    """Acceptance 6. Not "an EMA" -- the one the strategy itself calls, over
    its own bars, with its own parameters."""
    from bot.data.indicators import ema

    bars = _bars(60)
    strategy = _Strategy(bars=bars, params={"fast": 8, "slow": 30})
    overlays = {o.id: o for o in extract("ema", strategy)}

    closes = [float(b.close) for b in bars]
    expected = ema(closes, 8)
    drawn = [point["v"] for point in overlays["ema_fast"].payload["points"]]
    assert drawn == [float(v) for v in expected if v is not None]
    assert overlays["ema_fast"].payload["label"] == "EMA 8"
    assert overlays["ema_slow"].payload["label"] == "EMA 30"
    assert overlays["ema_fast"].provenance["module"] == "bot.data.indicators"


def test_the_donchian_channel_excludes_the_current_bar():
    """The strategy slices the N bars BEFORE the current one. A channel drawn
    including it would sit at a different price and never be broken."""
    bars = _bars(50)
    strategy = _Strategy(bars=bars, params={"channel": 30})
    overlays = {o.id: o for o in extract("donchian", strategy)}

    high_points = overlays["donchian_high"].payload["points"]
    last = high_points[-1]
    window = bars[-31:-1]
    assert last["v"] == max(b.high for b in window)
    assert last["t"] == bars[-1].timestamp.isoformat()
    assert "excluded" in overlays["donchian_high"].provenance["note"]


def test_a_strategy_with_no_feature_state_is_refused_not_drawn_empty():
    """An empty chart reads as "the strategy sees nothing", which is a claim
    about the market rather than about the plumbing."""
    with pytest.raises(FeatureUnavailable) as exc:
        extract("supertrend", _Strategy(bars=_bars(50)))
    assert "will not invent" in str(exc.value) or "no runtime feature extractor" in str(exc.value)

    assert "supertrend" not in supported()
    assert "smc" in supported() and "price_action_rejection" in supported()


def test_an_engine_that_has_not_run_yet_says_so():
    with pytest.raises(FeatureUnavailable) as exc:
        extract("smc", _Strategy(engine=None))
    assert "has not built its engine" in str(exc.value)


def test_too_few_bars_is_reported_rather_than_drawn():
    with pytest.raises(FeatureUnavailable) as exc:
        extract("ema", _Strategy(bars=_bars(3), params={"fast": 8, "slow": 30}))
    assert "nothing to draw yet" in str(exc.value)


def test_reading_survives_the_run_loop_mutating_its_state():
    """The engines hold plain dicts and the run loop adds to them every candle.

    Iterating one from a request thread raised "dictionary keys changed during
    iteration" -- reproducibly, on any busy instance, as a 500. Nothing is
    locked, because a slow reader must never be able to stall the bot.
    """
    import threading

    zones = {f"z{i}": _Stub(id=f"z{i}", role="support", original_role="support",
                            low=1.0, high=2.0, created_at=NOW, confirmed_at=NOW,
                            touch_count=0, active=True, source_swing_ids=[])
             for i in range(200)}
    strategy = _Strategy(_Stub(zones=zones, swings={}, events={}))

    stop = threading.Event()

    def churn():
        index = 10_000
        while not stop.is_set():
            zones[f"z{index}"] = zones["z0"]
            index += 1
            zones.pop(f"z{index - 40}", None)

    worker = threading.Thread(target=churn, daemon=True)
    worker.start()
    try:
        for _ in range(300):
            # Either it reads, or it raises the retryable FeatureUnavailable.
            # What it must never do is leak a RuntimeError as a 500.
            try:
                extract("price_action_rejection", strategy)
            except FeatureUnavailable:
                pass
    finally:
        stop.set()
        worker.join(timeout=2)


def test_a_persistent_race_is_reported_as_retryable():
    """If the retries genuinely cannot get a clean read, say so plainly."""
    class _Hostile(dict):
        def values(self):
            raise RuntimeError("dictionary keys changed during iteration")

    # Non-empty on purpose: an empty mapping is falsy and never reaches the
    # retry path at all, which is how the first draft of this test passed
    # without exercising anything.
    hostile = _Hostile()
    hostile["z0"] = object()
    strategy = _Strategy(_Stub(zones=hostile, swings={}, events={}))
    with pytest.raises(FeatureUnavailable) as exc:
        extract("price_action_rejection", strategy)
    assert "retry in a moment" in str(exc.value)
