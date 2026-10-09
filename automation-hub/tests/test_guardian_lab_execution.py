"""Real isolated broker fixtures; Guardian cannot mutate trading truth."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as app_module
from config import settings
from services.price_action_lab import PriceActionPaperAccount
from services.smc_strategy_lab import SMCPaperAccount
from services.guardian_lab_execution_read_model import lab_paper_execution_snapshot
from tradexa.guardian.lab_execution_integrity import reconcile_lab_paper
from tradexa.guardian.lab_execution_observer import GuardianLabExecutionObserver, lab_execution_view
from tradexa.guardian.incidents import GuardianIncidentEngine
from tradexa.guardian.store import GuardianStore

KEY = "guardian-isolated-paper-observer-12345"
URL = "http://app:8000/guardian/lab-execution"


@pytest.fixture(params=["PRICE_ACTION", "SMC"])
def source(tmp_path, request):
    lab = request.param
    path = tmp_path / (lab + ".db")
    account = (PriceActionPaperAccount if lab == "PRICE_ACTION" else SMCPaperAccount)(path)
    yield lab, path, account
    account._db.close()
    account.broker._c.close()
    if lab == "PRICE_ACTION":
        account.journal._db.close()


def submit(source, key="decision-1", **changes):
    lab, path, account = source
    args = dict(symbol="BTCUSDT", side="buy", order_type="limit", quantity=.01,
                limit_price=100, protection_stop_loss=90, protection_take_profit=120,
                strategy="fixture", strategy_version="1", timeframe="5m", candle_id=key)
    args.update(changes)
    return account.broker.submit(**args)


def fill(source, volume=100, price=100):
    return source[2].broker.process_candle("BTCUSDT", {
        "open": price, "high": price + 1, "low": price - 1, "close": price,
        "volume": volume, "timestamp": "2030-01-01T00:05:00+00:00"})


def snapshot(source):
    return lab_paper_execution_snapshot(source[1], source[0])


def view(source):
    return reconcile_lab_paper(snapshot(source))


def codes(source):
    return {row["code"] for row in view(source)["findings"]}


def envelope(source):
    return {"schema_version": 1, "scope": "ISOLATED_LAB_PAPER_BROKER",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "execution_integrity_verified": False, "snapshot": snapshot(source)}


def test_resting_partial_filled_reduce_and_protective_close_are_distinct(source):
    order = submit(source)
    assert view(source)["open_orders"] == 1
    assert view(source)["open_positions"] == 0
    fill(source, .2)
    result = view(source)
    assert result["open_positions"] == result["open_orders"] == 1
    assert result["orders"][0]["status"] == "partially_filled"
    assert result["orders"][0]["fill_quantity_matches"]
    fill(source)
    result = view(source)
    assert result["open_orders"] == 0
    assert result["orders"][0]["status"] == "filled"
    assert result["positions"][0]["entry_order_id"] == order["id"]
    assert result["positions"][0]["origin_order_matches"]
    assert result["positions"][0]["entry_to_stop_amount"] > 0
    submit(source, key="reduce-1", side="sell", order_type="market", reduce_only=True,
           quantity=.004, protection_stop_loss=None, protection_take_profit=None)
    fill(source)
    assert view(source)["positions"][0]["size"] == pytest.approx(.006)
    assert "EXIT_NOT_REDUCE_ONLY" not in codes(source)
    fill(source, price=80)
    result = view(source)
    assert result["open_positions"] == 0
    assert result["unlinked_sampled_fill_count"] == 1
    assert result["exit_link_state"] == "UNVERIFIED"
    assert result["journal_trade_verified"] is result["live_exposure_verified"] is False
    assert result["global_risk_amount"] is None
    assert "FILLED_QUANTITY_MISMATCH" not in codes(source)


def test_cancelled_unfilled_order_does_not_claim_position_or_execution_failure(source):
    order = submit(source)
    source[2].broker.cancel(order["id"])
    result = view(source)
    assert result["open_orders"] == result["open_positions"] == 0
    assert result["orders"][0]["status"] == "cancelled"
    assert result["orders"][0]["filled"] == 0
    assert "FILLED_QUANTITY_MISMATCH" not in codes(source)


def test_atomic_broker_fill_mismatch_is_reported_but_never_repaired(source, tmp_path):
    order = submit(source)
    fill(source)
    with source[2].broker._c:
        source[2].broker._c.execute("UPDATE v2_orders SET filled=.02,remaining=0 WHERE id=?", (order["id"],))
    store = GuardianStore(tmp_path / "guardian.db")
    collector = GuardianLabExecutionObserver(store, URL, KEY, source[0], fetch=lambda: envelope(source))
    assert collector.poll() == 1
    before = (len(source[2].broker.orders()), source[2].broker.positions(), source[2].broker.orders())
    for _ in range(100):
        assert collector.poll() == 0
        lab_execution_view(store)
    assert store.count() == 1
    assert before == (len(source[2].broker.orders()), source[2].broker.positions(), source[2].broker.orders())
    assert {"FILLED_QUANTITY_MISMATCH", "ORDER_QUANTITY_MISMATCH"} <= codes(source)
    assert view(source)["positions"][0]["entry_to_stop_amount"] is None
    engine = GuardianIncidentEngine(store)
    engine.scan()
    [incident] = engine.list()
    assert incident["confidence"] == "CONFIRMED"
    assert incident["state"] == "OPEN"
    assert engine.scan() == 0


def test_trailing_stop_above_entry_is_not_an_invalid_geometry(source):
    submit(source)
    fill(source)
    source[2].broker.set_protection("BTCUSDT", stop_loss=105)
    result = view(source)
    assert result["positions"][0]["entry_to_stop_amount"] == 0
    assert not any("GEOMETRY" in row["code"] for row in result["findings"])
    assert result["protection_execution_verified"] is False


def test_fill_price_mismatch_masks_amount_without_changing_order(source):
    submit(source)
    fill(source)
    with source[2].broker._c:
        source[2].broker._c.execute("UPDATE v2_orders SET average_price=999")
    assert "FILLED_PRICE_MISMATCH" in codes(source)
    assert view(source)["positions"][0]["entry_to_stop_amount"] is None
    assert source[2].broker.orders()[0]["average_price"] == 999


def test_missing_stop_masks_amount_and_opens_possible_incident(source, tmp_path):
    submit(source)
    fill(source)
    with source[2].broker._c:
        source[2].broker._c.execute("UPDATE v2_positions SET stop_loss=NULL")
    assert view(source)["positions"][0]["entry_to_stop_amount"] is None
    store = GuardianStore(tmp_path / "guardian.db")
    GuardianLabExecutionObserver(store, URL, KEY, source[0], fetch=lambda: envelope(source)).poll()
    engine = GuardianIncidentEngine(store)
    engine.scan()
    [incident] = engine.list()
    assert incident["confidence"] == "POSSIBLE"


def test_missing_origin_and_unsafe_exit_are_truthful_record_findings(source):
    submit(source)
    fill(source)
    with source[2].broker._c:
        source[2].broker._c.execute("UPDATE v2_positions SET entry_order_id='unproven-origin'")
        source[2].broker._c.execute("UPDATE v2_orders SET action_class='CLOSE',reduce_only=0")
    assert {"POSITION_ORIGIN_UNVERIFIED", "EXIT_NOT_REDUCE_ONLY"} <= codes(source)
    assert view(source)["positions"][0]["entry_to_stop_amount"] is None


def test_bounded_history_incompleteness_is_not_a_fill_mismatch(source):
    submit(source)
    fill(source)
    data = snapshot(source)
    data["fills"] = []
    data["fill_window_complete"] = False
    result = reconcile_lab_paper(data)
    assert "FILL_HISTORY_INCOMPLETE" in {row["code"] for row in result["findings"]}
    assert "FILLED_QUANTITY_MISMATCH" not in {row["code"] for row in result["findings"]}


def test_read_during_wal_write_uses_committed_snapshot(source):
    submit(source)
    fill(source)
    # Runtime SQLite is WAL; the SMC metadata connection shares that file.
    with sqlite3.connect(source[1]) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE v2_positions SET size=20")
        assert view(source)["positions"][0]["size"] == pytest.approx(.01)
        writer.rollback()


def test_restart_deduplicates_and_outage_invalidates_risk_without_losing_evidence(source, tmp_path):
    submit(source)
    fill(source)
    path = tmp_path / "guardian.db"
    store = GuardianStore(path)
    observer = GuardianLabExecutionObserver(store, URL, KEY, source[0], fetch=lambda: envelope(source))
    observer.poll()
    restarted = GuardianLabExecutionObserver(GuardianStore(path), URL, KEY, source[0], fetch=lambda: envelope(source))
    assert restarted.poll() == 0
    def fail():
        raise sqlite3.OperationalError("fixture outage")
    restarted.fetch = fail
    with pytest.raises(sqlite3.OperationalError):
        restarted.poll()
    row = next(row for row in lab_execution_view(store)["labs"] if row["lab"] == source[0])
    assert row["observation_state"] == "UNKNOWN"
    assert row["positions"][0]["entry_to_stop_amount"] is None
    assert len(source[2].broker.positions()) == len(source[2].broker.orders()) == 1
    restarted.fetch = lambda: envelope(source)
    assert restarted.poll() == 0
    row = next(row for row in lab_execution_view(store)["labs"] if row["lab"] == source[0])
    assert row["observation_state"] == "CURRENT"
    assert row["positions"][0]["entry_to_stop_amount"] > 0
    assert store.count() == 1
    future = datetime.now(timezone.utc) + timedelta(seconds=100)
    row = next(row for row in lab_execution_view(store, now=future)["labs"] if row["lab"] == source[0])
    assert row["positions"][0]["entry_to_stop_amount"] is None


def test_source_key_scope_unavailable_and_retry(source, monkeypatch):
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    setting = "price_action_paper_db" if source[0] == "PRICE_ACTION" else "smc_paper_db"
    monkeypatch.setattr(settings, setting, str(source[1]))
    client = TestClient(app_module.app)
    route = "/guardian/lab-execution?lab=" + source[0]
    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-Webhook-Secret": KEY}).status_code == 401
    headers = {"X-Guardian-Observer-Key": KEY}
    assert client.post(route, headers=headers).status_code != 200
    assert client.get(route, headers=headers).status_code == 200
    monkeypatch.setattr(settings, setting, str(source[1]) + ".absent")
    response = client.get(route, headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"]["state"] == "PERSISTENCE_BLOCKED"
    assert str(source[1]) not in response.text
    monkeypatch.setattr(settings, setting, str(source[1]))
    assert client.get(route, headers=headers).status_code == 200


def test_same_symbol_other_lab_is_rejected_and_data_unchanged(source, tmp_path):
    submit(source)
    data = envelope(source)
    other = "SMC" if source[0] == "PRICE_ACTION" else "PRICE_ACTION"
    observer = GuardianLabExecutionObserver(GuardianStore(tmp_path / "guardian.db"), URL, KEY, other, fetch=lambda: data)
    with pytest.raises(ValueError, match="identity mismatch"):
        observer.poll()
    assert observer.store.count() == 0
    altered = deepcopy(data["snapshot"])
    altered["orders"][0]["account_id"] = "other-account"
    assert "ORDER_IDENTITY_MISMATCH" in {r["code"] for r in reconcile_lab_paper(altered)["findings"]}


def test_session_and_setup_journal_links_do_not_imply_trade_finalization(source):
    order = submit(source)
    lab, path, account = source
    prefix = "pa" if lab == "PRICE_ACTION" else "smc"
    session = account._db.execute(f"SELECT id FROM {prefix}_sessions LIMIT 1").fetchone()[0]
    if lab == "PRICE_ACTION":
        account._db.execute("INSERT INTO pa_order_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (order["id"], session, "proposal", "setup", "zone", "long", "fixture", "{}",
                             "ORDER_PENDING", "fixture", "2030-01-01", "2030-01-01", 5))
        account._db.execute("INSERT INTO pa_journal_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("journal-1", session, None, "setup", "fixture", "1", "config", "engine", "dataset",
                             "BTCUSDT", "5m", "long", "paper", "fixture", "WATCHING", "pending",
                             "2030-01-01", None, "{}", "2030-01-01"))
    else:
        account._db.execute("INSERT INTO smc_order_meta(order_id,session_id,ownership,idempotency_key,setup_id,direction,status,reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (order["id"], session, "strategy", "decision-1", "setup", "long", "ORDER_PENDING", "fixture", "2030-01-01", "2030-01-01"))
    account._db.commit()
    result = view(source)
    assert result["orders"][0]["session_id"] == session
    assert "ORDER_SESSION_LINK_UNVERIFIED" not in codes(source)
    if lab == "PRICE_ACTION":
        assert result["orders"][0]["setup_journal_id"] == "journal-1"
        assert "PA_SETUP_JOURNAL_UNVERIFIED" not in codes(source)
    assert result["journal_trade_verified"] is False
    assert result["open_positions"] == 0


def test_source_read_only_connection_and_missing_file_never_create_state(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        lab_paper_execution_snapshot(path, "SMC")
    assert not path.exists()


def test_too_many_open_orders_fails_closed_instead_of_omitting_exposure(source):
    order = submit(source)
    conn = source[2].broker._c
    columns = [row[1] for row in conn.execute("PRAGMA table_info(v2_orders)")]
    template = dict(conn.execute("SELECT * FROM v2_orders WHERE id=?", (order["id"],)).fetchone())
    with conn:
        for index in range(32):
            row = {**template, "id": f"overflow-{index}", "order_key": None}
            conn.execute(f"INSERT INTO v2_orders ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                         [row[name] for name in columns])
    with pytest.raises(ValueError, match="bound"):
        snapshot(source)
    assert len(source[2].broker.orders()) == 33


def test_old_position_origin_survives_recent_order_window(source):
    original = submit(source)
    fill(source)
    for i in range(10):
        recent = submit(source, key=f"later-{i}", symbol="ETHUSDT")
        source[2].broker.cancel(recent["id"])
    data = snapshot(source)
    assert original["id"] in {row["id"] for row in data["orders"]}
    assert view(source)["positions"][0]["origin_order_matches"]


def test_history_window_and_query_deadline_are_bounded(source, monkeypatch):
    submit(source)
    fill(source)
    conn = source[2].broker._c
    columns = [row[1] for row in conn.execute("PRAGMA table_info(v2_fills)")]
    template = dict(conn.execute("SELECT * FROM v2_fills LIMIT 1").fetchone())
    with conn:
        for i in range(130):
            row = {**template, "id": f"history-{i}", "fill_key": None}
            conn.execute(f"INSERT INTO v2_fills ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                         [row[name] for name in columns])
    data = snapshot(source)
    assert len(data["fills"]) == 128
    assert data["fill_window_complete"] is False
    import services.guardian_lab_execution_read_model as read_model
    moments = iter([0., 10.])
    monkeypatch.setattr(read_model, "monotonic", lambda: next(moments, 10.))
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        snapshot(source)


def test_failed_guardian_checkpoint_rolls_back_event_and_recovers(source, tmp_path):
    submit(source)
    store = GuardianStore(tmp_path / "guardian.db")
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON observer_snapshot_state BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    observer = GuardianLabExecutionObserver(store, URL, KEY, source[0], fetch=lambda: envelope(source))
    with pytest.raises(sqlite3.IntegrityError):
        observer.poll()
    assert store.count() == 0
    assert store.observed_snapshot(observer.component) is None
    assert store.heartbeats()[observer.probe]["state"] == "FAILED"
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER fail_checkpoint")
    assert observer.poll() == 1
    assert observer.poll() == 0
    assert len(source[2].broker.orders()) == 1


def test_persistent_source_lock_is_structured_and_retry_is_read_only(source, monkeypatch):
    submit(source)
    account = source[2]
    account._db.close()
    account.broker._c.close()
    if source[0] == "PRICE_ACTION":
        account.journal._db.close()
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    setting = "price_action_paper_db" if source[0] == "PRICE_ACTION" else "smc_paper_db"
    monkeypatch.setattr(settings, setting, str(source[1]))
    client = TestClient(app_module.app)
    route = "/guardian/lab-execution?lab=" + source[0]
    headers = {"X-Guardian-Observer-Key": KEY}
    with sqlite3.connect(source[1]) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        for _ in range(2):
            response = client.get(route, headers=headers)
            assert response.status_code == 503
            assert response.json()["detail"]["state"] == "PERSISTENCE_BLOCKED"
        writer.rollback()
        response = client.get(route, headers=headers)
        assert response.status_code == 200
        assert len(response.json()["snapshot"]["orders"]) == 1
        assert writer.execute("SELECT COUNT(*) FROM v2_orders").fetchone()[0] == 1


@pytest.mark.parametrize("url", ["https://example.com/guardian/lab-execution", "http://app:8000/guardian/lab-execution?lab=SMC", "http://app:8000/api/v1/start"])
def test_observer_cannot_target_external_or_control_endpoints(tmp_path, url):
    with pytest.raises(ValueError, match="internal"):
        GuardianLabExecutionObserver(GuardianStore(tmp_path / "guardian.db"), url, KEY, "SMC")


def test_observer_redirect_cannot_forward_credential():
    from urllib.request import Request
    from tradexa.guardian.lab_execution_observer import _NoRedirect
    request = Request(URL, headers={"X-Guardian-Observer-Key": KEY})
    for target in ("https://example.com/collect", "http://app:8000/api/v1/start"):
        assert _NoRedirect().redirect_request(request, None, 302, "Found", {}, target) is None


@pytest.mark.parametrize("change", ["stale", "future", "incomplete", "nan", "duplicate", "foreign-fill"])
def test_invalid_contract_never_becomes_current(source, tmp_path, change):
    submit(source)
    fill(source)
    page = envelope(source)
    if change in {"stale", "future"}:
        page["observed_at"] = (datetime.now(timezone.utc) + timedelta(seconds=-100 if change == "stale" else 100)).isoformat()
    elif change == "incomplete":
        page["snapshot"]["open_coverage_complete"] = False
    elif change == "nan":
        page["snapshot"]["positions"][0]["size"] = float("nan")
    elif change == "duplicate":
        page["snapshot"]["orders"] *= 2
    else:
        page["snapshot"]["fills"][0]["account_id"] = "foreign"
    store = GuardianStore(tmp_path / "guardian.db")
    with pytest.raises(ValueError):
        GuardianLabExecutionObserver(store, URL, KEY, source[0], fetch=lambda: page).poll()
    assert store.count() == 0
    assert all(row["observation_state"] == "UNKNOWN" for row in lab_execution_view(store)["labs"])
