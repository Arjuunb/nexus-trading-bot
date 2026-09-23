"""How a veto gets its name on the dashboard.

The runtime had one way to name why a strategy did not trade: sniff the
strategy's free-text reason for keywords. A strategy whose vocabulary the
sniffer did not anticipate -- or which reported no text at all -- had every
refusal collapsed into "GATE_REJECTED: NO_SETUP".

That is not a cosmetic loss. "Nothing set up" and "a setup appeared, was
confirmed, and was refused because the nearest opposing level left it 15% of
the room it needed" are different problems with different answers, and the
board showed the same six words for both.
"""
from __future__ import annotations

import pytest

from services.signal_pipeline import gate_blocker


def name_blocker(strategy_decision: dict | None) -> str:
    """The naming rule from auto_engine, exercised in isolation.

    Kept in step with the engine by test_the_engine_uses_this_rule below, which
    reads the real source rather than trusting this copy.
    """
    decision_reason = str((strategy_decision or {}).get("reason") or "")
    reported = str((strategy_decision or {}).get("blocker_code") or "").strip()
    if reported:
        return f"GATE_REJECTED: {reported.upper()}"
    if any(word in decision_reason.upper()
           for word in ("WARM", "STALE", "R:R", "RR ", "RR_", "REWARD")):
        return gate_blocker("strategy", decision_reason)
    return "GATE_REJECTED: NO_SETUP"


@pytest.mark.parametrize("code", [
    "NET_RR_TOO_LOW", "TARGET_UNAVAILABLE", "STOP_DISTANCE_INVALID",
    "REGIME_NOT_ALIGNED", "REJECTION_FAILED", "CONFIRMATION_EXPIRED",
    "ZONE_CONSUMED", "NO_ELIGIBLE_ZONE",
    "EXISTING_EXPOSURE", "HTF_NOT_READY", "MISSING_CANDLE",
    "NON_REAL_DATA", "STALE_HTF_CANDLE", "WARMING_UP",
])
def test_a_reported_code_is_used_verbatim(code):
    """Every rulebook blocker must survive to the board under its own name."""
    named = name_blocker({"blocker_code": code, "reason": "anything at all"})
    assert named == f"GATE_REJECTED: {code}"


def test_every_rulebook_blocker_is_covered_by_that_parametrisation():
    """A new blocker added to the engine should fail this, not go unnoticed."""
    from services.pa_rulebook_v01 import Blocker

    covered = {
        "NET_RR_TOO_LOW", "TARGET_UNAVAILABLE", "STOP_DISTANCE_INVALID",
        "REGIME_NOT_ALIGNED", "REJECTION_FAILED", "CONFIRMATION_EXPIRED",
        "ZONE_CONSUMED", "NO_ELIGIBLE_ZONE",
        "EXISTING_EXPOSURE", "HTF_NOT_READY", "MISSING_CANDLE",
        "NON_REAL_DATA", "STALE_HTF_CANDLE", "WARMING_UP",
    }
    missing = {member.value for member in Blocker} - covered
    assert not missing, f"blockers with no attribution test: {sorted(missing)}"


def test_the_underscore_spelling_used_to_fall_through():
    """gate_blocker knew "RR_"; the guard deciding whether to call it did not,
    so NET_RR_TOO_LOW never reached the function that could name it."""
    reason = "CONFIRMED: NET_RR_TOO_LOW (regime BULL)"
    assert "RR " not in reason.upper() and "R:R" not in reason.upper()
    assert gate_blocker("strategy", reason) == "GATE_REJECTED: INSUFFICIENT_RR"
    assert name_blocker({"reason": reason}) == "GATE_REJECTED: INSUFFICIENT_RR"


def test_a_strategy_that_reports_nothing_still_falls_back():
    assert name_blocker(None) == "GATE_REJECTED: NO_SETUP"
    assert name_blocker({}) == "GATE_REJECTED: NO_SETUP"
    assert name_blocker({"reason": "waiting for a pullback"}) == "GATE_REJECTED: NO_SETUP"


def test_the_engine_uses_this_rule():
    """Pins the copy above to the real implementation.

    A test that quietly drifts from the code it describes is worse than none,
    and this file would keep passing while the engine reverted.
    """
    import inspect

    from services import auto_engine

    source = inspect.getsource(auto_engine)
    assert 'reported = str((strategy_decision or {}).get("blocker_code") or "").strip()' in source
    assert 'blocker = f"GATE_REJECTED: {reported.upper()}"' in source
    assert '"RR_"' in source
