"""The agent is reachable through the app the deploy actually runs.

Everything else about this wiring is proven against a runtime the test builds
itself. That proves the runtime; it does not prove that the assembled
application uses it, that the HTTP route reaches it, or that the status
endpoint -- which hydrates through a read-only copy of the runtime rather than
calling it directly -- still sees the agent. Those are the parts a deploy
finds out about, so they are worth a test of their own.
"""
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import webhook_api
from routers.native_smc import router
from services.smc_agent import SMCAgent
from services.smc_agent_journal import SMCAgentJournal, TAKEN
from services.smc_agent_runtime import (AgentGatedSMCPaperAccount,
                                        AgentSMCStrategyLabRuntime)
from services.smc_strategy_lab import SMCPaperConfig
from tests.test_smc_agent_live_wiring import Market, RULES, entry_ready

SMC = "/research/smc"


def test_the_application_builds_the_agent_wired_runtime():
    """Read-only: the objects the deployed process actually holds."""
    assert isinstance(webhook_api.smc_runtime, AgentSMCStrategyLabRuntime)
    assert isinstance(webhook_api.smc_paper, AgentGatedSMCPaperAccount)
    assert webhook_api.smc_runtime.agent is webhook_api.smc_agent
    assert webhook_api.smc_agent.journal is webhook_api.smc_agent_journal
    assert webhook_api.smc_runtime.account is webhook_api.smc_paper
    # The agent's own floor, not the strategy's.
    assert webhook_api.smc_agent.min_reward_to_risk == 3.0
    # The journal has to outlive a container, so it belongs in the data
    # directory the compose file mounts as a named volume.
    assert webhook_api.smc_agent_journal.path == webhook_api.settings.smc_agent_journal_db


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """The real router over a runtime built the way webhook_api builds it."""
    evaluation = entry_ready()
    price = float(evaluation["trade_plan"]["entry"])
    candle_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    candle = {"timestamp": candle_time.isoformat(), "open": price,
              "high": price + 1, "low": price - 1, "close": price, "volume": 1_000}
    monkeypatch.setattr(
        "services.native_smc_live_visual.live_visual_state",
        lambda *args, **kwargs: {
            "candles": [candle], "source_strategy": evaluation,
            "live_display": {"last_price": price},
            "data_provenance": {"last_closed_candle": candle_time.isoformat()}})

    account = AgentGatedSMCPaperAccount(tmp_path / "smc.db", starting_balance=100.0)
    journal = SMCAgentJournal(tmp_path / "agent.db")
    runtime = AgentSMCStrategyLabRuntime(
        Market(price), account, agent=SMCAgent(journal, equity=100.0),
        autostart=False)
    monkeypatch.setattr(webhook_api, "smc_paper", account, raising=False)
    monkeypatch.setattr(webhook_api, "smc_runtime", runtime, raising=False)
    monkeypatch.setattr(webhook_api, "_check_secret", lambda secret: None,
                        raising=False)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    yield client, account, journal
    journal.close()


def test_the_status_endpoint_reports_the_agent(app_client):
    client, _account, _journal = app_client

    body = client.get(f"{SMC}/bot-status").json()

    # The route hydrates through a copy of the runtime, so this is also the
    # check that the copy is still the agent runtime and not its base class.
    assert body["agent"]["attached"] is True
    assert body["agent"]["minimum_reward_to_risk"] == 3.0
    # A session in the default automatic mode: the lab places its own orders
    # and the agent is not the gate, so it must not claim to be.
    assert body["agent"]["is_approver"] is False


def test_switching_the_saved_mode_makes_the_agent_the_approver(app_client):
    client, _account, _journal = app_client

    configured = client.post(f"{SMC}/sessions/current/configuration",
                             json={"operating_mode": "manual_approval"})
    assert configured.status_code == 200
    assert configured.json()["session"]["operating_mode"] == "manual_approval"

    body = client.get(f"{SMC}/bot-status").json()
    assert body["agent"]["is_approver"] is True


def test_a_tick_through_the_route_lets_the_agent_take_the_trade(app_client):
    client, account, journal = app_client
    client.post(f"{SMC}/sessions/current/configuration",
                json={"operating_mode": "manual_approval"})

    body = client.post(f"{SMC}/paper/orders/reconcile").json()

    assert body["agent"]["outcome"] == TAKEN
    assert body["agent"]["executed"] is True
    assert body["real_execution_allowed"] is False
    trade = journal.trades()[0]
    assert trade["order_id"] == body["agent"]["order_id"]
    assert account.broker.order(trade["order_id"])["quantity"] == pytest.approx(trade["size"])


def test_a_second_tick_through_the_route_cannot_duplicate_the_order(app_client):
    client, account, journal = app_client
    client.post(f"{SMC}/sessions/current/configuration",
                json={"operating_mode": "manual_approval"})

    client.post(f"{SMC}/paper/orders/reconcile")
    again = client.post(f"{SMC}/paper/orders/reconcile").json()

    assert again["agent"]["outcome"] == "ALREADY_DECIDED"
    assert again["agent"]["previous_outcome"] == TAKEN
    assert len(journal.trades()) == 1
    strategy_orders = [row for row in account.state()["order_metadata"]
                       if row["ownership"] == "strategy"]
    assert len(strategy_orders) == 1


def test_an_automatic_session_reaches_the_route_unchanged(app_client):
    """The mode the deploy lands in. The lab places its own order and the
    agent journals nothing, exactly as it did before the agent existed."""
    client, account, journal = app_client

    body = client.post(f"{SMC}/paper/orders/reconcile").json()

    assert body["agent"]["enabled"] is False
    assert journal.decisions() == []
    strategy_orders = [row for row in account.state()["order_metadata"]
                       if row["ownership"] == "strategy"]
    assert len(strategy_orders) == 1
