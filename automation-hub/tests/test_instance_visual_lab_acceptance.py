"""Acceptance criteria for the Instance Visual Lab that live outside its router.

One sidebar item rather than one page per strategy; the existing labs still
reachable; and the two runtime guarantees the Lab asserts on its own page --
that a signals-only instance never creates an order, and that one candle's
decision cannot become two orders.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[2] / "automation-hub-dashboard" / "src"


# ------------------------------------------------------------------- sidebar

def test_exactly_one_visual_lab_item_was_added():
    """Requirement 1. Every instance strategy shares one page.

    The failure this guards is a sidebar that grows an item per strategy, which
    is how the same chart ends up implemented five times and disagreeing with
    itself four ways.
    """
    nav = (DASHBOARD / "app-context.ts").read_text()
    assert '"Instance Visual Lab"' in nav

    forbidden = ["Supply/Demand Lab", "Supertrend Lab", "Donchian Lab",
                 "Brain Lab", "Adaptive Trend Lab", "Liquidity Sweep Lab",
                 "Rulebook Lab", "Ensemble Lab", "EMA Lab"]
    for label in forbidden:
        assert f'"{label}"' not in nav, f"a per-strategy sidebar item appeared: {label}"


def test_the_existing_labs_are_untouched():
    """Acceptance 13 and 14: Price Action Lab and SMC Lab keep working."""
    nav = (DASHBOARD / "app-context.ts").read_text()
    app = (DASHBOARD / "App.tsx").read_text()
    for label, page in (("Price Action Lab", "PriceActionVisualPage"),
                        ("SMC Visual Lab", "NativeSMCVisualPage"),
                        ("SMC Strategy Lab", "SMCStrategyLabPage")):
        assert f'"{label}"' in nav, f"{label} vanished from the sidebar"
        assert f'case "{label}": return <{page}' in app, f"{label} lost its route"


def test_the_lab_page_does_not_reimplement_strategy_logic():
    """Requirement: an observability layer, not a second strategy engine.

    Indicator maths in the page is the beginning of the Lab and the runtime
    disagreeing, so the obvious spellings are refused outright.
    """
    page = (DASHBOARD / "pages" / "InstanceVisualLab.tsx").read_text()
    for banned in ("function atr(", "function ema(", "function rsi(",
                   "calculateSupertrend", "computeZones", "detectBOS",
                   "findSwing", "channelHigh"):
        assert banned not in page, f"the Lab must not compute {banned}"


def test_the_lab_page_has_no_order_controls():
    """Requirement 18: observability only."""
    page = (DASHBOARD / "pages" / "InstanceVisualLab.tsx").read_text()
    # Precise tokens, not substrings: "place" also matches ".replace(", which
    # is the same false positive that once matched "rsi" inside "version".
    for banned in ("apiPostJson", "apiPut", "apiDelete", "placeOrder",
                   "submitOrder", "setOperatingMode", "live_trading",
                   "method: \"POST\"", "method: \"DELETE\""):
        assert banned not in page, f"the Lab must not expose {banned}"
    assert "apiGet" in page, "the Lab reads, and only reads"
    assert "OBSERVABILITY ONLY" in page


# ---------------------------------------------------------------- idempotency

def test_one_candle_decision_cannot_become_two_orders(tmp_path):
    """Acceptance 12. The decision identity is unique by construction.

    Recording the same candle's decision twice returns the first row's id
    rather than inserting a second, so a retry or a double-tick cannot produce
    a second order from one decision.
    """
    from data.decision_store import DecisionStore

    store = DecisionStore(str(tmp_path / "decisions.db"))
    decision = {"symbol": "BTCUSDT", "decision": "accepted", "side": "long",
                "instance_id": "inst-1", "strategy": "pa_rulebook",
                "decision_identity": "BTCUSDT|5m|2026-09-16T03:00:00"}

    first = store.record(dict(decision))
    second = store.record(dict(decision))

    assert first == second, "the same candle produced two decision rows"
    assert store.count() == 1

    other = store.record({**decision,
                          "decision_identity": "BTCUSDT|5m|2026-09-16T03:05:00"})
    assert other != first
    assert store.count() == 2


def test_a_decision_without_an_identity_is_not_silently_deduplicated(tmp_path):
    """The unique index is partial. Rows with no identity are legacy or
    non-instance decisions and must not collapse into one another."""
    from data.decision_store import DecisionStore

    store = DecisionStore(str(tmp_path / "decisions.db"))
    row = {"symbol": "BTCUSDT", "decision": "rejected", "instance_id": "inst-1"}
    assert store.record(dict(row)) != store.record(dict(row))
    assert store.count() == 2


# --------------------------------------------------------------- signals only

def test_signals_only_never_creates_a_paper_order():
    """Acceptance 6, at the place the runtime decides it.

    A signals-only instance evaluates every candle and must stop at the order
    intent. The engine spells that as a distinct blocker rather than a silent
    no-op, which is what lets the Lab show SIGNALS_ONLY instead of "no setup".
    """
    import inspect

    from services import auto_engine

    source = inspect.getsource(auto_engine)
    assert 'blocker = "GATE_REJECTED: SIGNALS_ONLY"' in source
    assert 'kind == "signal"' in source

    from services.strategy_visual_registry import DecisionState, decision_state

    assert decision_state(blocker="SIGNALS_ONLY",
                          running=True) is DecisionState.SIGNALS_ONLY


@pytest.mark.parametrize("blocker", ["SIGNALS_ONLY", "PAUSED", "APPROVAL_REQUIRED"])
def test_modes_that_cannot_order_are_shown_at_the_order_gate(blocker):
    """Whatever the setup quality, these stop at ORDER_INTENT and the page has
    to say so -- a perfect setup behind SIGNALS_ONLY is still not a trade."""
    from services.strategy_visual_registry import (
        ADAPTERS, GateState, Stage, current_stage, resolve_gates,
    )

    gates = resolve_gates(ADAPTERS["pa_rulebook"], blocker=blocker)
    failing = [g for g in gates if g.state is GateState.FAIL]
    assert len(failing) == 1
    assert failing[0].gate.stage is Stage.ORDER_INTENT
    assert current_stage(gates) is Stage.ORDER_INTENT
