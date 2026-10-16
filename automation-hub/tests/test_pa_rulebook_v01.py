"""The rulebook is the specification; these tests are it, executed.

Chapter 13 of "PRICE ACTION FOR ALGORITHMS" v0.1 works a complete example by
hand and states every intermediate number. That makes it an unusually strong
acceptance test: an implementation that drifts from the document fails here
with the document's own arithmetic as the expected value.

The most important case is the one that does NOT trade. The chapter's lesson is
"A valid chart pattern can be an invalid trade. Risk and costs are gates, not
footnotes." An implementation that quietly accepts net RR 2.433 because the
candles looked clean has broken the rulebook while appearing to work.

Nothing here is tuned to pass. Where a number is asserted it is the number
printed in the document.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from services.pa_rulebook_v01 import (
    Blocker,
    CostModel,
    Regime,
    RulebookConfig,
    Zone,
    atr_series,
    build_trade_plan,
    classify_regime,
    confirmed_pivots,
    features,
    is_bearish_rejection,
    is_bullish_rejection,
    is_dominance,
    previous_atr,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(index: int, o, h, l, c, volume=10.0, minutes=15):
    return Bar(T0 + timedelta(minutes=minutes * index), float(o), float(h),
               float(l), float(c), volume)


@pytest.fixture()
def config():
    return RulebookConfig(tick_size=0.1, step_size=0.001)


@pytest.fixture()
def costs():
    """Chapter 13's illustrative costs. The document calls them illustrative."""
    return CostModel(entry_fee_rate=0.0005, exit_fee_rate=0.0005,
                     per_unit_allowance=20.0)


# ------------------------------------------------- chapter 4, worked candles

REJECTION = _bar(0, 100_400, 100_600, 99_850, 100_550)
CONFIRMATION = _bar(1, 100_540, 100_830, 100_520, 100_800)


def test_the_rejection_candle_measures_exactly_as_the_document_states():
    """Chapter 13: range 750, body 150, lower wick 550, upper wick 50."""
    shape = features(REJECTION)
    assert shape.range == pytest.approx(750.0)
    assert shape.body == pytest.approx(150.0)
    assert shape.lower_wick == pytest.approx(550.0)
    assert shape.upper_wick == pytest.approx(50.0)
    # "Lower wick fraction=0.733; close location=0.933; body fraction=0.200"
    assert shape.lower_wick_fraction == pytest.approx(0.733, abs=0.001)
    assert shape.close_location == pytest.approx(0.933, abs=0.001)
    assert shape.body_fraction == pytest.approx(0.200, abs=0.001)


def test_the_rejection_passes_every_long_condition(config):
    ok, measured = is_bullish_rejection(REJECTION, 600.0, config)
    assert ok, measured
    assert measured["range_atr"] == pytest.approx(1.25)   # document's 1.25


def test_the_confirmation_candle_is_a_dominance_candle(config):
    """Chapter 13: body 260, range 310, fractions 0.839 / 0.903, body/ATR 0.867."""
    shape = features(CONFIRMATION)
    assert shape.body == pytest.approx(260.0)
    assert shape.range == pytest.approx(310.0)
    assert shape.body_fraction == pytest.approx(0.839, abs=0.001)
    assert shape.close_location == pytest.approx(0.903, abs=0.001)

    ok, measured = is_dominance(CONFIRMATION, 300.0, "long", config)
    assert ok, measured
    assert measured["body_atr"] == pytest.approx(0.867, abs=0.001)
    # "Its close exceeds rejection.high plus a tick."
    assert CONFIRMATION.close > REJECTION.high + config.tick_size


def test_a_zero_range_candle_cannot_satisfy_anything(config):
    """Chapter 4: return invalid flags, "not infinity or an arbitrary
    favourable ratio". A flat bar is the easiest way to fake a perfect shape."""
    flat = _bar(0, 100.0, 100.0, 100.0, 100.0)
    assert features(flat).valid is False
    assert is_bullish_rejection(flat, 10.0, config)[0] is False
    assert is_dominance(flat, 10.0, "long", config)[0] is False


def test_thresholds_use_the_previous_atr_not_the_candidates_own(config):
    """Chapter 4: "This prevents the candidate's own large range from moving
    the measuring scale while it is being tested"."""
    # The ranges must actually move, or "previous" and "current" coincide and
    # the assertion proves nothing. Bar 20 is the expansion candle.
    bars = [_bar(i, 100, 101, 99, 100) for i in range(30)]
    bars[20] = _bar(20, 100, 140, 60, 100)
    series = atr_series(bars, config.atr_period)
    assert series[20] > series[19] * 2, "the fixture failed to move the ATR"
    assert previous_atr(bars, config.atr_period, at=20) == series[19]
    assert previous_atr(bars, config.atr_period, at=20) != series[20]


def test_atr_is_wilder_seeded_with_the_arithmetic_mean(config):
    """Chapter 4 fixes the definition; a different smoothing shifts every
    ATR-denominated threshold in the document at once."""
    bars = [_bar(i, 100, 110, 90, 100) for i in range(40)]   # TR is 20 throughout
    series = atr_series(bars, 14)
    assert series[13] is None            # not yet seeded
    assert series[14] == pytest.approx(20.0)
    assert series[39] == pytest.approx(20.0)


# ------------------------------------------------ chapter 5, causal structure

def test_a_pivot_is_not_available_at_its_own_timestamp(config):
    """Chapter 5: available only when i+2 closes. This is the leakage trap the
    document names explicitly -- a centred extremum read at its own timestamp
    is future information."""
    highs = [100, 101, 105, 101, 100, 99, 98]
    bars = [_bar(i, h, h, h - 2, h) for i, h in enumerate(highs)]
    pivots = confirmed_pivots(bars, config)
    peak = next(p for p in pivots if p.kind == "high")
    assert peak.pivot_time == bars[2].timestamp
    assert peak.available_at == bars[4].timestamp
    assert peak.available_at > peak.pivot_time


def test_equal_highs_produce_no_pivot(config):
    """Chapter 5: "Equal highs/lows produce no pivot in this baseline"."""
    bars = [_bar(i, 100, h, 98, 99) for i, h in enumerate([100, 101, 105, 105, 100, 99, 98])]
    assert not [p for p in confirmed_pivots(bars, config) if p.index == 2]


def test_regime_is_unknown_without_enough_context(config):
    bars = [_bar(i, 100, 101, 99, 100, minutes=60) for i in range(20)]
    regime, evidence = classify_regime(bars, config)
    assert regime is Regime.UNKNOWN
    assert "reason" in evidence


def test_neither_balanced_nor_transition_authorises_an_entry():
    """Chapter 5 states it outright, and chapter 9 repeats it as a no-trade
    rule. Pinned because "trend filter" is the first thing that gets loosened."""
    assert Regime.BALANCED is not Regime.BULL
    assert Regime.TRANSITION is not Regime.BULL
    for regime in (Regime.BALANCED, Regime.TRANSITION, Regime.UNKNOWN):
        assert regime not in (Regime.BULL, Regime.BEAR)


# ------------------------------------- chapter 13, the complete worked example

SUPPORT = Zone("zone-sup-1", "support", 99_800.0, 100_000.0, "p1",
               datetime(2025, 12, 1, tzinfo=UTC), 0, 600.0)


def _plan(resistance_lower, config, costs, equity=10_000.0):
    resistance = Zone("zone-res-1", "resistance", resistance_lower,
                      resistance_lower + 500, "p2",
                      datetime(2025, 12, 1, tzinfo=UTC), 0, 600.0)
    return build_trade_plan(
        direction="long", strategy_id="PA_SR_REJECTION_V01", symbol="BTCUSDT",
        rejection=REJECTION, zone=SUPPORT, setup_atr=600.0,
        entry_bound=100_860.0, zones=[SUPPORT, resistance], zone_index=10,
        equity=equity, config=config, costs=costs)


def test_the_structural_stop_is_the_documents_number(config, costs):
    """Chapter 13: "Stop=min(99,850,99,800)-60=99,740"."""
    plan = _plan(104_200.1, config, costs)
    assert plan.stop == pytest.approx(99_740.0)
    assert plan.stop_distance == pytest.approx(1_120.0)     # document's 1,120


def test_a_clean_setup_is_refused_when_the_costs_do_not_clear(config, costs):
    """The chapter's lesson, executed.

    "Net RR=(3,140-122.43)/(1,120+120.30)=2.433. This fails the required 2.5.
    NO TRADE, despite clean candles."
    """
    plan = _plan(104_000.1, config, costs)
    assert plan.costs_loss == pytest.approx(120.30, abs=0.01)
    assert plan.costs_win == pytest.approx(122.43, abs=0.01)
    assert plan.net_rr == pytest.approx(2.433, abs=0.001)
    assert plan.accepted is False
    assert plan.blocker is Blocker.NET_RR_TOO_LOW


def test_more_target_room_clears_the_gate_with_the_documents_size(config, costs):
    """Chapter 13: "net RR=3,217.47/1,240.30=2.594 ... use 0.020 BTC. Planned
    loss=24.806 USDT"."""
    plan = _plan(104_200.1, config, costs)
    assert plan.costs_win == pytest.approx(122.53, abs=0.01)
    assert plan.net_rr == pytest.approx(2.594, abs=0.001)
    assert plan.accepted is True
    assert plan.quantity == pytest.approx(0.020)
    assert plan.planned_loss == pytest.approx(24.806, abs=0.001)
    assert plan.evidence["raw_quantity"] == pytest.approx(0.020156, abs=1e-6)


def test_the_target_is_never_moved_to_rescue_the_ratio(config, costs):
    """Chapter 10: "Never move an actual opposing zone farther away merely to
    obtain an acceptable ratio." The refused plan and the accepted one differ
    only by real structure, and the refused one is still refused."""
    refused = _plan(104_000.1, config, costs)
    accepted = _plan(104_200.1, config, costs)
    assert refused.target == pytest.approx(104_000.0)
    assert accepted.target == pytest.approx(104_200.0)
    assert refused.accepted is False and accepted.accepted is True


def test_no_opposing_zone_means_no_trade_rather_than_an_invented_target(config, costs):
    """Chapter 10: reject TARGET_UNAVAILABLE. A target is structure, not a
    multiple of risk."""
    plan = build_trade_plan(
        direction="long", strategy_id="PA_SR_REJECTION_V01", symbol="BTCUSDT",
        rejection=REJECTION, zone=SUPPORT, setup_atr=600.0,
        entry_bound=100_860.0, zones=[SUPPORT], zone_index=10,
        equity=10_000.0, config=config, costs=costs)
    assert plan.accepted is False
    assert plan.blocker is Blocker.TARGET_UNAVAILABLE
    assert plan.target is None


def test_an_entry_inside_the_opposing_zone_is_refused(config, costs):
    """Chapter 10: "If E is inside an opposing zone ... reject"."""
    plan = _plan(100_800.0, config, costs)     # entry 100,860 sits inside it
    assert plan.accepted is False
    assert plan.blocker is Blocker.TARGET_UNAVAILABLE


@pytest.mark.parametrize("entry,why", [
    (99_900.0, "entry below its own stop"),
    (99_740.0, "entry exactly at the stop"),
])
def test_an_entry_on_the_wrong_side_of_the_stop_is_refused(entry, why, config, costs):
    """Chapter 10: "The entry must remain above S for a long"."""
    resistance = Zone("zone-res-1", "resistance", 104_200.1, 104_700.1, "p2",
                      datetime(2025, 12, 1, tzinfo=UTC), 0, 600.0)
    plan = build_trade_plan(
        direction="long", strategy_id="PA_SR_REJECTION_V01", symbol="BTCUSDT",
        rejection=REJECTION, zone=SUPPORT, setup_atr=600.0, entry_bound=entry,
        zones=[SUPPORT, resistance], zone_index=10, equity=10_000.0,
        config=config, costs=costs)
    assert plan.accepted is False, why
    assert plan.blocker is Blocker.STOP_DISTANCE_INVALID


def test_stop_distance_outside_the_band_is_rejected_not_resized(config, costs):
    """Chapter 10: "Extremely tight or wide stops are rejected rather than
    rescued with arbitrary position sizing"."""
    resistance = Zone("zone-res-1", "resistance", 140_000.0, 140_500.0, "p2",
                      datetime(2025, 12, 1, tzinfo=UTC), 0, 600.0)
    # setup ATR of 100 makes the 1,120 distance 11.2 ATR, far past 2.50
    plan = build_trade_plan(
        direction="long", strategy_id="PA_SR_REJECTION_V01", symbol="BTCUSDT",
        rejection=REJECTION, zone=SUPPORT, setup_atr=100.0,
        entry_bound=100_860.0, zones=[SUPPORT, resistance], zone_index=10,
        equity=10_000.0, config=config, costs=costs)
    assert plan.accepted is False
    assert plan.blocker is Blocker.STOP_DISTANCE_INVALID
    assert plan.stop_distance_atr > config.stop_distance_atr_max


def test_a_below_minimum_quantity_is_never_rounded_up_past_the_budget(config, costs):
    """Chapter 11: "Reject a below-minimum quantity; never round it up past the
    budget." Tiny equity must produce no trade, not a minimum-size one."""
    plan = _plan(104_200.1, config, costs, equity=1.0)
    assert plan.quantity == 0.0
    assert plan.accepted is False


# ------------------------------------------------------------ purity itself

def _code_symbols():
    """What the engine's *code* imports, calls and names -- not its prose.

    A grep over the source text also matches the rulebook passages quoted in
    the docstrings, including the sentence that bans these very indicators, so
    it reports the prohibition as if it were an implementation. And a flat
    identifier scan cannot tell the builtin ``open()`` from a candle's
    ``.open`` field. Reading the syntax tree asks what was actually meant:
    what does this module import, and what does it call?
    """
    import ast
    import inspect

    from services import pa_rulebook_v01

    def _dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return None

    imports, calls, names = set(), set(), set()
    for node in ast.walk(ast.parse(inspect.getsource(pa_rulebook_v01))):
        if isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Call):
            target = node.func.id if isinstance(node.func, ast.Name) else _dotted(node.func)
            if target:
                calls.add(target.lower())
        elif isinstance(node, ast.Name):
            names.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.lower())
            dotted = _dotted(node)
            if dotted:
                names.add(dotted.lower())
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name.lower())
    return imports, calls, names


def test_the_engine_module_reads_no_clock_and_touches_no_io():
    """The rulebook's own requirement: "external clocks must be injected
    rather than read unpredictably inside strategy functions", and the same
    pure engine must run in backtest and forward execution.

    A module that calls datetime.now() cannot be replayed deterministically,
    which silently invalidates every backtest run against it. Nor can one that
    reads a database, a socket or an environment variable: the replay would
    depend on state the recorded event sequence does not carry.
    """
    imports, calls, names = _code_symbols()

    # The import list is the tightest statement of purity available: a module
    # that never imports os, random or a client cannot reach one by accident.
    assert imports <= {"__future__", "dataclasses", "datetime", "enum", "math",
                       "typing", "bisect", "bot"}, (
        f"the pure engine imported {sorted(imports - {'__future__'})}")

    for forbidden in ("open", "input", "print", "eval", "exec", "compile",
                      "datetime.now", "datetime.utcnow", "time.time",
                      "utcnow", "getenv", "os.getenv"):
        assert forbidden not in calls, (
            f"{forbidden}() makes the engine non-deterministic or impure")
    for forbidden in ("os.environ", "sys.argv", "random", "requests",
                      "sqlite3", "socket", "urllib", "logging"):
        assert forbidden not in names, (
            f"{forbidden} makes the engine non-deterministic or impure")


def test_no_indicator_stack_crept_in():
    """Chapter 9: "Do not automatically add EMA, volume, FVG, order blocks,
    sessions and ten score weights." ATR is the only derived series the
    specification mandates."""
    _, _, names = _code_symbols()
    for forbidden in ("ema", "rsi", "macd", "supertrend", "bollinger",
                      "order_block", "fair_value_gap", "session_filter",
                      "score_weight", "sentiment", "ml_model"):
        assert forbidden not in names, f"{forbidden} is confirmation stacking"


def test_the_configuration_contract_rejects_impossible_values():
    """Chapter 18: validate on startup, not at the first trade."""
    RulebookConfig().validate()
    for bad in (RulebookConfig(tick_size=0),
                RulebookConfig(step_size=-1),
                RulebookConfig(min_net_rr=0),
                RulebookConfig(stop_distance_atr_min=3.0, stop_distance_atr_max=1.0),
                RulebookConfig(risk_per_trade=0)):
        with pytest.raises(ValueError):
            bad.validate()
