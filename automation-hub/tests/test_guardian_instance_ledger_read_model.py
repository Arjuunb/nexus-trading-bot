"""Guardian reads the primary instance paper ledger without mutating it."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app as app_module
import webhook_api
from config import settings
from data.ledger import SqliteLedger, SupabaseLedger
from services.guardian_instance_ledger_read_model import instance_paper_ledger_snapshot


KEY = "guardian-instance-ledger-read-key-12345"


def _open_pair(ledger, *, instance_id="instance-1", execution_id="execution-1",
               session="session-1"):
    return ledger.open_position_and_trade(
        position={"symbol": "BTCUSDT", "side": "long", "size": 2,
                  "entry": 100, "stop": 95, "instance_id": instance_id,
                  "simulation_session_id": session},
        trade={"symbol": "BTCUSDT", "side": "long", "size": 2,
               "entry": 100, "stop": 95, "instance_id": instance_id,
               "simulation_session_id": session},
        execution_id=execution_id)


def test_sqlite_primary_reads_atomic_pair_and_separates_legacy(tmp_path):
    ledger = SqliteLedger(tmp_path / "primary.db")
    pid, tid = _open_pair(ledger)
    ledger.open_position(symbol="ETHUSDT", side="long", size=1, entry=50,
                         stop=40)  # unowned legacy state is not an instance
    snapshot = instance_paper_ledger_snapshot(ledger)
    assert snapshot["atomic_snapshot"] is True
    assert snapshot["source_coverage_verified"] is True
    assert snapshot["instances"] == [{"instance_id": "instance-1",
                                      "open_positions": 1, "open_trades": 1,
                                      "risk_amount": 10.0, "risk_complete": True}]
    assert snapshot["findings"][0]["position_id"] == pid
    assert snapshot["findings"][0]["trade_id"] == tid
    assert snapshot["broker_fill_verified"] is False


def test_source_lock_blocks_structurally_then_read_recovers(tmp_path):
    path = tmp_path / "primary.db"
    ledger = SqliteLedger(path)
    _open_pair(ledger)
    writer = sqlite3.connect(path)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            instance_paper_ledger_snapshot(ledger)
    finally:
        writer.rollback()
        writer.close()
    assert instance_paper_ledger_snapshot(ledger)["instances"][0]["risk_amount"] == 10


def test_partial_reduce_observes_only_the_open_remainder_pair(tmp_path):
    ledger = SqliteLedger(tmp_path / "primary.db")
    pid, tid = _open_pair(ledger)
    position = ledger.get_positions("open", instance_id="instance-1")[0]
    remainder = {**position, "size": 1}
    new_pid, new_tid = ledger.reduce_position_and_trade(
        position=position, trade_id=tid, remainder_position=remainder,
        remainder_trade=remainder, exit_price=102, pnl=2, rr=0.2,
        closed_size=1, fees=0, equity_after_close=10002,
        instance_id="instance-1", execution_id="reduce-1")
    snapshot = instance_paper_ledger_snapshot(ledger)
    assert snapshot["instances"][0]["open_positions"] == 1
    assert snapshot["instances"][0]["open_trades"] == 1
    assert snapshot["instances"][0]["risk_amount"] == 5
    assert snapshot["findings"][0]["position_id"] == new_pid != pid
    assert snapshot["findings"][0]["trade_id"] == new_tid != tid
    assert snapshot["findings"][0]["execution_id"] == "reduce-1"
    assert snapshot["findings"][0]["codes"] == []


def test_more_than_bound_fails_instead_of_hiding_exposure(tmp_path):
    ledger = SqliteLedger(tmp_path / "primary.db")
    for number in range(65):
        _open_pair(ledger, instance_id=f"instance-{number}",
                   execution_id=f"execution-{number}")
    with pytest.raises(ValueError, match="coverage exceeded"):
        instance_paper_ledger_snapshot(ledger)


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.bound = None

    def select(self, _fields):
        return self

    def eq(self, key, value):
        self.filters.append(("eq", key, value))
        return self

    def neq(self, key, value):
        self.filters.append(("neq", key, value))
        return self

    def in_(self, key, values):
        self.filters.append(("in", key, tuple(values)))
        return self

    def limit(self, bound):
        self.bound = bound
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows[:self.bound])


def test_remote_primary_uses_only_bounded_reads_and_does_not_claim_atomicity(monkeypatch):
    rows = {
        "positions": [{"id": "p", "instance_id": "instance-1", "status": "open",
                       "simulation_session_id": "s", "symbol": "BTCUSDT", "side": "long",
                       "size": 2, "entry": 100, "stop": 95}],
        "paper_trades": [{"id": "t", "instance_id": "instance-1", "status": "open",
                          "source": "paper", "simulation_session_id": "s",
                          "symbol": "BTCUSDT", "side": "long", "size": 2, "entry": 100}],
        "paper_executions": [{"execution_id": "e", "instance_id": "instance-1",
                              "action": "OPEN", "position_id": "p", "trade_id": "t"}],
    }
    queries = {}
    remote = SupabaseLedger.__new__(SupabaseLedger)

    def table(name):
        queries[name] = _Query(rows[name])
        return queries[name]

    monkeypatch.setattr(remote, "_t", table)
    snapshot = instance_paper_ledger_snapshot(remote)
    assert snapshot["instances"][0]["risk_amount"] is None
    assert snapshot["instances"][0]["risk_complete"] is False
    assert snapshot["atomic_snapshot"] is False
    assert snapshot["source_coverage_verified"] is False
    assert all(query.bound == 65 for query in queries.values())
    assert ("neq", "instance_id", "") in queries["positions"].filters
    assert ("in", "position_id", ("p",)) in queries["paper_executions"].filters


def test_app_route_requires_observer_key_and_never_mutates_ledger(tmp_path, monkeypatch):
    ledger = SqliteLedger(tmp_path / "primary.db")
    _open_pair(ledger)
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(webhook_api, "ledger", ledger)
    client = TestClient(app_module.app)
    assert client.get("/guardian/instance-ledger").status_code == 401
    assert client.get("/guardian/instance-ledger", headers={
        "X-Webhook-Secret": settings.admin_key}).status_code == 401
    result = client.get("/guardian/instance-ledger", headers={
        "X-Guardian-Observer-Key": KEY})
    assert result.status_code == 200
    assert result.json()["snapshot"]["instances"][0]["open_positions"] == 1
    assert result.json()["snapshot"]["broker_fill_verified"] is False
    assert len(ledger.get_positions("open", instance_id="instance-1")) == 1
    assert len(ledger.get_paper_trades(instance_id="instance-1")) == 1


def test_primary_remote_failure_returns_blocked_without_sqlite_fallback(monkeypatch):
    remote = SupabaseLedger.__new__(SupabaseLedger)
    def fail(_table):
        raise RuntimeError("primary remote unavailable")
    monkeypatch.setattr(remote, "_t", fail)
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(webhook_api, "ledger", remote)
    response = TestClient(app_module.app).get("/guardian/instance-ledger", headers={
        "X-Guardian-Observer-Key": KEY})
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "state": "PERSISTENCE_BLOCKED",
        "code": "INSTANCE_LEDGER_EVIDENCE_UNAVAILABLE",
    }
