"""Decision provenance is immutable, scoped, and never relabels legacy rows."""
import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from services.guardian_read_model import lab_lifecycle_page
from services.guardian_lifecycle_outbox import install_lab_lifecycle_outbox
from services.guardian_decision_provenance import install_lab_decision_provenance
from services.lab_event_guard import (EventGuardedPriceActionPaperAccount,
                                      EventGuardedSMCPaperAccount)
from services.price_action_lab import PaperExecutionConfig
from tradexa.guardian.lab_lifecycle import GuardianLabLifecycle
from tradexa.guardian.reports import GuardianReports
from tradexa.guardian.store import GuardianStore

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
KEY = "guardian-provenance-independent-observer-key"
COMMIT_A, COMMIT_B = "a" * 40, "b" * 40


def account(tmp_path, lab):
    cls = EventGuardedPriceActionPaperAccount if lab == "PRICE_ACTION" else EventGuardedSMCPaperAccount
    return cls(tmp_path / f"{lab}.db")


def record(acct, lab, at=NOW):
    if lab == "PRICE_ACTION":
        return acct.record_evaluation({"snapshot": {"strategy_traces": []}, "proposals": []},
                                     Bar(at, 100, 101, 99, 100, 10), {"state": "SYNCHRONIZED"})
    return acct.record_evaluation({"state": "WATCHING", "missing_conditions": ["rejection"]},
                                 candle_time=at.isoformat())


@pytest.mark.parametrize("lab", ["PRICE_ACTION", "SMC"])
def test_real_wrapper_captures_exact_saved_settings_without_orders(tmp_path, monkeypatch, lab):
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.setenv("GIT_COMMIT", COMMIT_A)
    acct = account(tmp_path, lab)
    decision = record(acct, lab)
    [transition] = lab_lifecycle_page(acct.path, lab)["transitions"]
    p = transition["decision_provenance"]
    payload = json.loads(decision["payload_json"])
    config = payload["saved_execution_config" if lab == "PRICE_ACTION" else "saved_configuration"]
    expected = {"symbol": decision["symbol"], "timeframe": decision["timeframe"], **config}
    assert p["saved_config"] == expected
    assert p["saved_config_hash"] == hashlib.sha256(json.dumps(
        expected, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert p["code_commit"] == COMMIT_A
    assert p["strategy_id"] == decision["strategy_id"]
    assert p["strategy_version"] == decision["strategy_version"]
    assert p["state"] == "CAPTURED_SAVED_SETTINGS"
    assert p["source_attestation_verified"] is False
    assert p["full_strategy_config_verified"] is False
    assert acct.broker.orders() == [] and acct.broker.positions() == []
    original = copy.deepcopy(p)
    monkeypatch.setenv("GIT_COMMIT", COMMIT_B)
    record(acct, lab)  # duplicate decision is not a new evaluation
    acct._advance_evaluation(decision["correlation_id"], "GATE_REJECTED", "blocked")
    rows = lab_lifecycle_page(acct.path, lab)["transitions"]
    assert len(rows) == 2 and all(row["decision_provenance"] == original for row in rows)
    table = ("pa" if lab == "PRICE_ACTION" else "smc") + "_guardian_decision_provenance"
    assert acct._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    for query in (f"DELETE FROM {table}", f"UPDATE {table} SET code_commit='x'"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            acct._db.execute(query)


@pytest.mark.parametrize("lab", ["PRICE_ACTION", "SMC"])
def test_writer_connection_identity_survives_other_writer_and_restart(tmp_path, monkeypatch, lab):
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.setenv("GIT_COMMIT", COMMIT_A)
    a = account(tmp_path, lab)
    record(a, lab)
    monkeypatch.setenv("GIT_COMMIT", COMMIT_B)
    b = account(tmp_path, lab)
    record(a, lab, NOW + timedelta(minutes=5))
    record(b, lab, NOW + timedelta(minutes=10))
    commits = [row["decision_provenance"]["code_commit"] for row in lab_lifecycle_page(a.path, lab)["transitions"]]
    assert commits == [COMMIT_A, COMMIT_A, COMMIT_B]
    restarted = account(tmp_path, lab)
    record(restarted, lab)
    assert len(lab_lifecycle_page(a.path, lab)["transitions"]) == 3
    assert a.broker.orders() == [] and a.broker.positions() == []


def raw_source(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE pa_evaluations(correlation_id TEXT PRIMARY KEY,
        session_id TEXT,idempotency_key TEXT,candle_time TEXT,symbol TEXT,timeframe TEXT,
        strategy_id TEXT,strategy_version TEXT,state TEXT,reason TEXT,
        missing_conditions_json TEXT,payload_json TEXT,created_at TEXT,updated_at TEXT)""")
    install_lab_lifecycle_outbox(conn, "pa")
    return conn


def raw_record(conn, identity, config=None):
    payload = {"saved_execution_config": config} if config is not None else {}
    conn.execute("INSERT INTO pa_evaluations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (identity, "session", identity, NOW.isoformat(), "BTCUSDT", "5m",
                  "PA1_SR_REJECTION", "1.1.0", "WATCHING", "waiting", "[]",
                  json.dumps(payload), NOW.isoformat(), NOW.isoformat()))
    conn.commit()


def test_legacy_and_uninstrumented_writers_stay_unknown_and_history_is_unchanged(tmp_path):
    path = tmp_path / "legacy.db"
    conn = raw_source(path)
    raw_record(conn, "old", {"risk_pct": .5})
    before = lab_lifecycle_page(path, "PRICE_ACTION")["transitions"][0]
    install_lab_decision_provenance(conn, "pa", environment={"GIT_COMMIT": COMMIT_B})
    conn.execute("UPDATE pa_evaluations SET state='GATE_REJECTED' WHERE correlation_id='old'")
    conn.commit()
    foreign = sqlite3.connect(path)
    raw_record(foreign, "foreign", {"risk_pct": .5})
    rows = lab_lifecycle_page(path, "PRICE_ACTION")["transitions"]
    assert all(row["decision_provenance"]["state"] == "UNKNOWN" for row in rows)
    assert rows[0] == before
    assert conn.execute("SELECT COUNT(*) FROM pa_guardian_decision_provenance").fetchone()[0] == 0
    assert all(row["decision_provenance"]["code_commit"] is None for row in rows)


@pytest.mark.parametrize("env", [{}, {"GIT_COMMIT": "secret=do-not-export"},
    {"GIT_COMMIT": COMMIT_A, "RENDER_GIT_COMMIT": COMMIT_B}, {"GIT_COMMIT": "a" * 7}])
def test_missing_invalid_or_conflicting_commit_is_not_certified_or_leaked(tmp_path, env):
    path = tmp_path / "db"
    conn = raw_source(path)
    install_lab_decision_provenance(conn, "pa", environment=env)
    raw_record(conn, "decision", {"risk_pct": .5})
    p = lab_lifecycle_page(path, "PRICE_ACTION")["transitions"][0]["decision_provenance"]
    assert p["code_commit"] is None and p["source_attestation_verified"] is False
    assert "do-not-export" not in json.dumps(p)


def test_allowlisted_snapshot_ignores_quote_heartbeat_secrets_and_is_bounded(tmp_path):
    path = tmp_path / "db"
    conn = raw_source(path)
    install_lab_decision_provenance(conn, "pa", environment={"GIT_COMMIT": COMMIT_A})
    raw_record(conn, "one", {"risk_pct": .5, "operating_mode": "signals_only",
               "api_key": "do-not-export", "rolling_window": list(range(10000))})
    raw_record(conn, "two", {"risk_pct": .5, "operating_mode": "signals_only", "heartbeat": 999})
    p, q = [row["decision_provenance"] for row in lab_lifecycle_page(path, "PRICE_ACTION")["transitions"]]
    assert p["saved_config_hash"] == q["saved_config_hash"]
    assert "api_key" not in json.dumps(p) and "rolling_window" not in json.dumps(p)
    assert len(json.dumps(p)) < 2000
    assert p["state"] == "INCOMPLETE_SAVED_SETTINGS"  # missing required settings, never pretend complete
    before = conn.execute("SELECT COUNT(*) FROM pa_guardian_decision_provenance").fetchone()[0]
    for _ in range(100):
        lab_lifecycle_page(path, "PRICE_ACTION")
    assert conn.execute("SELECT COUNT(*) FROM pa_guardian_decision_provenance").fetchone()[0] == before


@pytest.mark.parametrize("risk", [.3333333333333333, 1.000000000000001])
def test_snapshot_preserves_numeric_precision_instead_of_sqlite_reserialization(tmp_path, risk):
    path = tmp_path / "db"
    conn = raw_source(path)
    install_lab_decision_provenance(conn, "pa", environment={"GIT_COMMIT": COMMIT_A})
    raw_record(conn, "decision", {"risk_pct": risk})
    p = lab_lifecycle_page(path, "PRICE_ACTION")["transitions"][0]["decision_provenance"]
    assert p["saved_config"]["risk_pct"] == risk
    expected = {"symbol": "BTCUSDT", "timeframe": "5m", "risk_pct": risk}
    assert p["saved_config_hash"] == hashlib.sha256(json.dumps(
        expected, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def test_invalid_boolean_risk_is_not_silently_converted_to_numeric_one(tmp_path):
    path = tmp_path / "db"
    conn = raw_source(path)
    install_lab_decision_provenance(conn, "pa", environment={"GIT_COMMIT": COMMIT_A})
    raw_record(conn, "decision", {"risk_pct": True})
    snapshot = conn.execute("SELECT saved_config_json FROM pa_guardian_decision_provenance").fetchone()[0]
    assert json.loads(snapshot)["risk_pct"] is True
    with pytest.raises(ValueError, match="numeric setting"):
        lab_lifecycle_page(path, "PRICE_ACTION")


def test_provenance_failure_is_atomic_with_evaluation_and_outbox(tmp_path):
    path = tmp_path / "db"
    conn = raw_source(path)
    install_lab_decision_provenance(conn, "pa", environment={"GIT_COMMIT": COMMIT_A})
    conn.execute("""CREATE TRIGGER fail_provenance BEFORE INSERT ON pa_guardian_decision_provenance
                    BEGIN SELECT RAISE(ABORT,'injected provenance failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected provenance failure"):
        raw_record(conn, "decision", {"risk_pct": .5})
    for table in ("pa_evaluations", "pa_guardian_lifecycle", "pa_guardian_decision_provenance"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    conn.execute("DROP TRIGGER fail_provenance")
    raw_record(conn, "decision", {"risk_pct": .5})
    for table in ("pa_evaluations", "pa_guardian_lifecycle", "pa_guardian_decision_provenance"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1


def test_forged_export_does_not_advance_collector_cursor_and_retry_recovers(tmp_path):
    path = tmp_path / "source.db"
    conn = raw_source(path)
    install_lab_decision_provenance(conn, "pa", environment={"GIT_COMMIT": COMMIT_A})
    raw_record(conn, "decision", {"risk_pct": .5})
    page = lab_lifecycle_page(path, "PRICE_ACTION")
    tamper = True
    def fetch(lab, after, anchor):
        view = copy.deepcopy(page) if lab == "PRICE_ACTION" else {
            "lab": "SMC", "coverage": "POST_INSTALL_MATERIAL_LIFECYCLE", "after": after,
            "anchor": anchor, "has_more": False, "next_after": after, "next_anchor": anchor,
            "transitions": []}
        if tamper:
            view["transitions"][0]["decision_provenance"]["saved_config_hash"] = "b" * 64
        return {"schema_version": 1, "observed_at": NOW.isoformat(),
                "scope": "POST_INSTALL_MATERIAL_LIFECYCLE", "feed_health_verified": False,
                "execution_integrity_verified": False, "page": view}
    store = GuardianStore(tmp_path / "guardian.db")
    collector = GuardianLabLifecycle(store, "http://app:8000/guardian/lifecycle", KEY,
                                     fetch=fetch, clock=lambda: NOW)
    with pytest.raises(ValueError, match="hash or identity"):
        collector.poll()
    assert store.count() == 0 and store.observer_cursor("pa_lifecycle") == (0, "")
    tamper = False
    assert collector.poll() == 1
    assert store.observer_cursor("pa_lifecycle") == (page["next_after"], page["next_anchor"])
    assert conn.execute("SELECT COUNT(*) FROM pa_evaluations").fetchone()[0] == 1


def test_collector_restart_keeps_identity_and_report_does_not_certify_partial_config(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.setenv("GIT_COMMIT", COMMIT_A)
    monkeypatch.setattr("services.price_action_lab._iso", lambda: (NOW - timedelta(minutes=1)).isoformat())
    monkeypatch.setattr("services.smc_strategy_lab._iso", lambda: (NOW - timedelta(minutes=1)).isoformat())
    accounts = {lab: account(tmp_path, lab) for lab in ("PRICE_ACTION", "SMC")}
    for lab, acct in accounts.items():
        record(acct, lab, NOW - timedelta(minutes=10))
        if lab == "PRICE_ACTION":
            acct.configure(execution_config=PaperExecutionConfig(risk_pct=.75))
        else:
            acct._db.execute("UPDATE smc_sessions SET risk_pct=.75")
        record(acct, lab, NOW - timedelta(minutes=5))
    def fetch(lab, after, anchor):
        return {"schema_version": 1, "observed_at": NOW.isoformat(),
                "scope": "POST_INSTALL_MATERIAL_LIFECYCLE", "feed_health_verified": False,
                "execution_integrity_verified": False,
                "page": lab_lifecycle_page(accounts[lab].path, lab, after=after, anchor=anchor)}
    store = GuardianStore(tmp_path / "guardian.db")
    def collector():
        return GuardianLabLifecycle(GuardianStore(store.path), "http://app:8000/guardian/lifecycle",
                                    KEY, fetch=fetch, clock=lambda: NOW)
    assert collector().poll() == 4
    assert collector().poll() == 0 and store.count() == 4
    for event in store.recent():
        p = event["metadata"]["decision_provenance"]
        assert event["metadata"]["code_commit"] == COMMIT_A
        assert event["metadata"]["saved_config_hash"] == p["saved_config_hash"]
        assert "config_hash" not in event["metadata"]  # never claim a full engine configuration
        assert event["metadata"]["exact_version_verified"] is False
    report = GuardianReports(store).generate("DAILY", NOW.replace(hour=0), now=NOW + timedelta(days=1))
    groups = report["strategies"]
    assert len(groups) == 4  # different saved risk settings must not be pooled
    assert all(group["exact_version_verified"] is False for group in groups)
    assert all(group["saved_config_hash"] for group in groups)
