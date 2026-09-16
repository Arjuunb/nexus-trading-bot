"""Comparing candidate target models on identical terms.

The properties worth pinning are not the arithmetic. They are that a variant is
never scored on softer terms than the shipped model, that a confirmation it
cannot price is counted rather than dropped, that an intrabar tie is resolved
as a loss rather than a win, and that a winner is reported as a hypothesis
rather than as money.
"""
from __future__ import annotations

import ast
import importlib.util
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.types import Bar

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pa_target_model_study.py"
START = datetime(2026, 3, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def study():
    spec = importlib.util.spec_from_file_location("pa_target_model_study", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bars(path):
    """`path` is a list of (high, low) per 5M bar after the confirmation."""
    return [Bar(START + timedelta(minutes=5 * (i + 1)), h, h, low, low, 1.0)
            for i, (h, low) in enumerate(path)]


def _record(**kw):
    zones = kw.pop("zones", [])
    plan = {"entry_bound": 100.0, "stop": 99.0, "target": 102.0}
    plan.update(kw.pop("plan", {}))
    return {"index": 1, "at": START.isoformat(), "direction": kw.pop("direction", "long"),
            "setup_atr15": kw.pop("atr", 1.0), "zones_in_view": zones,
            "plan": plan, **kw}


def _run(study, records, bars, *, max_hold=288, min_net_rr=2.5):
    from services.pa_rulebook_v01 import CostModel

    out = io.StringIO()
    result = study.study({"meta": {"symbol": "BTCUSDT", "complete": True},
                          "confirmations": records},
                         bars, max_hold=max_hold, min_net_rr=min_net_rr,
                         tick=0.1, costs=CostModel(), out=out)
    return result, out.getvalue()


# ───────────────────────── identical terms ─────────────────────────

def test_every_variant_is_scored_with_the_shipped_arithmetic(study):
    """A variant measured on a softer cost model would win by construction."""
    tree = ast.parse(SCRIPT.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_net_rr")
    body = ast.unparse(fn)
    assert "costs.loss_path" in body and "costs.win_path" in body
    # An AST walk, not a substring scan over the file: the prose here explains
    # that the tick rounding is never a discount, and a text search for
    # "discount" matches that explanation. What must not exist is a BRANCH on
    # which variant is being scored.
    scoring = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "study")
    for node in ast.walk(scoring):
        if not isinstance(node, (ast.If, ast.IfExp)):
            continue
        condition = ast.unparse(node.test)
        assert "name" not in condition, \
            f"scoring must not branch on the variant: {condition}"
    # And the gate is compared once, against one threshold.
    gates = [ast.unparse(n) for n in ast.walk(scoring)
             if isinstance(n, ast.Compare) and "min_net_rr" in ast.unparse(n)]
    assert gates == ["net < min_net_rr"], gates


def test_the_study_does_not_modify_the_strategy(study):
    """It imports the rulebook for its cost model and config, and writes nothing."""
    tree = ast.parse(SCRIPT.read_text())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for banned in ("write_text", "execute", "commit", "post", "save"):
        assert banned not in called, f"a study must not {banned}"


# ───────────────────────── counted, not dropped ─────────────────────────

def test_a_confirmation_a_variant_cannot_price_is_counted(study):
    """Silently dropping them would make a variant that prices three trades
    look like one that prices fifty and refuses forty-seven."""
    # One opposing zone only: next_zone_out has nothing to aim at.
    record = _record(zones=[{"origin": "pivot", "retired": False, "kind": "resistance",
                             "lower": 102.0, "upper": 103.0}])
    result, text = _run(study, [record], _bars([(100.5, 99.5)] * 5))
    assert result["variants"]["next_zone_out"]["undefined"] == 1
    assert result["variants"]["next_zone_out"]["priced"] == 0
    assert "never skipped" in text


def test_the_second_zone_is_what_next_zone_out_aims_at(study):
    record = _record(zones=[
        {"origin": "pivot", "retired": False, "kind": "resistance", "lower": 102.0, "upper": 103.0},
        {"origin": "pivot", "retired": False, "kind": "resistance", "lower": 108.0, "upper": 109.0},
    ])
    targets = study._targets(record, 0.1)
    assert targets["next_zone_out"] == pytest.approx(107.9)


def test_a_flip_zone_cannot_supply_a_target(study):
    """Chapter 10: a flip object is not an original zone. The shipped model
    excludes them and so must any variant, or they are not comparable."""
    record = _record(zones=[
        {"origin": "pivot", "retired": False, "kind": "resistance", "lower": 102.0, "upper": 103.0},
        {"origin": "flip", "retired": False, "kind": "resistance", "lower": 105.0, "upper": 106.0},
        {"origin": "pivot", "retired": True, "kind": "resistance", "lower": 106.0, "upper": 107.0},
    ])
    assert study._targets(record, 0.1)["next_zone_out"] is None


# ───────────────────────── outcomes, honestly ─────────────────────────

def test_a_target_reached_before_the_stop_is_a_win(study):
    verdict, held = study._outcome(_bars([(100.5, 99.5), (104.0, 99.5)]), START,
                                   "long", 100.0, 99.0, 103.0, 288)
    assert verdict == "target" and held == 2


def test_a_stop_reached_first_is_a_loss(study):
    verdict, _ = study._outcome(_bars([(100.5, 98.5)]), START,
                                "long", 100.0, 99.0, 103.0, 288)
    assert verdict == "stop"


def test_a_bar_spanning_both_is_resolved_as_a_loss(study):
    """OHLC cannot say which came first. A study that called this a win would
    be measuring its own optimism, and every wide-range bar would flatter it."""
    verdict, _ = study._outcome(_bars([(104.0, 98.0)]), START,
                                "long", 100.0, 99.0, 103.0, 288)
    assert verdict == "ambiguous"

    result, text = _run(study, [_record(plan={"target": 103.0})],
                        _bars([(104.0, 98.0)]), min_net_rr=0.1)
    control = result["variants"]["control"]
    assert control["ambiguous"] == 1 and control["target"] == 0
    assert control["r_sum"] == pytest.approx(-1.0)
    assert "resolved as a LOSS" in text


def test_an_unresolved_trade_is_neither_a_win_nor_a_loss(study):
    verdict, _ = study._outcome(_bars([(100.5, 99.5)] * 10), START,
                                "long", 100.0, 99.0, 103.0, 5)
    assert verdict == "timeout"
    result, text = _run(study, [_record(plan={"target": 103.0})],
                        _bars([(100.5, 99.5)] * 10), max_hold=5, min_net_rr=0.1)
    control = result["variants"]["control"]
    assert control["timeout"] == 1
    assert control["r_sum"] == pytest.approx(0.0)
    assert "counted in neither win nor loss" in text


def test_a_short_is_measured_in_its_own_direction(study):
    verdict, _ = study._outcome(_bars([(100.5, 96.0)]), START,
                                "short", 100.0, 101.0, 97.0, 288)
    assert verdict == "target"
    verdict, _ = study._outcome(_bars([(101.5, 99.0)]), START,
                                "short", 100.0, 101.0, 97.0, 288)
    assert verdict == "stop"


def test_bars_before_the_confirmation_are_not_looked_at(study):
    """Resolving an outcome on candles that closed before the decision would be
    the retrospective-order trap chapter 2 forbids, wearing a statistic."""
    before = [Bar(START - timedelta(minutes=5), 104.0, 104.0, 98.0, 98.0, 1.0)]
    verdict, held = study._outcome(before + _bars([(100.5, 99.5)]), START,
                                   "long", 100.0, 99.0, 103.0, 288)
    assert verdict == "unresolved" and held == 1


# ───────────────────────── the gate, and the claim ─────────────────────────

def test_a_variant_below_the_gate_never_becomes_a_trade(study):
    """Passing the gate is what makes a plan a trade. A variant whose targets
    are closer must show as fewer trades, not as cheap wins."""
    result, _ = _run(study, [_record(plan={"target": 100.5})],
                     _bars([(101.0, 99.5)]), min_net_rr=2.5)
    control = result["variants"]["control"]
    assert control["priced"] == 1 and control["passed"] == 0
    assert control["target"] == 0 and control["stop"] == 0


def test_a_winning_variant_is_reported_as_a_hypothesis(study):
    """Never describe an untested change as profitable."""
    zones = [{"origin": "pivot", "retired": False, "kind": "resistance",
              "lower": 100.4, "upper": 100.5},
             {"origin": "pivot", "retired": False, "kind": "resistance",
              "lower": 110.0, "upper": 111.0}]
    records = [_record(zones=zones, plan={"target": 100.3}) for _ in range(6)]
    _, text = _run(study, records, _bars([(112.0, 99.5)] * 3))
    assert "HYPOTHESIS, not a result" in text
    assert "out-of-sample" in text and "forward paper" in text
    for banned in ("profitable", "this will earn", "recommended"):
        assert banned not in text.lower()


def test_too_few_resolved_trades_says_so_rather_than_ranking(study):
    """One or two trades is not a comparison, and printing a winner from it
    would be the same thin-sample error the league makes at 13."""
    _, text = _run(study, [_record(plan={"target": 104.0})],
                   _bars([(105.0, 99.5)]))
    assert "No variant resolved enough trades to compare" in text
    assert "does not trade often enough to measure" in text
    assert "HYPOTHESIS" not in text


def test_a_partial_replay_audit_is_flagged(study):
    from services.pa_rulebook_v01 import CostModel

    out = io.StringIO()
    study.study({"meta": {"symbol": "BTCUSDT", "complete": False},
                 "confirmations": []}, _bars([(100.0, 100.0)]),
                max_hold=288, min_net_rr=2.5, tick=0.1, costs=CostModel(), out=out)
    assert "PARTIAL checkpoint" in out.getvalue()


# ───────────────────────── the gate, stated as a price ─────────────────────────

def test_the_gate_exact_target_actually_satisfies_the_gate(study):
    """If the solve is off, the row that answers the central question is the
    one row that is wrong."""
    from services.pa_rulebook_v01 import CostModel

    costs = CostModel()
    for direction, entry, stop in (("long", 100.0, 99.0), ("short", 100.0, 101.5),
                                   ("long", 64_000.0, 63_200.0)):
        tick = 0.1
        target = study._gate_exact_target(entry, stop, direction, 2.5, costs, tick)
        net = study._net_rr(entry, stop, target, costs)
        # At or just above the gate, never below: rounding to the next tick is
        # a cost to this variant and must not become a break for it.
        assert net >= 2.5, (direction, entry, net)
        # ...and by at most that one tick, expressed in R. A looser bound would
        # not notice a solve that overshot.
        risk = abs(entry - stop) + costs.loss_path(entry, stop)
        assert net - 2.5 <= tick / risk + 1e-9, (direction, entry, net)
        if direction == "long":
            assert target > entry
        else:
            assert target < entry


def test_the_gate_costs_more_than_its_own_multiple(study):
    """A target at exactly 2.5x the stop does NOT clear a 2.5R net gate: the
    costs come out of the reward. Pinning it because it is the least obvious
    number in the table and the one most likely to be 'corrected' later."""
    from services.pa_rulebook_v01 import CostModel

    costs = CostModel()
    entry, stop = 100.0, 99.0
    naive = entry + 2.5 * abs(entry - stop)
    assert study._net_rr(entry, stop, naive, costs) < 2.5
    assert study._gate_exact_target(entry, stop, "long", 2.5, costs, 0.1) > naive


def test_gate_exact_is_priced_for_every_confirmation(study):
    """Unlike a structural target it can always be computed, so a blank here
    would mean the study lost a record rather than could not price one."""
    result, _ = _run(study, [_record() for _ in range(4)],
                     _bars([(100.5, 99.5)] * 3))
    assert result["variants"]["gate_exact"]["priced"] == 4
    assert result["variants"]["gate_exact"]["undefined"] == 0
    assert result["variants"]["gate_exact"]["passed"] == 4
