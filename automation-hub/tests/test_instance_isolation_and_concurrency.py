"""User isolation, lifecycle atomicity, and exactly one execution owner.

Every test here pins a defect found in the production audit. If a future
change reintroduces one, this file fails rather than the platform quietly
going back to trusting an instance_id, swallowing a Start, or letting two
processes trade the same paper account.
"""
import os
import threading
import time
from datetime import datetime, timezone

import pytest

from data.ledger import DuplicateOrderIntent, SqliteLedger
from services.tenancy import OWNER_TENANT
from services.trading_instances import (
    TradingInstanceManager, WorkerLeaseError,
)


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


@pytest.fixture
def fast_worker(monkeypatch):
    """A worker whose thread never runs, so a test owns all the timing."""
    from services.auto_engine import AutoStrategyEngine
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1])


def _manager(path=":memory:"):
    ledger = SqliteLedger(path)
    return ledger, TradingInstanceManager(ledger, strategy_factory=_factory,
                                          live=False, live_poll_s=60)


def _create(manager, symbol="BTCUSDT", owner_id=OWNER_TENANT):
    return manager.create(symbol=symbol, strategy_key="brain",
                          strategy_label="Decision Brain", strategy_version="v1",
                          timeframe="5m", risk_per_trade_pct=0.005,
                          capital_allocation=1_000, owner_id=owner_id)


# ------------------------------------------------------------- ownership
def test_an_instance_id_alone_does_not_grant_access():
    _ledger, manager = _manager()
    mine = _create(manager, "BTCUSDT", owner_id="user-a")
    theirs = _create(manager, "ETHUSDT", owner_id="user-b")

    assert manager.instance_for(mine.id, "user-a").id == mine.id
    # Knowing the id is not enough, and the refusal looks exactly like "no
    # such instance" so it cannot be used to enumerate other accounts' ids.
    with pytest.raises(KeyError):
        manager.instance_for(theirs.id, "user-a")
    with pytest.raises(KeyError):
        manager.instance_for("does-not-exist", "user-a")


def test_every_lifecycle_action_verifies_ownership(fast_worker):
    _ledger, manager = _manager()
    theirs = _create(manager, "ETHUSDT", owner_id="user-b")

    for action in (lambda: manager.start(theirs.id, owner_id="user-a"),
                   lambda: manager.stop(theirs.id, owner_id="user-a"),
                   lambda: manager.pause(theirs.id, owner_id="user-a"),
                   lambda: manager.resume(theirs.id, owner_id="user-a"),
                   lambda: manager.delete(theirs.id, owner_id="user-a"),
                   lambda: manager.update_configuration(theirs.id, owner_id="user-a",
                                                        capital_allocation=50)):
        with pytest.raises(KeyError):
            action()
    assert theirs.id in manager._instances       # untouched


def test_listing_and_snapshots_are_owner_scoped(fast_worker):
    _ledger, manager = _manager()
    mine = _create(manager, "BTCUSDT", owner_id="user-a")
    theirs = _create(manager, "ETHUSDT", owner_id="user-b")

    rows, _positions, _trades = manager.snapshot(owner_id="user-a")

    assert [row["id"] for row in rows] == [mine.id]
    assert theirs.id not in {row["id"] for row in rows}
    assert {item.id for item in manager.owned_instances("user-b")} == {theirs.id}


def test_api_routes_refuse_another_owners_instance(monkeypatch, fast_worker):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from routers import instances as instance_api

    _ledger, manager = _manager()
    theirs = _create(manager, "ETHUSDT", owner_id="user-b")
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)
    monkeypatch.setattr(instance_api._wa, "_check_secret", lambda _s: None)
    monkeypatch.setattr(instance_api, "_owner", lambda _request: "user-a")

    for call in (lambda: instance_api.instance_detail(theirs.id),
                 lambda: instance_api.instance_status(theirs.id),
                 lambda: instance_api.instance_positions(theirs.id),
                 lambda: instance_api.instance_orders(theirs.id),
                 lambda: instance_api.instance_metrics(theirs.id),
                 lambda: instance_api.instance_trades(theirs.id),
                 lambda: instance_api.instance_logs(theirs.id),
                 lambda: instance_api.instance_action(theirs.id, "start"),
                 lambda: instance_api.delete_instance(theirs.id),
                 lambda: instance_api.instance_reconciliation(theirs.id)):
        with pytest.raises(HTTPException) as refused:
            call()
        # 404, never 403: a different answer would confirm the id exists.
        assert refused.value.status_code == 404


def test_single_owner_deployments_keep_seeing_every_instance(monkeypatch):
    """The ownership seam must change nothing while multi-user is off."""
    from routers import instances as instance_api

    monkeypatch.delenv("HUB_MULTI_USER", raising=False)
    assert instance_api._owner(object()) == OWNER_TENANT

    _ledger, manager = _manager()
    created = _create(manager)
    assert created.owner_id == OWNER_TENANT
    rows, _p, _t = manager.snapshot(owner_id=OWNER_TENANT)
    assert [row["id"] for row in rows] == [created.id]


# ----------------------------------------------------------- concurrency
def test_a_start_racing_a_stop_is_not_silently_swallowed(monkeypatch):
    """The API reported state=running while the instance ended stopped."""
    from services.auto_engine import AutoStrategyEngine

    real_stop = AutoStrategyEngine.stop

    def slow_stop(self, reason="stopped"):
        time.sleep(0.4)          # stands in for joining the worker thread
        return real_stop(self, reason)

    monkeypatch.setattr(AutoStrategyEngine, "stop", slow_stop)
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1])

    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    reported = {}

    def do_stop():
        manager.stop(instance.id)

    def do_start():
        time.sleep(0.15)          # land inside the old unlocked window
        row = manager.start(instance.id)
        reported["state"] = row.state
        reported["desired"] = row.desired_running

    threads = [threading.Thread(target=do_stop), threading.Thread(target=do_start)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    persisted = manager._instances[instance.id]
    running = bool(manager._runtime.get(instance.id)
                   and manager._runtime[instance.id][0].running)
    # Whichever action wins, what the caller was told must match what happened.
    assert (reported["state"] == "running") == (persisted.state == "running")
    assert (persisted.state == "running") == running


def test_concurrent_starts_create_exactly_one_worker(fast_worker):
    _ledger, manager = _manager()
    instance = _create(manager)
    results, errors = [], []

    def start():
        try:
            results.append(manager.start(instance.id))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(results) == 6                       # idempotent, all succeed
    assert len({id(manager._runtime[instance.id][0])}) == 1
    ownership = manager.worker_ownership(instance.id)
    assert ownership["held"] and ownership["this_process"]


def test_delete_while_the_worker_is_active_is_refused(fast_worker):
    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)

    with pytest.raises(ValueError, match="Stop the Trading Instance"):
        manager.delete(instance.id)
    assert instance.id in manager._instances


# ----------------------------------------------- one execution owner
def test_a_second_process_cannot_run_the_same_instance(tmp_path, fast_worker):
    path = str(tmp_path / "shared.db")
    _ledger_a, first = _manager(path)
    instance = _create(first)
    first.start(instance.id)

    _ledger_b, second = _manager(path)          # a second container, same database
    with pytest.raises(WorkerLeaseError, match="already owned by worker"):
        second.start(instance.id)

    ownership = second.worker_ownership(instance.id)
    assert ownership["held"] and ownership["this_process"] is False
    assert ownership["process_id"] == os.getpid()
    assert instance.id not in second._runtime


def test_ownership_is_handed_over_on_a_clean_stop(tmp_path, fast_worker):
    path = str(tmp_path / "shared.db")
    _a, first = _manager(path)
    instance = _create(first)
    first.start(instance.id)
    first.stop(instance.id)

    _b, second = _manager(path)
    second.start(instance.id)                    # the successor may take over
    assert second.worker_ownership(instance.id)["this_process"] is True


def test_a_graceful_shutdown_releases_every_lease(tmp_path, fast_worker):
    path = str(tmp_path / "shared.db")
    _a, first = _manager(path)
    instance = _create(first)
    first.start(instance.id)
    first.shutdown()

    assert first.worker_ownership(instance.id)["held"] is False


def test_an_expired_lease_may_be_taken_over(tmp_path, fast_worker):
    path = str(tmp_path / "shared.db")
    _a, first = _manager(path)
    instance = _create(first)
    first.worker_lease_ttl_s = 30
    first.start(instance.id)

    _b, second = _manager(path)
    # Rewind the expiry the way a crashed process's lease ages out.
    second.store.claim_worker_lease(
        instance.id, worker_id=first._worker_id, process_id=1, host="old",
        ttl_seconds=-1)
    second.start(instance.id)

    assert second.worker_ownership(instance.id)["this_process"] is True


def test_an_unreadable_lease_expiry_fails_closed(tmp_path):
    path = str(tmp_path / "shared.db")
    _a, manager = _manager(path)
    instance = _create(manager)
    manager.store.claim_worker_lease(
        instance.id, worker_id="other-worker", process_id=99, host="elsewhere",
        ttl_seconds=60)
    with manager.ledger._lock:
        manager.ledger._c.execute(
            "UPDATE instance_worker_leases SET lease_expires_at='not-a-timestamp'")
        manager.ledger._c.commit()

    with pytest.raises(WorkerLeaseError, match="unreadable"):
        manager.start(instance.id)


def test_a_worker_that_loses_its_lease_stops_itself(tmp_path, fast_worker):
    from services.instance_supervisor import InstanceSupervisor

    path = str(tmp_path / "shared.db")
    _a, manager = _manager(path)
    instance = _create(manager)
    manager.start(instance.id)
    # The supervisor only renews for a worker it considers alive, which means a
    # live thread. Give the fake engine one that simply parks.
    engine = manager._runtime[instance.id][0]
    idle = threading.Event()
    engine._thread = threading.Thread(target=idle.wait, daemon=True)
    engine._thread.start()
    # Somebody else took ownership while this worker was running. Written
    # directly, because claim_worker_lease correctly refuses to steal a live
    # lease -- what is under test is what this worker does once it has lost one.
    with manager.ledger._lock:
        manager.ledger._c.execute(
            "UPDATE instance_worker_leases SET worker_id='another-process', "
            "process_id=4242, host='elsewhere' WHERE instance_id=?", (instance.id,))
        manager.ledger._c.commit()

    report = InstanceSupervisor(manager, interval_s=60).sweep()

    assert [row["action"] for row in report] == ["lease_lost"]
    # The runtime is gone...
    assert instance.id not in manager._runtime
    # ...but the operator's intent is not. Clearing it here would write "no
    # longer wanted" into the row every process reads, so nothing would
    # restart this instance once the split brain resolved.
    assert manager._instances[instance.id].desired_running is True
    idle.set()


# ------------------------------------------------------ order idempotency
def test_the_database_refuses_a_second_order_for_one_idempotency_key():
    ledger = SqliteLedger(":memory:")
    assert ledger.duplicate_order_constraint["enforced"] is True
    key = "auto:instance-one:BTCUSDT:5m:2026-09-13T10:00:00+00:00:buy"

    ledger.insert_webhook_event(alert_id=key, symbol="BTCUSDT", side="BUY",
                                entry=100, stop=95, payload={}, status="accepted",
                                instance_id="instance-one")

    with pytest.raises(DuplicateOrderIntent):
        ledger.insert_webhook_event(alert_id=key, symbol="BTCUSDT", side="BUY",
                                    entry=100, stop=95, payload={}, status="accepted",
                                    instance_id="instance-one")

    # A different instance on the same pair and candle is independent...
    ledger.insert_webhook_event(alert_id=key, symbol="BTCUSDT", side="BUY",
                                entry=100, stop=95, payload={}, status="accepted",
                                instance_id="instance-two")
    # ...and one order legitimately moves through more than one status.
    ledger.insert_webhook_event(alert_id=key, symbol="BTCUSDT", side="BUY",
                                entry=100, stop=95, payload={}, status="pending",
                                instance_id="instance-one")


def test_a_replayed_candle_cannot_create_a_second_order():
    """The reconnect case: the same candle evaluated twice."""
    from services.auto_engine import AutoStrategyEngine

    ledger = SqliteLedger(":memory:")
    engine = AutoStrategyEngine.__new__(AutoStrategyEngine)
    engine.live, engine.timeframe, engine.ledger = True, "5m", ledger
    engine.ledger.instance_id = "instance-one"
    stamp = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)

    first = AutoStrategyEngine._auto_execution_id(engine, "BTCUSDT", stamp, "buy")
    second = AutoStrategyEngine._auto_execution_id(engine, "BTCUSDT", stamp, "buy")

    assert first == second                      # deterministic, not a counter
    assert "instance-one" in first and "2026-09-13T10:00:00" in first

    ledger.insert_webhook_event(alert_id=first, symbol="BTCUSDT", side="BUY",
                                entry=1, stop=0.5, payload={}, status="accepted",
                                instance_id="instance-one")
    with pytest.raises(DuplicateOrderIntent):
        ledger.insert_webhook_event(alert_id=second, symbol="BTCUSDT", side="BUY",
                                    entry=1, stop=0.5, payload={}, status="accepted",
                                    instance_id="instance-one")


def test_the_pipeline_reports_a_duplicate_instead_of_crashing_the_worker():
    from services.controls import TradingControl
    from execution.paper_engine import PaperExecutionEngine
    from services.signal_pipeline import SignalPipeline
    from services.trading_instances import InstanceLedger

    ledger = SqliteLedger(":memory:")
    scoped = InstanceLedger(ledger, "instance-one")
    pipeline = SignalPipeline(scoped, PaperExecutionEngine(scoped, 1_000),
                              TradingControl(), equity=1_000)
    key = "auto:instance-one:BTCUSDT:5m:2026-09-13T10:00:00+00:00:buy"
    scoped.insert_webhook_event(alert_id=key, symbol="BTCUSDT", side="BUY",
                                entry=100, stop=95, payload={}, status="accepted")

    result = pipeline.process({"alert_id": key, "symbol": "BTCUSDT", "side": "BUY",
                               "entry": 100.0, "stop": 95.0})

    # Either the application guard or the durable constraint catches it; what
    # matters is that the worker reports a duplicate rather than raising.
    assert result.accepted is False
    assert result.stage in ("dedup", "duplicate")


def test_a_failed_configuration_rebuild_leaves_the_instance_still_desired(monkeypatch, fast_worker):
    """An edit is not a decision to stop running.

    The rebuild path used stop(), which clears desired_running and releases the
    lease -- so an edit made during a brief venue outage left the instance
    un-desired, unsupervised, and dark until somebody noticed.
    """
    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    original_start = manager.start
    calls = {"n": 0}

    def failing_start(instance_id, **kwargs):
        calls["n"] += 1
        if calls["n"] > 0:
            raise RuntimeError("Binance USD-M feed unavailable")
        return original_start(instance_id, **kwargs)

    monkeypatch.setattr(manager, "start", failing_start)
    with pytest.raises(RuntimeError):
        manager.update_configuration(instance.id, timeframe="15m")

    assert manager._instances[instance.id].desired_running is True


def test_racing_claimants_produce_exactly_one_execution_owner(tmp_path):
    """The lease must be exclusive in the WRITE, not in a read before it.

    A read-then-upsert is not exclusive at all: two processes both read "no
    live lease", both run an unconditional ON CONFLICT DO UPDATE, and the
    second simply wins -- leaving two workers on one paper account.
    """
    from services.trading_instances import InstanceStore

    path = str(tmp_path / "shared.db")
    _ledger, seed = _manager(path)
    instance = _create(seed)
    stores = [InstanceStore(SqliteLedger(path)) for _ in range(8)]
    won, refused = [], []
    barrier = threading.Barrier(len(stores))

    def claim(index, store):
        barrier.wait()
        try:
            store.claim_worker_lease(instance.id, worker_id=f"worker-{index}",
                                     process_id=1000 + index, host=f"host-{index}",
                                     ttl_seconds=300)
            won.append(index)
        except WorkerLeaseError:
            refused.append(index)

    threads = [threading.Thread(target=claim, args=(index, store))
               for index, store in enumerate(stores)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(won) == 1, f"{len(won)} processes believed they owned this instance"
    assert len(refused) == len(stores) - 1
    assert stores[0].worker_lease(instance.id)["worker_id"] == f"worker-{won[0]}"


def test_a_claim_that_never_becomes_an_order_does_not_block_a_retry():
    """The claim is a lock, not a record of something that happened."""
    from execution.paper_engine import PaperExecutionEngine
    from services.controls import TradingControl
    from services.fill_model import RealisticFill
    from services.signal_pipeline import SignalPipeline
    from services.trading_instances import InstanceLedger

    ledger = SqliteLedger(":memory:")
    scoped = InstanceLedger(ledger, "inst-1")
    rejecting = PaperExecutionEngine(scoped, 10_000,
                                     fill_model=RealisticFill(reject_prob=1.0))
    pipeline = SignalPipeline(scoped, rejecting, TradingControl(), equity=10_000)
    payload = {"alert_id": "retry-me", "symbol": "BTCUSDT", "side": "BUY",
               "entry": 100.0, "stop": 95.0}

    first = pipeline.process(dict(payload))
    assert first.accepted is False

    # The same candle, retried against a working engine, must still trade.
    pipeline.paper = PaperExecutionEngine(scoped, 10_000)
    second = pipeline.process(dict(payload))
    assert second.accepted is True, second.reason


def test_one_order_leaves_exactly_one_webhook_row():
    """The claim becomes the order's row rather than leaving a second."""
    from execution.paper_engine import PaperExecutionEngine
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline
    from services.trading_instances import InstanceLedger

    ledger = SqliteLedger(":memory:")
    scoped = InstanceLedger(ledger, "inst-1")
    pipeline = SignalPipeline(scoped, PaperExecutionEngine(scoped, 10_000),
                              TradingControl(), equity=10_000)

    assert pipeline.process({"alert_id": "one-row", "symbol": "BTCUSDT",
                             "side": "BUY", "entry": 100.0, "stop": 95.0}).accepted

    rows = [row for row in ledger.get_webhook_events() if row["alert_id"] == "one-row"]
    assert len(rows) == 1
    assert rows[0]["status"] == "accepted"


def test_a_deliberate_single_slot_is_not_reverted_on_restart(tmp_path):
    """One slot is a legal choice now; the migration must be genuinely once."""
    path = str(tmp_path / "l.db")
    _ledger, manager = _manager(path)
    assert manager.max_slots == 3                 # the migration ran
    manager.configure(max_active_slots=1)         # the operator chooses one

    for _restart in range(3):
        _again, reopened = _manager(path)
        assert reopened.max_slots == 1


def test_an_exception_during_the_fill_does_not_bar_the_candle_forever():
    """A stranded claim is permanent: the dedup constraint has no time component."""
    from execution.paper_engine import PaperExecutionEngine
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline
    from services.trading_instances import InstanceLedger

    ledger = SqliteLedger(":memory:")
    scoped = InstanceLedger(ledger, "inst-1")

    class _Exploding(PaperExecutionEngine):
        def open(self, **kwargs):
            raise RuntimeError("ledger write failed mid-fill")

    pipeline = SignalPipeline(scoped, _Exploding(scoped, 10_000),
                              TradingControl(), equity=10_000)
    payload = {"alert_id": "boom", "symbol": "BTCUSDT", "side": "BUY",
               "entry": 100.0, "stop": 95.0}

    with pytest.raises(RuntimeError, match="ledger write failed"):
        pipeline.process(dict(payload))

    # No claim may be left behind...
    assert [row for row in ledger.get_webhook_events()
            if row["alert_id"] == "boom" and row["status"] == "claimed"] == []
    # ...so the same candle is still tradeable once the fault clears.
    pipeline.paper = PaperExecutionEngine(scoped, 10_000)
    assert pipeline.process(dict(payload)).accepted is True


def test_promoting_onto_an_existing_row_reports_a_duplicate_not_a_crash():
    """Both call sites handle DuplicateOrderIntent; a raw IntegrityError escapes."""
    ledger = SqliteLedger(":memory:")
    key = "auto:inst-1:BTCUSDT:5m:2026-09-13T10:00:00+00:00:buy"
    ledger.insert_webhook_event(alert_id=key, symbol="BTCUSDT", side="BUY", entry=1,
                                stop=0.5, payload={}, status="accepted",
                                instance_id="inst-1")
    ledger.insert_webhook_event(alert_id=key, symbol="BTCUSDT", side="BUY", entry=1,
                                stop=0.5, payload={}, status="claimed",
                                instance_id="inst-1")

    with pytest.raises(DuplicateOrderIntent):
        ledger.promote_webhook_event(alert_id=key, status="accepted",
                                     instance_id="inst-1")


def test_a_claim_can_be_released_on_every_ledger_backend():
    """The release was a no-op off SQLite, where nothing else frees the key."""
    from data.ledger import SqliteLedger as _Sqlite

    assert hasattr(_Sqlite, "release_webhook_claim")
    from data.ledger import SupabaseLedger
    assert hasattr(SupabaseLedger, "release_webhook_claim")

    ledger = _Sqlite(":memory:")
    ledger.insert_webhook_event(alert_id="k", symbol="BTCUSDT", side="BUY", entry=1,
                                stop=0.5, payload={}, status="claimed",
                                instance_id="inst-1")
    assert ledger.release_webhook_claim("k", "inst-1") == 1
    # The key is free again, so the candle can be retried.
    ledger.insert_webhook_event(alert_id="k", symbol="BTCUSDT", side="BUY", entry=1,
                                stop=0.5, payload={}, status="claimed",
                                instance_id="inst-1")


def test_a_claim_never_makes_the_duplicate_guard_trip():
    """A claim is a lock, not an order. Both ledgers must agree on that."""
    import inspect

    from data.ledger import SupabaseLedger
    from services.dedup import DuplicateGuard

    ledger = SqliteLedger(":memory:")
    ledger.insert_webhook_event(alert_id="auto:x", symbol="BTCUSDT", side="BUY", entry=1,
                                stop=0.5, payload={}, status="claimed",
                                instance_id="inst-1")
    assert DuplicateGuard(ledger, window_seconds=0).is_duplicate("auto:x") is False

    # The remote ledger excluded only 'rejected', so a stranded claim blocked
    # the candle there and nowhere else. Asserted against the query it builds,
    # not against its comment -- a source-text check the comment itself
    # satisfies cannot fail when the behaviour regresses.
    recorded = {}

    class _Query:
        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def gte(self, *_a, **_k): return self
        def neq(self, key, value):
            recorded.setdefault("neq", []).append((key, value)); return self
        @property
        def not_(self): return self
        def in_(self, key, values):
            recorded["excluded"] = list(values); return self
        def limit(self, *_a, **_k): return self
        def execute(self): return type("R", (), {"data": []})()

    remote = SupabaseLedger.__new__(SupabaseLedger)
    remote._t = lambda _name: _Query()
    assert remote.webhook_seen("auto:x", "1970-01-01T00:00:00+00:00") is False
    assert "claimed" in recorded.get("excluded", []), recorded


def test_a_rejection_reason_travels_on_the_result_not_on_the_engine():
    """One engine is shared by the decision thread and the quote notifier.

    A reason parked on the engine could be erased or overwritten by the other
    thread between the rejection and the read, hiding a systematic capital stop
    behind a random-rejection reason.
    """
    from execution.paper_engine import PaperExecutionEngine
    from services.fill_model import RealisticFill
    from services.trading_instances import InstanceLedger

    ledger = SqliteLedger(":memory:")
    engine = PaperExecutionEngine(InstanceLedger(ledger, "inst-1"), 1_000,
                                  fill_model=RealisticFill(reject_prob=1.0))

    unfunded = engine.open(symbol="BTCUSDT", side="BUY", size=100.0, entry=60_000.0,
                           stop=59_000.0, alert_id="unfunded")
    assert unfunded.action == "rejected"
    assert unfunded.reason == engine.UNFUNDED_BLOCKER

    # An affordable order rejected by the fill model is a different thing, and
    # the earlier rejection cannot colour it.
    fill_rejected = engine.open(symbol="BTCUSDT", side="BUY", size=0.001, entry=100.0,
                                stop=95.0, alert_id="fill-reject")
    assert fill_rejected.action == "rejected"
    assert fill_rejected.reason != engine.UNFUNDED_BLOCKER
    assert not hasattr(engine, "last_blocker")
