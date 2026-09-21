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
