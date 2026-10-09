"""Durable source provenance must not become Guardian trading authority."""
import json
import sqlite3
import ast
from copy import deepcopy
from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone

import pytest

from execution.paper_broker_v2 import PaperBrokerV2


@pytest.fixture
def broker(tmp_path):
    instance = PaperBrokerV2(tmp_path / "smc.db", account_type="SMC_LAB", execution_engine="SMC_LAB",
                             fee_rate=0, spread_bps=0, slippage_bps=0, participation_rate=1)
    yield instance
    instance._c.close()


def submit(broker, key="decision-1", **changes):
    values = dict(symbol="BTCUSDT", side="buy", order_type="market", quantity=1,
                  strategy="SMC_M1_SWEEP_REVERSAL", timeframe="5m", candle_id=key)
    values.update(changes)
    return broker.submit(**values)


def candle(broker, price=100, volume=100):
    return broker.process_candle("BTCUSDT", dict(open=price, high=price + 1, low=price - 1,
                                                close=price, volume=volume))


def transitions(broker):
    return [json.loads(row["fill_position_json"]) for row in broker._c.execute(
        "SELECT fill_position_json FROM v2_fills ORDER BY rowid")]


def test_entry_and_protective_exit_retain_original_position_and_execution_ids(broker):
    order = submit(broker)
    candle(broker)
    [position] = broker.positions()
    broker.set_protection("BTCUSDT", stop_loss=90, take_profit=120)
    candle(broker, price=80)
    opened, closed = transitions(broker)
    assert opened["before"] is None and opened["effect"] == "OPEN"
    assert opened["after"]["position_id"] == position["position_id"]
    assert opened["after"]["entry_order_id"] == order["id"]
    assert opened["after"]["entry_execution_key"] == "decision-1"
    assert closed["before"] == opened["after"]
    assert closed["after"] is None and closed["effect"] == "CLOSE"
    assert closed["reduce_only"] is True and closed["persisted_order"] is False
    assert closed["order_id"].startswith("protective-")
    assert len(broker.orders()) == 1 and broker.positions() == []
    assert [r["price"] for r in broker._c.execute("SELECT price FROM v2_fills ORDER BY rowid")] == [100, 80]


def test_restart_preserves_source_fill_transition_without_reconstructing_it(broker):
    submit(broker)
    candle(broker)
    before = transitions(broker)
    with closing(sqlite3.connect(broker.path)) as db:
        committed = db.execute("SELECT fill_position_json FROM v2_fills").fetchone()[0]
    restarted = PaperBrokerV2(broker.path, account_type="SMC_LAB", execution_engine="SMC_LAB")
    try:
        assert transitions(restarted) == before
        assert restarted._c.execute("SELECT fill_position_json FROM v2_fills").fetchone()[0] == committed
        assert len(restarted.orders()) == len(restarted.positions()) == 1
    finally:
        restarted._c.close()


def test_fill_insert_failure_rolls_back_position_and_origin_together(broker):
    order = submit(broker)
    before = broker.account(persist_metrics=False)
    broker._c.execute("CREATE TRIGGER injected BEFORE INSERT ON v2_fills BEGIN SELECT RAISE(ABORT, 'fixture disk full'); END")
    with pytest.raises(sqlite3.IntegrityError):
        candle(broker)
    assert broker.positions() == [] and broker.fills() == []
    assert broker.order(order["id"])["filled"] == 0
    assert broker.account(persist_metrics=False)["balance"] == before["balance"]
    broker._c.execute("DROP TRIGGER injected")
    candle(broker)
    assert len(transitions(broker)) == len(broker.positions()) == 1


@pytest.mark.parametrize("account_type", ["PAPER", "PA_LAB"])
def test_non_smc_accounts_do_not_capture_smc_provenance(tmp_path, account_type):
    instance = PaperBrokerV2(tmp_path / "other.db", account_type=account_type, execution_engine=account_type,
                             fee_rate=0, spread_bps=0, slippage_bps=0, participation_rate=1)
    try:
        submit(instance)
        candle(instance)
        assert instance._c.execute("SELECT fill_position_json FROM v2_fills").fetchone()[0] is None
        from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
        with pytest.raises(ValueError, match="isolated SMC account"):
            smc_fill_transition_page(instance.path)
    finally:
        instance._c.close()


def test_query_only_export_keeps_legacy_fills_unknown_and_reads_exact_source_ids(broker):
    from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, price=80)
    before = list(broker._c.iterdump())
    page = smc_fill_transition_page(broker.path)
    assert page["atomic_snapshot"] is True and page["full_lifecycle_verified"] is False
    assert [r["capture_state"] for r in page["fills"]] == ["RECORDED_SOURCE_TRANSITION"] * 2
    assert page["fills"][1]["transition"]["before"]["entry_execution_key"] == "decision-1"
    for _ in range(100):
        assert smc_fill_transition_page(broker.path, after=page["next_after"], anchor=page["next_anchor"])["fills"] == []
    assert list(broker._c.iterdump()) == before
    broker._c.execute("UPDATE v2_fills SET fill_position_json=NULL WHERE rowid=1")  # disposable legacy fixture
    broker._c.commit()
    legacy = smc_fill_transition_page(broker.path)["fills"][0]
    assert legacy["transition"] is None and legacy["capture_state"] == "UNVERIFIED_LEGACY_FILL"


def test_partial_fills_scale_reduce_close_and_new_same_symbol_position_are_distinct(broker):
    first = submit(broker, quantity=2)
    candle(broker, volume=.5)
    candle(broker, volume=1.5)
    second = submit(broker, "scale", quantity=1)
    candle(broker)
    submit(broker, "reduce", side="sell", quantity=1, reduce_only=True)
    candle(broker, price=101)
    submit(broker, "close", side="sell", quantity=2, reduce_only=True)
    candle(broker, price=102)
    submit(broker, "new-same-symbol", quantity=1)
    candle(broker, price=103)
    rows = transitions(broker)
    assert [r["effect"] for r in rows] == ["OPEN", "INCREASE", "INCREASE", "REDUCE", "CLOSE", "OPEN"]
    original = rows[0]["after"]["position_id"]
    assert all(r["before"]["position_id"] == original for r in rows[1:5])
    assert rows[2]["order_id"] == second["id"]
    assert rows[2]["after"]["entry_order_id"] == first["id"]  # net origin, NOT lot allocation
    assert rows[5]["after"]["position_id"] != original
    assert rows[5]["after"]["entry_execution_key"] == "new-same-symbol"
    assert len(broker.orders()) == 5 and len(broker.positions()) == 1
    assert broker.account(persist_metrics=False)["realized_pnl"] == 5


def test_reversal_records_both_original_and_new_position_without_merging_keys(broker):
    first = submit(broker)
    candle(broker)
    second = submit(broker, "reverse", side="sell", quantity=2)
    candle(broker, price=110)
    opened, reversed_ = transitions(broker)
    assert reversed_["effect"] == "REVERSE" and reversed_["reduce_only"] is False
    assert reversed_["before"] == opened["after"]
    assert reversed_["before"]["entry_order_id"] == first["id"]
    assert reversed_["after"]["entry_order_id"] == second["id"]
    assert reversed_["after"]["entry_execution_key"] == "reverse"
    assert reversed_["after"]["position_id"] != reversed_["before"]["position_id"]
    assert reversed_["after"]["side"] == "short" and reversed_["after"]["size"] == 1
    assert broker.account(persist_metrics=False)["realized_pnl"] == 10


@pytest.mark.parametrize("side,stop,price", [("buy", 90, 80), ("sell", 110, 120)])
def test_partial_protective_exit_records_remaining_original_position(broker, side, stop, price):
    submit(broker, side=side)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=stop)
    candle(broker, price=price, volume=.4)
    candle(broker, price=price)
    opened, reduced, closed = transitions(broker)
    assert reduced["effect"] == "REDUCE" and reduced["after"]["size"] == pytest.approx(.6)
    assert reduced["before"] == opened["after"] and closed["before"] == reduced["after"]
    assert closed["effect"] == "CLOSE" and closed["after"] is None
    assert broker.positions() == [] and len(broker.fills()) == 3


@pytest.mark.parametrize("boundary", ["format", "after_fill_insert", "order_update"])
def test_provenance_fill_and_order_update_failure_boundaries_rollback_together(broker, monkeypatch, boundary):
    import execution.paper_broker_v2 as module
    order = submit(broker)
    if boundary == "format":
        real = module.encode_transition
        def fault(**fields):
            real(**fields)
            raise RuntimeError("fixture crash while formatting")
        monkeypatch.setattr(module, "encode_transition", fault)
    else:
        clause = "AFTER INSERT ON v2_fills" if boundary == "after_fill_insert" else "BEFORE UPDATE OF filled ON v2_orders"
        broker._c.execute(f"CREATE TRIGGER injected {clause} BEGIN SELECT RAISE(ABORT, 'fixture crash'); END")
    with pytest.raises((RuntimeError, sqlite3.IntegrityError)):
        candle(broker)
    assert len(broker.orders()) == 1 and broker.positions() == [] and broker.fills() == []
    assert broker.order(order["id"])["status"] == "open"
    assert broker.account(persist_metrics=False)["balance"] == 10000
    if boundary == "format":
        monkeypatch.setattr(module, "encode_transition", real)
    else:
        broker._c.execute("DROP TRIGGER injected")
    candle(broker)
    assert len(broker.positions()) == len(transitions(broker)) == 1


def test_failed_exit_keeps_original_position_and_its_entry_provenance(broker):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    original = broker.positions()[0]
    captured = transitions(broker)
    broker._c.execute("CREATE TRIGGER injected BEFORE INSERT ON v2_fills BEGIN SELECT RAISE(ABORT, 'fixture full'); END")
    with pytest.raises(sqlite3.IntegrityError):
        candle(broker, price=80)
    assert broker.positions()[0] == original and transitions(broker) == captured
    broker._c.execute("DROP TRIGGER injected")
    candle(broker, price=80)
    assert broker.positions() == [] and len(transitions(broker)) == 2


def test_duplicate_quote_and_restart_never_add_another_fill_or_provenance(broker):
    submit(broker)
    now = datetime.now(timezone.utc).isoformat()
    quote = {"bid": 100, "ask": 100.1, "mark": 100, "received_at": now,
             "event_timestamp": now, "sequence": 1, "quote_event_id": "quote-1"}
    broker.process_tick("BTCUSDT", quote)
    original = transitions(broker)
    restarted = PaperBrokerV2(broker.path, account_type="SMC_LAB", execution_engine="SMC_LAB")
    try:
        for _ in range(5):
            assert restarted.process_tick("BTCUSDT", quote)["accepted"] is False
        assert transitions(restarted) == original
        assert len(restarted.orders()) == len(restarted.fills()) == len(restarted.positions()) == 1
    finally:
        restarted._c.close()


def test_export_restore_preserves_captured_json_and_legacy_restore_does_not_invent_it(broker):
    submit(broker)
    candle(broker)
    snapshot = broker.export_state()
    captured = snapshot["fills"][0]["fill_position_json"]
    broker.restore_state(snapshot)
    assert broker.fills()[0]["fill_position_json"] == captured
    legacy = deepcopy(snapshot)
    legacy["fills"][0].pop("fill_position_json")
    broker.restore_state(legacy)  # disposable same-account fixture only
    assert broker.fills()[0]["fill_position_json"] is None
    assert len(broker.orders()) == len(broker.positions()) == 1


def test_reading_pre_migration_database_never_installs_schema_or_reconstructs_history(broker):
    from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
    submit(broker)
    candle(broker)
    broker._c.execute("ALTER TABLE v2_fills DROP COLUMN fill_position_json")
    broker._c.commit()
    before = list(broker._c.iterdump())
    page = smc_fill_transition_page(broker.path)
    assert page["fills"][0]["transition"] is None and list(broker._c.iterdump()) == before
    restarted = PaperBrokerV2(broker.path, account_type="SMC_LAB", execution_engine="SMC_LAB")
    try:
        assert restarted.fills()[0]["fill_position_json"] is None
        assert len(restarted.orders()) == len(restarted.positions()) == 1
    finally:
        restarted._c.close()


@pytest.mark.parametrize("missing", [None, ""])
def test_missing_legacy_entry_order_key_remains_missing_not_inferred(broker, missing):
    submit(broker)
    candle(broker)
    broker._c.execute("UPDATE v2_positions SET entry_order_id=?", (missing,))
    broker._c.commit()
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, price=80)
    closed = transitions(broker)[-1]
    assert closed["before"]["position_id"]
    assert closed["before"]["entry_order_id"] is closed["before"]["entry_execution_key"] is None


@pytest.mark.parametrize("field,value", [("account_id", "different-account"), ("symbol", "ETHUSDT"),
                                        ("side", "sell"), ("execution_engine", "PA_LAB")])
def test_foreign_or_mismatched_parent_cannot_supply_original_execution_key(broker, field, value):
    order = submit(broker)
    candle(broker)
    broker._c.execute(f"UPDATE v2_orders SET {field}=? WHERE id=?", (value, order["id"]))
    broker._c.commit()
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, price=80)
    closed = transitions(broker)[-1]
    assert closed["before"]["entry_order_id"] == order["id"]
    assert closed["before"]["entry_execution_key"] is closed["before"]["entry_timeframe"] is None


def test_source_capture_has_no_guardian_database_network_or_runtime_dependency(broker, monkeypatch):
    from tradexa.guardian.store import GuardianStore
    def outage(*args, **kwargs):
        raise AssertionError("Guardian outage reached source fill")
    monkeypatch.setattr(GuardianStore, "__init__", outage)
    submit(broker)
    candle(broker)
    assert len(transitions(broker)) == 1
    root = Path(__file__).resolve().parents[1] / "execution"
    for name in ("paper_broker_v2.py", "paper_fill_provenance.py"):
        tree = ast.parse((root / name).read_text())
        modules = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(x.startswith(("tradexa.guardian", "services.smc", "requests", "urllib", "websocket")) for x in modules)


@pytest.mark.parametrize("path", ["remediation", "liquidation"])
def test_synthetic_close_failures_rollback_and_preserve_original_position(broker, path):
    broker.leverage = 5  # isolated fixture only; no runtime configuration change
    submit(broker)
    candle(broker)
    original = broker.positions()[0]
    prior = transitions(broker)
    broker._c.execute("CREATE TRIGGER injected BEFORE INSERT ON v2_fills BEGIN SELECT RAISE(ABORT, 'fixture full'); END")
    def close():
        if path == "remediation":
            return broker.close_position_at_mark("BTCUSDT", 70, reason="LEGACY_POSITION_REMEDIATION")
        return broker.process_mark("BTCUSDT", 70)
    with pytest.raises(sqlite3.IntegrityError):
        close()
    assert broker._c.in_transaction is False
    assert broker.positions()[0] == original and transitions(broker) == prior
    broker._c.execute("DROP TRIGGER injected")
    close()
    assert broker.positions() == [] and len(transitions(broker)) == 2
    assert transitions(broker)[-1]["before"]["entry_execution_key"] == "decision-1"


def test_read_export_paginates_all_retained_rows_and_late_timestamps(broker):
    from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
    for index in range(70):
        submit(broker, f"decision-{index}", quantity=.01)
        candle(broker)
    after, anchor, captured = 0, "", []
    while True:
        page = smc_fill_transition_page(broker.path, after=after, anchor=anchor)
        captured += page["fills"]
        after, anchor = page["next_after"], page["next_anchor"]
        if not page["has_more"]:
            break
    assert len(captured) == len({r["fill_id"] for r in captured}) == 70
    submit(broker, "late", quantity=.01)
    candle(broker)
    broker._c.execute("UPDATE v2_fills SET timestamp='2001-01-01T00:00:00+00:00' WHERE rowid=71")
    broker._c.commit()
    [late] = smc_fill_transition_page(broker.path, after=after, anchor=anchor)["fills"]
    assert late["source_sequence"] == 71 and late["timestamp"].startswith("2001-")


@pytest.mark.parametrize("change", ["first", "anchor", "delete", "account"])
def test_read_cursor_never_silently_resets_after_source_change(broker, change):
    from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
    for index in range(3):
        submit(broker, f"decision-{index}", quantity=.01)
        candle(broker)
    page = smc_fill_transition_page(broker.path)
    if change == "account":
        broker._c.execute("UPDATE v2_account SET account_id='other-paper-account'")
    elif change == "delete":
        broker._c.execute("DELETE FROM v2_fills WHERE rowid=3")
    else:
        rowid = 1 if change == "first" else 3
        broker._c.execute("UPDATE v2_fills SET timestamp='2001-01-01T00:00:00+00:00' WHERE rowid=?", (rowid,))
    broker._c.commit()
    with pytest.raises(ValueError):
        smc_fill_transition_page(broker.path, after=page["next_after"], anchor=page["next_anchor"])


@pytest.mark.parametrize("damage", ["identity", "effect", "extra", "credential", "number", "flag", "deep", "large", "null", "unicode"])
def test_malformed_or_unsafe_source_evidence_fails_closed_without_repair(broker, damage):
    from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
    submit(broker)
    candle(broker)
    value = transitions(broker)[0]
    if damage == "identity":
        value["fill_id"] = "other-fill"
    elif damage == "effect":
        value["effect"] = "CLOSE"
    elif damage == "extra":
        value["raw_quote"] = {"untrusted": True}
    elif damage == "credential":
        value["after"]["entry_execution_key"] = "Bearer fixture-secret"
    elif damage == "number":
        value["after"]["size"] = 10**1000
    elif damage == "flag":
        value["reduce_only"] = "false"
    elif damage == "unicode":
        value["after"]["entry_execution_key"] = "\ud800"
    raw = ("[" * 1100 + "0" + "]" * 1100 if damage == "deep" else
           "x" * 8193 if damage == "large" else "null" if damage == "null" else json.dumps(value))
    broker._c.execute("UPDATE v2_fills SET fill_position_json=?", (raw,))
    broker._c.commit()
    with pytest.raises(ValueError):
        smc_fill_transition_page(broker.path)
    assert len(broker.orders()) == len(broker.positions()) == len(broker.fills()) == 1
    assert broker.fills()[0]["fill_position_json"] == raw


def test_wal_read_during_write_is_atomic_and_missing_database_is_not_created(broker, tmp_path):
    from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
    submit(broker)
    candle(broker)
    original = smc_fill_transition_page(broker.path)
    with closing(sqlite3.connect(broker.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE v2_fills SET fill_position_json='invalid'")
        assert smc_fill_transition_page(broker.path) == original
        writer.rollback()
    assert smc_fill_transition_page(broker.path) == original
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        smc_fill_transition_page(missing)
    assert not missing.exists()


@pytest.mark.parametrize("after,anchor,limit", [(True, "", 32), (-1, "", 32), (0, "invalid", 32),
    (1, "", 32), (0, "", 0), (0, "", 33), (0, "", True), (2**63, "a" * 64, 32)])
def test_read_rejects_invalid_cursors_before_opening_database(tmp_path, after, anchor, limit):
    from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
    with pytest.raises(ValueError):
        smc_fill_transition_page(tmp_path / "missing.db", after=after, anchor=anchor, limit=limit)


def test_source_api_auth_persistent_lock_retry_and_refresh_preserve_records(broker, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module
    from config import settings
    submit(broker)
    candle(broker)
    before = list(broker._c.iterdump())
    broker._c.close()
    with closing(sqlite3.connect(broker.path)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    observer = "guardian-source-transition-key-123456789"
    monkeypatch.setattr(settings, "guardian_observer_key", observer)
    monkeypatch.setattr(settings, "smc_paper_db", broker.path)
    client, route = TestClient(app_module.app), "/guardian/smc-fill-transitions"
    headers = {"X-Guardian-Observer-Key": observer}
    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-Webhook-Secret": settings.admin_key}).status_code == 401
    assert client.get(route, headers={"X-Guardian-Observer-Key": settings.admin_key}).status_code == 401
    assert client.post(route, headers={"X-Webhook-Secret": settings.admin_key}).status_code == 405
    assert client.get(route + "?after=-1", headers=headers).status_code == 422
    with closing(sqlite3.connect(broker.path)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        for _ in range(2):
            response = client.get(route, headers=headers)
            assert response.status_code == 503
            assert response.json()["detail"] == {"state": "PERSISTENCE_BLOCKED", "code": "SMC_FILL_TRANSITIONS_UNAVAILABLE"}
            assert broker.path not in response.text and observer not in response.text
        writer.rollback()
    for _ in range(100):
        response = client.get(route, headers=headers)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert response.json()["execution_integrity_verified"] is False
    with closing(sqlite3.connect(broker.path)) as db:
        assert list(db.iterdump()) == before
    monkeypatch.setattr(settings, "guardian_observer_key", "")
    assert client.get(route, headers=headers).status_code == 401


@pytest.mark.parametrize("damage", ["malformed", "unicode"])
def test_source_api_malformed_evidence_is_redacted_and_does_not_invoke_broker(broker, monkeypatch, damage):
    from fastapi.testclient import TestClient
    import app as app_module
    from config import settings
    submit(broker)
    candle(broker)
    value = transitions(broker)[0]
    value["after"]["entry_execution_key"] = "\ud800"
    raw = "private fixture path/key" if damage == "malformed" else json.dumps(value)
    broker._c.execute("UPDATE v2_fills SET fill_position_json=?", (raw,))
    broker._c.commit()
    def prohibited(*args, **kwargs):
        raise AssertionError("query-only source constructed a broker")
    monkeypatch.setattr(PaperBrokerV2, "__init__", prohibited)
    monkeypatch.setattr(settings, "guardian_observer_key", "guardian-source-transition-key-123456789")
    monkeypatch.setattr(settings, "smc_paper_db", broker.path)
    response = TestClient(app_module.app).get("/guardian/smc-fill-transitions", headers={
        "X-Guardian-Observer-Key": settings.guardian_observer_key})
    assert response.status_code == 503 and "private fixture" not in response.text
