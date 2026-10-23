"""Phase 1 end-to-end: login -> create bot -> start (paper) -> dashboard.

Skips cleanly if FastAPI isn't installed (so the parent engine's suite still
runs without the hub extra).
"""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import app as hub_app  # noqa: E402
from bots.manager import BotManager  # noqa: E402
from dashboard.stream import HubEventHub  # noqa: E402


@pytest.fixture()
def client():
    # Fresh manager + sessions + event hub per test.
    hub_app.manager = BotManager()
    hub_app.hub_events = HubEventHub()
    hub_app._sessions.clear()
    return TestClient(hub_app.app, follow_redirects=False)


@pytest.fixture()
def legacy_dashboard(monkeypatch):
    """Phase 1 HTML/SSE assertions exercise the retained server renderer.

    Built React assets contain a boot shell; their DOM is verified in browser
    tests. Local generated bundles must not silently switch these HTML tests
    to a different renderer.
    """
    monkeypatch.setattr(hub_app, "_LANDING_READY", False)
    monkeypatch.setattr(hub_app, "_WEBUI_READY", False)


def _login(client) -> None:
    r = client.post("/login", data={"username": "admin", "password": "admin"})
    assert r.status_code == 303
    token = r.cookies.get(hub_app.COOKIE)
    assert token and hub_app._verify_session(token) == "admin"  # signed, stateless


def test_dashboard_requires_login(client):
    dashboard = "/app" if hub_app._LANDING_READY else "/"
    r = client.get(dashboard)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_rejects_bad_password(client):
    r = client.post("/login", data={"username": "admin", "password": "nope"})
    assert r.status_code == 303
    assert "error" in r.headers["location"]


def test_full_phase1_flow(client, legacy_dashboard):
    _login(client)

    # Dashboard renders with the spec's KPI labels + sidebar.
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "TradeLogX Nexus" in body
    for label in ("Running Bots", "Paper Bots", "Alerts",
                  "Active Bots", "Risk Center", "Recent Activity"):
        assert label in body
    assert "P&amp;L" in body  # Today's P&L KPI (HTML-escaped)
    for nav in ("Overview", "Bots", "Strategies", "Paper Trading",
                "Live Trading", "Risk Center", "Analytics", "Logs",
                "Notifications", "Settings"):
        assert nav in body

    # Create an EMA bot.
    r = client.post("/bots", data={
        "name": "EMA Trend Bot", "strategy": "ema", "exchange": "binance",
        "symbol": "BTCUSDT", "timeframe": "1h", "mode": "paper",
        "risk_per_trade": "1.0", "max_daily_loss": "3.0",
    })
    assert r.status_code == 303
    bots = hub_app.manager.list()
    assert len(bots) == 1
    bot = bots[0]
    assert bot.config.strategy == "ema"

    # Start it -> paper run executes, runtime gets populated.
    r = client.post(f"/bots/{bot.id}/start")
    assert r.status_code == 303
    assert bot.runtime.state.value == "Paper Mode"
    assert "num_trades" in bot.runtime.metrics
    assert bot.runtime.equity_curve

    # Pause it.
    r = client.post(f"/bots/{bot.id}/pause")
    assert r.status_code == 303
    assert bot.runtime.state.value == "Paused"

    # Emergency stop halts everything.
    client.post(f"/bots/{bot.id}/start")
    r = client.post("/emergency-stop")
    assert r.status_code == 303
    assert bot.runtime.state.value == "Stopped"


def test_secondary_pages_render(client):
    _login(client)
    for path in ("/bots", "/bots/new", "/strategies", "/paper-trading",
                 "/risk-center", "/analytics", "/logs", "/live-trading",
                 "/notifications", "/settings", "/users"):
        assert client.get(path).status_code == 200


def test_admin_can_add_user(client):
    import uuid
    _login(client)
    page = client.get("/users").text
    assert "admin" in page and "Add User" in page  # seeded admin + admin form
    uname = "op_" + uuid.uuid4().hex[:8]
    r = client.post("/users", data={"username": uname, "password": "pw",
                                    "role": "operator"})
    assert r.status_code == 303
    assert hub_app.store.get_user(uname) is not None
    # New user can authenticate with the hashed password.
    assert hub_app.store.authenticate(uname, "pw") is not None


def test_overview_has_live_stream_client(client, legacy_dashboard):
    _login(client)
    body = client.get("/").text
    assert "Live Feed" in body
    assert "EventSource" in body and "/events/stream" in body


def test_go_live_streams_events_to_hub(client):
    _login(client)
    r = client.post("/bots", data={
        "name": "Streamer", "strategy": "ema", "exchange": "binance",
        "symbol": "BTCUSDT", "timeframe": "1h", "mode": "live",
        "risk_per_trade": "1.0", "max_daily_loss": "3.0",
    })
    bot = hub_app.manager.list()[0]
    client.post(f"/bots/{bot.id}/go-live")
    hub_app.manager.runner(bot.id).wait(timeout=15)   # finite replay feed ends

    state = client.get("/events/state").json()
    types = {e["type"] for e in state["events"]}
    assert "lifecycle" in types          # published on go-live
    assert "run_finished" in types       # forwarded from the runner
    # forwarded events carry the bot identity
    assert any(e.get("bot_id") == bot.id for e in state["events"])


def test_edit_and_backtest_bot(client):
    _login(client)
    client.post("/bots", data={
        "name": "Editable", "strategy": "ema", "exchange": "binance",
        "symbol": "BTCUSDT", "timeframe": "1h", "mode": "paper",
        "risk_per_trade": "1.0", "max_daily_loss": "3.0",
    })
    bot = hub_app.manager.list()[0]

    # Edit form prefilled, then save changes.
    assert "Editable" in client.get(f"/bots/{bot.id}/edit").text
    r = client.post(f"/bots/{bot.id}/edit", data={
        "name": "Edited", "strategy": "rsi", "symbol": "ETHUSDT",
        "timeframe": "15m", "risk_per_trade": "2.0", "max_daily_loss": "4.0",
        "max_drawdown": "12.0", "max_consecutive_losses": "3",
    })
    assert r.status_code == 303
    assert bot.config.name == "Edited" and bot.config.symbol == "ETHUSDT"
    assert bot.config.strategy == "rsi"
    assert abs(bot.config.risk.max_drawdown_pct - 0.12) < 1e-9

    # Ad-hoc backtest renders results without changing state.
    page = client.get(f"/bots/{bot.id}/backtest").text
    assert "Backtest" in page and "Win rate" in page and "Trade History" in page
    assert bot.runtime.state.value == "Created"


def test_health_open(client, monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "bcb1e58cafebabedeadbeef")
    monkeypatch.setenv("GIT_BRANCH", "main")
    monkeypatch.setenv("DEPLOYED_AT", "2026-08-28T12:00:00Z")
    health = client.get("/health").json()
    assert health["status"] == "ok"
    assert health["commit"] == "bcb1e58cafebabedeadbeef"
    assert health["commit_short"] == "bcb1e58"
    assert health["branch"] == "main"
    assert health["deployed_at"] == "2026-08-28T12:00:00Z"
