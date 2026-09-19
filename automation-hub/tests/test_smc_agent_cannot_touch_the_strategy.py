"""The agent may observe the SMC strategy. It may not change it.

Everything else about this agent is a trading concern. This file is the
constraint: the SMC Lab strategy is read-only, the agent's learning must never
alter it, and an improvement the agent believes in is a note for a human rather
than an edit.

The strongest check here is the end-to-end one: run the agent's whole life --
observe, take, close, review, weekly review, file a proposal -- and then ask the
freeze whether the strategy still hashes to what it did before. A promise that
survives the full lifecycle is worth more than any amount of code reading.
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import smc_agent, smc_agent_journal, smc_agent_review
from services import smc_strategy_freeze as freeze
from services.smc_agent import SMCAgent
from services.smc_agent_journal import SMCAgentJournal
from services.smc_agent_review import review_closed_trades, weekly_review

ROOT = Path(__file__).resolve().parents[1]
AGENT_MODULES = (smc_agent, smc_agent_journal, smc_agent_review)
#: Modules the agent may read from and must never write into.
STRATEGY_NAMES = {"native_smc", "smc_strategy_v1", "smc_strategy_ladder",
                  "smc_strategy_lab", "native_smc_live_visual",
                  "smc_strategy_freeze"}
BASELINE = json.loads((ROOT / "data" / "smc_decision_path_freeze.json")
                      .read_text(encoding="utf-8"))
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _plan(entry=100.0, stop=99.0, target_2=103.0):
    return {"entry": entry, "stop": stop, "target_1": 102.0, "target_2": target_2,
            "risk_percent": 0.5}


def _evaluation(state="ENTRY_READY", plan=None):
    return {"strategy_id": "SMC_SOURCE_V1", "state": state,
            "data_identity": {"symbol": "BTCUSDT", "timeframe": "5m",
                              "selected_candle": NOW.isoformat()},
            "ordered_condition_results": [{"label": "Bullish FVG", "status": "PASS"}],
            "missing_conditions": [], "proposal": {"direction": "bullish"},
            "setup_id": "s1", "proposal_id": "p1",
            "trade_plan": plan if plan is not None else _plan()}


# ──────────────────── the whole lifecycle, then the freeze ────────────────────

def test_a_full_agent_lifecycle_leaves_the_strategy_byte_identical():
    """Observe, trade, close, review, weekly-review, propose — then check."""
    before = freeze.source_manifest()
    assert before == BASELINE["source_manifest"], "precondition: start unchanged"

    journal = SMCAgentJournal(":memory:")
    agent = SMCAgent(journal, equity=10_000.0, clock=lambda: NOW)

    taken = agent.observe(_evaluation())
    agent.observe(_evaluation(plan=_plan(target_2=101.5)))      # skipped: under 3R
    agent.observe(_evaluation(state="WATCHING", plan=None))      # not ready
    agent.observe(_evaluation(), can_trade=False, blocked_reason="feed down")
    journal.close_trade(taken["trade_id"], exit_price=99.0, realised_r=-1.0,
                        result="LOSS", close_reason="stop",
                        closed_at=NOW.isoformat())
    review_closed_trades(journal)
    weekly_review(journal, end=NOW + timedelta(minutes=1))
    journal.propose_improvement(
        target="STRATEGY", title="Consider premium-location longs",
        rationale="several setups failed only the location gate")

    assert freeze.source_manifest() == BASELINE["source_manifest"], (
        "the agent changed a strategy file while trading")
    assert freeze.behaviour_fingerprint() == BASELINE["behaviour_fingerprint"], (
        "the agent changed what the strategy decides")
    journal.close()


def test_the_agent_did_real_work_in_that_lifecycle():
    """Otherwise the test above passes because nothing happened."""
    journal = SMCAgentJournal(":memory:")
    agent = SMCAgent(journal, equity=10_000.0, clock=lambda: NOW)
    taken = agent.observe(_evaluation())
    journal.close_trade(taken["trade_id"], exit_price=99.0, realised_r=-1.0,
                        result="LOSS", close_reason="stop",
                        closed_at=NOW.isoformat())
    reviews = review_closed_trades(journal)
    assert taken["outcome"] == "TAKEN" and reviews
    assert reviews[0]["verdict"] == "CORRECT_BUT_LOST"
    journal.close()


# ─────────────────────── no agent module can write code ───────────────────────

@pytest.mark.parametrize("module", AGENT_MODULES, ids=lambda m: m.__name__)
def test_no_agent_module_assigns_into_the_strategy(module):
    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            root = target
            while isinstance(root, ast.Attribute):
                root = root.value
            assert getattr(root, "id", "") not in STRATEGY_NAMES, (
                f"{module.__name__} assigns into the read-only strategy")


@pytest.mark.parametrize("module", AGENT_MODULES, ids=lambda m: m.__name__)
def test_no_agent_module_opens_a_file_for_writing(module):
    """The journal writes to SQLite through its own connection. Nothing in the
    agent should be opening files at all, and certainly not for writing."""
    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        assert name not in {"write_text", "write_bytes", "writelines"}, (
            f"{module.__name__} writes a file")
        if name == "open":
            mode = next((a.value for a in node.args[1:]
                         if isinstance(a, ast.Constant)), "r")
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            assert "w" not in str(mode) and "a" not in str(mode), (
                f"{module.__name__} opens a file for writing")


@pytest.mark.parametrize("module", AGENT_MODULES, ids=lambda m: m.__name__)
def test_no_agent_module_executes_generated_code(module):
    """exec/eval would route around every other check in this file."""
    tree = ast.parse(Path(module.__file__).read_text())
    called = {getattr(n.func, "id", "") for n in ast.walk(tree)
              if isinstance(n, ast.Call)}
    assert not called & {"exec", "eval", "compile", "__import__"}, module.__name__


# ───────────────── improvements are filed and left alone ─────────────────

def test_a_strategy_improvement_is_only_ever_recorded():
    journal = SMCAgentJournal(":memory:")
    journal.propose_improvement(target="STRATEGY", title="Loosen the location gate",
                                rationale="it declined six otherwise valid setups")
    row = journal.proposed_improvements(target="STRATEGY")[0]
    assert row["status"] == "PROPOSED" and row["applied"] is False
    assert freeze.source_manifest() == BASELINE["source_manifest"]
    journal.close()


def test_no_agent_module_can_mark_a_proposal_applied():
    """There is no code path to act on one. If a row ever reads applied=True,
    something outside this system did it — which is exactly what that column
    exists to make visible."""
    for module in AGENT_MODULES:
        source = Path(module.__file__).read_text()
        assert "applied=1" not in source and "applied = 1" not in source
        assert "SET applied" not in source


def test_the_review_layer_never_recommends_editing_the_strategy_itself():
    """Agent findings are things the AGENT can change about itself. Anything
    about the strategy belongs in a proposal a human reads."""
    journal = SMCAgentJournal(":memory:")
    for _ in range(4):
        journal.record_decision(symbol="BTCUSDT", timeframe="5m",
                                smc_state="ENTRY_READY", outcome="REJECTED",
                                reason_code="MINIMUM_REWARD_TO_RISK",
                                reason="under the floor", at=NOW.isoformat())
    out = weekly_review(journal, end=NOW + timedelta(minutes=1))
    assert journal.proposed_improvements() == []
    for finding in out["agent_findings"]:
        assert finding["area"] in {"execution", "journalling", "risk management",
                                   "detection", "discipline", "monitoring"}
    journal.close()
