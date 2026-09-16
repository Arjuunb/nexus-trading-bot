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


# ------------------------------------------------------- live chart layer

def _page() -> str:
    return (DASHBOARD / "pages" / "InstanceVisualLab.tsx").read_text()


def test_the_forming_candle_can_never_produce_a_decision():
    """Acceptance 3. The venue stream is display only.

    A forming candle that looks like a breakout is not one until the backend
    closes it and the engine judges it. The socket therefore drops closed
    frames (k.x) -- the backend owns those -- and the forming candle is passed
    to the renderer, never to a marker.
    """
    page = _page()
    assert "if (!k || k.x) return;" in page, "closed frames must be left to the backend"
    assert "display only" in page.lower()
    assert "FORMING" in page
    # The decision markers come from the timeline, which is backend evidence.
    assert "events={timeline?.events ?? []}" in page


def test_overlays_come_from_the_features_endpoint_only():
    """Acceptance 4, 5, 6, 7, 8: geometry is fetched, never computed here."""
    page = _page()
    assert "/research/instance-visual/features" in page
    assert "overlays={features?.overlays ?? []}" in page
    for banned in ("function ema(", "function atr(", "calcEma", "computeFVG",
                   "detectBOS", "findPivots", "buildZones"):
        assert banned not in page, f"the chart must not compute {banned}"


def test_unavailable_overlays_do_not_blank_the_decision_panels():
    """A strategy whose engine exposes nothing must still show why it is not
    trading -- that is the panel the operator came for."""
    page = _page()
    assert "featureError" in page
    assert "ivl-alert-soft" in page
    assert "Candles and decisions are still shown" in page


def test_toggles_are_built_from_the_declared_features():
    """Acceptance 16 / requirement 25: only toggles relevant to this strategy."""
    page = _page()
    assert "features?.declared_features" in page
    assert "TOGGLE_GROUP" in page


def test_the_chart_is_not_polled_at_one_cadence():
    """Requirement 29: do not re-fetch the whole history every second."""
    page = _page()
    assert "loadState(selected), 4000" in page
    assert "loadFeatures(selected), 15000" in page
    assert "loadCandles(selected), 60000" in page


def test_the_socket_reconnects_with_backoff_and_does_not_duplicate():
    """Acceptance 18: a reconnect rebuilds from the authoritative snapshot and
    must not add state of its own. The forming candle is a single slot, not a
    list, so a reconnect cannot append a second copy of anything."""
    page = _page()
    assert "Math.min(1000 * 2 ** Math.min(attempts, 5), 30000)" in page
    assert "formingRef.current = null;" in page      # cleared on teardown
    assert "setForming(formingRef.current)" in page  # one slot, overwritten


def test_position_levels_use_real_position_state():
    """Acceptance 12, 13: SL/TP/entry drawn from the instance's position."""
    page = _page()
    assert "position={state.position}" in page
    assert 'level(position.entry' in page
    assert 'level(position.stop, "stop", "SL")' in page
    assert 'level(position.target, "target", "TP")' in page
    assert "Entry (actual fill)" in page
