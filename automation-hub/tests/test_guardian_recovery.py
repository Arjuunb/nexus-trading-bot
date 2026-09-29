"""Guardian Phase 7: controlled recovery, off by default.

The crash is real: the real TradingInstanceManager and engine, with a
strategy that fails on a live candle inside the worker thread. The restart,
when the owner enables it, is the manager's own staged Full Bot Reboot
through the app's real bridge (``webhook_api._guardian_restarter``).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.guardian import health as h
from services.guardian.recovery import ACTIONS, RecoveryController
from services.guardian.store import GuardianStore
from tests.test_guardian import _lifecycle, _manager, _service
from tests.test_journal_integrity import _StaleThenFresh, _wait_for


def _failing(_k, symbol):
    from strategies.brain_strategy import DecisionBrain

    class Failing(DecisionBrain):
        def on_bar(self, bar):
            raise RuntimeError("strategy fault on a live candle")
    return Failing(symbol)


def _crashed(tmp_path):
    """A real instance whose worker crashed on its first live candle."""
    manager, inst = _manager(tmp_path, _failing, fresh=True)
    step = 300
    anchor = int(datetime.now(timezone.utc).timestamp()) // step * step
    manager.store.save_market_state(inst.id, last_processed_candle_timestamp=datetime.fromtimestamp(
        anchor - 3 * step, tz=timezone.utc).isoformat())
    manager.start(inst.id)
    assert _wait_for(lambda: "INSTANCE_ERROR" in _lifecycle(manager, inst))
    return manager, inst


def _recovery_actions(store):
    return [(a["action"], a["result"]) for a in reversed(store.actions(100)) if a["action"] in ACTIONS]


def _run(tmp_path, *, enabled):
    import webhook_api
    from services.guardian.bus import EventBus
    store = GuardianStore()
    bus = EventBus(store)
    manager, inst = _crashed(tmp_path)
    recovery = RecoveryController(store, restart_instance=webhook_api._guardian_restarter({"main": manager}),
                                  enabled=enabled)
    svc = _service(store, bus, manager, recovery=recovery, incident_verify_s=0)
    try:
        assert _wait_for(lambda: svc.cycle()[f"instance:{inst.id}"].effective == h.FAILED)
        for _ in range(3):
            svc.cycle()
        bus.flush()
    finally:
        manager.shutdown()
        _StaleThenFresh.fresh = False
    return store, manager, inst, svc


def test_nothing_is_automatic_by_default_the_recommendation_is_recorded_not_taken(tmp_path):
    store, manager, inst, svc = _run(tmp_path, enabled=None)
    [incident] = svc.incidents.list()
    assert incident["key"] == f"instance:{inst.id}" and incident["diagnosis"]["confidence"] == "CONFIRMED"
    assert _recovery_actions(store) == [("GATHER_DIAGNOSTICS", "SUCCESS"),
                                        ("RESTART_INSTANCE_WORKER", "NOT_TAKEN_POLICY_DISABLED")]
    assert inst.id not in manager._reboots                    # nothing was restarted
    [diag] = [a for a in store.actions(100) if a["action"] == "GATHER_DIAGNOSTICS"]
    assert diag["evidence"]["detail"]["incident"]["id"] == incident["id"]


def test_an_enabled_restart_is_the_managers_own_staged_reboot(tmp_path):
    store, manager, inst, _ = _run(tmp_path, enabled={"RESTART_INSTANCE_WORKER"})
    assert ("RESTART_INSTANCE_WORKER", "SUCCESS") in _recovery_actions(store)
    assert inst.id in manager._reboots                        # the manager's own reboot, with its checks
    [restarted] = store.events(event_type="worker_restarted", instance_id=inst.id)
    assert "Guardian requested a staged reboot" in restarted["reason"]
    assert len([a for a in _recovery_actions(store) if a[0] == "RESTART_INSTANCE_WORKER"]) == 1   # once


def test_guardian_never_starts_an_instance_its_owner_stopped(tmp_path):
    import webhook_api
    manager, inst = _manager(tmp_path, _failing, fresh=True)       # created, never started
    try:
        store = GuardianStore()
        recovery = RecoveryController(store, restart_instance=webhook_api._guardian_restarter({"main": manager}),
                                      enabled={"RESTART_INSTANCE_WORKER"})
        row = recovery.execute("RESTART_INSTANCE_WORKER", inst.id, reason="test", policy="TEST", evidence={})
        assert row["result"] == "FAILED" and "never starts an instance" in row["evidence"]["detail"]
        assert inst.id not in manager._reboots and manager.instance_for(inst.id).desired_running is False
    finally:
        manager.shutdown()
        _StaleThenFresh.fresh = False


def test_restarts_are_rate_limited_and_cooled_down():
    clock = {"t": 1_000_000.0}
    calls = []
    store = GuardianStore()
    recovery = RecoveryController(store, restart_instance=lambda iid: calls.append(iid) or {"ok": True},
                                  enabled={"RESTART_INSTANCE_WORKER"}, max_per_hour=2, cooldown_s=900,
                                  clock=lambda: clock["t"])

    def restart(target):
        return recovery.execute("RESTART_INSTANCE_WORKER", target, reason="t", policy="TEST", evidence={})["result"]
    assert restart("a") == "SUCCESS"
    assert restart("a") == "SKIPPED_COOLDOWN"
    assert restart("b") == "SUCCESS"
    assert restart("c") == "SKIPPED_RATE_LIMIT"               # 2 per hour
    clock["t"] += 3601
    assert restart("a") == "SUCCESS"
    assert calls == ["a", "b", "a"]
    assert len(store.actions(100)) == 5                       # every decision recorded, taken or not


def test_only_allow_listed_operational_actions_exist():
    store = GuardianStore()
    recovery = RecoveryController(store, enabled={"CLOSE_POSITION", "RESTART_INSTANCE_WORKER"})
    assert set(ACTIONS) == {"GATHER_DIAGNOSTICS", "RESTART_INSTANCE_WORKER"}
    assert "CLOSE_POSITION" not in recovery.enabled
    assert recovery.execute("CLOSE_POSITION", "BTCUSDT", reason="t", policy="T", evidence={})["result"] == "REFUSED"
    # enabled, but the app never handed Guardian a restart: it cannot do it
    assert recovery.execute("RESTART_INSTANCE_WORKER", "x", reason="t", policy="T",
                            evidence={})["result"] == "REFUSED"
    never = recovery.status()["never"]
    assert "orders or positions" in never and "paper to live" in never and "deployments" in never


@pytest.mark.parametrize("confidence", ["PROBABLE", "POSSIBLE", "UNKNOWN"])
def test_an_uncertain_diagnosis_never_leads_to_a_restart(confidence):
    node = h.Component(id="instance:x", label="x", kind="instance", raw=h.FAILED, facts={"alive": False})
    incident = {"id": 1, "key": "instance:x", "severity": "HIGH", "diagnosis": {"confidence": confidence}}
    plan = RecoveryController(GuardianStore(), enabled={"RESTART_INSTANCE_WORKER"}).plan(incident, {"instance:x": node})
    assert plan == [("GATHER_DIAGNOSTICS", "instance:x")]


def test_the_deployed_policy_is_diagnostics_only_unless_the_owner_enables_more():
    import webhook_api
    assert webhook_api.guardian.recovery.enabled == {"GATHER_DIAGNOSTICS"}
    assert webhook_api.broker_registry.live_locked() is True


def test_the_stated_boundary_follows_the_policies_actually_on():
    from services.guardian.bus import EventBus
    from services.guardian.service import GuardianService
    store = GuardianStore()
    off = GuardianService(store, EventBus(store), recovery=RecoveryController(store, restart_instance=lambda i: {}))
    assert off.snapshot()["boundary"]["mode"] == "read-only" and off.snapshot()["boundary"]["may_change"] == []
    on = GuardianService(store, EventBus(store), recovery=RecoveryController(
        store, restart_instance=lambda i: {}, enabled={"RESTART_INSTANCE_WORKER"}))
    assert on.snapshot()["boundary"]["mode"] == "read-only + owner-enabled recovery"
    assert "staged reboot" in on.snapshot()["boundary"]["may_change"][0]
