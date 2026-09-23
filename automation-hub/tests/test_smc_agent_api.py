"""The read-only agent endpoint behind the agent panel.

The agent approves and places its own orders with nobody in the loop, so the
only way to hold it to account is to read back every decision it made -- not
just the ones that became trades. An agent declining everything and an agent
that is not running produce the same empty trade list, and those need to look
different.
"""
import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import webhook_api
from routers.native_smc import router
from services.smc_agent_journal import SMCAgentJournal
from services.smc_strategy_lab import SMCPaperAccount


@pytest.fixture()
def api(monkeypatch, tmp_path):
    account = SMCPaperAccount(tmp_path / "smc-agent-api.db")
    journal = SMCAgentJournal(tmp_path / "agent-journal.db")
    monkeypatch.setattr(webhook_api, "smc_paper", account)
    monkeypatch.setattr(webhook_api, "smc_agent_journal", journal)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), journal


def record(journal, *, outcome, candle, reason="judged", code="JUDGED", rr=4.0):
    return journal.record_decision(
        symbol="BTCUSDT", timeframe="5m", smc_state="ENTRY_READY",
        outcome=outcome, reason_code=code, reason=reason,
        gates={"reward_to_risk": rr}, candle_time=candle,
        proposal_id=f"p-{candle}")


def test_the_panel_endpoint_never_offers_real_execution(api):
    client, _ = api
    body = client.get("/research/smc/agent").json()

    assert body["paper_only"] is True
    assert body["real_execution_allowed"] is False


def test_an_agent_that_rejected_everything_is_not_an_empty_panel(api):
    """The distinction the panel exists for: nothing traded because the agent
    judged and said no, versus nothing traded because nothing is running."""
    client, journal = api
    record(journal, outcome="REJECTED", candle="2026-09-21T10:00:00+00:00",
           code="RR_BELOW_MINIMUM",
           reason="reward-to-risk 1.80 is below the 3.0 minimum", rr=1.8)
    record(journal, outcome="REJECTED", candle="2026-09-21T10:05:00+00:00",
           code="SIZE_BELOW_FLOOR",
           reason="size 0.004 is below the 0.01 floor", rr=5.0)

    body = client.get("/research/smc/agent").json()

    assert body["decision_counts"] == {"REJECTED": 2}
    assert len(body["decisions"]) == 2
    assert body["trades"] == []
    reasons = [row["reason"] for row in body["decisions"]]
    assert any("below the 3.0 minimum" in text for text in reasons)
    assert any("below the 0.01 floor" in text for text in reasons)
    # The measured number, not just the prose, so the panel can show it.
    assert {row["gates"]["reward_to_risk"] for row in body["decisions"]} == {1.8, 5.0}


def test_decisions_can_be_filtered_to_one_outcome(api):
    client, journal = api
    record(journal, outcome="TAKEN", candle="2026-09-21T10:00:00+00:00")
    record(journal, outcome="REJECTED", candle="2026-09-21T10:05:00+00:00")
    record(journal, outcome="NOT_READY", candle="2026-09-21T10:10:00+00:00")
    record(journal, outcome="MISSED", candle="2026-09-21T10:15:00+00:00")

    taken = client.get("/research/smc/agent?outcome=taken").json()

    assert [row["outcome"] for row in taken["decisions"]] == ["TAKEN"]
    assert taken["decision_counts"] == {"TAKEN": 1}


def test_the_panel_reports_why_the_agent_cannot_act(api):
    """A silent agent with a dead feed must say so here. This is the page
    someone opens when nothing is happening, so the blocker belongs on it."""
    client, _ = api

    body = client.get("/research/smc/agent").json()

    assert "execution_state" in body
    assert isinstance(body["blockers"], list)
    assert "state" in body["feed"]
    assert "transport_diagnostics" in body["feed"]


def test_the_limit_is_bounded_so_one_request_cannot_read_the_whole_journal(api):
    client, journal = api
    for minute in range(0, 30, 5):
        record(journal, outcome="REJECTED",
               candle=f"2026-09-21T10:{minute:02d}:00+00:00")

    assert len(client.get("/research/smc/agent?limit=2").json()["decisions"]) == 2
    assert client.get("/research/smc/agent?limit=100000").status_code == 200
    assert client.get("/research/smc/agent?limit=0").status_code == 200


def test_the_endpoint_needs_no_secret_because_it_changes_nothing(api):
    """It is a read. Requiring the admin key here would only mean the panel
    could not load, and there is nothing on it to protect that /paper and
    /bot-status do not already expose."""
    client, _ = api

    assert client.get("/research/smc/agent").status_code == 200


def test_every_outcome_the_journal_defines_survives_the_endpoint(api):
    """The four are not interchangeable and the panel must not blur them.
    REJECTED is the agent choosing not to trade; NOT_READY is the strategy
    never offering one; MISSED is a trade the agent should have taken and
    could not, which is the only one of the four that means something is
    broken."""
    client, journal = api
    from services.smc_agent_journal import DECISION_OUTCOMES

    for index, outcome in enumerate(DECISION_OUTCOMES):
        record(journal, outcome=outcome,
               candle=f"2026-09-21T11:{index * 5:02d}:00+00:00")

    body = client.get("/research/smc/agent").json()

    assert body["decision_counts"] == {name: 1 for name in DECISION_OUTCOMES}


# ──────────────────────────────── policy ──────────────────────────────────
from dataclasses import asdict  # noqa: E402

from services.smc_agent_context import ContextPolicy  # noqa: E402
from services.smc_agent_memory import MemoryPolicy  # noqa: E402
from services.smc_agent_policy_store import AgentPolicyStore  # noqa: E402
from services.smc_agent_trade_manager import TradeManagementPolicy  # noqa: E402


class FakeRuntime:
    def __init__(self):
        self.trade_policy = TradeManagementPolicy()
        self.context_policy = ContextPolicy()
        self.memory_policy = MemoryPolicy()


@pytest.fixture()
def policy_api(monkeypatch, tmp_path):
    store = AgentPolicyStore(tmp_path / "policy.json")
    runtime = FakeRuntime()
    monkeypatch.setattr(webhook_api, "smc_agent_policies", store, raising=False)
    monkeypatch.setattr(webhook_api, "smc_runtime", runtime, raising=False)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), store, runtime


SECRET = {"x-webhook-secret": webhook_api.settings.admin_key}


def test_every_rule_set_is_off_until_someone_turns_it_on(policy_api):
    client, _, _ = policy_api

    body = client.get("/research/smc/agent/policy").json()

    assert body["trade_management"]["enabled"] is False
    assert body["context"]["enabled"] is False
    assert body["memory"]["enabled"] is False
    assert "hypotheses to backtest" in body["note"]


def test_saving_a_policy_needs_the_admin_key(policy_api):
    """Everything else on the agent surface only reads. This changes how the
    agent trades, so it is the one call that is protected."""
    client, _, runtime = policy_api

    refused = client.post("/research/smc/agent/policy",
                          json={"trade_management": {"enabled": True}})

    assert refused.status_code == 401
    assert runtime.trade_policy.enabled is False


def test_a_saved_policy_is_applied_to_the_running_agent(policy_api):
    client, store, runtime = policy_api

    saved = client.post("/research/smc/agent/policy", headers=SECRET, json={
        "trade_management": {"enabled": True, "breakeven_at_r": 1.0,
                             "trail_after_r": 2.0},
        "context": {"enabled": True, "max_consecutive_losses": 3},
        "memory": {"enabled": True, "min_sample": 25}})

    assert saved.status_code == 200
    assert saved.json()["applied"] is True
    assert runtime.trade_policy.enabled is True
    assert runtime.trade_policy.trail_after_r == 2.0
    assert runtime.context_policy.max_consecutive_losses == 3
    assert runtime.memory_policy.min_sample == 25
    # And it survives a restart.
    assert store.load()["memory"].min_sample == 25


def test_a_rejected_value_changes_neither_the_file_nor_the_agent(policy_api):
    """Validation happens before anything is written. A policy that is half
    applied is one nobody can reason about."""
    client, store, runtime = policy_api
    client.post("/research/smc/agent/policy", headers=SECRET,
                json={"context": {"enabled": True, "max_consecutive_losses": 3}})

    refused = client.post("/research/smc/agent/policy", headers=SECRET, json={
        "trade_management": {"enabled": True},
        "context": {"enabled": True, "max_consecutive_losses": 3},
        "memory": {"enabled": True, "min_sample": 2}})     # below the floor

    assert refused.status_code == 400
    assert "anecdote" in refused.json()["detail"]
    assert runtime.trade_policy.enabled is False, "a rejected save was applied"
    assert store.load()["context"].max_consecutive_losses == 3


def test_the_endpoint_reports_the_running_policy_not_the_saved_file(policy_api):
    """A file the runtime never picked up must not read back as in force."""
    client, store, runtime = policy_api
    store.save({"trade_management": {"enabled": True}, "context": {}, "memory": {}})

    body = client.get("/research/smc/agent/policy").json()

    assert body["trade_management"]["enabled"] is False


def test_an_unreadable_policy_file_falls_back_to_everything_off(tmp_path):
    """A policy store that could fail open would be a way to turn a trading
    behaviour on by corrupting a file."""
    path = tmp_path / "broken.json"
    path.write_text("{ this is not json")

    loaded = AgentPolicyStore(path).load()

    assert [policy.enabled for policy in loaded.values()] == [False, False, False]


def test_one_broken_section_does_not_disable_the_others(tmp_path):
    path = tmp_path / "partial.json"
    path.write_text('{"context": {"enabled": true, "max_consecutive_losses": 3},'
                    ' "memory": {"enabled": true, "min_sample": 1}}')

    loaded = AgentPolicyStore(path).load()

    assert loaded["context"].enabled is True
    assert loaded["memory"].enabled is False, "an invalid section was accepted"


def test_an_unknown_field_is_ignored_rather_than_accepted(tmp_path):
    store = AgentPolicyStore(tmp_path / "p.json")

    saved = store.save({"trade_management": {"enabled": True, "moon_phase": 3},
                        "context": {}, "memory": {}})

    assert saved["trade_management"].enabled is True
    assert not hasattr(saved["trade_management"], "moon_phase")
