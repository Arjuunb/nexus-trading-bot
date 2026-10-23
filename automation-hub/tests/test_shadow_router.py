"""Synthetic market fixtures test observation integrity, never profitability."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
import sqlite3

import pytest

from bot.types import Bar
from services.shadow_research import ShadowResearchStore
from services.strategy_identity import observed_strategy_identity
from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy
from strategies.adaptive_trend_pullback.config import AdaptiveTrendPullbackConfig
from services.shadow_router import ShadowRouter, FrozenSignalAdapter
from services.shadow_observation_store import ShadowObservationStore

T = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def candidate(quality=75):
    strategy = AdaptiveTrendPullbackStrategy("XRPUSDT", config=AdaptiveTrendPullbackConfig(quality_minimum=quality))
    identity = observed_strategy_identity(strategy, strategy_id=strategy.name, timeframe="5m")
    return {"candidate_id": "candidate-" + str(quality), "owner_id": "alice", "strategy_id": strategy.name,
            "strategy_version": identity["strategy_version"], "config_fingerprint": identity["strategy_config_hash"],
            "frozen_config": identity["configuration"], "symbol": "XRPUSDT", "timeframe": "5m",
            "venue": "binance_usdm", "market_type": "perpetual", "validation_run_ids": [],
            "source_hash": identity["source_hash"]}


def frame(*, direction=1):
    context = {}
    for tf, count in (("5m", 180), ("15m", 90), ("1h", 90), ("4h", 80)):
        rows = []
        for i in range(count):
            centre = 200 + direction * i * .45 + math.sin(i / 3) * 1.3
            o, c = centre - direction * .18, centre + direction * .18
            rows.append(Bar(T - timedelta(seconds=SECONDS[tf] * (count - i)), o, max(o, c) + 1, min(o, c) - 1, c, 100 + i))
        context[tf] = rows
    pullback = context["15m"]; anchor = pullback[-5].close
    for offset, i in enumerate(range(len(pullback) - 4, len(pullback))):
        c = anchor - direction * .08 * offset
        pullback[i] = Bar(pullback[i].timestamp, c + direction * .05, c + .7, c - .7, c, 90)
    entries = context["5m"]; boundary = max(b.high for b in entries[-9:-1]) if direction > 0 else min(b.low for b in entries[-9:-1])
    c, o = boundary + direction, boundary - direction * .5
    entries[-1] = Bar(entries[-1].timestamp, o, max(o, c) + .2, min(o, c) - .2, c, 500)
    return {"venue": "binance_usdm", "market_type": "perpetual", "symbol": "XRPUSDT", "timeframe": "5m",
            "source": "BINANCE_USDM_PUBLIC", "finalized": True, "connected": True,
            "available_at": T.isoformat(), "stream_observed_at": T.isoformat(), "decision_time": T.isoformat(),
            "context": context, "htf_health": {"1h": True, "15m": True, "4h": True}}


@pytest.fixture
def setup(tmp_path):
    raw = ShadowResearchStore(tmp_path / "shadow.sqlite")
    store = ShadowObservationStore(raw)
    cand = candidate()
    clock = [T - timedelta(seconds=1)]
    router = ShadowRouter(store, candidate_reader=lambda cid, owner: deepcopy(cand) if cid == cand["candidate_id"] and owner == "alice" else None,
                          clock=lambda: clock[0])
    request = {"candidate_id": cand["candidate_id"], "idempotency_key": "shadow-test-1", "authorization_reason": "Human reviewed shadow observation",
               "lookback_spec": FrozenSignalAdapter(cand).lookback, "authorize": True}
    registration = router.register(request, owner_id="alice")
    router.action(registration["registration_id"], "VALIDATE", owner_id="alice", reason="reviewed")
    router.action(registration["registration_id"], "START", owner_id="alice", reason="observe only")
    clock[0] = T + timedelta(seconds=1)
    return router, store, raw, registration, clock, request


def test_explicit_authorization_frozen_identity_and_idempotency(setup):
    router, store, _, reg, _, request = setup
    assert router.register(request, owner_id="alice")["registration_id"] == reg["registration_id"]
    assert store.get(reg["registration_id"], owner_id="bob") is None
    with pytest.raises(KeyError): router.action(reg["registration_id"], "START", owner_id="bob", reason="forged")
    with pytest.raises(ValueError): router.register({**request, "authorization_reason": "changed"}, owner_id="alice")
    with pytest.raises(sqlite3.IntegrityError):
        raw = store._store
        with raw._lock, raw._db: raw._db.execute("UPDATE shadow_registrations SET frozen_json='{}'")


@pytest.mark.parametrize("field,value,reason", [("authorize", False, "AUTHORIZATION_REQUIRED"), ("lookback_spec", {}, "LOOKBACK_UNVERIFIED")])
def test_registration_gates(setup, field, value, reason):
    router, _, _, _, _, request = setup
    reg = router.register({**request, "idempotency_key": "shadow-other", field: value}, owner_id="alice")
    result = router.action(reg["registration_id"], "VALIDATE", owner_id="alice", reason="review")
    assert result["status"] == "BLOCKED"
    assert reason in result["blocking_reasons"]
    with pytest.raises(ValueError): router.action(reg["registration_id"], "START", owner_id="alice", reason="bypass")


@pytest.mark.parametrize("direction,expected", [(1, "SIGNAL_LONG"), (-1, "SIGNAL_SHORT")])
def test_real_strategy_signals_remain_unmodified_and_no_orders(setup, direction, expected):
    router, store, raw, reg, _, _ = setup
    market = frame(direction=direction)
    result = router.evaluate(reg["registration_id"], market, owner_id="alice")
    # The adapter invokes the existing strategy's on_bar, with no broker.
    direct = FrozenSignalAdapter(candidate()).evaluate(deepcopy(market["context"]), T)
    assert result["status"] == expected
    assert result["strategy_decision"] == direct["strategy_decision"]
    assert result["execution_mode"] == "shadow_observation"
    assert raw.table_counts()["shadow_orders"] == raw.table_counts()["shadow_fills"] == 0
    assert store.decisions(reg["registration_id"], owner_id="alice")[0]["decision_id"] == result["decision_id"]


@pytest.mark.parametrize("change,reason", [({"finalized": False}, "INVALID_CANDLE"), ({"connected": False}, "FEED_DISCONNECTED"),
    ({"available_at": (T + timedelta(minutes=1)).isoformat()}, "DATA_NOT_AVAILABLE"),
    ({"stream_observed_at": (T - timedelta(minutes=1)).isoformat()}, "STALE_CANDLES"),
    ({"venue": "spot"}, "SOURCE_MISMATCH")])
def test_live_data_gates_are_journalled(setup, change, reason):
    router, store, _, reg, _, _ = setup
    result = router.evaluate(reg["registration_id"], {**frame(), **change}, owner_id="alice")
    assert result["status"].startswith("BLOCKED_")
    assert reason in result["rejection_reasons"]
    assert store.analytics(reg["registration_id"], owner_id="alice")["signal_count"] == 0


@pytest.mark.parametrize("missing,reason", [("1h", "MISSING_CONTEXT"), ("5m", "MISSING_CANDLE")])
def test_missing_context_fail_closed(setup, missing, reason):
    router, _, _, reg, _, _ = setup
    market = frame(); market["context"][missing] = []
    result = router.evaluate(reg["registration_id"], market, owner_id="alice")
    assert reason in result["rejection_reasons"]


def test_short_history_and_stale_htf_are_blocked(setup):
    router, _, _, reg, _, _ = setup
    market = frame(); market["context"]["1h"] = market["context"]["1h"][-2:]
    assert router.evaluate(reg["registration_id"], market, owner_id="alice")["status"] == "BLOCKED_WARMUP"


def test_gap_and_conflicting_duplicate_fail_closed(setup):
    router, _, _, reg, _, _ = setup
    market = frame(); market["context"]["5m"].pop(-3)
    result = router.evaluate(reg["registration_id"], market, owner_id="alice")
    assert "DATA_GAP" in result["rejection_reasons"]


def test_duplicate_replay_and_revised_candle_do_not_rewrite_decisions(setup):
    router, store, _, reg, _, _ = setup
    market = frame(); first = router.evaluate(reg["registration_id"], market, owner_id="alice")
    for _ in range(3): assert router.evaluate(reg["registration_id"], market, owner_id="alice")["decision_id"] == first["decision_id"]
    market["context"]["5m"][-1] = replace(market["context"]["5m"][-1], volume=999)
    with pytest.raises(ValueError, match="CANDLE_REVISION_CONFLICT"): router.evaluate(reg["registration_id"], market, owner_id="alice")
    assert len(store.decisions(reg["registration_id"], owner_id="alice")) == 1
    assert any(e["action"] == "CANDLE_REVISION_CONFLICT" for e in store.events(reg["registration_id"], owner_id="alice"))


def test_duplicate_transport_health_change_does_not_invent_a_candle_revision(setup):
    router, store, _, reg, clock, _ = setup
    market = frame(); first = router.evaluate(reg["registration_id"], market, owner_id="alice")
    clock[0] += timedelta(seconds=20); market["htf_health"]["4h"] = False
    assert router.evaluate(reg["registration_id"], market, owner_id="alice") == first
    assert store.analytics(reg["registration_id"], owner_id="alice")["conflicting_events"] == 0
    assert any(e["action"] == "REPLAY_IGNORED" for e in store.events(reg["registration_id"], owner_id="alice"))


def test_future_context_never_changes_decision(setup):
    router, _, _, reg, _, _ = setup
    market = frame(); first = router.evaluate(reg["registration_id"], market, owner_id="alice")
    for tf, bars in market["context"].items():
        bars.append(Bar(T + timedelta(days=1), 1000, 2000, 500, 1800, 10000))
    assert router.evaluate(reg["registration_id"], market, owner_id="alice")["decision_id"] == first["decision_id"]


def test_pause_resume_and_no_backfilled_observations(setup):
    router, store, _, reg, clock, _ = setup
    router.action(reg["registration_id"], "PAUSE", owner_id="alice", reason="pause")
    assert router.evaluate(reg["registration_id"], frame(), owner_id="alice") is None
    clock[0] += timedelta(minutes=10)
    router.action(reg["registration_id"], "RESUME", owner_id="alice", reason="resume")
    assert router.evaluate(reg["registration_id"], frame(), owner_id="alice") is None
    assert store.decisions(reg["registration_id"], owner_id="alice") == []
    assert any(e["action"] == "OBSERVATION_GAP" for e in store.events(reg["registration_id"], owner_id="alice"))


@pytest.mark.parametrize("action", ["PLACE_ORDER", "CANCEL_ORDER", "MODIFY_POSITION", "MODIFY_BALANCE", "CHANGE_LEVERAGE", "CHANGE_RISK_LIMIT", "CHANGE_ACTIVE_STRATEGY", "PROMOTE_CHAMPION", "ENABLE_LIVE_TRADING"])
def test_forbidden_capabilities_rejected(setup, action):
    router, _, _, reg, _, _ = setup
    with pytest.raises(PermissionError): router.action(reg["registration_id"], action, owner_id="alice", reason="forged")


def test_matched_comparison_missing_active_is_unknown(setup):
    router, _, _, reg, _, _ = setup
    router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    result = router.comparison(reg["registration_id"], owner_id="alice")
    assert result["matched_opportunities"] == 0 and result["agreement_rate"] is None
    assert result["active_state"] == "NO_DECISION_RECORDED"


def test_lease_expiration_fences_old_worker_and_records_gap(setup):
    _, store, _, reg, _, _ = setup
    first = store.claim(reg["registration_id"], "worker-1", now=T.timestamp(), lease_seconds=10)
    assert store.claim(reg["registration_id"], "worker-2", now=T.timestamp(), lease_seconds=10) is None
    second = store.claim(reg["registration_id"], "worker-2", now=T.timestamp() + 11, lease_seconds=10)
    assert second["lease_token"] != first["lease_token"]
    assert not store.heartbeat(reg["registration_id"], first["lease_token"], now=T.timestamp() + 11, lease_seconds=10)
    assert store.heartbeat(reg["registration_id"], second["lease_token"], now=T.timestamp() + 11, lease_seconds=10)


def test_store_migration_is_additive_and_repeatable(setup):
    _, store, raw, reg, _, _ = setup
    ShadowObservationStore(raw)
    assert store.get(reg["registration_id"], owner_id="alice")
    assert raw._db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert raw._db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_signal_free_opportunity_is_still_journalled(setup):
    router, store, _, reg, _, _ = setup
    market = frame()
    rows = market["context"]["5m"]
    rows[-1] = replace(rows[-1], open=rows[-2].close, close=rows[-2].close, high=rows[-2].close + .1, low=rows[-2].close - .1, volume=1)
    assert router.evaluate(reg["registration_id"], market, owner_id="alice")["status"] == "NO_SIGNAL"
    assert store.analytics(reg["registration_id"], owner_id="alice")["eligible_opportunities"] == 1


@pytest.mark.parametrize("mutation,reason", [
    (lambda f: f["htf_health"].update({"1h": False}), "STALE_HTF_CANDLE"),
    (lambda f: f["context"]["1h"].pop(), "STALE_HTF_CANDLE"),
    (lambda f: f["context"]["5m"].append(replace(f["context"]["5m"][-1], volume=123)), "CONFLICTING_CANDLE"),
    (lambda f: f["context"]["5m"].reverse(), "INVALID_CANDLE_ORDER"),
    (lambda f: f["context"]["5m"].__setitem__(-1, replace(f["context"]["5m"][-1], close=float("nan"))), "INVALID_CANDLE"),
])
def test_invalid_context_is_not_silently_repaired(setup, mutation, reason):
    router, _, _, reg, _, _ = setup
    market = frame(); mutation(market)
    assert reason in router.evaluate(reg["registration_id"], market, owner_id="alice")["rejection_reasons"]


def test_empty_optional_context_keeps_existing_signal(setup):
    router, _, _, reg, _, _ = setup
    market = frame(); market["context"]["4h"] = []
    result = router.evaluate(reg["registration_id"], market, owner_id="alice")
    assert result["status"] == "SIGNAL_LONG"
    assert result["quality_warnings"] == ["OPTIONAL_CONTEXT_UNAVAILABLE:4h"]


def test_evaluator_error_is_evidence_not_a_signal(setup, monkeypatch):
    router, store, _, reg, _, _ = setup
    def broken(*args): raise RuntimeError("private diagnostic detail must not be persisted")
    monkeypatch.setattr(FrozenSignalAdapter, "evaluate", broken)
    result = router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    assert result["status"] == "EVALUATION_ERROR"
    assert result["rejection_reasons"] == ["EVALUATOR_ERROR:RuntimeError"]
    assert store.analytics(reg["registration_id"], owner_id="alice")["evidence_completeness"] == "PARTIAL"


def test_pause_during_evaluation_fences_completion(setup, monkeypatch):
    router, store, _, reg, _, _ = setup
    evaluate = FrozenSignalAdapter.evaluate
    def pause(adapter, context, cutoff):
        result = evaluate(adapter, context, cutoff)
        router.action(reg["registration_id"], "PAUSE", owner_id="alice", reason="cancel during computation")
        return result
    monkeypatch.setattr(FrozenSignalAdapter, "evaluate", pause)
    with pytest.raises(ValueError, match="STALE_SHADOW_ATTEMPT"):
        router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    assert store.decisions(reg["registration_id"], owner_id="alice") == []


def test_actual_stale_worker_result_is_rejected(setup):
    router, store, _, reg, clock, _ = setup
    old = store.claim(reg["registration_id"], "old", now=T.timestamp(), lease_seconds=1)
    store.claim(reg["registration_id"], "new", now=T.timestamp()+2, lease_seconds=60)
    clock[0] = T + timedelta(seconds=3)
    with pytest.raises(ValueError, match="STALE_SHADOW_ATTEMPT"):
        router.evaluate(reg["registration_id"], frame(), owner_id="alice", lease_token=old["lease_token"])
    assert store.decisions(reg["registration_id"], owner_id="alice") == []


def test_two_database_connections_race_for_one_registration(setup):
    import threading
    _, store, raw, reg, _, _ = setup
    other_raw = ShadowResearchStore(raw.path)
    other = ShadowObservationStore(other_raw)
    barrier = threading.Barrier(2); claims = []
    def claim(db, worker):
        barrier.wait(); claims.append(db.claim(reg["registration_id"], worker, now=T.timestamp()))
    threads = [threading.Thread(target=claim, args=(db, str(i))) for i, db in enumerate((store, other))]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert len(claims) == 2 and sum(c is not None for c in claims) == 1


def test_claim_response_never_inherits_replacement_workers_token(setup, monkeypatch):
    from contextlib import contextmanager
    _, store, _, reg, _, _ = setup
    transaction = store.transaction
    armed = [True]; replacement = []
    @contextmanager
    def race_after_commit():
        with transaction() as db: yield db
        if armed[0]:
            armed[0] = False
            replacement.append(store.claim(reg["registration_id"], "replacement", now=T.timestamp()+11, lease_seconds=60))
    monkeypatch.setattr(store, "transaction", race_after_commit)
    original = store.claim(reg["registration_id"], "original", now=T.timestamp(), lease_seconds=10)
    assert original["lease_owner"] == "original"
    assert original["lease_token"] != replacement[0]["lease_token"]


def test_observations_and_audit_have_database_immutability(setup):
    router, _, raw, reg, _, _ = setup
    router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    for sql in ("DELETE FROM shadow_observations", "UPDATE shadow_observations SET status='NO_SIGNAL'", "DELETE FROM shadow_observation_events"):
        with pytest.raises(sqlite3.IntegrityError):
            with raw._db: raw._db.execute(sql)


def test_matching_active_evidence_and_paused_missing_are_distinct(setup):
    router, _, _, reg, _, _ = setup
    router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    router.active_reader = lambda row, owner: {**row, "status": "NO_SIGNAL"}
    compared = router.comparison(reg["registration_id"], owner_id="alice")
    assert compared["matched_opportunities"] == 1 and compared["agreement_rate"] == 0
    router.active_reader = lambda row, owner: {**row, "status": "ACTIVE_STRATEGY_PAUSED"}
    assert router.comparison(reg["registration_id"], owner_id="alice")["matched_opportunities"] == 0
    router.active_reader = lambda row, owner: {**row, "candle_hash": "different"}
    assert router.comparison(reg["registration_id"], owner_id="alice")["matched_opportunities"] == 0


@pytest.mark.parametrize("field,value,reason", [("strategy_version", "future", "STRATEGY_VERSION_MISMATCH"), ("source_hash", "0"*64, "STRATEGY_SOURCE_CHANGED"), ("config_fingerprint", "0"*64, "CONFIGURATION_IDENTITY_MISMATCH"), ("frozen_config", {}, "FULL_EFFECTIVE_CONFIGURATION_REQUIRED")])
def test_frozen_identity_cannot_be_guessed(field, value, reason):
    cand = candidate(); cand[field] = value
    with pytest.raises(ValueError, match=reason): FrozenSignalAdapter(cand)


def test_variants_have_isolated_strategy_state():
    first, second = FrozenSignalAdapter(candidate()), FrozenSignalAdapter(candidate(80))
    market = frame()
    first.evaluate(market["context"], T)
    assert second.strategy.bars == []
    assert first.strategy is not second.strategy
    assert first.candidate["config_fingerprint"] != second.candidate["config_fingerprint"]


def test_financial_ledger_and_order_methods_are_untouched(setup, tmp_path, monkeypatch):
    from test_strategy_evidence_runtime import runtime
    ledger, journal, decisions, paper, pipeline, _ = runtime(tmp_path)
    before = ledger.get_authoritative_evidence_snapshot()
    journal_before = journal.list(); decisions_before = decisions.list()
    calls = []
    def forbidden(*args, **kwargs): calls.append(args); raise AssertionError("shadow attempted execution")
    monkeypatch.setattr(paper, "process_quote", forbidden)
    monkeypatch.setattr(pipeline, "process", forbidden)
    router, _, raw, reg, clock, _ = setup
    router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    router.action(reg["registration_id"], "PAUSE", owner_id="alice", reason="pause")
    clock[0] += timedelta(minutes=10)
    router.action(reg["registration_id"], "RESUME", owner_id="alice", reason="resume")
    router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    router.action(reg["registration_id"], "STOP", owner_id="alice", reason="stop")
    assert calls == [] and ledger.get_authoritative_evidence_snapshot() == before
    assert journal.list() == journal_before and decisions.list() == decisions_before
    assert raw.table_counts()["shadow_orders"] == raw.table_counts()["shadow_fills"] == 0


@pytest.mark.parametrize("strategy_id", ["price_action_rejection", "price_action_flip_retest"])
def test_price_action_reuses_native_engine_with_causal_bootstrap(strategy_id):
    from strategies.price_action_rejection import PriceActionRejectionStrategy, PriceActionFlipRetestStrategy
    from services.native_price_action import PriceActionConfig
    cls = PriceActionRejectionStrategy if strategy_id == "price_action_rejection" else PriceActionFlipRetestStrategy
    strategy = cls("XRPUSDT", config=PriceActionConfig(symbol="XRPUSDT"))
    identity = observed_strategy_identity(strategy, strategy_id=strategy_id, timeframe="5m")
    cand = {**candidate(), "strategy_id": strategy_id, "strategy_version": identity["strategy_version"], "frozen_config": identity["configuration"], "config_fingerprint": identity["strategy_config_hash"], "source_hash": identity["source_hash"]}
    first = FrozenSignalAdapter(cand); second = FrozenSignalAdapter(cand)
    assert first.lookback["required_candles"]["5m"] == 400
    context = frame()["context"]
    before = [Bar(T-timedelta(minutes=5*(420-i)), 200+i*.01, 202+i*.01, 198+i*.01, 201+i*.01, 100) for i in range(240)]
    context["5m"] = before + context["5m"]
    result = first.evaluate(context, T)
    future = deepcopy(context)
    future["1h"].append(Bar(T + timedelta(hours=1), 1, 100000, .1, 90000, 1))
    assert second.evaluate(future, T) == result
    assert result["signal_strength"] is None
    assert first.strategy._engine is not second.strategy._engine


def test_reviewed_current_source_can_register_existing_candidate_without_historical_attestation(setup):
    router, _, _, _, _, request = setup
    original = candidate(); original.pop("source_hash")
    router.candidate_reader = lambda cid, owner: deepcopy(original) if owner == "alice" else None
    requirements = router.requirements(original["candidate_id"], owner_id="alice")
    reg = router.register({**request, "idempotency_key": "reviewed-source-new", "reviewed_source_hash": requirements["reviewed_source_hash"]}, owner_id="alice")
    assert router.validate(reg)["valid"] is True
    assert reg["source_identity_basis"] == "HUMAN_REVIEWED_CURRENT_ADAPTER_NOT_HISTORICAL_ATTESTATION"


def test_resource_quota_and_retention_limit_fail_closed(setup):
    router, store, _, reg, _, request = setup
    with pytest.raises(ValueError, match="SHADOW_RESOURCE_QUOTA"):
        store.create({**candidate(), "registration_id": "another", "owner_id": "alice", "registered_at": T.isoformat()}, {**request, "idempotency_key": "quota-test"}, max_per_owner=1)
    router.policy = {**router.policy, "max_observations": 0}
    with pytest.raises(ValueError, match="SHADOW_RETENTION_LIMIT"):
        router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    assert store.decisions(reg["registration_id"], owner_id="alice") == []


def test_observation_duration_stops_without_replay(setup):
    router, store, _, reg, clock, _ = setup
    clock[0] += timedelta(days=31)
    assert router.evaluate(reg["registration_id"], {**frame(), "decision_time": clock[0].isoformat()}, owner_id="alice") is None
    assert store.get(reg["registration_id"], owner_id="alice")["status"] == "STOPPED"


def test_migration_preserves_representative_legacy_shadow_rows(tmp_path):
    raw = ShadowResearchStore(tmp_path / "old-shadow.sqlite")
    legacy = raw.record_decision(engine="PA", account_id="old-observer", strategy_id="PA1_SR_REJECTION", strategy_version="1.1.0", config_hash="legacy-config", candle_id="legacy-candle", action_class="OBSERVE", direction=None, blocker="NO_SETUP", decision_timestamp=T, snapshot_lineage="legacy-snapshot", context={"fixture": True})
    before = [tuple(r) for r in raw._db.execute("SELECT * FROM shadow_decisions")]
    ShadowObservationStore(raw); ShadowObservationStore(raw)
    assert [tuple(r) for r in raw._db.execute("SELECT * FROM shadow_decisions")] == before
    assert legacy["config_hash"] == "legacy-config"
    assert raw._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_delayed_worker_start_records_observation_gap(setup):
    _, store, _, reg, _, _ = setup
    store.claim(reg["registration_id"], "late-worker", now=(T + timedelta(minutes=20)).timestamp())
    assert any(e["payload"].get("reason") == "DELAYED_INITIAL_WORKER_START" for e in store.events(reg["registration_id"], owner_id="alice"))


def test_latency_limit_discards_signal_but_preserves_error_evidence(setup):
    router, _, _, reg, _, _ = setup
    router.policy = {**router.policy, "max_latency_seconds": 0}
    result = router.evaluate(reg["registration_id"], frame(), owner_id="alice")
    assert result["status"] == "EVALUATION_ERROR" and result["signal_direction"] is None
    assert result["rejection_reasons"] == ["EVALUATION_LATENCY_LIMIT"]
