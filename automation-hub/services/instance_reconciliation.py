"""Prove an instance's durable state is coherent before it is called healthy.

After a restart the runtime rebuilds itself from the database. That database
is not automatically trustworthy: the process may have died between two writes
that were meant to happen together, another process may still own the
instance, or an operator may have deleted an instance whose worker is still
alive somewhere.

Every check here answers one question with evidence, and the result is
advisory in exactly one direction: a discrepancy BLOCKS the instance. Nothing
in this module repairs anything or relaxes a gate, because a reconciliation
that silently fixed its own findings would hide the very thing it exists to
surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


#: Severity that blocks new entries. ``warning`` is recorded and shown but does
#: not close the gate, because the condition cannot produce a wrong trade.
BLOCKING, WARNING = "blocking", "warning"


@dataclass
class Finding:
    check: str
    severity: str
    detail: str
    evidence: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"check": self.check, "severity": self.severity,
                "detail": self.detail, "evidence": dict(self.evidence)}


@dataclass
class ReconciliationResult:
    instance_id: str
    checked_at: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(item.severity == BLOCKING for item in self.findings)

    @property
    def ok(self) -> bool:
        return not self.findings

    def public(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "checked_at": self.checked_at,
            "ok": self.ok,
            "blocked": self.blocked,
            "findings": [item.public() for item in self.findings],
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def reconcile(manager, instance_id: str, *,
              expect_worker: bool = True) -> ReconciliationResult:
    """Cross-check one instance's durable state. Never mutates anything.

    ``expect_worker`` says whether a live worker SHOULD exist right now. At
    startup it must be False: the persisted state is legitimately ``running``
    from before the last shutdown and restoration has not built the worker yet,
    so demanding one there would block every clean restart. Once the runtime is
    supposed to be up -- a supervisor sweep, an operator's on-demand check --
    a persisted ``running`` with no worker is a real discrepancy.
    """
    result = ReconciliationResult(instance_id=instance_id, checked_at=_now())
    inst = manager._instances.get(instance_id)
    if inst is None:
        result.findings.append(Finding(
            "instance_exists", BLOCKING,
            "a worker is running for an instance that no longer exists"))
        return result

    ledger = manager.ledger
    session = inst.simulation_session_id
    try:
        positions = ledger.get_positions("open", instance_id=instance_id,
                                         simulation_session_id=session)
        trades = ledger.get_paper_trades(instance_id=instance_id,
                                         simulation_session_id=session)
    except Exception as exc:  # a database that cannot answer is not "healthy"
        result.findings.append(Finding(
            "ledger_readable", BLOCKING,
            f"instance ledger could not be read: {type(exc).__name__}: {exc}"))
        return result

    # Counted per symbol, not set membership. Keying by symbol alone collapsed
    # several open positions on one pair into a single entry, so an instance
    # holding two BTCUSDT positions with only one BTCUSDT trade row -- exactly
    # the shape a process death between two writes leaves -- reconciled clean.
    from collections import Counter

    open_trades = Counter(str(row.get("symbol")) for row in trades
                          if str(row.get("status")) == "open")
    open_positions = Counter(str(row.get("symbol")) for row in positions)

    for symbol in sorted(set(open_positions) | set(open_trades)):
        held, recorded = open_positions[symbol], open_trades[symbol]
        if held > recorded:
            result.findings.append(Finding(
                "position_without_trade", BLOCKING,
                f"{held} open {symbol} position(s) but {recorded} open trade row(s)",
                {"symbol": symbol, "positions": held, "trades": recorded,
                 "position_ids": [row.get("id") for row in positions
                                  if str(row.get("symbol")) == symbol]}))
        elif recorded > held:
            result.findings.append(Finding(
                "trade_without_position", BLOCKING,
                f"{recorded} open {symbol} trade row(s) but {held} open position(s)",
                {"symbol": symbol, "positions": held, "trades": recorded,
                 "trade_ids": [row.get("id") for row in trades
                               if str(row.get("symbol")) == symbol
                               and str(row.get("status")) == "open"]}))

    # --- durable state claims a running worker; is one actually there?
    runtime = manager._runtime.get(instance_id)
    thread = getattr(runtime[0], "_thread", None) if runtime else None
    worker_alive = bool(runtime and runtime[0].running
                        and thread is not None and thread.is_alive())
    if expect_worker and inst.state == "running" and not worker_alive:
        result.findings.append(Finding(
            "worker_matches_state", BLOCKING,
            "durable state says running but no live worker owns this instance",
            {"state": inst.state}))

    # --- exactly one execution owner
    ownership = manager.worker_ownership(instance_id)
    if not ownership.get("known"):
        result.findings.append(Finding(
            "worker_lease_readable", BLOCKING,
            f"worker ownership could not be determined: {ownership.get('detail')}"))
    elif worker_alive and ownership.get("held") and not ownership.get("this_process"):
        result.findings.append(Finding(
            "single_execution_owner", BLOCKING,
            "a worker is running here while another process holds the lease",
            {"worker_id": ownership.get("worker_id"), "host": ownership.get("host"),
             "process_id": ownership.get("process_id")}))

    # --- parked intents must belong to this instance's current session
    try:
        pending = manager.store.market_state(instance_id).get("pending_orders_json") or {}
    except Exception as exc:
        result.findings.append(Finding(
            "pending_orders_readable", BLOCKING,
            f"pending order state could not be read: {type(exc).__name__}: {exc}"))
        pending = {}
    quarantined = pending.get("quarantined_intents") or {}
    if quarantined:
        result.findings.append(Finding(
            "intent_ownership", BLOCKING,
            f"{len(quarantined)} parked intent(s) are quarantined pending ownership repair",
            {"symbols": sorted(quarantined)}))

    # --- every record must carry this instance's id
    stray = [row.get("id") for row in trades if str(row.get("instance_id") or "") != instance_id]
    if stray:
        result.findings.append(Finding(
            "records_carry_instance_id", BLOCKING,
            f"{len(stray)} trade row(s) returned for this instance carry a different instance_id",
            {"trade_ids": stray[:10]}))

    # --- the durable order-idempotency constraint must actually be installed
    constraint = getattr(ledger, "duplicate_order_constraint", None)
    if isinstance(constraint, dict) and not constraint.get("enforced", True):
        result.findings.append(Finding(
            "order_idempotency_constraint", WARNING,
            "the unique (alert_id, instance_id, status) index is not installed; "
            "duplicate protection is application-level only",
            {k: v for k, v in constraint.items() if k != "enforced"}))

    return result


def reconcile_all(manager) -> dict[str, ReconciliationResult]:
    return {instance_id: reconcile(manager, instance_id)
            for instance_id in list(manager._instances)}
