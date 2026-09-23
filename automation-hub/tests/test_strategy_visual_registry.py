"""The Visual Lab may show only what a strategy actually consumes.

A renderer that can draw CHoCH will draw CHoCH for anything, and a chart that
implies Donchian consults market structure is worse than no chart -- it is a
confident wrong answer to "why did it take that trade".

So the adapters' claims are checked against the implementations here. A
declared feature the module shows no sign of using fails, and a feature the
module plainly uses but the adapter omits fails too.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from services.strategy_visual_registry import (
    ADAPTERS,
    EXECUTION_GATES,
    MARKET_DATA_GATES,
    RISK_GATES,
    BLOCKER_EXPLANATIONS,
    DecisionState,
    Feature,
    GateState,
    Stage,
    adapter_for,
    current_stage,
    decision_state,
    explain,
    gate_sequence,
    resolve_gates,
)

ROOT = Path(__file__).resolve().parents[1]

#: Symbols that betray a feature actually being computed or consumed. Matched
#: against the module's identifiers, not its prose, so a docstring mentioning
#: liquidity does not count as consuming it.
FEATURE_SYMBOLS = {
    Feature.DONCHIAN_CHANNEL: ("channel", "donchian"),
    Feature.SUPERTREND: ("supertrend",),
    Feature.EMA: ("ema", "ema_fast", "ema_slow", "fast", "slow"),
    Feature.RSI: ("rsi",),
    Feature.ADX: ("adx",),
    Feature.ATR_BAND: ("atr", "atr_mult", "atr_period", "_bracket"),
    Feature.SWING: ("swing", "swings", "pivot", "pivots", "structure"),
    Feature.SUPPORT: ("support", "zone", "zones"),
    Feature.RESISTANCE: ("resistance", "zone", "zones"),
    Feature.SUPPLY: ("supply", "zone", "zones"),
    Feature.DEMAND: ("demand", "zone", "zones"),
    Feature.LIQUIDITY: ("liquidity", "sweep"),
    Feature.LIQUIDITY_SWEEP: ("sweep", "liquidity_sweep"),
    Feature.BOS: ("bos", "break_of_structure", "structure"),
    Feature.CHOCH: ("choch", "change_of_character"),
    Feature.FVG: ("fvg", "fair_value_gap", "imbalance"),
    Feature.POI: ("poi", "zone", "zones"),
    Feature.REJECTION_CANDLE: ("rejection", "reject"),
    Feature.DOMINANT_CANDLE: ("dominant", "dominance"),
    Feature.ZONE_FLIP: ("flip", "flipped"),
    Feature.OPPOSING_ZONE_TARGET: ("opposing", "target"),
    Feature.TREND_STRUCTURE: ("trend", "structure", "regime"),
    Feature.REGIME: ("regime", "bias"),
    Feature.HTF_BIAS: ("htf", "bias", "context", "higher"),
    Feature.VOLUME: ("volume",),
}


def _symbols(*module_names: str) -> set[str]:
    """Every identifier a module and its package define or reference.

    An AST walk rather than a substring scan over the text: substring scans are
    how "rsi" was once found inside "version", and how a docstring banning a
    thing counted as using it.
    """
    paths = []
    for module_name in module_names:
        origin = Path(importlib.import_module(module_name).__file__)
        if origin.name == "__init__.py":
            paths.extend(sorted(origin.parent.glob("*.py")))
        else:
            paths.append(origin)

    names: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name.lower())
            elif isinstance(node, ast.arg):
                names.add(node.arg.lower())
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # String constants are how several engines name their own
                # features ("zone_rejection", "bos"), so they count as usage.
                names.add(node.value.lower())
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.lower())
                names.update(alias.name.lower() for alias in node.names)
    return names


@pytest.mark.parametrize("strategy_id", sorted(ADAPTERS))
def test_every_declared_feature_is_used_by_the_implementation(strategy_id):
    adapter = ADAPTERS[strategy_id]
    names = _symbols(adapter.module, *adapter.engine_modules)
    blob = " ".join(names)
    for feature in adapter.features:
        markers = FEATURE_SYMBOLS[feature]
        where = ", ".join((adapter.module,) + adapter.engine_modules)
        assert any(marker in blob for marker in markers), (
            f"{strategy_id} declares {feature.value} but {where} shows "
            f"no sign of it (looked for {markers})")


def test_donchian_does_not_claim_structure_it_never_reads():
    """The concrete case: a 44-line channel breakout with no structural input.

    If this ever passes with those features declared, the chart is telling the
    operator the strategy weighed evidence it has never seen.
    """
    declared = set(ADAPTERS["donchian"].features)
    for forbidden in (Feature.CHOCH, Feature.BOS, Feature.FVG, Feature.LIQUIDITY,
                      Feature.SUPPLY, Feature.DEMAND, Feature.EMA, Feature.RSI,
                      Feature.VOLUME, Feature.HTF_BIAS, Feature.REGIME):
        assert forbidden not in declared, f"donchian must not display {forbidden.value}"


def test_only_smc_style_strategies_claim_smc_features():
    smc_only = {Feature.BOS, Feature.CHOCH, Feature.FVG, Feature.LIQUIDITY_SWEEP}
    for strategy_id, adapter in ADAPTERS.items():
        overlap = smc_only & set(adapter.features)
        if overlap:
            assert strategy_id in {"smc", "liquidity_sweep"}, (
                f"{strategy_id} claims {[f.value for f in overlap]}")


def test_every_adapter_resolves_through_the_authoritative_catalog():
    """Strategy identity must be one thing everywhere. An adapter for a key the
    catalog does not know would describe a strategy nothing can run."""
    from services.strategy_registry import REGISTRY

    catalog = set(REGISTRY)
    for strategy_id in ADAPTERS:
        assert strategy_id in catalog, f"{strategy_id} is not in the strategy catalog"
        assert adapter_for(strategy_id) is ADAPTERS[strategy_id]
    assert adapter_for("not_a_strategy") is None
    assert adapter_for("") is None


def test_every_adapter_matches_the_factory_that_builds_it():
    """The module an adapter describes must be the module the factory imports."""
    source = (ROOT / "services" / "strategy_factory.py").read_text()
    for strategy_id, adapter in ADAPTERS.items():
        assert f'key == "{strategy_id}"' in source, (
            f"{strategy_id} has an adapter but the factory cannot build it")
        assert adapter.module in source, (
            f"{strategy_id}'s adapter names {adapter.module}, which the factory "
            "does not import for it")


def test_every_gate_blocker_has_a_human_explanation():
    """A code with no sentence is half a telemetry system."""
    missing = set()
    for adapter in ADAPTERS.values():
        for gate in gate_sequence(adapter):
            missing |= {code for code in gate.blockers
                        if code not in BLOCKER_EXPLANATIONS}
    assert not missing, f"blocker codes with no explanation: {sorted(missing)}"


def test_an_unknown_code_is_named_rather_than_explained_away():
    assert explain("") == ""
    assert "no explanation is registered" in explain("SOMETHING_NEW")
    assert explain("GATE_REJECTED: NO_SETUP") == BLOCKER_EXPLANATIONS["NO_SETUP"]


# ------------------------------------------------------------ gate resolution

def test_a_blocker_locates_itself_in_the_sequence():
    """Everything before the failing gate passed; everything after is waiting.

    This is the whole reason the Lab cannot drift from the strategy: it has no
    opinion about the conditions, only about where the runtime's own code sits.
    """
    adapter = ADAPTERS["pa_rulebook"]
    results = resolve_gates(adapter, blocker="GATE_REJECTED: NET_RR_TOO_LOW")
    states = [r.state for r in results]
    failing = states.index(GateState.FAIL)

    assert all(s is GateState.PASS for s in states[:failing])
    assert all(s is GateState.WAITING for s in states[failing + 1:])
    assert results[failing].gate.id == "net_rr"
    assert results[failing].blocker == "NET_RR_TOO_LOW"
    assert "does not pay after costs" in results[failing].public()["explanation"]


def test_an_earlier_blocker_leaves_the_later_gates_waiting_not_failed():
    """A setup that never reached the reward test has not failed the reward
    test, and must not be drawn as though it had."""
    adapter = ADAPTERS["pa_rulebook"]
    results = {r.gate.id: r.state for r in
               resolve_gates(adapter, blocker="NO_ELIGIBLE_ZONE")}
    assert results["zone"] is GateState.FAIL
    assert results["net_rr"] is GateState.WAITING
    assert results["feed_synchronized"] is GateState.PASS


def test_an_unregistered_blocker_does_not_pass_every_gate():
    """A veto nobody mapped must not be rendered as a clean run. That silent
    pass is exactly how NET_RR_TOO_LOW hid behind "no setup"."""
    results = resolve_gates(ADAPTERS["donchian"], blocker="SOMETHING_UNMAPPED")
    assert all(r.state is GateState.WAITING for r in results)
    assert not any(r.state is GateState.PASS for r in results)


def test_no_blocker_with_an_open_position_passes_everything():
    results = resolve_gates(ADAPTERS["donchian"], blocker=None, position_open=True)
    assert all(r.state is GateState.PASS for r in results)
    assert current_stage(results, position_open=True) is Stage.POSITION


def test_scanning_shows_market_data_passed_and_the_rest_waiting():
    results = resolve_gates(ADAPTERS["donchian"], blocker=None)
    by_stage = {r.gate.id: r.state for r in results}
    assert by_stage["feed_synchronized"] is GateState.PASS
    assert by_stage["closing_break"] is GateState.WAITING


def test_the_current_stage_is_where_the_bar_stopped():
    adapter = ADAPTERS["smc"]
    assert current_stage(resolve_gates(adapter, blocker="STALE_HTF_CANDLE")) is Stage.MARKET_DATA
    assert current_stage(resolve_gates(adapter, blocker="NO_BOS")) is Stage.SETUP
    assert current_stage(resolve_gates(adapter, blocker="DAILY_LOSS_LIMIT")) is Stage.RISK_CHECK
    assert current_stage(resolve_gates(adapter, blocker="SIGNALS_ONLY")) is Stage.ORDER_INTENT


# --------------------------------------------------------------- state badge

def test_a_blocked_instance_is_never_shown_as_merely_running():
    """The spec's own rule: do not show ACTIVE if the strategy is blocked."""
    assert not hasattr(DecisionState, "ACTIVE")
    assert decision_state(blocker="STALE_MARKET_DATA", running=True) is DecisionState.DATA_BLOCKED
    assert decision_state(blocker="SIGNALS_ONLY", running=True) is DecisionState.SIGNALS_ONLY
    assert decision_state(blocker="DAILY_LOSS_LIMIT", running=True) is DecisionState.RISK_BLOCKED
    assert decision_state(blocker="NET_RR_TOO_LOW", running=True) is DecisionState.SIGNAL_REJECTED
    assert decision_state(blocker="HTF_NOT_READY", running=True) is DecisionState.WAITING_FOR_HTF


def test_a_stopped_instance_is_stopped_whatever_its_last_blocker_was():
    assert decision_state(blocker=None, running=False) is DecisionState.STOPPED
    assert decision_state(blocker="NO_SETUP", running=False) is DecisionState.STOPPED


def test_an_open_position_outranks_a_scanning_blocker():
    assert decision_state(blocker="NO_SETUP", running=True) is DecisionState.SCANNING
    assert decision_state(blocker="NO_SETUP", running=True,
                          position_open=True) is DecisionState.POSITION_OPEN
    # but never over a data problem: a stale feed is still a stale feed.
    assert decision_state(blocker="STALE_MARKET_DATA", running=True,
                          position_open=True) is DecisionState.DATA_BLOCKED


def test_signals_only_is_its_own_state():
    """Requirement 6: SIGNALS_ONLY must be visible as itself, because an
    instance in that mode can never create an order however good the setup."""
    assert decision_state(blocker="SIGNALS_ONLY", running=True) is DecisionState.SIGNALS_ONLY
