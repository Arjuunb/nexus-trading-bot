"""The Price Action strategies can now say why they did not trade.

The engine evaluates every condition on every closed candle and records which
are unmet, in the order it requires them. None of that reached the runtime:
these strategies had no decision_report, so "price never reached a zone" and
"price reached one and failed the rejection test" both arrived at the dashboard
as GATE_REJECTED: NO_SETUP.
"""
from __future__ import annotations

import pytest

from strategies.price_action_rejection import (
    PriceActionFlipRetestStrategy, PriceActionRejectionStrategy,
)


class _Trace:
    def __init__(self, strategy_id, direction, state, conditions, missing,
                 next_event="", setup_id=None):
        self.strategy_id = strategy_id
        self.direction = direction
        self.state = state
        self.conditions = tuple({"key": key, "status": status}
                                for key, status in conditions)
        self.missing_conditions = tuple(missing)
        self.next_required_event = next_event
        self.setup_id = setup_id


class _Snapshot:
    def __init__(self, traces):
        self.strategy_traces = tuple(traces)


def _strategy(traces=None):
    strategy = PriceActionRejectionStrategy("BTCUSDT")
    strategy._last_snapshot = _Snapshot(traces) if traces is not None else None
    return strategy


def test_before_any_candle_it_reports_warm_up_not_no_setup():
    report = _strategy().decision_report()
    assert report["blocker_code"] == "WARMUP"


@pytest.mark.parametrize("condition,expected", [
    ("zone", "NO_ELIGIBLE_ZONE"),
    ("rejection", "REJECTION_FAILED"),
    ("trend", "TREND_NOT_ALIGNED"),
    ("pullback_zone", "NO_ELIGIBLE_ZONE"),
    ("pullback_rejection", "REJECTION_FAILED"),
    ("role_flip", "NO_ROLE_FLIP"),
    ("retest", "RETEST_NOT_HELD"),
    ("false_break", "NO_FALSE_BREAK"),
    ("reversal_close", "NO_REVERSAL_CLOSE"),
    ("pin_bar_only", "PIN_BAR_REQUIRED"),
    ("first_touch_only", "NOT_FIRST_TOUCH"),
])
def test_each_engine_condition_maps_to_its_own_blocker(condition, expected):
    strategy = _strategy([_Trace("PA1_SR_REJECTION", "bullish", "WATCHING",
                                 [(condition, "MISSING")], [condition])])
    assert strategy.decision_report()["blocker_code"] == expected


def test_the_first_unmet_condition_is_the_blocker():
    """The engine lists them in the order it requires them, so the earliest is
    what actually stopped the setup. A later one was never reached, and a
    condition never reached has not failed."""
    strategy = _strategy([_Trace(
        "PA1_SR_REJECTION", "bullish", "WATCHING",
        [("zone", "MISSING"), ("rejection", "MISSING")],
        ["zone", "rejection"])])
    report = strategy.decision_report()
    assert report["blocker_code"] == "NO_ELIGIBLE_ZONE"
    assert report["missing_conditions"] == ["zone", "rejection"]


def test_the_developing_direction_is_the_one_reported():
    """A strategy evaluates both directions; the trace with fewer unmet
    conditions is the setup actually developing, and reporting the other one
    would describe a side that is nowhere near firing."""
    strategy = _strategy([
        _Trace("PA1_SR_REJECTION", "bearish", "WATCHING",
               [("zone", "MISSING"), ("rejection", "MISSING")], ["zone", "rejection"]),
        _Trace("PA1_SR_REJECTION", "bullish", "WATCHING",
               [("zone", "PASS"), ("rejection", "MISSING")], ["rejection"]),
    ])
    report = strategy.decision_report()
    assert report["direction"] == "bullish"
    assert report["blocker_code"] == "REJECTION_FAILED"
    assert report["passed_conditions"] == ["zone"]


def test_a_complete_setup_reports_no_blocker():
    strategy = _strategy([_Trace("PA1_SR_REJECTION", "bullish", "ORDER_PENDING",
                                 [("zone", "PASS"), ("rejection", "PASS")], [],
                                 setup_id="s-1")])
    report = strategy.decision_report()
    assert report["blocker_code"] is None
    assert report["decision"] == "ENTER"
    assert report["state"] == "ORDER_PENDING"
    assert report["setup_id"] == "s-1"


def test_another_strategy_s_trace_is_not_borrowed():
    """Two setups share one engine. Reporting the other one's conditions would
    explain a refusal this strategy never made."""
    strategy = _strategy([_Trace("PA3_FLIP_RETEST", "bullish", "WATCHING",
                                 [("role_flip", "MISSING")], ["role_flip"])])
    assert strategy.decision_report()["blocker_code"] == "NO_SETUP"


def test_the_flip_retest_strategy_reads_its_own_trace():
    strategy = PriceActionFlipRetestStrategy("BTCUSDT")
    strategy._last_snapshot = _Snapshot([
        _Trace(strategy.pa_strategy_id, "bullish", "WATCHING",
               [("role_flip", "MISSING")], ["role_flip"])])
    assert strategy.decision_report()["blocker_code"] == "NO_ROLE_FLIP"


def test_every_mapped_condition_is_one_the_engine_actually_emits():
    """The map must track the engine. A condition renamed there should fail
    here rather than silently degrade to "no setup" in production."""
    import ast
    from pathlib import Path as _Path

    from services import native_price_action

    # An AST walk, not a text match: the engine passes some keys on the line
    # after the call, so a single-line pattern reports a condition missing that
    # is plainly there -- which is what the first draft of this test did.
    tree = ast.parse(_Path(native_price_action.__file__).read_text())
    emitted = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "_condition" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                emitted.add(first.value)

    assert emitted, "no engine conditions were found; the scan itself is broken"
    for condition in PriceActionRejectionStrategy._BLOCKER_BY_CONDITION:
        assert condition in emitted, (
            f"'{condition}' is mapped but the engine emits {sorted(emitted)}")


def test_every_mapped_blocker_is_explained_and_locatable():
    """A code the gate sequence cannot locate leaves every gate WAITING, which
    is how the panel would go quiet instead of naming the cause."""
    from services.strategy_visual_registry import (
        ADAPTERS, BLOCKER_EXPLANATIONS, GateState, gate_sequence, resolve_gates,
    )

    codes = set(PriceActionRejectionStrategy._BLOCKER_BY_CONDITION.values())
    for code in codes:
        assert code in BLOCKER_EXPLANATIONS, f"{code} has no human explanation"

    known = set()
    for strategy_id in ("price_action_rejection", "price_action_flip_retest"):
        for gate in gate_sequence(ADAPTERS[strategy_id]):
            known |= set(gate.blockers)
    missing = codes - known
    assert not missing, f"no gate claims these blockers: {sorted(missing)}"

    # And they really do resolve to a failing gate rather than a silent wait.
    for code in ("NO_ELIGIBLE_ZONE", "REJECTION_FAILED", "TREND_NOT_ALIGNED"):
        results = resolve_gates(ADAPTERS["price_action_rejection"], blocker=code)
        assert any(r.state is GateState.FAIL for r in results), code
