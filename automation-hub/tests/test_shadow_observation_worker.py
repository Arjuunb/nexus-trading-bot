from copy import deepcopy
from datetime import timedelta

import pytest

from services.shadow_observation_worker import ShadowObservationWorker
from services.shadow_router import ShadowRouter
from services.shadow_observation_store import ShadowObservationStore
from services.shadow_research import ShadowResearchStore
from test_shadow_router import setup, frame, T


class Subscription:
    def __init__(self, hub): self.hub = hub; self.stopped = False
    def start(self, symbol, timeframe, *, quotes):
        assert quotes is False
        self.timeframe = timeframe
        self.hub.started.append((symbol, timeframe))
        return True
    def stop(self): self.stopped = True
    def snapshot(self):
        data = self.hub.market
        return {"closed_bars": deepcopy(data["context"][self.timeframe]), "connection": {
            "transport_state": "CONNECTED" if data["connected"] else "DISCONNECTED",
            "reliable": data["connected"] and data["htf_health"].get(self.timeframe, True),
            "last_candle_update": data["stream_observed_at"]}}


class Hub:
    def __init__(self): self.market = frame(); self.started = []; self.subs = []
    def subscription(self, *, consumer_id, candle_notice):
        # Callback has no compute or I/O; transport remains the shared hub's.
        candle_notice(None)
        sub = Subscription(self); self.subs.append(sub); return sub


def test_worker_evaluates_only_live_opportunities_and_stops_owned_subscriptions(setup):
    router, store, _, reg, clock, _ = setup
    clock[0] = T - timedelta(seconds=1)
    hub = Hub(); worker = ShadowObservationWorker(router, hub)
    worker.tick()
    assert store.decisions(reg["registration_id"], owner_id="alice") == []
    clock[0] = T + timedelta(seconds=1); worker.tick(); worker.tick()
    assert len(store.decisions(reg["registration_id"], owner_id="alice")) == 1
    assert len(hub.started) == 4
    worker.stop()
    assert all(sub.stopped for sub in hub.subs)


def test_restart_rebuilds_warmup_and_records_gap_without_backfill(setup):
    router, store, raw, reg, clock, _ = setup
    clock[0] = T - timedelta(seconds=1)
    hub = Hub(); worker = ShadowObservationWorker(router, hub)
    worker.tick(); clock[0] = T + timedelta(seconds=1); worker.tick(); worker.stop()
    clock[0] = T + timedelta(minutes=12)
    other_store = ShadowObservationStore(ShadowResearchStore(raw.path))
    other_router = ShadowRouter(other_store, candidate_reader=router.candidate_reader, clock=router.clock)
    restarted = ShadowObservationWorker(other_router, hub)
    restarted.tick()
    assert len(other_store.decisions(reg["registration_id"], owner_id="alice")) == 1
    assert any(e["action"] == "OBSERVATION_GAP" for e in other_store.events(reg["registration_id"], owner_id="alice"))
    restarted.stop()


def test_disconnected_feed_records_blocked_opportunity_not_no_signal(setup):
    router, store, _, reg, clock, _ = setup
    clock[0] = T - timedelta(seconds=1)
    hub = Hub(); hub.market["connected"] = False
    worker = ShadowObservationWorker(router, hub)
    worker.tick(); clock[0] = T + timedelta(seconds=6); worker.tick()
    record = store.decisions(reg["registration_id"], owner_id="alice")[0]
    assert record["status"] == "BLOCKED_STALE_DATA"
    assert "FEED_DISCONNECTED" in record["rejection_reasons"]
    worker.stop()


def test_database_failure_is_retried_and_does_not_duplicate_evidence(setup, monkeypatch):
    router, store, _, reg, clock, _ = setup
    clock[0] = T - timedelta(seconds=1)
    worker = ShadowObservationWorker(router, Hub()); worker.tick()
    clock[0] = T + timedelta(seconds=1)
    commit = store.commit
    def disconnected(*args, **kwargs): raise ConnectionError("unavailable")
    monkeypatch.setattr(store, "commit", disconnected); worker.tick()
    assert store.decisions(reg["registration_id"], owner_id="alice") == []
    monkeypatch.setattr(store, "commit", commit); worker.tick(); worker.tick()
    assert len(store.decisions(reg["registration_id"], owner_id="alice")) == 1
    assert store.analytics(reg["registration_id"], owner_id="alice")["evidence_completeness"] == "PARTIAL"
    worker.stop()


def test_pause_fences_existing_worker_before_next_opportunity(setup):
    router, store, _, reg, clock, _ = setup
    clock[0] = T - timedelta(seconds=1)
    worker = ShadowObservationWorker(router, Hub()); worker.tick()
    # Avoid starting a real background thread for this deterministic checkpoint.
    router.worker = None
    router.action(reg["registration_id"], "PAUSE", owner_id="alice", reason="stop scheduled observation")
    clock[0] = T + timedelta(seconds=1); worker.tick()
    assert store.decisions(reg["registration_id"], owner_id="alice") == []
    assert not worker._owned


def test_shadow_migration_is_not_discovered_by_postgres_runner():
    from data.pg_migrations import discover
    assert not any(m.version == "0003_shadow_observation" for m in discover())


def test_bootstrap_failure_retries_on_same_subscription_with_bounded_backoff(setup):
    router, _, _, _, clock, _ = setup
    hub = Hub()
    class Flaky(Subscription):
        running = False
        attempts = 0
        def start(self, symbol, timeframe, *, quotes):
            super().start(symbol, timeframe, quotes=quotes)
            self.attempts += 1; self.running = self.attempts >= 2
            return self.running
    def subscribe(**kwargs):
        sub = Flaky(hub); hub.subs.append(sub); return sub
    hub.subscription = subscribe
    worker = ShadowObservationWorker(router, hub); worker.tick()
    assert len(hub.subs) == 4 and all(not s.running for s in hub.subs)
    clock[0] += timedelta(seconds=6); worker.tick()
    assert len(hub.subs) == 4 and all(s.running for s in hub.subs)
    assert all(owned["retry_seconds"] <= 30 for owned in worker._owned.values())
    worker.stop()
