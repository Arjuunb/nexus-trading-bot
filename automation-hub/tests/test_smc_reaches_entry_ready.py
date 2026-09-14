"""The SMC engine completes its sequence and places a paper order.

Every other SMC test runs on random-walk candles, where the engine opens setups
and invalidates or expires all of them -- 164 setups and zero proposals over
2400 bars. That is the honest verdict on a random walk, but it means nothing
anywhere asserted that the engine can reach ENTRY_READY *at all*. An engine
that could never complete its chain would have passed the entire suite.

So this file builds the sequence the engine is looking for, candle by candle,
and follows it to an open paper position. It is also the reference for what the
Strategy Lab draws: the assertions below are exactly the fields the lab's
"Ordered evidence" panel and chart overlay read, so if the attribution a user
needs to trust a setup ever stops being recorded, this fails.

Nothing here is tuned. Every threshold is read off the shipped config, and the
fixture is built to satisfy the rules rather than the rules relaxed to admit the
fixture.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar, SignalType
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services.controls import TradingControl
from services.native_smc import SetupPhase, SMCConfig, SMCMarketStructureEngine
from services.signal_pipeline import SignalPipeline
from strategies.smc_strategy import SMCStrategy

UTC = timezone.utc
TF = timedelta(minutes=5)

#: The warm-up must fill the engine's internal higher-timeframe bucket, which
#: needs 51 closed 4h candles before it will report a bias at all.
WARMUP_BARS = 2600
#: 08:00 UTC is inside the engine's London window (07-11 Europe/London, which
#: is UTC in early March). Outside a session the engine refuses the entry, so
#: the anchor is load-bearing, not decoration.
ANCHOR = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)


def _bar(ts, o, h, l, c, v=100.0):
    return Bar(ts, o, h, l, c, v)


def _sequence() -> tuple[list[Bar], int]:
    """A bullish sweep -> CHoCH -> FVG -> retest -> rejection, in order."""
    bars: list[Bar] = []
    price, ts = 100.0, ANCHOR - TF * WARMUP_BARS

    # Warm-up: a steady advance, so the higher-timeframe bias resolves bullish.
    for _ in range(WARMUP_BARS):
        nxt = price + 0.02
        bars.append(_bar(ts, price, max(price, nxt) + 0.05, min(price, nxt) - 0.05, nxt))
        price, ts = nxt, ts + TF

    # A pivot high for the break to clear. internal_pivot_length is 5, so it
    # needs five candles either side before the engine will confirm it.
    pivot_high = price + 3.0
    bars.append(_bar(ts, price, pivot_high, price - 0.1, price + 0.2)); ts += TF
    price += 0.2
    for _ in range(6):
        nxt = price - 0.15
        bars.append(_bar(ts, price, price + 0.05, nxt - 0.05, nxt)); price, ts = nxt, ts + TF
    for _ in range(6):
        nxt = price - 0.10
        bars.append(_bar(ts, price, price + 0.05, nxt - 0.05, nxt)); price, ts = nxt, ts + TF

    start = len(bars)
    recent_low = min(row.low for row in bars[-10:])

    # 1. Liquidity sweep: take out the ten-bar low, close back above it.
    bars.append(_bar(ts, price, price + 0.10, recent_low - 0.60, recent_low + 0.35))
    price, ts = recent_low + 0.35, ts + TF

    # 2. Impulse through the pivot high -> change of character. Volume stays at
    #    the warm-up level on purpose: a surge here would open a fair-value gap
    #    on these candles and the setup would adopt THAT as its point of
    #    interest instead of the one below.
    for target in (pivot_high - 1.2, pivot_high + 0.9):
        bars.append(_bar(ts, price, target + 0.05, price - 0.05, target, 100.0))
        price, ts = target, ts + TF

    # 3. The point of interest: a bullish fair-value gap. The engine wants
    #    bar.low above the high of two candles back, the middle candle closing
    #    above it too, and a volume surge on that middle candle.
    two_high = price + 0.05
    bars.append(_bar(ts, price, two_high, price - 0.05, price, 100.0)); ts += TF
    mid = price + 1.8
    bars.append(_bar(ts, price, mid + 0.05, price - 0.02, mid, 900.0)); ts += TF
    gap_low = two_high + 0.9
    top = gap_low + 1.2
    bars.append(_bar(ts, gap_low + 0.1, top, gap_low, top - 0.1, 200.0)); ts += TF

    # 4. Retest into the gap, rejected by a hammer: the lower wick is at least
    #    twice the body and the upper wick no larger than it. The low stays
    #    above the gap's floor -- dipping under it would mitigate the gap and
    #    the engine would correctly abandon the setup.
    open_ = gap_low + 0.15
    bars.append(_bar(ts, open_, open_ + 0.05, two_high + 0.02, open_ - 0.10, 300.0))
    return bars, start


@pytest.fixture(scope="module")
def completed():
    bars, start = _sequence()
    engine = SMCMarketStructureEngine(SMCConfig(symbol="BTCUSDT", timeframe="5m"))
    for row in bars:
        engine.process_closed_bar(row)
    return engine, bars, start


# ------------------------------------------------------------- the sequence

def test_the_engine_reaches_entry_ready_and_proposes(completed):
    engine, _bars, _start = completed
    assert engine._htf_bias() == 1, "the warm-up must establish a bullish bias"
    ready = [row for row in engine.setups.values() if row.phase is SetupPhase.ENTRY_READY]
    assert ready, (
        "the engine never completed its sequence on candles built to satisfy "
        "every one of its rules -- it cannot produce a trade on any data")
    assert engine.proposals, "ENTRY_READY produced no proposal"


def test_every_phase_is_visited_in_order(completed):
    """The lab draws this progression; it must be the real one."""
    engine, bars, start = completed
    walked = [(t.from_phase, t.to_phase, t.reason)
              for t in engine.transitions if t.timestamp >= bars[start].timestamp]
    assert [step[1] for step in walked] == [
        "LIQUIDITY_SWEPT", "STRUCTURE_SHIFT_CONFIRMED", "POI_CREATED",
        "WAITING_RETEST", "REJECTION_CONFIRMED", "ENTRY_READY",
    ], walked
    reasons = [step[2] for step in walked]
    assert reasons[0] == "liquidity swept"
    assert "CHOCH" in reasons[1] or "BOS" in reasons[1]
    assert reasons[2] == "ordered FVG created"
    assert reasons[4] == "POI retest and rejection"


def test_each_step_names_the_object_that_caused_it(completed):
    """"Why did it fire" must be answerable from the record, not inferred.

    Without these ids the lab can show that a trade happened but not which
    sweep, which break of structure or which gap produced it -- which is the
    entire reason the visual labs exist.
    """
    engine, _bars, _start = completed
    setup = next(row for row in engine.setups.values() if row.phase is SetupPhase.ENTRY_READY)
    assert setup.sweep_id and setup.sweep_id in engine.events
    assert setup.structure_id and setup.structure_id in engine.events
    assert setup.poi_id and setup.poi_id in engine.fvgs
    assert setup.first_touch_at is not None, "the retest candle was not recorded"
    for transition in setup.transitions:
        if transition.to_phase in ("LIQUIDITY_SWEPT", "POI_CREATED", "WAITING_RETEST",
                                   "REJECTION_CONFIRMED", "ENTRY_READY",
                                   "STRUCTURE_SHIFT_CONFIRMED"):
            assert transition.object_id, (
                f"{transition.to_phase} names no object, so the chart cannot "
                "highlight what caused it")


def test_the_point_of_interest_is_the_gap_price_actually_returned_to(completed):
    engine, bars, _start = completed
    setup = next(row for row in engine.setups.values() if row.phase is SetupPhase.ENTRY_READY)
    gap = engine.fvgs[setup.poi_id]
    retest = bars[-1]
    assert gap.direction == "bullish"
    assert gap.active and not gap.mitigated, "the gap was consumed, not respected"
    assert retest.low <= gap.top and retest.high >= gap.bottom
    assert retest.low >= gap.bottom, "a dip below the floor would mitigate it"


def test_the_proposal_carries_a_usable_bracket(completed):
    engine, _bars, _start = completed
    proposal = next(iter(engine.proposals.values()))
    assert proposal.direction == "bullish"
    assert proposal.stop < proposal.entry < proposal.target
    assert proposal.rr_ratio == 2.5, "the shipped reward multiple"
    assert proposal.risk_distance == pytest.approx(proposal.entry - proposal.stop)


# ------------------------------------------------- the constraints that bite

def test_the_same_setup_outside_a_session_is_refused():
    """The engine only takes entries in London or New York hours.

    This is a real limit on how often SMC can fire on a 24/7 market, and it is
    asserted rather than left for a user to discover as silence.
    """
    bars, _start = _sequence()
    shifted = [Bar(row.timestamp - timedelta(hours=6), row.open, row.high,
                   row.low, row.close, row.volume) for row in bars]
    engine = SMCMarketStructureEngine(SMCConfig(symbol="BTCUSDT", timeframe="5m"))
    for row in shifted:
        engine.process_closed_bar(row)
    assert engine._session_name(shifted[-1]) == "inactive"
    assert not engine.proposals, "an out-of-session entry was taken"
    waiting = [row for row in engine.setups.values()
               if row.phase is SetupPhase.WAITING_RETEST]
    assert waiting, "the setup should still be waiting, not invalidated"


def test_the_whole_sequence_must_fit_inside_the_expiry_window(completed):
    """setup_expiry_bars is 10, so everything above happens within 10 candles."""
    engine, bars, start = completed
    setup = next(row for row in engine.setups.values() if row.phase is SetupPhase.ENTRY_READY)
    entry = next(t for t in setup.transitions if t.to_phase == "ENTRY_READY")
    swept = next(t for t in setup.transitions if t.to_phase == "LIQUIDITY_SWEPT")
    span = (entry.timestamp - swept.timestamp) // TF
    assert span <= SMCConfig(symbol="BTCUSDT").setup_expiry_bars, (
        f"the sequence took {span} candles; the engine expires a setup after "
        f"{SMCConfig(symbol='BTCUSDT').setup_expiry_bars}")


# --------------------------------------------------- through to a paper order

def test_the_setup_becomes_an_open_paper_position():
    """The whole point: a visible setup that a user can watch become an order."""
    bars, _start = _sequence()
    strategy = SMCStrategy("BTCUSDT")
    signal = None
    for row in bars:
        emitted = strategy.on_bar(row)
        if emitted is not None:
            signal = emitted
    assert signal is not None, "the adapter emitted nothing for a completed setup"
    assert signal.type is SignalType.LONG
    assert signal.stop_loss < signal.entry < signal.take_profit

    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, 10_000)
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=10_000,
                              risk_per_trade_pct=0.01, exposure_limit_pct=0.5)
    result = pipeline.process({
        "alert_id": f"smc-{signal.timestamp.isoformat()}",
        "symbol": "BTCUSDT", "side": "BUY",
        "entry": signal.entry, "stop": signal.stop_loss, "target": signal.take_profit,
        "reason": signal.reason, "snapshot": getattr(signal, "snapshot", None),
        "strategy": "smc", "timeframe": "5m", "mode": "paper",
        "timestamp": signal.timestamp.isoformat(),
    })

    assert result.accepted, f"refused at {result.stage}: {result.reason}"
    position = paper.open_position("BTCUSDT")
    assert position is not None, "a completed SMC setup placed no paper order"
    assert position["stop"] == pytest.approx(signal.stop_loss)
    # And the order can be traced back to the structure that caused it. This
    # is what the Strategy Lab shows next to the fill, and what makes a paper
    # record worth anything when deciding whether to trust the strategy.
    provenance = signal.snapshot or {}
    assert provenance.get("setup_id") and provenance.get("proposal_id")
    assert provenance["sweep_id"], "the order does not name its liquidity sweep"
    assert provenance["structure_id"], "the order does not name its break of structure"
    assert provenance["poi_id"], "the order does not name the point of interest"
    assert provenance["dealing_range_area"] in ("premium", "discount", "equilibrium")
    assert provenance["htf_bias"] == 1
    assert provenance["session"] in ("London", "New York")
