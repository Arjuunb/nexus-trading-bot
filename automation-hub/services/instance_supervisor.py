"""Backend-owned supervision for Trading Instance workers.

Before this existed, an instance worker had exactly one chance to survive: the
five in-thread reconnect attempts inside :class:`AutoStrategyEngine`.  Beyond
that the worker thread returned, the instance went to ``error``, and nothing in
the process ever looked at it again.  Startup restoration only runs at startup,
the :class:`~services.watchdog.Watchdog` only watches the legacy singleton
engine, and the dashboard is a read-only poller -- so the only thing that could
bring the instance back was a person opening the page and pressing Start.

That is the mechanism behind "the market data is broken after I log in again":
nothing was broken about the login.  The worker had died hours earlier, during
a Binance blip or a container restart that happened to land in one, and the
first time anyone noticed was the next time they looked.

This supervisor closes that loop.  It is a single daemon thread owned by the
application process, it reads durable desired state from the database, and it
repairs the difference:

* an instance that is desired-running but has no live worker is started
* an instance whose worker thread has died is replaced
* a paused instance is kept alive with its entry gate closed, because pause is
  an entry gate and not a shutdown
* every repair is rate-limited per instance by exponential backoff, so a
  genuinely broken configuration produces a bounded, observable retry stream
  rather than a hot loop

It deliberately never *creates* intent.  A stopped instance stays stopped; only
an operator action or a successful start changes what is desired.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from services.instance_telemetry import event_payload, format_event, log_event
from services.trading_instances import InstanceNotDesired, WorkerLeaseError

#: Worker states that mean "this instance has no usable execution runtime".
_NEEDS_REPAIR = {"error", "stopped", "created", "degraded"}

#: First retry delay, doubled per consecutive failure up to ``MAX_BACKOFF_S``.
BASE_BACKOFF_S = 15.0
MAX_BACKOFF_S = 600.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InstanceSupervisor:
    """Reconcile durable desired state with live workers, forever."""

    def __init__(self, manager, *, interval_s: float = 20.0,
                 base_backoff_s: float = BASE_BACKOFF_S,
                 max_backoff_s: float = MAX_BACKOFF_S,
                 clock=time.monotonic):
        self.manager = manager
        self.interval_s = max(1.0, float(interval_s))
        self.base_backoff_s = max(1.0, float(base_backoff_s))
        self.max_backoff_s = max(self.base_backoff_s, float(max_backoff_s))
        self.clock = clock
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._failures: dict[str, int] = {}
        self._next_attempt: dict[str, float] = {}
        self.last_sweep: str | None = None
        self.sweeps = 0
        self.repairs = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_report: list[dict] = []

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="instance-supervisor", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout_s)
        self._thread = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and not self._stop.is_set())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception as exc:  # a supervisor that dies supervises nothing
                # log_event skips the store when there is no instance_id, so a
                # supervisor-level failure had nowhere to go and could repeat
                # silently on every tick. Record it where an operator looks,
                # and always leave a line on stdout as the last resort.
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.errors += 1
                payload = event_payload(None, "SUPERVISOR_ERROR", status="error",
                                        detail=self.last_error)
                print("[instance-supervisor] " + format_event(payload), flush=True)
                try:
                    self.manager.ledger.log(
                        level="error", stage="instance-supervisor",
                        message=format_event(payload))
                except Exception:  # noqa: BLE001 — stdout already has it
                    pass
            self._stop.wait(self.interval_s)

    # ---------------------------------------------------------------- sweep
    def _worker_alive(self, instance_id: str) -> bool:
        return self.manager.worker_alive(instance_id)

    def sweep(self) -> list[dict]:
        """One reconciliation pass. Returns what it did, for tests and status."""
        report: list[dict] = []
        now = self.clock()
        store = getattr(self.manager, "store", None)
        if store is not None and not getattr(store, "available", True):
            # A degraded ledger is already reported at startup; restarting
            # workers against it would only produce write failures.
            self.last_sweep, self.sweeps = _now(), self.sweeps + 1
            self.last_report = report
            return report

        with self.manager._lock:
            # One snapshot, under the lock. Iterating the live dict here raced
            # create()/delete() -- "dictionary changed size during iteration"
            # would abandon the whole sweep, including any dead worker it was
            # about to repair.
            trading = [inst for inst in self.manager._instances.values()
                       if inst.mode == "trading"]
        candidates = [inst for inst in trading if inst.desired_running]
        running = sum(1 for inst in trading if self._worker_alive(inst.id))

        for inst in sorted(candidates, key=lambda item: item.created_at):
            if self._stop.is_set():
                break
            if self._worker_alive(inst.id):
                self._failures.pop(inst.id, None)
                self._next_attempt.pop(inst.id, None)
                # A live worker must keep proving it owns this instance. If the
                # renewal fails the lease was taken or expired underneath us,
                # which means another process may now be trading this account:
                # stop this worker rather than run a second execution owner.
                if not self.manager._renew_lease(inst):
                    report.append({"instance_id": inst.id, "action": "lease_lost"})
                    try:
                        # halt_runtime, not stop: losing a lease is not the
                        # operator deciding this instance should stop. stop()
                        # writes desired_running=False into the row every
                        # process reads, so nothing would ever restart it once
                        # the split brain resolved.
                        self.manager.halt_runtime(
                            inst.id, reason="worker lease lost to another owner")
                    except Exception:  # noqa: BLE001 — the lease is gone either way
                        pass
                continue
            if inst.state == "blocked":
                # Reconciliation found the durable records disagreeing. A
                # restart would rebuild the same worker over the same
                # unexplained state, so this needs a person, not a retry.
                report.append({"instance_id": inst.id, "action": "blocked",
                               "reason": inst.last_error})
                continue
            if inst.state not in _NEEDS_REPAIR and inst.state != "paused":
                # starting / bootstrapping / warming / syncing / recovering are
                # transitions the worker is actively driving. A worker with a
                # live thread was already skipped above, so reaching here with
                # one of those states means the thread is gone and the state is
                # simply the last one it managed to persist -- repair it.
                pass
            due = self._next_attempt.get(inst.id)
            if due is not None and now < due:
                report.append({"instance_id": inst.id, "action": "backoff",
                               "retry_in_s": round(due - now, 1),
                               "failures": self._failures.get(inst.id, 0)})
                continue
            if running >= self.manager.max_slots:
                report.append({"instance_id": inst.id, "action": "no_slot",
                               "max_slots": self.manager.max_slots})
                log_event(self.manager, inst, "INSTANCE_ERROR", status="blocked",
                          detail=f"no free trading slot ({self.manager.max_slots})")
                continue
            paused = inst.state == "paused"
            log_event(self.manager, inst, "INSTANCE_STARTING", status="repairing",
                      detail=f"supervisor repair from state={inst.state}")
            try:
                restored = self.manager.start(inst.id, entry_gate_closed=paused,
                                              only_if_desired=True)
            except InstanceNotDesired:
                # The operator stopped or deleted it while this sweep waited.
                # That is the operator's decision, not a failure to retry.
                self._failures.pop(inst.id, None)
                self._next_attempt.pop(inst.id, None)
                report.append({"instance_id": inst.id, "action": "stand_down"})
                continue
            except WorkerLeaseError as exc:
                # Somebody else owns it. That is not a fault to retry hard:
                # back off quietly and let the lease expire if the holder is
                # genuinely gone. Starting anyway would duplicate every order.
                self._next_attempt[inst.id] = now + self.max_backoff_s
                report.append({"instance_id": inst.id, "action": "lease_held",
                               "error": str(exc)})
                log_event(self.manager, inst, "INSTANCE_ERROR", status="blocked",
                          detail=f"another worker owns this instance: {exc}")
                continue
            except Exception as exc:
                attempts = self._failures.get(inst.id, 0) + 1
                self._failures[inst.id] = attempts
                delay = min(self.max_backoff_s,
                            self.base_backoff_s * (2 ** (attempts - 1)))
                self._next_attempt[inst.id] = now + delay
                report.append({"instance_id": inst.id, "action": "failed",
                               "attempt": attempts, "retry_in_s": round(delay, 1),
                               "error": f"{type(exc).__name__}: {exc}"})
                log_event(self.manager, inst, "INSTANCE_ERROR", status="retrying",
                          detail=(f"supervisor start attempt {attempts} failed, retrying in "
                                  f"{delay:.0f}s: {type(exc).__name__}: {exc}"))
                continue
            self._failures.pop(inst.id, None)
            self._next_attempt.pop(inst.id, None)
            running += 1
            self.repairs += 1
            report.append({"instance_id": inst.id, "action": "restored",
                           "entry_gate_closed": restored.state == "paused"})
            log_event(self.manager, inst, "INSTANCE_RESTORED", status="running",
                      detail="supervisor restored worker from durable desired state")

        self.last_sweep, self.sweeps = _now(), self.sweeps + 1
        self.last_report = report
        return report

    # --------------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "running": self.running,
            "interval_s": self.interval_s,
            "sweeps": self.sweeps,
            "repairs": self.repairs,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_sweep": self.last_sweep,
            "last_report": list(self.last_report),
            "backoff": {key: {"consecutive_failures": value,
                              "retry_in_s": round(max(0.0, self._next_attempt.get(key, 0) - self.clock()), 1)}
                        for key, value in self._failures.items()},
        }
