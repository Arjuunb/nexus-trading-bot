"""A paper-ledger poll is change-only evidence, never a trading instruction."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tradexa.guardian.instance_ledger_integrity import reconcile_instance_paper_ledger
from tradexa.guardian.instance_ledger_observer import GuardianInstanceLedgerObserver
from tradexa.guardian.store import GuardianStore

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
URL = "http://app:8000/guardian/instance-ledger"
KEY = "independent-guardian-instance-ledger-key-12345"


def _snapshot(*, stop=95):
    positions = [{"id": "p1", "instance_id": "i1", "simulation_session_id": "s1",
                  "status": "open", "symbol": "BTCUSDT", "side": "long",
                  "size": 2, "entry": 100, "stop": stop}]
    trades = [{"id": "t1", "instance_id": "i1", "simulation_session_id": "s1",
               "status": "open", "source": "paper", "symbol": "BTCUSDT",
               "side": "long", "size": 2, "entry": 100}]
    links = [{"execution_id": "e1", "instance_id": "i1", "action": "OPEN",
              "position_id": "p1", "trade_id": "t1"}]
    result = reconcile_instance_paper_ledger(
        positions, trades, links, atomic_snapshot=True)
    result["source_coverage_verified"] = True
    return result


def _view(*, stop=95, observed_at=NOW):
    return {"schema_version": 1,
            "scope": "INSTANCE_ATTRIBUTED_PAPER_LEDGER_ONLY",
            "observed_at": observed_at.isoformat(),
            "feed_health_verified": False,
            "execution_integrity_verified": False,
            "snapshot": _snapshot(stop=stop)}


def _observer(store, fetch):
    return GuardianInstanceLedgerObserver(
        store, URL, KEY, fetch=fetch, clock=lambda: NOW + timedelta(seconds=1))


def test_repeated_polls_and_restart_do_not_amplify_unchanged_ledger(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    observer = _observer(store, lambda: _view())
    assert observer.poll() == 1
    assert sum(observer.poll() for _ in range(100)) == 0
    assert store.count() == 1
    assert _observer(GuardianStore(store.path), lambda: _view()).poll() == 0
    assert store.count() == 1
    assert store.heartbeats()["guardian_instance_ledger_probe"]["state"] == "HEALTHY"


def test_material_stop_change_appends_one_new_observation(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    state = {"stop": 95}
    observer = _observer(store, lambda: _view(stop=state["stop"]))
    assert observer.poll() == 1
    state["stop"] = 96
    assert observer.poll() == 1
    assert observer.poll() == 0
    assert store.count() == 2
    latest = store.recent(1)[0]
    assert latest["evidence"]["instances"][0]["risk_amount"] == 8


def test_failed_evidence_insert_cannot_advance_dedupe_checkpoint(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    with sqlite3.connect(store.path) as conn:
        conn.execute("""CREATE TRIGGER fail_snapshot BEFORE INSERT ON observer_snapshot_state
                     BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END""")
    observer = _observer(store, lambda: _view())
    with pytest.raises(sqlite3.IntegrityError):
        observer.poll()
    assert store.count() == 0
    assert store.heartbeats() == {}
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observer_snapshot_state").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_snapshot")
    assert observer.poll() == 1
    assert observer.poll() == 0
    assert store.count() == 1


def test_stale_or_overclaiming_source_fails_closed(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    with pytest.raises(ValueError, match="stale"):
        _observer(store, lambda: _view(observed_at=NOW - timedelta(minutes=3))).poll()
    view = _view()
    view["snapshot"]["atomic_snapshot"] = False
    with pytest.raises(ValueError, match="coverage"):
        _observer(store, lambda: view).poll()
    view["snapshot"]["source_coverage_verified"] = False
    with pytest.raises(ValueError, match="risk claim"):
        _observer(store, lambda: view).poll()
    assert store.count() == 0
    assert store.heartbeats() == {}


def test_observer_rejects_public_url_or_short_key(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    with pytest.raises(ValueError, match="internal"):
        GuardianInstanceLedgerObserver(store,
                                       "https://trade-logx.com/guardian/instance-ledger", KEY)
    with pytest.raises(ValueError, match="independent"):
        GuardianInstanceLedgerObserver(store, URL, "short")
