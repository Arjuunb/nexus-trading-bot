"""Standalone Guardian contracts: retained evidence never becomes authority."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
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
from tradexa.guardian.smc_fill_positions import (
    COMPONENT, MAX_RESPONSE_BYTES, PAYLOAD_SCOPE, PROBE, SCOPE,
    GuardianSMCFillPositions, cursor_anchor, project_fill_position, smc_fill_positions_view,
)
from tradexa.guardian.smc_intent_history import FIELDS
from tradexa.guardian.smc_position_links import PROBES, smc_position_links_view
from tradexa.guardian.store import GuardianStore

NOW = datetime.now(timezone.utc)
URL = "http://app:8000/guardian/smc-fill-transitions"
KEY = "independent-observer-key-123456789"
READ_KEY = "independent-reader-key-123456789"


@pytest.fixture
def store(tmp_path):
    result = GuardianStore(tmp_path / "guardian.db")
    for name in PROBES:
        result.record_heartbeat(name, "HEALTHY", observed_at=NOW)
    return result


def row(n=1, *, account="account-1", key="decision-1", position="position-1", order="order-1"):
    value = dict(schema_version=1, scope=PAYLOAD_SCOPE, account_id=account, fill_id=f"fill-{n}",
                 order_id=order, symbol="BTCUSDT", side="buy", quantity=1, price=100,
                 reduce_only=False, persisted_order=True, before=None,
                 after=dict(position_id=position, entry_order_id=order, entry_execution_key=key,
                            entry_timeframe="5m", side="long", size=1, entry_price=100), effect="OPEN")
    return dict(source_sequence=n, fill_id=value["fill_id"], order_id=order, symbol="BTCUSDT",
                side="buy", quantity=1, price=100, timestamp=NOW.isoformat(), transition=value,
                capture_state="RECORDED_SOURCE_TRANSITION")


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
        event_type="smc_fill_position_observed", timestamp=NOW,
        event_id=digest(["smc-fill-position-event-v1", account, value["fill_id"]]),
        order_id=value["order_id"], lab_id="SMC", symbol=value["symbol"],
        reason="RECORDED_PAPER_POSITION_TRANSITION_ONLY",
        evidence={"account_id": account, "account_type": "SMC_LAB", "fill": value},
        metadata={"coverage": SCOPE, "paper_only": True, "full_lifecycle_verified": False})
    store.append(event)
    return event


def intent(store, *, state="COMPLETE", order="order-1", origin="a"*64, **changes):
    value = dict.fromkeys(FIELDS)
    value.update(source_sequence=store.count()+1, id=f"intent-event-{store.count()}", execution_key="decision-1",
                 state=state, broker_order_id=order, trade_id="trade-1", created_at=NOW.isoformat(),
                 intent_id="intent-1", session_id="session-1", symbol="BTCUSDT", timeframe="5m",
                 candle_time=NOW.isoformat(), proposal_id="decision-1", error_recorded=False)
    value.update(changes)
    event = GuardianEvent(source_service=PROBES[0], source_component="smc_intent_history",
        event_type="smc_intent_transition_observed", timestamp=NOW, execution_id="decision-1",
        order_id=order, lab_id="SMC", state_after=state, symbol=value["symbol"], timeframe=value["timeframe"],
        evidence={"transition": value, "journal_origin": origin})
    store.append(event)


def request(store, route="/v1/smc-fill-transitions", query="", *, key=READ_KEY, method="GET"):
    app = getattr(store, "_fixture_http_app", None)
    if app is None:
        app = GuardianService(store, source_keys={"guardian_probe": KEY}, read_key=READ_KEY, required_components=("guardian", *PROBES))
        store._fixture_http_app = app
    result = {}
    def respond(status, headers):
        result.update(status=int(status.split()[0]), headers=dict(headers))
    raw = b"".join(app(dict(REQUEST_METHOD=method, PATH_INFO=route, QUERY_STRING=query,
        HTTP_X_GUARDIAN_KEY=key, CONTENT_LENGTH="0", **{"wsgi.input": io.BytesIO()}), respond))
    return result["status"], json.loads(raw), result["headers"]


@pytest.mark.parametrize("change", [
    "schema", "scope", "integrity", "stale", "future", "naive", "account", "atomic", "capture", "lifecycle",
    "after", "anchor", "has_more", "empty_more", "too_many", "first", "previous", "next_after", "next_anchor",
    "duplicate", "future_fill", "extra_field", "account_mismatch", "effect", "flag", "nan", "overflow",
    "surrogate", "secret", "legacy_flag", "response_bound", "payload_quantity_bool", "payload_price_bool",
])
def test_malformed_source_never_advances_checkpoint_or_imports_partial_page(store, change):
    view = envelope([row()])
    page, fill = view["page"], view["page"]["fills"][0]
    if change == "schema": view["schema_version"] = True
    elif change == "scope": view["scope"] = "OTHER"
    elif change == "integrity": view["execution_integrity_verified"] = True
    elif change in {"stale", "future"}: view["observed_at"] = (NOW+timedelta(seconds=-100 if change=="stale" else 10)).isoformat()
    elif change == "naive": view["observed_at"] = NOW.replace(tzinfo=None).isoformat()
    elif change == "account": page["account_type"] = "PAPER"
    elif change == "atomic": page["atomic_snapshot"] = 1
    elif change == "capture": page["source_capture_post_install_only"] = False
    elif change == "lifecycle": page["full_lifecycle_verified"] = True
    elif change == "after": page["after"] = True
    elif change == "anchor": page["anchor"] = "a"*64
    elif change == "has_more": page["has_more"] = 1
    elif change == "empty_more": page["has_more"], page["fills"] = True, []
    elif change == "too_many": page["fills"] = [row(n) for n in range(1,34)]
    elif change == "first": page["first_fill"] = None
    elif change == "previous": page["previous_fill"] = row()
    elif change == "next_after": page["next_after"] = 2
    elif change == "next_anchor": page["next_anchor"] = "b"*64
    elif change == "duplicate": page["fills"].append(deepcopy(fill))
    elif change == "future_fill": fill["timestamp"] = (NOW+timedelta(seconds=10)).isoformat()
    elif change == "extra_field": fill["runtime_quote"] = 100
    elif change == "account_mismatch": fill["transition"]["account_id"] = "another-account"
    elif change == "effect": fill["transition"]["effect"] = "CLOSE"
    elif change == "flag": fill["transition"]["reduce_only"] = 0
    elif change == "nan": fill["price"] = float("nan")
    elif change == "overflow": fill["quantity"] = 10**400
    elif change == "surrogate": fill["order_id"] = "bad\ud800"
    elif change == "secret": fill["symbol"] = "Bearer private-value"
    elif change == "legacy_flag": fill["transition"] = None
    elif change == "response_bound": view["padding"] = "x"*MAX_RESPONSE_BYTES
    elif change == "payload_quantity_bool": fill["transition"]["quantity"] = True
    elif change == "payload_price_bool": fill["price"], fill["transition"]["price"] = 1, True
    collector = GuardianSMCFillPositions(store, URL, KEY, fetch=lambda *args:view, clock=lambda:NOW)
    with pytest.raises((ValueError, TypeError, OverflowError)):
        collector.poll()
    assert store.count() == 0 and store.observer_cursor(COMPONENT) == (0, "")
    assert smc_fill_positions_view(store, now=NOW)["history_state"] == "UNKNOWN"
    assert store.heartbeats()[PROBE]["reason"] == "FILL_POSITION_SOURCE_UNAVAILABLE"


@pytest.mark.parametrize("url", ["https://app:8000/guardian/smc-fill-transitions", "http://outside:8000/guardian/smc-fill-transitions",
    "http://app:80/guardian/smc-fill-transitions", "http://a:b@app:8000/guardian/smc-fill-transitions",
    URL+"?after=1", URL+"#unsafe", "http://app:8000/api/v1/start"])
def test_only_internal_read_url_is_allowed(store, url):
    with pytest.raises(ValueError): GuardianSMCFillPositions(store, url, KEY)


@pytest.mark.parametrize("fault", ["oversized", "non_200", "timeout"])
def test_transport_bounded_independent_key_no_redirects(store, monkeypatch, fault):
    import tradexa.guardian.smc_fill_positions as module
    class Response:
        status = 503 if fault=="non_200" else 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, bound):
            assert bound == MAX_RESPONSE_BYTES+1
            return b"x"*bound
    class Opener:
        def open(self, req, timeout):
            assert timeout == 3
            assert dict(req.header_items()) == {"X-guardian-observer-key": KEY}
            assert req.full_url == URL+"?after=0&anchor="
            if fault=="timeout": raise TimeoutError("private source URL")
            return Response()
    def build(handler):
        assert handler.redirect_request(None, None, 302, "", {}, "http://outside") is None
        return Opener()
    monkeypatch.setattr(module, "build_opener", build)
    with pytest.raises((ValueError, TimeoutError)):
        GuardianSMCFillPositions(store, URL, KEY, clock=lambda:NOW).poll()
    assert store.count()==0 and "private" not in str(store.heartbeats())


def test_concurrent_collectors_cannot_advance_same_page_twice(store):
    view = envelope([row()])
    second = GuardianSMCFillPositions(store, URL, KEY, fetch=lambda *args:view, clock=lambda:NOW)
    def racing_fetch(*args):
        assert second.poll() == 1
        return view
    with pytest.raises(ValueError):
        GuardianSMCFillPositions(store, URL, KEY, fetch=racing_fetch, clock=lambda:NOW).poll()
    assert store.count()==1 and store.observer_cursor(COMPONENT)[0]==1


def test_changed_replay_cannot_overwrite_retained_event_or_advance_cursor(store):
    observe(store)
    changed = row()
    changed["price"] = changed["transition"]["price"] = 101
    view = envelope([changed])
    with pytest.raises(ValueError):
        GuardianSMCFillPositions(store, URL, KEY, fetch=lambda *args:view, clock=lambda:NOW).poll()
    assert store.count()==1 and store.observer_cursor(COMPONENT)==(0, "")
    assert smc_fill_positions_view(store, now=NOW)["events"][0]["evidence"]["fill"]["price"]==100


def test_exact_account_position_pair_does_not_import_another_accounts_exit(store):
    intent(store)
    observe(store)
    foreign = row(2, account="account-2", key="other-decision", position="position-1", order="other-order")
    observe(store, foreign, account="account-2")
    view = smc_position_links_view(store, "decision-1", now=NOW)
    assert len(view["position_transitions"])==1 and view["paper_account_ids"]==["account-1"]
    assert view["observed_exit_fill_ids"]==[] and view["paper_account_binding_verified"] is False


def test_reduce_only_identity_with_impossible_direction_is_conflicting_not_an_exit(store):
    intent(store)
    observe(store)
    bad = row(2)
    value = bad["transition"]
    value.update(before=deepcopy(value["after"]), after=None, effect="CLOSE", reduce_only=True)
    observe(store, bad)
    view = smc_position_links_view(store, "decision-1", now=NOW)
    assert view["link_state"]=="CONFLICTING_EVIDENCE" and view["observed_exit_fill_ids"]==[]
    assert "REDUCE_ONLY_TRANSITION_CONFLICT" in view["findings"]


@pytest.mark.parametrize("state", ["FAILED", "DEGRADED", "stale", "missing"])
def test_unavailable_observer_masks_claims_but_retains_historical_rows(store, state):
    intent(store)
    observe(store)
    if state=="missing":
        with closing(sqlite3.connect(store.path)) as db: db.execute("DELETE FROM heartbeats WHERE component=?", (PROBE,)); db.commit()
    else:
        store.record_heartbeat(PROBE, "HEALTHY" if state=="stale" else state,
            observed_at=NOW-timedelta(seconds=100) if state=="stale" else NOW)
    view = smc_position_links_view(store, "decision-1", now=NOW)
    assert view["link_state"]=="UNKNOWN" and view["current_position_state_verified"] is False
    assert len(view["position_transitions"])==1 and view["latest_recorded_state"]=="COMPLETE"


@pytest.mark.parametrize("conflict,expected", [
    ("order", "ORIGIN_ORDER_LINK_CONFLICT"), ("symbol", "ORIGIN_MARKET_CONFLICT"),
    ("session", "INTENT_IDENTITY_CONFLICT"), ("journal", "MULTIPLE_JOURNAL_ORIGINS"),
    ("trade", "MULTIPLE_RECORDED_TRADE_IDS"), ("failed", "FAILED_INTENT_WITH_RECORDED_FILL"),
    ("account", "MULTIPLE_PAPER_ACCOUNTS"), ("position", "MULTIPLE_ORIGIN_POSITION_IDS"),
])
def test_conflicting_identity_never_claims_position_exit_link(store, conflict, expected):
    intent(store, order="other-order" if conflict=="order" else "order-1",
           symbol="ETHUSDT" if conflict=="symbol" else "BTCUSDT",
           state="EXECUTION_FAILED" if conflict=="failed" else "COMPLETE")
    observe(store)
    if conflict=="session": intent(store, session_id="other-session")
    elif conflict=="journal": intent(store, origin="b"*64)
    elif conflict=="trade": intent(store, trade_id="other-trade")
    elif conflict=="account": observe(store, row(2, account="account-2"), account="account-2")
    elif conflict=="position": observe(store, row(2, position="position-2"))
    view = smc_position_links_view(store, "decision-1", now=NOW)
    assert view["link_state"]=="CONFLICTING_EVIDENCE" and expected in view["findings"]
    assert view["observed_exit_fill_ids"]==[] and view["exit_link_verified"] is False


def test_uncertain_intent_is_not_reclassified_and_missing_intent_not_inferred(store):
    observe(store)
    assert smc_position_links_view(store, "decision-1", now=NOW)["link_state"]=="INSUFFICIENT_EVIDENCE"
    intent(store, state="EXECUTION_UNCERTAIN")
    view = smc_position_links_view(store, "decision-1", now=NOW)
    assert view["latest_recorded_state"]=="EXECUTION_UNCERTAIN" and "EXECUTION_UNCERTAIN_RECORDED" in view["findings"]
    assert "MISSED" not in str(view) and "No order was placed" not in str(view)


def test_bounded_old_key_lookup_does_not_scan_recent_unrelated_history(store):
    intent(store)
    observe(store)
    for n in range(2,260): observe(store, row(n, key=f"other-{n}", position=f"position-{n}", order=f"order-{n}"))
    view = smc_position_links_view(store, "decision-1", now=NOW)
    assert view["link_state"]=="EXPLICIT_POSITION_LINKS_OBSERVED" and len(view["position_transitions"])==1
    with closing(sqlite3.connect(store.path)) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT sequence FROM events WHERE source_service='guardian_smc_fill_positions' "
            "AND event_type='smc_fill_position_observed' AND json_extract(payload_json,'$.evidence.fill.transition.after.entry_execution_key')=? ORDER BY sequence LIMIT 129", ("decision-1",)).fetchall()
    assert "SEARCH" in str(plan) and "SCAN" not in str(plan)


def test_row_bound_masks_claims_instead_of_approving_truncated_links(store):
    intent(store)
    for n in range(1,131): observe(store, row(n))
    view = smc_position_links_view(store, "decision-1", now=NOW)
    assert view["truncated"] is True and view["link_state"]=="UNKNOWN"
    assert len(view["position_transitions"])<=128 and view["latest_recorded_state"] is None
    assert view["observed_exit_fill_ids"]==[] and "LINK_EVIDENCE_TRUNCATED" in view["findings"]


@pytest.mark.parametrize("route,query", [("/v1/smc-fill-transitions", ""), ("/v1/smc-position-links", "execution_key=decision-1")])
def test_read_only_auth_lock_and_retry_without_recreating_missing_store(store, route, query):
    for key in ("", KEY): assert request(store, route, query, key=key)[0]==401
    assert request(store, route, query, method="POST")[0]==405
    assert request(store, route, query)[0]==200
    assert request(store, route, query)[2]["Cache-Control"]=="no-store"
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN EXCLUSIVE")
        assert request(store, route, query)[:2]==(503, {"error":"PERSISTENCE_UNAVAILABLE"})
        db.rollback()
    assert request(store, route, query)[0]==200
    store.path.unlink()
    assert request(store, route, query)[0]==503 and not store.path.exists()


@pytest.mark.parametrize("route,query", [
    ("/v1/smc-fill-transitions", "after=-1"), ("/v1/smc-fill-transitions", "after=2&after=3"),
    ("/v1/smc-fill-transitions", "after=9999999999999999999999"), ("/v1/smc-fill-transitions", "limit=100"),
    ("/v1/smc-position-links", ""), ("/v1/smc-position-links", "execution_key=decision-1&limit=1"),
    ("/v1/smc-position-links", "execution_key=x&execution_key=y"), ("/v1/smc-position-links", "execution_key=a/b"),
])
def test_http_rejects_unbounded_or_ambiguous_query(store, route, query):
    assert request(store, route, query)[0]==400 and store.count()==0


@pytest.mark.parametrize("damage", ["payload", "secret", "identity", "execution_state", "oversized"])
def test_damaged_cached_evidence_returns_sanitized_503_not_claim_or_secret(store, damage):
    intent(store)
    event = observe(store)
    value = json.loads(event.canonical_json())
    if damage=="payload": value["evidence"]["fill"]["transition"]["effect"]="CLOSE"
    elif damage=="secret": value["metadata"]["api_key"]="never-expose"
    elif damage=="identity": value["event_id"]="another-event"
    elif damage=="execution_state": value["state_after"]="COMPLETE"
    elif damage=="oversized": value["padding"]="x"*17000
    with closing(sqlite3.connect(store.path)) as db:
        # Simulate an externally damaged/owner-restored cache, not a supported update.
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET payload_json=? WHERE event_id=?", (json.dumps(value), event.event_id)); db.commit()
    for route, query in (("/v1/smc-fill-transitions", ""), ("/v1/smc-position-links", "execution_key=decision-1")):
        status, result, _ = request(store, route, query)
        assert (status, result)==(503, {"error":"FILL_POSITION_EVIDENCE_UNAVAILABLE"})


def test_wal_read_available_during_short_write_and_hundred_refreshes_write_nothing(store):
    intent(store)
    observe(store)
    count = store.count()
    assert request(store)[0]==200  # service startup occurs before the concurrent writer
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE heartbeats SET reason='uncommitted' WHERE component=?", (PROBE,))
        for _ in range(100):
            assert request(store)[0]==200
            assert request(store, "/v1/smc-position-links", "execution_key=decision-1")[0]==200
        db.rollback()
    assert store.count()==count and store.observer_cursor(COMPONENT)==(0, "")


def test_standalone_import_has_no_source_or_trading_dependency():
    result = subprocess.run([sys.executable, "-c", "import sys; import tradexa.guardian.service; "
        "assert not any(k.startswith(('execution.', 'services.', 'bot.')) for k in sys.modules)"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode==0, result.stderr
