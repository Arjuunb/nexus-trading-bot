"""Standalone exit observation is retained evidence, never trading authority."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import sqlite3
import subprocess
import sys

import pytest

from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.lab_fill_history import digest
from tradexa.guardian.service import GuardianService
from tradexa.guardian.smc_exit_fills import (
    COMPONENT, PROBE, SCOPE, PAYLOAD_SCOPE, MAX_RESPONSE_BYTES,
    GuardianSMCExitFills, cursor_anchor, decode_exit_fill, smc_exit_fills_view,
)
from tradexa.guardian.store import GuardianStore

NOW = datetime.now(timezone.utc)
URL = "http://app:8000/guardian/smc-exit-fills"
KEY = "independent-observer-key-123456789"
READ_KEY = "independent-reader-key-123456789"


@pytest.fixture
def store(tmp_path):
    return GuardianStore(tmp_path / "guardian.db")


def row(n=1, *, account="account-1", key="decision-1", position="position-1"):
    payload = dict(schema_version=1, scope=PAYLOAD_SCOPE, account_id=account,
        fill_id=f"fill-{n}", order_id=f"synthetic-exit-{n}", symbol="BTCUSDT", side="sell",
        quantity=1, closed_quantity=1, price=90, raw_reference_price=90, reduce_only=True,
        persisted_order=False, position=dict(position_id=position, entry_order_id="entry-1",
            entry_execution_key=key, entry_timeframe="5m", side="long", size=1, entry_price=100),
        protection=dict(stop_loss=90, take_profit=120, trailing_offset=None, peak_price=None),
        order=dict(type="market", limit_price=None, stop_price=None, trailing_offset=None),
        trigger_kind="POSITION_STOP_LOSS", trigger_price=90, effective_stop=90,
        effective_peak=None, fill_source="CANDLE", observation=dict(timestamp=None,
            quote_event_id=None, open=100, high=101, low=80, close=95, bid=None, ask=None))
    return dict(source_sequence=n, fill_id=payload["fill_id"], order_id=payload["order_id"],
        symbol="BTCUSDT", side="sell", quantity=1, price=90, timestamp=NOW.isoformat(),
        exit_evidence=payload, capture_state="RECORDED_SOURCE_EXIT")


def envelope(rows, account="account-1"):
    return dict(schema_version=1, scope=SCOPE, observed_at=NOW.isoformat(), execution_integrity_verified=False,
        page=dict(account_id=account, account_type="SMC_LAB", atomic_snapshot=True,
            source_capture_post_install_only=True, full_lifecycle_verified=False,
            after=0, anchor="", first_fill=rows[0] if rows else None, previous_fill=None,
            fills=rows, has_more=False, next_after=rows[-1]["source_sequence"] if rows else 0,
            next_anchor=cursor_anchor(account, rows[0], rows[-1]) if rows else ""))


def observe(store, value=None, account="account-1"):
    value = value or row()
    event = GuardianEvent(source_service=PROBE, source_component=COMPONENT,
        event_type="smc_exit_evidence_observed", timestamp=NOW,
        event_id=digest(["smc-exit-fill-event-v1", account, value["fill_id"]]),
        order_id=value["order_id"], lab_id="SMC", symbol=value["symbol"],
        reason="RECORDED_PAPER_EXIT_EVIDENCE_ONLY",
        evidence={"account_id": account, "account_type": "SMC_LAB", "fill": value},
        metadata={"coverage": SCOPE, "paper_only": True, "full_lifecycle_verified": False})
    store.append(event)
    return event


def collect(store, view):
    return GuardianSMCExitFills(store, URL, KEY, fetch=lambda *args:view, clock=lambda:NOW).poll()


def request(store, *, query="", key=READ_KEY, method="GET"):
    app = getattr(store, "_fixture_app", None)
    if app is None:
        app = GuardianService(store, source_keys={"guardian_probe": KEY}, read_key=READ_KEY,
                              required_components=("guardian", PROBE))
        store._fixture_app = app
    result = {}
    def respond(status, headers):
        result.update(status=int(status.split()[0]), headers=dict(headers))
    raw = b"".join(app(dict(REQUEST_METHOD=method, PATH_INFO="/v1/smc-exit-fills", QUERY_STRING=query,
        HTTP_X_GUARDIAN_KEY=key, CONTENT_LENGTH="0", **{"wsgi.input": io.BytesIO()}), respond))
    return result["status"], json.loads(raw), result["headers"]


@pytest.mark.parametrize("change", [
    "schema", "scope", "integrity", "stale", "future", "naive", "account", "atomic", "capture", "lifecycle",
    "after", "anchor", "has_more", "empty_more", "too_many", "unordered", "duplicate", "sequence_bool",
    "sequence_overflow", "first", "previous", "checkpoint", "next_anchor", "row_time", "quantity_bool",
    "price_nan", "quantity_overflow", "payload_bool", "payload_account", "payload_fill", "payload_closed",
    "payload_direction", "payload_trigger", "payload_source", "payload_reduce", "legacy_flag", "secret", "unicode",
    "extra_root", "extra_page", "extra_row", "response_bound", "payload_bound",
])
def test_bad_source_page_fails_without_event_or_cursor_commit(store, change):
    value = envelope([row()])
    page, fill = value["page"], value["page"]["fills"][0]
    payload = fill["exit_evidence"]
    if change == "schema": value["schema_version"] = True
    elif change == "scope": value["scope"] = "OTHER"
    elif change == "integrity": value["execution_integrity_verified"] = True
    elif change == "stale": value["observed_at"] = (NOW-timedelta(seconds=100)).isoformat()
    elif change == "future": value["observed_at"] = (NOW+timedelta(seconds=10)).isoformat()
    elif change == "naive": value["observed_at"] = NOW.replace(tzinfo=None).isoformat()
    elif change == "account": page["account_type"] = "PRICE_ACTION"
    elif change == "atomic": page["atomic_snapshot"] = False
    elif change == "capture": page["source_capture_post_install_only"] = False
    elif change == "lifecycle": page["full_lifecycle_verified"] = True
    elif change == "after": page["after"] = True
    elif change == "anchor": page["anchor"] = "a"*64
    elif change == "has_more": page["has_more"] = 1
    elif change == "empty_more": page["fills"], page["has_more"] = [], True
    elif change == "too_many": page["fills"] = [row(n) for n in range(1,34)]
    elif change == "unordered": page["fills"] = [row(2), row(1)]
    elif change == "duplicate": page["fills"].append(deepcopy(fill))
    elif change == "sequence_bool": fill["source_sequence"] = True
    elif change == "sequence_overflow": fill["source_sequence"] = 2**63
    elif change == "first": page["first_fill"] = row(2)
    elif change == "previous": page["previous_fill"] = row()
    elif change == "checkpoint": page["next_after"] = 2
    elif change == "next_anchor": page["next_anchor"] = "a"*64
    elif change == "row_time": fill["timestamp"] = (NOW+timedelta(seconds=10)).isoformat()
    elif change == "quantity_bool": fill["quantity"] = True
    elif change == "price_nan": fill["price"] = float("nan")
    elif change == "quantity_overflow": fill["quantity"] = 10**500
    elif change == "payload_bool": payload["price"] = True
    elif change == "payload_account": payload["account_id"] = "other"
    elif change == "payload_fill": payload["fill_id"] = "other"
    elif change == "payload_closed": payload["closed_quantity"] = .5
    elif change == "payload_direction": payload["position"]["side"] = "short"
    elif change == "payload_trigger": payload["trigger_price"] = 91
    elif change == "payload_source": payload["fill_source"] = "MARK"
    elif change == "payload_reduce": payload["reduce_only"] = False
    elif change == "legacy_flag": fill["exit_evidence"] = None
    elif change == "secret": payload["position"]["entry_execution_key"] = "Bearer private-value"
    elif change == "unicode": payload["position"]["position_id"] = "\ud800"
    elif change == "extra_root": value["invented"] = True
    elif change == "extra_page": page["invented"] = True
    elif change == "extra_row": fill["invented"] = True
    elif change == "response_bound": value["padding"] = "x"*MAX_RESPONSE_BYTES
    elif change == "payload_bound":
        for k in ("account_id", "fill_id", "order_id", "symbol"): payload[k] = "界"*256
        for k in ("position_id", "entry_order_id", "entry_execution_key", "entry_timeframe"):
            payload["position"][k] = "界"*256
    with pytest.raises((ValueError, TypeError, OverflowError)):
        collect(store, value)
    assert store.count() == 0 and store.observer_cursor(COMPONENT) == (0, "")
    assert smc_exit_fills_view(store, now=NOW)["history_state"] == "UNKNOWN"
    assert store.heartbeats()[PROBE]["reason"] == "EXIT_FILL_SOURCE_UNAVAILABLE"


@pytest.mark.parametrize("url", ["https://app:8000/guardian/smc-exit-fills", "http://outside:8000/guardian/smc-exit-fills",
    "http://app:80/guardian/smc-exit-fills", "http://a:b@app:8000/guardian/smc-exit-fills",
    URL+"?after=1", URL+"#unsafe", "http://app:8000/api/v1/start"])
def test_only_internal_read_url_is_allowed(store, url):
    with pytest.raises(ValueError): GuardianSMCExitFills(store, url, KEY)


@pytest.mark.parametrize("key", [None, "", "short", 100])
def test_collector_requires_independent_long_key(store, key):
    with pytest.raises(ValueError): GuardianSMCExitFills(store, URL, key)


@pytest.mark.parametrize("fault", ["oversized", "non_200", "timeout", "duplicate_json"])
def test_bounded_transport_independent_header_and_no_redirect(store, monkeypatch, fault):
    import tradexa.guardian.smc_exit_fills as module
    class Response:
        status = 503 if fault == "non_200" else 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, bound):
            assert bound == MAX_RESPONSE_BYTES+1
            if fault == "duplicate_json":
                raw = json.dumps(envelope([row()]))
                return ('{"scope":"OTHER",'+raw[1:]).encode()
            return b"x"*bound
    class Opener:
        def open(self, req, timeout):
            assert timeout == 3
            assert dict(req.header_items()) == {"X-guardian-observer-key": KEY}
            assert req.full_url == URL+"?after=0&anchor="
            if fault == "timeout": raise TimeoutError("private source URL")
            return Response()
    def build(handler):
        assert handler.redirect_request(None, None, 302, "", {}, "http://outside") is None
        return Opener()
    monkeypatch.setattr(module, "build_opener", build)
    with pytest.raises((ValueError, TimeoutError)):
        GuardianSMCExitFills(store, URL, KEY, clock=lambda:NOW).poll()
    assert store.count() == 0 and "private" not in str(store.heartbeats())


def test_concurrent_collectors_cannot_advance_same_page_twice(store):
    view = envelope([row()])
    second = GuardianSMCExitFills(store, URL, KEY, fetch=lambda *args:view, clock=lambda:NOW)
    def racing_fetch(*args):
        assert second.poll() == 1
        return view
    with pytest.raises(ValueError):
        GuardianSMCExitFills(store, URL, KEY, fetch=racing_fetch, clock=lambda:NOW).poll()
    assert store.count() == 1 and store.observer_cursor(COMPONENT)[0] == 1


def test_changed_replay_cannot_overwrite_retained_event_or_advance_cursor(store):
    observe(store)
    changed = row()
    changed["price"] = changed["exit_evidence"]["price"] = 89
    with pytest.raises(ValueError): collect(store, envelope([changed]))
    assert store.count() == 1 and store.observer_cursor(COMPONENT) == (0, "")
    assert smc_exit_fills_view(store, now=NOW)["events"][0]["evidence"]["fill"]["price"] == 90


def test_account_fill_identity_is_separate_and_missing_parent_is_not_invented(store):
    first = observe(store)
    foreign = row(account="account-2", key=None, position=None)
    second = observe(store, foreign, account="account-2")
    assert first.event_id != second.event_id
    view = smc_exit_fills_view(store, now=NOW)
    assert len(view["events"]) == 2
    position = view["events"][1]["evidence"]["fill"]["exit_evidence"]["position"]
    assert position["entry_execution_key"] is position["position_id"] is None
    assert all(view[k] is False for k in view if k.endswith("_verified"))


def test_empty_source_and_legacy_null_are_unknown_not_no_exit_or_current_protection(store):
    assert collect(store, envelope([])) == 0
    assert smc_exit_fills_view(store, now=NOW)["history_state"] == "CAUGHT_UP_AT_LAST_POLL"
    legacy = row()
    legacy["exit_evidence"] = None
    legacy["capture_state"] = "UNVERIFIED_NO_EXIT_CAPTURE"
    assert collect(store, envelope([legacy])) == 1
    view = smc_exit_fills_view(store, now=NOW)
    assert view["events"][0]["event_type"] == "smc_exit_evidence_observed"
    assert "MISSED" not in str(view) and "No order was placed" not in str(view)
    assert view["journal_close_verified"] is view["current_protection_verified"] is False


@pytest.mark.parametrize("state", ["FAILED", "DEGRADED", "stale", "missing"])
def test_observer_failure_never_hides_historical_evidence_or_claims_current_health(store, state):
    observe(store)
    if state != "missing":
        store.record_heartbeat(PROBE, "HEALTHY" if state == "stale" else state,
            observed_at=NOW-timedelta(seconds=100) if state == "stale" else NOW)
    view = smc_exit_fills_view(store, now=NOW)
    assert view["history_state"] == ("IMPORTING" if state == "DEGRADED" else "UNKNOWN")
    assert len(view["events"]) == 1 and view["current_protection_verified"] is False


@pytest.mark.parametrize("damage", ["payload", "secret", "identity", "execution_state", "oversized", "duplicate_json", "instance_claim"])
def test_damaged_cache_returns_sanitized_503_without_certification(store, damage):
    event = observe(store)
    value = json.loads(event.canonical_json())
    if damage == "payload": value["evidence"]["fill"]["exit_evidence"]["trigger_price"] = 80
    elif damage == "secret": value["metadata"]["api_key"] = "never-expose"
    elif damage == "identity": value["event_id"] = "another-event"
    elif damage == "execution_state": value["state_after"] = "COMPLETE"
    elif damage == "oversized": value["padding"] = "x"*17000
    elif damage == "instance_claim": value["instance_id"] = "foreign-instance"
    raw = json.dumps(value)
    if damage == "duplicate_json": raw = '{"state_after":"COMPLETE",'+raw[1:]
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET payload_json=? WHERE event_id=?", (raw, event.event_id))
        db.commit()
    assert request(store)[:2] == (503, {"error": "EXIT_EVIDENCE_UNAVAILABLE"})


def test_read_only_auth_lock_retry_and_missing_store_not_recreated(store):
    for key in ("", KEY): assert request(store, key=key)[0] == 401
    assert request(store, method="POST")[0] == 405
    assert request(store)[0] == 200
    assert request(store)[2]["Cache-Control"] == "no-store"
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN EXCLUSIVE")
        for _ in range(2):
            assert request(store)[:2] == (503, {"error": "PERSISTENCE_UNAVAILABLE"})
        db.rollback()
    assert request(store)[0] == 200
    store.path.unlink()
    assert request(store)[0] == 503 and not store.path.exists()


def test_wal_read_during_write_and_100_dashboard_refreshes_write_nothing(store):
    observe(store)
    store.record_heartbeat(PROBE, "HEALTHY", observed_at=NOW)
    assert request(store)[0] == 200
    count = store.count()
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE heartbeats SET reason='uncommitted' WHERE component=?", (PROBE,))
        for _ in range(100): assert request(store)[0] == 200
        db.rollback()
        plan = db.execute("EXPLAIN QUERY PLAN SELECT sequence FROM events INDEXED BY smc_exit_history WHERE source_service='guardian_smc_exit_fills' "
            "AND event_type='smc_exit_evidence_observed' AND sequence>? ORDER BY sequence LIMIT 33", (0,)).fetchall()
    assert "SEARCH" in str(plan) and "smc_exit_history" in str(plan)
    assert store.count() == count and store.observer_cursor(COMPONENT) == (0, "")


def test_standalone_service_import_has_no_trading_source_dependency():
    result = subprocess.run([sys.executable, "-c", "import sys; import tradexa.guardian.service; "
        "assert not any(k.startswith(('execution.', 'services.', 'bot.')) for k in sys.modules)"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_decoder_rejects_duplicate_json_fields_before_evidence_projection():
    raw = json.dumps(row()["exit_evidence"])
    with pytest.raises(ValueError): decode_exit_fill('{"side":"buy",'+raw[1:])
