"""Guardian's independent evidence foundation must not affect trading paths."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tradexa.guardian.events import GuardianEvent, GuardianEventError
from tradexa.guardian.health import component_health
from tradexa.guardian.store import GuardianStore


def _event(**changes) -> GuardianEvent:
    event = GuardianEvent(
        source_service="instance_worker",
        source_component="risk_gate",
        event_type="risk_check_failed",
        event_id="decision_12345678",
        severity="WARNING",
        reason="insufficient reward to risk",
        evidence={"rr": 1.2, "required_rr": 1.5},
    )
    return replace(event, **changes)


def test_append_is_idempotent_and_raw_evidence_is_immutable(tmp_path):
    path = tmp_path / "guardian" / "events.db"
    store = GuardianStore(path)
    event = _event()

    assert store.append(event) is True
    assert store.append(event) is False
    assert store.count() == 1
    assert store.recent()[0]["evidence"] == {"rr": 1.2, "required_rr": 1.5}
    assert store.recent()[0]["received_at"]
    assert path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(GuardianEventError, match="collision"):
        store.append(replace(event, reason="different"))
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE events SET payload_json='different' WHERE event_id=?", (event.event_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM events WHERE event_id=?", (event.event_id,))
    assert store.count() == 1


def test_event_rejects_credentials_invalid_clock_and_oversized_evidence():
    with pytest.raises(GuardianEventError, match="unsafe field"):
        _event(evidence={"api_key": "redacted"}).canonical_json()
    with pytest.raises(GuardianEventError, match="credential-like"):
        _event(reason="Bearer abc123").canonical_json()
    with pytest.raises(GuardianEventError, match="timezone"):
        _event(timestamp=datetime(2026, 1, 1)).canonical_json()
    with pytest.raises(GuardianEventError, match="oversized"):
        _event(evidence={"detail": "x" * 2049}).canonical_json()
    with pytest.raises(GuardianEventError, match="unsupported"):
        _event(evidence={"object": object()}).canonical_json()


def test_heartbeat_reports_unknown_until_fresh_evidence_arrives(tmp_path):
    store = GuardianStore(tmp_path / "events.db")
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    required = ("market_feed", "smc_worker", "guardian")
    initial = component_health(store.heartbeats(), required, now=now)
    assert initial["state"] == "UNKNOWN"
    assert initial["evidence_complete"] is False

    for component in required:
        store.record_heartbeat(component, "HEALTHY", observed_at=now)
    healthy = component_health(store.heartbeats(), required, now=now)
    assert healthy["state"] == "HEALTHY"
    assert healthy["evidence_complete"] is True

    store.record_heartbeat("market_feed", "BLOCKED", reason="stale candle", observed_at=now)
    blocked = component_health(store.heartbeats(), required, now=now)
    assert blocked["state"] == "BLOCKED"
    assert blocked["components"]["market_feed"]["reason"] == "stale candle"

    stale = component_health(store.heartbeats(), required, now=now + timedelta(seconds=91))
    assert stale["state"] == "UNKNOWN"
    assert stale["evidence_complete"] is False


def test_heartbeat_does_not_store_credential_text(tmp_path):
    store = GuardianStore(tmp_path / "events.db")
    with pytest.raises(GuardianEventError, match="credential-like"):
        store.record_heartbeat("market_feed", "FAILED", reason="token=secret-value")
    assert store.heartbeats() == {}


def test_concurrent_reader_does_not_block_short_event_writes(tmp_path):
    store = GuardianStore(tmp_path / "events.db")

    def write_one(number: int) -> None:
        store.append(_event(event_id=f"decision_{number:08d}"))

    def read_many() -> None:
        for _ in range(100):
            assert isinstance(store.recent(10), list)

    with ThreadPoolExecutor(max_workers=3) as pool:
        writer = pool.submit(lambda: [write_one(n) for n in range(50)])
        reader_a = pool.submit(read_many)
        reader_b = pool.submit(read_many)
        for future in (writer, reader_a, reader_b):
            future.result()
    assert store.count() == 50


def test_guardian_store_owns_only_its_database(tmp_path):
    GuardianStore(tmp_path / "guardian.db").append(_event())
    assert (tmp_path / "guardian.db").exists()
    assert all(path.name.startswith("guardian.db") for path in tmp_path.iterdir())


def test_existing_insecure_guardian_database_is_rejected(tmp_path):
    path = tmp_path / "guardian.db"
    path.touch(mode=0o644)
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="owner-only"):
        GuardianStore(path)


def test_symlink_cannot_redirect_guardian_to_another_database(tmp_path):
    other = tmp_path / "other.db"
    other.touch(mode=0o600)
    (tmp_path / "guardian.db").symlink_to(other)
    with pytest.raises(PermissionError, match="symlink"):
        GuardianStore(tmp_path / "guardian.db")
