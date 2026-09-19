"""The SMC decision path is read-only, and this is what checks it.

An agent layer is being built around the SMC Lab strategy: execution,
journalling, review, memory. The whole premise is that the agent OBSERVES the
strategy rather than altering it. "We did not change the strategy" is a claim,
and a claim nobody can check is worth nothing, so these tests check it.

If one of these fails, either a decision-path file was edited or the strategy
now decides differently. Neither is automatically wrong -- but neither may pass
silently, and the baseline must not be regenerated to make the failure go away
unless the change was deliberate and the new behaviour was reviewed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services import smc_strategy_freeze as freeze

BASELINE = json.loads(
    (Path(__file__).resolve().parents[1] / "data" /
     "smc_decision_path_freeze.json").read_text(encoding="utf-8"))


# ───────────────────────────── the source lock ─────────────────────────────

def test_no_decision_path_file_has_been_edited():
    """A one-character change to a threshold, a condition or an ordering moves
    the hash. This is the lock that catches an edit."""
    current = freeze.source_manifest()
    baseline = BASELINE["source_manifest"]
    drifted = {p for p in set(baseline) | set(current)
               if baseline.get(p) != current.get(p)}
    assert not drifted, (
        "the SMC strategy is read-only and these files changed: "
        f"{sorted(drifted)}. If that was deliberate, the new behaviour has to "
        "be reviewed and the baseline in data/smc_decision_path_freeze.json "
        "updated on purpose — not to silence this test.")


def test_every_decision_path_file_still_exists():
    """A file renamed out from under the manifest would otherwise pass as
    'unchanged' on a lookup that found nothing."""
    assert "MISSING" not in freeze.source_manifest().values()


# ─────────────────────────── the behaviour lock ───────────────────────────

def test_the_strategy_still_decides_what_it_decided():
    """Catches a change in what the strategy DOES when the files look
    untouched: a dependency that moved underneath it, a default that shifted,
    conditions evaluated in a new order."""
    assert freeze.behaviour_fingerprint() == BASELINE["behaviour_fingerprint"], (
        "the SMC strategy reached a different decision on the canonical "
        "sequence. The files may be untouched — something underneath them "
        "moved. Find out what before touching this baseline.")


def test_the_decision_is_reproducible():
    """A fingerprint over a non-deterministic decision would fail at random
    and teach everyone to ignore it."""
    assert freeze.behaviour_fingerprint() == freeze.behaviour_fingerprint()


def test_the_canonical_sequence_still_drives_the_engine_to_a_proposal():
    """The fixture has to keep exercising a real decision. If it degraded to
    candles the engine ignores, both locks above would keep passing over a
    strategy that had stopped being tested at all."""
    snapshot = freeze.decision_snapshot()
    assert snapshot["htf_bias"] == 1, "the warm-up must establish a bullish bias"
    assert "SetupPhase.ENTRY_READY" in snapshot["setup_phases"]
    assert snapshot["proposals"], "the engine produced no proposal to pin"


def test_the_recorded_shipped_verdict_is_what_the_contract_returns():
    """Stated in the open rather than hidden inside a hash: on this sequence
    the engine completes its sequence and the contract still declines, because
    the dealing range is in premium and the setup is bullish. That is the
    shipped strategy working as designed, and the agent must not 'fix' it."""
    evaluation = freeze.decision_snapshot()["evaluation"]
    assert evaluation["state"] == "WATCHING"
    assert evaluation["missing_conditions"] == ["Premium / discount location"]
    passed = [row["label"] for row in evaluation["ordered_condition_results"]
              if row.get("status") == "PASS"]
    assert passed == ["Completed HTF direction", "Sell-side sweep",
                      "Bullish BOS / CHoCH", "Bullish FVG",
                      "Exact POI retest", "Bullish rejection"]


# ──────────────────────────── the verdict object ────────────────────────────

def test_verify_reports_intact_against_the_recorded_baseline():
    verdict = freeze.verify(BASELINE["source_manifest"],
                            fingerprint=BASELINE["behaviour_fingerprint"])
    assert verdict.intact and "unchanged" in verdict.describe()


def test_verify_names_an_edited_file():
    tampered = {**BASELINE["source_manifest"],
                "services/smc_strategy_v1.py": "0" * 64}
    verdict = freeze.verify(tampered, fingerprint=BASELINE["behaviour_fingerprint"])
    assert not verdict.intact
    assert verdict.source_changed == ("services/smc_strategy_v1.py",)
    assert "smc_strategy_v1.py" in verdict.describe()


def test_verify_reports_a_behaviour_change_separately_from_an_edit():
    """They fail differently and have different causes: a refactor moves the
    source hash alone, an upstream change moves the fingerprint alone."""
    verdict = freeze.verify(BASELINE["source_manifest"], fingerprint="0" * 64)
    assert not verdict.intact
    assert verdict.source_changed == () and verdict.behaviour_changed is True
    assert "behaviour moved" in verdict.describe()


def test_omitting_the_fingerprint_is_not_counted_as_a_behaviour_pass():
    """A caller that checks only the source must not be told behaviour was
    verified. Silence about an unrun check reads as a pass."""
    verdict = freeze.verify(BASELINE["source_manifest"])
    assert verdict.behaviour_changed is False and verdict.actual_fingerprint == ""
    assert verdict.expected_fingerprint == ""
