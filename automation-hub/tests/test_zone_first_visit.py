"""``first_touch_only`` must be satisfiable, and must change nothing when off.

The switch gated on ``zone.touch_count <= 1``. touch_count increments for every
bar whose range overlaps the zone, and the bars that form the confirming swing
already overlap it before the zone becomes active, so the count is at least 2
by the time any setup can exist. Measured over a structured run: minimum 2,
maximum 13, never 1. Enabling the switch therefore produced zero proposals on
any input -- a dead control rather than a strict one.

The gate now keys on ``visit_count``: price arriving from outside the zone is a
new visit; further bars spent inside it are not.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from bot.data.resample import resample
from bot.types import Bar
from services.mtf_policy import native_timeframes
from services.native_price_action import NativePriceActionEngine, PriceActionConfig

SUPPORT, RESISTANCE = 100.0, 110.0
_TFS = native_timeframes("5m")


def _oscillating(n: int = 900, seed: int = 5) -> list[Bar]:
    """Price rejecting a fixed support and resistance, repeatedly."""
    rng = random.Random(seed)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars: list[Bar] = []
    price, direction = 105.0, 1
    for index in range(n):
        nxt = price + rng.uniform(0.15, 0.55) * direction
        if nxt >= RESISTANCE:
            nxt, direction = RESISTANCE - rng.uniform(0.05, 0.3), -1
        if nxt <= SUPPORT:
            nxt, direction = SUPPORT + rng.uniform(0.05, 0.3), 1
        high = min(max(price, nxt) + rng.uniform(0.02, 0.18), RESISTANCE + 0.25)
        low = max(min(price, nxt) - rng.uniform(0.02, 0.18), SUPPORT - 0.25)
        bars.append(Bar(timestamp=start + timedelta(minutes=5 * index), open=price,
                        high=high, low=low, close=nxt, volume=rng.uniform(80, 400)))
        price = nxt
    return bars


def _run(bars: list[Bar], **overrides):
    engine = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", **overrides))
    proposal_ids: list[str] = []
    for index, bar in enumerate(bars):
        window = bars[: index + 1]
        engine.set_native_mtf_context({tf: resample(window, tf) for tf in _TFS})
        engine.process_closed_bar(bar)
        for pid in engine.proposals:
            if pid not in proposal_ids:
                proposal_ids.append(pid)
    return engine, proposal_ids


@pytest.fixture(scope="module")
def bars() -> list[Bar]:
    return _oscillating()


def test_touch_count_never_reaches_one_so_it_cannot_gate_a_first_touch(bars):
    """The reason the old gate was unsatisfiable, pinned as a fact."""
    engine, _ = _run(bars)
    touches = [zone.touch_count for zone in engine.zones.values()]
    assert touches, "the fixture must produce zones"
    assert min(touches) >= 2, (
        "If a zone can exist with touch_count <= 1 this test's premise is wrong "
        "and the gate should be re-examined rather than this assertion relaxed.")


def test_visit_count_distinguishes_a_return_from_staying_put(bars):
    engine, _ = _run(bars)
    for zone in engine.zones.values():
        assert zone.visit_count >= 1
        assert zone.visit_count <= zone.touch_count, (
            "a visit spans one or more touching bars, so it can never exceed them")
    assert min(z.visit_count for z in engine.zones.values()) == 1, (
        "a freshly confirmed zone has been visited once; without this the "
        "first_touch_only gate is unsatisfiable exactly as before")


def test_enabling_first_touch_only_filters_rather_than_silencing(bars):
    _, production = _run(bars)
    _, filtered = _run(bars, first_touch_only=True)

    assert production, "the fixture must produce proposals to filter"
    assert filtered, (
        "first_touch_only produced nothing at all -- the switch is dead again")
    assert len(filtered) < len(production), "a filter must remove something"
    assert set(filtered) <= set(production), (
        "the filter must select from the proposals the strategy already makes, "
        "never create different ones")


def test_the_default_path_is_untouched_by_the_visit_counter(bars):
    """first_touch_only is off in production; that path must not move."""
    _, first = _run(bars)
    _, again = _run(bars)
    assert first == again, "the engine must stay deterministic"
    assert len(first) == 33, (
        "The production proposal count changed. The visit counter is bookkeeping "
        "only and must not alter what the strategy proposes when the switch is "
        "off; a change here means alpha moved.")
