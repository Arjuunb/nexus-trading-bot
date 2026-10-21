"""Acceptance boot must inspect existing financial state without changing it."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_acceptance_flag(value, monkeypatch):
    from services.acceptance_mode import enabled
    monkeypatch.setenv("HUB_ACCEPTANCE_READ_ONLY", value)
    assert enabled()


def test_invalid_flag_fails_closed(monkeypatch):
    from services.acceptance_mode import enabled
    monkeypatch.setenv("HUB_ACCEPTANCE_READ_ONLY", "tru")
    with pytest.raises(RuntimeError, match="HUB_ACCEPTANCE_READ_ONLY"):
        enabled()


def test_read_only_connection_sees_committed_wal_and_rejects_writes(tmp_path):
    from services.acceptance_mode import existing_connection
    path = tmp_path / "finance.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE records(value INTEGER)")
    writer.execute("INSERT INTO records VALUES (42)")
    writer.commit()
    with existing_connection(path) as reader:
        assert reader.execute("SELECT value FROM records").fetchone()[0] == 42
        for sql in ("DELETE FROM records", "CREATE TABLE injected(x)",
                    "PRAGMA query_only=OFF", "ATTACH ':memory:' AS other"):
            with pytest.raises(sqlite3.DatabaseError):
                reader.execute(sql)
    writer.close()


def test_missing_database_is_never_created(tmp_path):
    from services.acceptance_mode import existing_connection
    path = tmp_path / "missing.db"
    with pytest.raises((sqlite3.Error, RuntimeError)):
        existing_connection(path)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["account", "journal", "v2", "pa", "smc"])
def test_existing_financial_store_reopens_without_changing_rows(kind, tmp_path, monkeypatch):
    from data.account_store import AccountStore
    from data.journal_store import JournalStore
    from execution.paper_broker_v2 import PaperBrokerV2
    from services.price_action_lab import PriceActionPaperAccount
    from services.smc_strategy_lab import SMCPaperAccount
    factories = {"account": AccountStore, "journal": JournalStore,
                 "v2": PaperBrokerV2, "pa": PriceActionPaperAccount,
                 "smc": SMCPaperAccount}
    monkeypatch.delenv("HUB_ACCEPTANCE_READ_ONLY", raising=False)
    path = tmp_path / (kind + ".db")
    original = factories[kind](path)
    if kind == "account":
        original.seed_if_empty(12345)
    with sqlite3.connect(path) as db:
        before = list(db.iterdump())
    monkeypatch.setenv("HUB_ACCEPTANCE_READ_ONLY", "1")
    reopened = factories[kind](path)
    connection = getattr(reopened, "_c", None) or reopened._db
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("CREATE TABLE forbidden(value)")
    if kind == "account":
        assert reopened.seed_if_empty(99999)["initial_capital"] == 12345
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


def test_native_account_owner_conflict_still_rejected(tmp_path, monkeypatch):
    from execution.paper_broker_v2 import PaperBrokerV2
    path = tmp_path / "native.db"
    PaperBrokerV2(path, evidence_owner="alice")
    monkeypatch.setenv("HUB_ACCEPTANCE_READ_ONLY", "1")
    with pytest.raises(ValueError, match="OWNER_CONFLICT"):
        PaperBrokerV2(path, evidence_owner="bob")


@pytest.mark.parametrize("kind", ["account", "journal", "v2", "pa", "smc"])
def test_acceptance_never_creates_missing_financial_stores(kind, tmp_path, monkeypatch):
    from data.account_store import AccountStore
    from data.journal_store import JournalStore
    from execution.paper_broker_v2 import PaperBrokerV2
    from services.price_action_lab import PriceActionPaperAccount
    from services.smc_strategy_lab import SMCPaperAccount
    factories = {"account": AccountStore, "journal": JournalStore,
                 "v2": PaperBrokerV2, "pa": PriceActionPaperAccount, "smc": SMCPaperAccount}
    path = tmp_path / "missing.db"
    monkeypatch.setenv("HUB_ACCEPTANCE_READ_ONLY", "1")
    with pytest.raises((sqlite3.Error, RuntimeError)):
        factories[kind](path)
    assert not path.exists()


@pytest.mark.parametrize("auth_mode", ["legacy", "supabase"])
def test_full_boot_retains_saved_intent_and_financial_state(tmp_path, monkeypatch, auth_mode):
    from data.account_store import AccountStore
    from data.journal_store import JournalStore
    from data.ledger import SqliteLedger
    from execution.paper_broker_v2 import PaperBrokerV2
    from services.grid_engine import GridBot, GridRunner
    from services.price_action_lab import PriceActionPaperAccount
    from services.smc_strategy_lab import SMCPaperAccount
    from services.trading_instances import TradingInstanceManager

    monkeypatch.delenv("HUB_ACCEPTANCE_READ_ONLY", raising=False)
    AccountStore(tmp_path / "account.db").seed_if_empty(9876)
    JournalStore(tmp_path / "journal.db")
    PaperBrokerV2(tmp_path / "paper_broker_v2.db")
    PriceActionPaperAccount(tmp_path / "price_action_paper.db")
    SMCPaperAccount(tmp_path / "smc_strategy_paper.db")
    ledger = SqliteLedger(tmp_path / "ledger.db")
    manager = TradingInstanceManager(ledger, strategy_factory=lambda *_: None,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain",
        strategy_label="Decision Brain", strategy_version="v1", timeframe="5m",
        risk_per_trade_pct=.005, capital_allocation=1000)
    instance.desired_running, instance.state = True, "running"
    # A legacy saved instance must not receive a new financial session on boot.
    instance.simulation_session_id, instance.simulation_session_number = "", 0
    manager.store.save(instance)
    grid = GridRunner(GridBot(symbol="BTCUSDT", timeframe="5m", lower=10,
                             upper=20, levels=5, geometric=False,
                             investment=100, start_price=15), lambda *_: [], ledger)
    grid_snapshot = grid.snapshot()
    grid_snapshot["running"] = True
    (tmp_path / "grid.json").write_text(json.dumps(grid_snapshot))
    (tmp_path / "runtime_settings.json").write_text(json.dumps({"engine_desired_running": 1}))

    def snapshot():
        result = {}
        for path in sorted(tmp_path.glob("*.db")):
            if path.name not in {"account.db", "journal.db", "paper_broker_v2.db",
                                 "price_action_paper.db", "smc_strategy_paper.db", "ledger.db"}:
                continue
            with sqlite3.connect(path) as db:
                result[path.name] = list(db.iterdump())
        for name in ("grid.json", "runtime_settings.json"):
            result[name] = (tmp_path / name).read_text()
        return result

    before = snapshot()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HUB_", "SUPABASE_"))
           and k not in {"PYTEST_CURRENT_TEST", "RENDER", "DYNO"}}
    env.update(HUB_DATA_DIR=str(tmp_path), HUB_ACCEPTANCE_READ_ONLY="1",
               HUB_AUTO_ENGINE="1", HUB_USE_LIVE_DATA="1", HUB_MONITOR_AGENT="1",
               HUB_RESEARCH_AUTOSTART="1", HUB_ENABLE_EXTERNAL_LIVE="1",
               HUB_AUTH_MODE=auth_mode)
    root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join([str(root / "automation-hub"), str(root)])
    script = '''
import threading, os
if os.environ["HUB_AUTH_MODE"] == "supabase":
    # Real authentication code, isolated provider responses. This is not a
    # claim of target/provider acceptance and makes no network requests.
    import data.ledger
    data.ledger.get_ledger = lambda path: data.ledger.SqliteLedger(path)
    from services.supabase_auth import SupabaseAuth
    def provider(self, path, **kwargs):
        ordinary = kwargs["token"] == "ordinary-user"
        identity = "22222222-2222-2222-2222-222222222222" if ordinary else "11111111-1111-1111-1111-111111111111"
        if path == "/auth/v1/user":
            return {"id": identity, "email": "fixture@example.com", "email_confirmed_at": "2026-01-01T00:00:00Z"}
        return [{"id": identity, "full_name": "Fixture", "role": "user" if ordinary else "admin"}]
    SupabaseAuth._request = provider
started = []
original = threading.Thread.start
def reject_start(self):
    started.append(self.name)
    raise AssertionError("unexpected startup thread: " + self.name)
threading.Thread.start = reject_start
import app
app._start_auto_engine()
app._shutdown_all_runtimes()
assert started == [], started
assert app.webhook_api.instance_manager._runtime == {}
assert app.webhook_api.engine.autostart_enabled
threading.Thread.start = original
from fastapi.testclient import TestClient
from services.session_auth import sign
with TestClient(app.app) as client:
    path = "/api/v2/strategy-intelligence/sessions"
    assert client.get(path).status_code == 401
    if app.settings.auth_mode == "supabase":
        response = client.post("/auth/supabase/session", headers={"Authorization": "Bearer fixture-admin"}, json={"remember": False})
        assert response.status_code == 200, response.text
        assert "hub_supabase_access" in client.cookies
    else:
        client.cookies.set(app.COOKIE, sign("admin", app.settings.secret_key, ttl_days=1))
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert client.post("/api/v1/controls/resume").status_code == 409
    assert client.get("/health").json()["acceptance_read_only"] is True
    assert client.get("/price-action/status").status_code == 409
    if app.settings.auth_mode == "supabase":
        response = client.get(path, headers={"Authorization": "Bearer ordinary-user"})
        assert response.status_code == 403, response.text
assert app.webhook_api.instance_manager._runtime == {}
'''
    result = subprocess.run([sys.executable, "-c", script], env=env, cwd=root,
                            text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-5000:]
    assert snapshot() == before
