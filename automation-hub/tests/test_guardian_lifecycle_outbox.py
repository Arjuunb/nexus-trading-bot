"""Material lab transitions must survive missed polls without write amplification."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
from config import settings
from services.guardian_lifecycle_outbox import install_lab_lifecycle_outbox
from services.guardian_read_model import lab_lifecycle_page
from services.lab_event_guard import (EventGuardedPriceActionPaperAccount,
                                      EventGuardedSMCPaperAccount)
from bot.types import Bar
from tradexa.guardian.lab_lifecycle import GuardianLabLifecycle
from tradexa.guardian.store import GuardianStore
from tradexa.guardian.decision_traces import decision_traces


KEY = "independent-guardian-lifecycle-observer-key"
NOW = "2026-10-02T12:00:00+00:00"


def _source(path, lab):
    table = "pa" if lab == "PRICE_ACTION" else "smc"
    conn = sqlite3.connect(path)
    conn.execute(f"""CREATE TABLE {table}_evaluations(
        correlation_id TEXT PRIMARY KEY,session_id TEXT,idempotency_key TEXT,
        candle_time TEXT,symbol TEXT,timeframe TEXT,strategy_id TEXT,
        strategy_version TEXT,{"model_id TEXT," if table == "smc" else ""}
        state TEXT,reason TEXT,missing_conditions_json TEXT,payload_json TEXT,
        created_at TEXT,updated_at TEXT)""")
    install_lab_lifecycle_outbox(conn, table)
    return conn, table


@pytest.mark.parametrize("lab", ["PRICE_ACTION", "SMC"])
def test_material_transition_only_and_no_refresh_amplification(tmp_path, lab):
    path = tmp_path / "source.db"
    conn, prefix = _source(path, lab)
    trace_key = "trace" if prefix == "pa" else "source_evaluation"
    conditions_key = "conditions" if prefix == "pa" else "ordered_condition_results"
    payload = {trace_key: {conditions_key: [
        {"key": "liquidity", "status": "PASS", "rolling_window": list(range(100))},
        {"key": "rejection", "status": "MISSING"},
    ]}}
    cols = ("correlation_id,session_id,idempotency_key,candle_time,symbol,timeframe,"
            "strategy_id,strategy_version," + ("model_id," if prefix == "smc" else "") +
            "state,reason,missing_conditions_json,payload_json,created_at,updated_at")
    values = ["decision-1", "session-1", "key-1", NOW, "BTCUSDT", "5m", "strategy", "1",
              *(["model"] if prefix == "smc" else []), "WATCHING", "waiting",
              '["rejection"]', json.dumps(payload), NOW, NOW]
    conn.execute(f"INSERT INTO {prefix}_evaluations({cols}) VALUES ({','.join('?' for _ in values)})", values)
    conn.commit()
    first = lab_lifecycle_page(path, lab)
    assert len(first["transitions"]) == 1
    assert first["transitions"][0]["conditions"] == [
        {"key": "liquidity", "status": "PASS"},
        {"key": "rejection", "status": "MISSING"},
    ]
    for i in range(100):
        payload["display_quote"] = i
        payload["heartbeat"] = i
        payload["rolling_window"] = list(range(i, i + 10))
        conn.execute(f"UPDATE {prefix}_evaluations SET payload_json=?,updated_at=?",
                     (json.dumps(payload), NOW))
    conn.commit()
    assert len(lab_lifecycle_page(path, lab)["transitions"]) == 1
    payload["lifecycle"] = [{"state": "ORDER_SUBMITTED", "reason": "submitted",
                             "order_id": "order-1", "at": NOW}]
    conn.execute(f"UPDATE {prefix}_evaluations SET state=?,reason=?,payload_json=?",
                 ("ORDER_SUBMITTED", "submitted", json.dumps(payload)))
    payload["lifecycle"].append({"state": "FILLED", "reason": "filled", "order_id": "order-1",
                                 "fill": {"order_id": "order-1", "quantity": 0.1,
                                          "price": 100, "debug_note": "not exported"}})
    conn.execute(f"UPDATE {prefix}_evaluations SET state=?,reason=?,payload_json=?",
                 ("FILLED", "filled", json.dumps(payload)))
    conn.commit()
    page = lab_lifecycle_page(path, lab, after=first["next_after"], anchor=first["next_anchor"])
    assert [row["state"] for row in page["transitions"]] == ["ORDER_SUBMITTED", "FILLED"]
    assert page["transitions"][-1]["fill"]["quantity"] == 0.1
    assert "debug_note" not in page["transitions"][-1]["fill"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(f"DELETE FROM {prefix}_guardian_lifecycle WHERE sequence=1")
    conn.close()


def test_outbox_failure_rolls_back_evaluation_and_retry(tmp_path):
    path = tmp_path / "source.db"
    conn, prefix = _source(path, "PRICE_ACTION")
    conn.execute(f"""CREATE TRIGGER fail_lifecycle BEFORE INSERT ON {prefix}_guardian_lifecycle
                    BEGIN SELECT RAISE(ABORT,'injected outbox failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected outbox failure"):
        conn.execute("""INSERT INTO pa_evaluations VALUES
                     ('decision','session','key',?,?,?,?,?,?,?,?,?,?,?)""",
                     (NOW, "BTCUSDT", "5m", "strategy", "1", "WATCHING", "waiting",
                      "[]", "{}", NOW, NOW))
    assert conn.execute("SELECT COUNT(*) FROM pa_evaluations").fetchone()[0] == 0
    conn.execute("DROP TRIGGER fail_lifecycle")
    conn.execute("""INSERT INTO pa_evaluations VALUES
                 ('decision','session','key',?,?,?,?,?,?,?,?,?,?,?)""",
                 (NOW, "BTCUSDT", "5m", "strategy", "1", "WATCHING", "waiting",
                  "[]", "{}", NOW, NOW))
    conn.commit()
    assert len(lab_lifecycle_page(path, "PRICE_ACTION")["transitions"]) == 1
    conn.close()


def test_failed_transition_keeps_prior_state_and_recovers(tmp_path):
    path = tmp_path / "source.db"
    conn, prefix = _source(path, "PRICE_ACTION")
    conn.execute("""INSERT INTO pa_evaluations VALUES
                 ('decision','session','key',?,?,?,?,?,?,?,?,?,?,?)""",
                 (NOW, "BTCUSDT", "5m", "strategy", "1", "WATCHING", "waiting",
                  "[]", "{}", NOW, NOW))
    conn.commit()
    conn.execute(f"""CREATE TRIGGER fail_transition BEFORE INSERT ON {prefix}_guardian_lifecycle
                    WHEN NEW.state='FILLED'
                    BEGIN SELECT RAISE(ABORT,'injected transition failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected transition failure"):
        conn.execute("UPDATE pa_evaluations SET state='FILLED' WHERE correlation_id='decision'")
    assert conn.execute("SELECT state FROM pa_evaluations").fetchone()[0] == "WATCHING"
    assert len(lab_lifecycle_page(path, "PRICE_ACTION")["transitions"]) == 1
    conn.execute("DROP TRIGGER fail_transition")
    conn.execute("UPDATE pa_evaluations SET state='FILLED' WHERE correlation_id='decision'")
    conn.commit()
    assert [row["state"] for row in lab_lifecycle_page(path, "PRICE_ACTION")["transitions"]] == [
        "WATCHING", "FILLED"]
    conn.close()


def test_route_requires_independent_key_and_is_read_only(tmp_path, monkeypatch):
    pa = tmp_path / "pa.db"
    conn, _ = _source(pa, "PRICE_ACTION")
    conn.close()
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "price_action_paper_db", str(pa))
    client = TestClient(app_module.app)
    assert client.get("/guardian/lifecycle?lab=PRICE_ACTION").status_code == 401
    assert client.get("/guardian/lifecycle?lab=PRICE_ACTION", headers={
        "X-Webhook-Secret": settings.admin_key}).status_code == 401
    response = client.get("/guardian/lifecycle?lab=PRICE_ACTION", headers={
        "X-Guardian-Observer-Key": KEY})
    assert response.status_code == 200
    assert response.json()["page"]["transitions"] == []
    assert response.headers["cache-control"] == "no-store"


def test_missing_source_outbox_is_structured_503(tmp_path, monkeypatch):
    pa = tmp_path / "legacy.db"
    sqlite3.connect(pa).close()
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "price_action_paper_db", str(pa))
    response = TestClient(app_module.app).get("/guardian/lifecycle?lab=PRICE_ACTION",
                                              headers={"X-Guardian-Observer-Key": KEY})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SOURCE_LIFECYCLE_UNAVAILABLE"
    assert str(pa) not in response.text


def test_restart_imports_lifecycle_once_and_source_cursor_is_checked(tmp_path):
    paths = {}
    for lab in ("PRICE_ACTION", "SMC"):
        path = tmp_path / f"{lab}.db"
        conn, prefix = _source(path, lab)
        cols = ("correlation_id,session_id,idempotency_key,candle_time,symbol,timeframe,"
                "strategy_id,strategy_version," + ("model_id," if prefix == "smc" else "") +
                "state,reason,missing_conditions_json,payload_json,created_at,updated_at")
        values = [f"{prefix}-decision", "session", "stable-key", NOW, "BTCUSDT", "5m",
                  "strategy", "1", *(["model"] if prefix == "smc" else []),
                  "WATCHING", "waiting", "[]", "{}", NOW, NOW]
        conn.execute(f"INSERT INTO {prefix}_evaluations({cols}) VALUES ({','.join('?' for _ in values)})", values)
        conn.commit()
        conn.close()
        paths[lab] = path

    def fetch(lab, after, anchor):
        return {"schema_version": 1, "observed_at": NOW,
                "scope": "POST_INSTALL_MATERIAL_LIFECYCLE",
                "feed_health_verified": False, "execution_integrity_verified": False,
                "page": lab_lifecycle_page(paths[lab], lab, after=after, anchor=anchor)}

    clock = lambda: datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    store = GuardianStore(tmp_path / "guardian.db")
    collector = GuardianLabLifecycle(store, "http://app:8000/guardian/lifecycle", KEY,
                                     fetch=fetch, clock=clock)
    assert collector.poll() == 2
    assert collector.poll() == 0
    restarted = GuardianLabLifecycle(GuardianStore(store.path),
                                     "http://app:8000/guardian/lifecycle", KEY,
                                     fetch=fetch, clock=clock)
    assert restarted.poll() == 0
    assert store.count() == 2
    traces = decision_traces(store)
    assert len(traces["traces"]) == 2
    assert all(row["post_install_lifecycle_evidence"] for row in traces["traces"])
    assert traces["lifecycle_history_complete"] is False
    pa_page = lab_lifecycle_page(paths["PRICE_ACTION"], "PRICE_ACTION")
    with pytest.raises(ValueError, match="cursor is invalid"):
        lab_lifecycle_page(paths["PRICE_ACTION"], "PRICE_ACTION",
                           after=pa_page["next_after"], anchor="wrong")


def test_real_account_initializers_capture_decisions_without_changing_strategy(tmp_path):
    candle = Bar(datetime(2026, 10, 2, 12, tzinfo=timezone.utc), 100, 102, 99, 101, 100)
    pa = EventGuardedPriceActionPaperAccount(tmp_path / "pa.db")
    visual = {"snapshot": {"strategy_traces": [{
        "strategy_id": "PA1_SR_REJECTION", "missing_conditions": ["rejection"],
        "next_required_event": "wait for rejection",
        "conditions": [{"key": "rejection", "status": "MISSING"}],
    }]}, "proposals": []}
    pa.record_evaluation(visual, candle, {"state": "SYNCHRONIZED"})
    pa.record_evaluation(visual, candle, {"state": "SYNCHRONIZED"})
    pa_events = lab_lifecycle_page(pa.path, "PRICE_ACTION")["transitions"]
    assert len(pa_events) == 1
    assert pa_events[0]["state"] == "WATCHING"
    smc = EventGuardedSMCPaperAccount(tmp_path / "smc.db")
    evaluation = {"state": "WATCHING", "missing_conditions": ["rejection"],
                  "next_required_event": "wait for rejection",
                  "ordered_condition_results": [{"key": "rejection", "status": "MISSING"}]}
    smc.record_evaluation(evaluation, candle_time=NOW)
    smc.record_evaluation(evaluation, candle_time=NOW)
    smc_events = lab_lifecycle_page(smc.path, "SMC")["transitions"]
    assert len(smc_events) == 1
    assert smc_events[0]["state"] == "WATCHING"
