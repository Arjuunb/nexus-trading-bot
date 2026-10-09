"""Real isolated source fills -> bounded read export -> independent Guardian."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

import pytest

from execution.paper_broker_v2 import PaperBrokerV2

KEY = "independent-exit-observer-key-123456789"
URL = "http://app:8000/guardian/smc-exit-fills"


@pytest.fixture
def sources(tmp_path):
    from tradexa.guardian.store import GuardianStore
    broker = PaperBrokerV2(tmp_path / "smc.db", account_type="SMC_LAB", execution_engine="SMC_LAB",
                           leverage=5, fee_rate=0, spread_bps=0, slippage_bps=0, participation_rate=1)
    store = GuardianStore(tmp_path / "guardian.db")
    yield broker, store
    broker._c.close()


def candle(broker, price=100, **changes):
    values = dict(open=price, high=price+1, low=price-1, close=price, volume=100)
    values.update(changes)
    return broker.process_candle("BTCUSDT", values)


def entry(broker, key="decision-1", **changes):
    values = dict(symbol="BTCUSDT", side="buy", order_type="market", quantity=1,
                  timeframe="5m", candle_id=key)
    values.update(changes)
    order = broker.submit(**values)
    candle(broker)
    return order


def page(broker, **args):
    from services.guardian_smc_exit_fill_read_model import smc_exit_fill_page
    return smc_exit_fill_page(broker.path, **args)


def envelope(value):
    from tradexa.guardian.smc_exit_fills import SCOPE
    return dict(schema_version=1, scope=SCOPE, observed_at=datetime.now(timezone.utc).isoformat(),
                execution_integrity_verified=False, page=value)


def collector(sources, store=None, fetch=None):
    from tradexa.guardian.smc_exit_fills import GuardianSMCExitFills
    broker, original = sources
    return GuardianSMCExitFills(store or original, URL, KEY,
        fetch=fetch or (lambda after, anchor: envelope(page(broker, after=after, anchor=anchor))))


def test_source_snapshot_and_retained_import_preserve_exact_exit_identity(sources):
    from tradexa.guardian.smc_exit_fills import smc_exit_fills_view
    broker, store = sources
    order = entry(broker)
    position = broker.positions()[0]
    broker.set_protection("BTCUSDT", stop_loss=90, take_profit=120)
    candle(broker, 80)
    before = list(broker._c.iterdump())
    projected = page(broker)
    assert projected["atomic_snapshot"] is True and projected["full_lifecycle_verified"] is False
    first, exit_ = projected["fills"]
    assert first["exit_evidence"] is None and first["capture_state"] == "UNVERIFIED_NO_EXIT_CAPTURE"
    evidence = exit_["exit_evidence"]
    assert exit_["capture_state"] == "RECORDED_SOURCE_EXIT"
    assert evidence["position"]["entry_execution_key"] == "decision-1"
    assert evidence["position"]["position_id"] == position["position_id"]
    assert evidence["position"]["entry_order_id"] == order["id"]
    assert evidence["trigger_kind"] == "POSITION_STOP_LOSS" and evidence["price"] == 80
    assert collector(sources).poll() == 2
    view = smc_exit_fills_view(store)
    assert view["history_state"] == "CAUGHT_UP_AT_LAST_POLL"
    assert view["events"][1]["evidence"]["fill"] == exit_
    assert view["execution_integrity_verified"] is view["journal_close_verified"] is False
    assert view["position_lifecycle_verified"] is view["current_protection_verified"] is False
    assert list(broker._c.iterdump()) == before


@pytest.mark.parametrize("boundary", ["event", "cursor", "after_commit"])
def test_failed_import_is_atomic_or_keeps_committed_progress(sources, boundary):
    from tradexa.guardian.smc_exit_fills import COMPONENT, smc_exit_fills_view
    from tradexa.guardian.store import GuardianStore
    broker, store = sources
    entry(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    with closing(sqlite3.connect(store.path)) as db:
        if boundary == "event":
            db.execute("CREATE TRIGGER injected AFTER INSERT ON events BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        elif boundary == "cursor":
            db.execute("CREATE TRIGGER injected BEFORE INSERT ON observer_cursors BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        else:
            db.execute("CREATE TRIGGER injected BEFORE INSERT ON heartbeats WHEN NEW.state!='FAILED' BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):
        collector(sources).poll()
    expected = 2 if boundary == "after_commit" else 0
    assert store.count() == expected and store.observer_cursor(COMPONENT)[0] == expected
    assert smc_exit_fills_view(store)["history_state"] == "UNKNOWN"
    assert len(broker.orders()) == 1 and not broker.positions() and len(broker.fills()) == 2
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TRIGGER injected")
    restarted = GuardianStore(store.path)
    assert collector(sources, restarted).poll() == 2-expected
    assert restarted.count() == 2


def test_restart_and_100_refreshes_do_not_duplicate_or_rewrite_source_history(sources):
    from tradexa.guardian.smc_exit_fills import COMPONENT, smc_exit_fills_view
    from tradexa.guardian.store import GuardianStore
    broker, store = sources
    entry(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    original = list(broker._c.iterdump())
    assert collector(sources).poll() == 2
    cursor = store.observer_cursor(COMPONENT)
    retained = smc_exit_fills_view(store)["events"]
    restarted = GuardianStore(store.path)
    for _ in range(100):
        assert collector(sources, restarted).poll() == 0
        assert smc_exit_fills_view(restarted)["events"] == retained
    assert restarted.count() == 2 and restarted.observer_cursor(COMPONENT) == cursor
    assert list(broker._c.iterdump()) == original


def test_32_row_paging_late_timestamps_and_restart(sources):
    from tradexa.guardian.smc_exit_fills import smc_exit_fills_view
    from tradexa.guardian.store import GuardianStore
    broker, store = sources
    for n in range(35):
        entry(broker, f"decision-{n}")
        broker.set_protection("BTCUSDT", stop_loss=90)
        candle(broker, 80)
    assert collector(sources).poll() == 32
    assert smc_exit_fills_view(store)["history_state"] == "IMPORTING"
    assert collector(sources, GuardianStore(store.path)).poll() == 32
    assert collector(sources).poll() == 6
    entry(broker, "late")
    broker._c.execute("UPDATE v2_fills SET timestamp='2000-01-01T00:00:00Z' WHERE rowid=71")
    broker._c.commit()
    assert collector(sources).poll() == 1
    after, rows = 0, []
    while True:
        view = smc_exit_fills_view(store, after=after)
        rows.extend(view["events"])
        after = view["next_after"]
        if not view["has_more"]:
            break
    assert len(rows) == len({r["event_id"] for r in rows}) == 71
    assert rows[-1]["timestamp"].startswith("2000-")


@pytest.mark.parametrize("legacy", ["null", "pre_column"])
def test_legacy_evidence_is_not_reconstructed_and_reads_do_not_migrate(sources, legacy):
    broker, store = sources
    entry(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    if legacy == "null":
        broker._c.execute("UPDATE v2_fills SET fill_exit_json=NULL")
    else:
        broker._c.execute("ALTER TABLE v2_fills DROP COLUMN fill_exit_json")
    broker._c.commit()
    before = list(broker._c.iterdump())
    assert all(r["exit_evidence"] is None and r["capture_state"] == "UNVERIFIED_NO_EXIT_CAPTURE" for r in page(broker)["fills"])
    assert collector(sources).poll() == 2
    assert list(broker._c.iterdump()) == before


@pytest.mark.parametrize("side,price,kind", [("buy", 80, "POSITION_STOP_LOSS"), ("sell", 120, "POSITION_STOP_LOSS"), ("buy", 130, "POSITION_TAKE_PROFIT"), ("sell", 70, "POSITION_TAKE_PROFIT")])
def test_actual_trigger_contract_agrees_between_source_and_standalone_guardian(sources, side, price, kind):
    from tradexa.guardian.smc_exit_fills import project_exit_fill, cursor_anchor
    broker, _ = sources
    entry(broker, side=side)
    broker.set_protection("BTCUSDT", stop_loss=90 if side == "buy" else 110,
                          take_profit=120 if side == "buy" else 80)
    candle(broker, price)
    value = page(broker)
    assert value["fills"][-1]["exit_evidence"]["trigger_kind"] == kind
    assert [project_exit_fill(r, value["account_id"]) for r in value["fills"]] == value["fills"]
    assert cursor_anchor(value["account_id"], value["first_fill"], value["fills"][-1]) == value["next_anchor"]


@pytest.mark.parametrize("damage", ["fill_id", "account", "position", "reduce_flag", "effect", "missing_transition", "duplicate_json", "oversized", "secret"])
def test_source_rejects_contradictory_or_bad_evidence_without_importing_or_writing(sources, damage):
    from tradexa.guardian.smc_exit_fills import COMPONENT, smc_exit_fills_view
    broker, store = sources
    entry(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    row = broker._c.execute("SELECT * FROM v2_fills WHERE rowid=2").fetchone()
    value, transition = json.loads(row["fill_exit_json"]), json.loads(row["fill_position_json"])
    if damage == "fill_id": value["fill_id"] = "another"
    elif damage == "account": value["account_id"] = "another"
    elif damage == "position": value["position"]["position_id"] = "another"
    elif damage == "reduce_flag": value["reduce_only"] = False
    elif damage == "effect": transition["effect"] = "OPEN"
    elif damage == "secret": value["symbol"] = "Bearer private-key"
    raw = json.dumps(value)
    if damage == "duplicate_json": raw = '{"fill_id":"another",' + raw[1:]
    elif damage == "oversized": raw = "x"*8193
    broker._c.execute("UPDATE v2_fills SET fill_exit_json=?,fill_position_json=? WHERE rowid=2",
                      (raw, None if damage == "missing_transition" else json.dumps(transition)))
    broker._c.commit()
    before = list(broker._c.iterdump())
    with pytest.raises(ValueError): collector(sources).poll()
    assert store.count() == 0 and store.observer_cursor(COMPONENT) == (0, "")
    assert smc_exit_fills_view(store)["history_state"] == "UNKNOWN"
    assert list(broker._c.iterdump()) == before


@pytest.mark.parametrize("change", ["account", "first", "previous", "deleted"])
def test_material_cursor_change_stops_import_without_reset(sources, change):
    from tradexa.guardian.smc_exit_fills import COMPONENT
    broker, store = sources
    entry(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    assert collector(sources).poll() == 2
    expected = store.observer_cursor(COMPONENT)
    if change == "account": broker._c.execute("UPDATE v2_account SET account_id='other'")
    elif change == "deleted": broker._c.execute("DELETE FROM v2_fills WHERE rowid=2")
    else: broker._c.execute("UPDATE v2_fills SET timestamp='2000-01-01T00:00:00Z' WHERE rowid=?", (1 if change == "first" else 2,))
    broker._c.commit()
    with pytest.raises(ValueError): collector(sources).poll()
    assert store.count() == 2 and store.observer_cursor(COMPONENT) == expected


def test_source_wal_reads_remain_available_and_missing_file_is_not_created(sources):
    from services.guardian_smc_exit_fill_read_model import smc_exit_fill_page
    broker, _ = sources
    entry(broker)
    expected = page(broker)
    with closing(sqlite3.connect(broker.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE v2_account SET balance=0")
        assert page(broker) == expected
        writer.rollback()
    missing = Path(broker.path).with_name("missing.db")
    with pytest.raises(sqlite3.OperationalError): smc_exit_fill_page(missing)
    assert not missing.exists()


def test_source_api_auth_lock_retry_sanitization_and_no_mutation(sources, monkeypatch):
    from fastapi.testclient import TestClient
    from config import settings
    import app as source_app
    broker, _ = sources
    entry(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, 80)
    before = list(broker._c.iterdump())
    broker._c.close()
    with closing(sqlite3.connect(broker.path)) as db: db.execute("PRAGMA journal_mode=DELETE")
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "smc_paper_db", broker.path)
    client, route = TestClient(source_app.app), "/guardian/smc-exit-fills"
    headers = {"X-Guardian-Observer-Key": KEY}
    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-Webhook-Secret": settings.admin_key}).status_code == 401
    assert client.get(route, headers={"X-Guardian-Observer-Key": settings.admin_key}).status_code == 401
    assert client.post(route, headers={"X-Webhook-Secret": settings.admin_key}).status_code == 405
    assert client.get(route+"?after=-1", headers=headers).status_code == 422
    for query in ("after=1&after=2", "anchor=a&anchor=b", "limit=999", "lab=PRICE_ACTION"):
        assert client.get(route+"?"+query, headers=headers).status_code == 422
    with closing(sqlite3.connect(broker.path)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        for _ in range(2):
            r = client.get(route, headers=headers)
            assert r.status_code == 503 and r.json()["detail"]["state"] == "PERSISTENCE_BLOCKED"
            assert KEY not in r.text and broker.path not in r.text
        writer.rollback()
    for _ in range(100):
        r = client.get(route, headers=headers)
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
        assert r.json()["execution_integrity_verified"] is False
    with closing(sqlite3.connect(broker.path)) as db: assert list(db.iterdump()) == before
    monkeypatch.setattr(settings, "guardian_observer_key", "")
    assert client.get(route, headers=headers).status_code == 401


@pytest.mark.parametrize("kind", ["POSITION_TRAILING_STOP", "ORDER_TRAILING_STOP", "ORDER_REDUCE_ONLY",
    "NETTING_FILL", "PAPER_LIQUIDATION", "LEGACY_POSITION_REMEDIATION", "TICK_STOP", "PARTIAL_STOP"])
def test_all_actual_exit_branches_import_without_changing_source_or_origin(sources, kind):
    from execution.paper_exit_provenance import decode_exit_fill as source_decode
    from tradexa.guardian.smc_exit_fills import decode_exit_fill, smc_exit_fills_view
    broker, store = sources
    original_order = entry(broker)
    original_position = broker.positions()[0]
    if kind == "POSITION_TRAILING_STOP":
        broker.set_protection("BTCUSDT", stop_loss=90, trailing_offset=5)
        candle(broker, high=111, low=100)
    elif kind in ("ORDER_TRAILING_STOP", "ORDER_REDUCE_ONLY", "NETTING_FILL"):
        options = dict(symbol="BTCUSDT", side="sell", quantity=2 if kind == "NETTING_FILL" else .5,
            order_type="trailing_stop" if kind == "ORDER_TRAILING_STOP" else "market",
            timeframe="5m", candle_id="exit-decision", reduce_only=kind != "NETTING_FILL")
        if kind == "ORDER_TRAILING_STOP": options["trailing_offset"] = 5
        broker.submit(**options)
        candle(broker, high=110, low=100)
    elif kind == "PAPER_LIQUIDATION": broker.process_mark("BTCUSDT", .01)
    elif kind == "LEGACY_POSITION_REMEDIATION": broker.close_position_at_mark("BTCUSDT", 100, reason=kind)
    else:
        broker.set_protection("BTCUSDT", stop_loss=90)
        if kind == "TICK_STOP":
            stamp = datetime.now(timezone.utc).isoformat()
            broker.process_tick("BTCUSDT", dict(bid=80, ask=80.1, mark=80, received_at=stamp,
                event_timestamp=stamp, sequence=1, quote_event_id="exit-tick"))
        else:
            candle(broker, 80, volume=.4)
            candle(broker, 80)
    before = list(broker._c.iterdump())
    rows = page(broker)["fills"]
    assert collector(sources).poll() == len(rows)
    view = smc_exit_fills_view(store)
    assert [e["evidence"]["fill"] for e in view["events"]] == rows
    for value in rows[1:]:
        evidence = value["exit_evidence"]
        raw = json.dumps(evidence)
        assert decode_exit_fill(raw) == source_decode(raw) == evidence
        expected_kind = "POSITION_STOP_LOSS" if kind in ("TICK_STOP", "PARTIAL_STOP") else kind
        assert evidence["trigger_kind"] == expected_kind
        assert evidence["position"]["entry_execution_key"] == "decision-1"
        assert evidence["position"]["position_id"] == original_position["position_id"]
        assert evidence["position"]["entry_order_id"] == original_order["id"]
    if kind == "NETTING_FILL":
        assert broker.positions()[0]["position_id"] != original_position["position_id"]
        assert rows[-1]["exit_evidence"]["closed_quantity"] == 1
    if kind == "PARTIAL_STOP": assert [r["quantity"] for r in rows[1:]] == pytest.approx([.4, .6])
    assert list(broker._c.iterdump()) == before


@pytest.mark.parametrize("field", ["account_type", "account_id", "execution_engine"])
def test_non_smc_or_foreign_source_cannot_be_exported(sources, field):
    broker, store = sources
    entry(broker)
    if field == "account_type": broker._c.execute("UPDATE v2_account SET account_type='PA_LAB'")
    else: broker._c.execute(f"UPDATE v2_fills SET {field}='OTHER'")
    broker._c.commit()
    with pytest.raises(ValueError): collector(sources).poll()
    assert store.count() == 0


def test_source_reader_never_constructs_broker_or_journal_and_queries_are_bounded(sources, monkeypatch):
    from services.guardian_smc_exit_fill_read_model import smc_exit_fill_page
    from services.smc_agent_journal import SMCAgentJournal
    broker, _ = sources
    entry(broker)
    def forbidden(*args, **kwargs): raise AssertionError("read opened a writer")
    monkeypatch.setattr(PaperBrokerV2, "__init__", forbidden)
    monkeypatch.setattr(SMCAgentJournal, "__init__", forbidden)
    assert len(page(broker)["fills"]) == 1
    for args in ({"limit": 33}, {"limit": True}, {"limit": 0}, {"after": True},
                 {"after": -1}, {"after": 2**63}, {"after": 1}, {"anchor": "a"*64}):
        with pytest.raises(ValueError): smc_exit_fill_page(broker.path, **args)
