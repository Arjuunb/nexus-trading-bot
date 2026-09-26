"""Builds canonical trade and decision records from durable execution facts.

Why a projector and not more journal calls: the Journal went empty because a
journal call sat on a code path that forward-paper fills stopped taking
(docs/JOURNAL_AUDIT.md). Facts the execution layer already writes durably --
the paper ledger, the lab brokers, the SMC agent's intents -- cannot be
skipped that way. Projecting them gives three properties at once:

* a crash or a failed journal write loses nothing: the next pass rebuilds the
  record from the same facts (reconciliation repairs the journal);
* one execution is one record: the key is the execution identity the ledger
  already enforces, and the store has UNIQUE constraints on it;
* the setup snapshot is the one frozen at decision time -- the pipeline
  payload persisted before the order -- never recomputed from later prices.

Nothing here decides, sizes or places a trade, and no strategy code is
touched. The lab databases are read, never written.

Sources and what they become:

  main paper ledger, instance rows    -> INSTANCE        (FORWARD_PAPER / SIMULATION)
  main paper ledger, no instance      -> LEGACY_ENGINE   (the retired autonomous engine, webhooks)
  adaptive lab ledger                 -> ADAPTIVE_LAB
  SMC lab broker + metadata           -> SMC_LAB (agent-linked when the SMC agent decided it)
  SMC agent intents with no fill      -> SMC_LAB, EXECUTION_FAILED / EXECUTION_UNCERTAIN
  PA lab broker + metadata + journal  -> PA_LAB
  old trade_decision_journal rows     -> matched to the ledger, else LEGACY_MIGRATION
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from data.trade_record_store import TradeRecordStore, record_id_for, utcnow

#: |realized R| at or below this is a breakeven, not a win or a loss. A trade
#: stopped at entry loses its fees; calling that a loss would hide stop
#: management in the loss count. Documented on every record as outcome_basis.
BREAKEVEN_R = 0.05
#: SignalPipeline's close-side vocabulary (services/signal_pipeline.py).
_CLOSE_SIDES = {"REDUCE", "CLOSE", "EXIT", "FLAT", "FLATTEN"}
#: A trade-less forward intent missing from the instance's parked intents for
#: this long has provably not filled: it was dropped, not delayed.
INTENT_GRACE_S = 3600


def _f(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(value) -> Optional[str]:
    """Normalise to an ISO UTC string, or None. Never invents a time."""
    if value in (None, "", "None"):
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            stamp = datetime.fromtimestamp(float(value), timezone.utc)
        except (TypeError, ValueError):
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat()


def _dt(value) -> Optional[datetime]:
    iso = _ts(value)
    return datetime.fromisoformat(iso) if iso else None


def _ms_between(a, b) -> Optional[float]:
    x, y = _dt(a), _dt(b)
    if x is None or y is None:
        return None
    return round((y - x).total_seconds() * 1000, 1)


def trading_session(value) -> Optional[str]:
    """UTC session bucket of the signal time (deterministic, documented)."""
    stamp = _dt(value)
    if stamp is None:
        return None
    hour = stamp.hour
    if hour < 7:
        return "ASIA"
    if hour < 12:
        return "LONDON"
    if hour < 16:
        return "LONDON_NY_OVERLAP"
    if hour < 21:
        return "NEW_YORK"
    return "LATE_US"


def classify_outcome(net_pnl: Optional[float], realized_r: Optional[float]) -> Optional[str]:
    if realized_r is not None:
        if abs(realized_r) <= BREAKEVEN_R:
            return "BREAKEVEN"
        return "WIN" if realized_r > 0 else "LOSS"
    if net_pnl is None:
        return None
    return "WIN" if net_pnl > 0 else "LOSS" if net_pnl < 0 else "BREAKEVEN"


def _side(value) -> Optional[str]:
    text = str(value or "").lower()
    if text in ("long", "buy"):
        return "long"
    if text in ("short", "sell"):
        return "short"
    return text or None


def _json(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _rr(entry, stop, target) -> Optional[float]:
    entry, stop, target = _f(entry), _f(stop), _f(target)
    if None in (entry, stop, target) or entry == stop:
        return None
    return round(abs(target - entry) / abs(entry - stop), 4)


def _completeness(missing: list[str], core: Iterable[str]) -> str:
    gaps = set(missing) & set(core)
    if not missing:
        return "FULL"
    if gaps:
        return "MINIMAL" if len(gaps) >= 3 else "PARTIAL"
    return "PARTIAL"


def _result_fields(*, entry, stop, side, legs: list[dict], risk_amount, exit_reason,
                   mae_r=None, mfe_r=None) -> dict:
    """Deterministic result from exit legs: [{price, size, pnl (net), fees}]."""
    size = sum(_f(l.get("size")) or 0.0 for l in legs)
    if not legs or size <= 0:
        return {}
    exit_price = sum((_f(l.get("price")) or 0.0) * (_f(l.get("size")) or 0.0) for l in legs) / size
    net = sum(_f(l.get("pnl")) or 0.0 for l in legs)
    fees = sum(_f(l.get("fees")) or 0.0 for l in legs)
    funding = None
    if any(l.get("funding") is not None for l in legs):
        funding = sum(_f(l.get("funding")) or 0.0 for l in legs)
    risk = _f(risk_amount)
    realized_r = round(net / risk, 4) if risk and risk > 0 else None
    entry_f, stop_f = _f(entry), _f(stop)
    achieved = None
    if entry_f is not None and stop_f is not None and entry_f != stop_f:
        sign = 1.0 if side == "long" else -1.0
        achieved = round(sign * (exit_price - entry_f) / abs(entry_f - stop_f), 4)
    mae = _f(mae_r)
    return {
        "actual_exit": round(exit_price, 10), "exit_reason": exit_reason,
        "gross_pnl": round(net + fees, 10), "fees": round(fees, 10), "funding": funding,
        "net_pnl": round(net, 10), "realized_r": realized_r, "achieved_rr": achieved,
        "mae_r": mae, "mfe_r": _f(mfe_r),
        "max_trade_drawdown": (round(abs(mae) * risk, 10) if mae is not None and risk else None),
        "outcome": classify_outcome(net, realized_r),
    }


def _timeline(rec: dict) -> list[dict]:
    """The canonical stage list with timestamps; stages with no time are PENDING/UNKNOWN."""
    closed = rec.get("status") == "CLOSED"
    stages = [
        ("SIGNAL", rec.get("signal_detected_at")),
        ("DECISION", rec.get("decision_created_at")),
        ("RISK_CHECK", rec.get("decision_created_at") if rec.get("risk_check_json") else None),
        ("INTENT", rec.get("intent_created_at")),
        ("ORDER", rec.get("order_submitted_at")),
        ("FILL", rec.get("entry_filled_at")),
        ("POSITION_OPEN", rec.get("position_opened_at")),
        ("EXIT", rec.get("exit_filled_at") or rec.get("exit_signal_at")),
        ("POSITION_CLOSED", rec.get("position_closed_at")),
        ("JOURNAL_FINALIZED", None),
    ]
    status = rec.get("status")
    out = []
    reached = True
    for stage, at in stages:
        if stage == "JOURNAL_FINALIZED":
            state = "DONE" if status in ("CLOSED", "CANCELLED", "REJECTED",
                                         "EXECUTION_FAILED") else "PENDING"
        elif at:
            state = "DONE"
        elif stage in ("EXIT", "POSITION_CLOSED") and not closed:
            state = "PENDING"
        elif stage == "FILL" and status in ("EXECUTION_FAILED", "CANCELLED", "REJECTED"):
            state = status
            reached = False
        elif stage == "FILL" and status == "EXECUTION_UNCERTAIN":
            state = "UNCERTAIN"
        elif not reached:
            state = "NOT_REACHED"
        else:
            state = "UNKNOWN"
        out.append({"stage": stage, "at": at, "status": state})
    return out


def _finish(rec: dict, missing: list[str], core: Iterable[str]) -> dict:
    rec["decision_latency_ms"] = _ms_between(rec.get("signal_detected_at"),
                                             rec.get("decision_created_at"))
    rec["execution_latency_ms"] = _ms_between(
        rec.get("order_submitted_at") or rec.get("decision_created_at"),
        rec.get("entry_filled_at"))
    for field in ("decision_latency_ms", "execution_latency_ms"):
        # A negative latency means the sources' clocks disagree. Storing it
        # would present a wrong number as a measurement.
        if rec[field] is not None and rec[field] < 0:
            rec[field] = None
            missing.append(f"{field}_clock_inconsistent")
    opened = rec.get("entry_filled_at") or rec.get("position_opened_at")
    closed = rec.get("position_closed_at") or rec.get("exit_filled_at")
    duration = _ms_between(opened, closed)
    rec["trade_duration_s"] = round(duration / 1000, 1) if duration is not None else None
    rec["missing_json"] = sorted(set(missing))
    rec["data_completeness"] = _completeness(missing, core)
    rec.setdefault("source_ref_json", {})
    rec["source_ref_json"]["outcome_basis"] = (
        f"realized R within ±{BREAKEVEN_R} is BREAKEVEN; otherwise the sign of net P&L")
    return rec


# ======================================================================
# decision classification
# ======================================================================
def classify_decision(final_state: str, stage: str, blocker: str, reason: str) -> str:
    fs = str(final_state or "").upper()
    text = f"{stage} {blocker} {reason}".upper()
    if fs in ("FILLED",):
        return "TRADE_OPENED"
    if fs == "PENDING_INTENT":
        return "TRADE_OPENED"
    if fs == "SIGNALS_ONLY" or "SIGNALS_ONLY" in text:
        return "SIGNALS_ONLY"
    if fs == "APPROVAL_REQUIRED":
        return "APPROVAL_REQUIRED"
    if "DUPLICATE" in text or "DEDUP" in text:
        return "DUPLICATE_PREVENTED"
    if "UNCERTAIN" in text:
        return "EXECUTION_UNCERTAIN"
    if "NEWS" in text or "BLACKOUT" in text or "EVENT_GUARD" in text:
        return "NEWS_BLACKOUT"
    if "STALE" in text or "WARM" in text or "MARKET_QUALITY" in text or "DATA_" in text:
        return "STALE_DATA"
    if "FEED" in text or "DISCONNECT" in text or "UNAVAILABLE" in text:
        return "FEED_UNAVAILABLE"
    if "SESSION" in text or "TRADING_DAY" in text or "OUTSIDE" in text:
        return "SESSION_BLOCKED"
    if "HTF" in text or "HIGHER-TIMEFRAME" in text or "HIGHER TIMEFRAME" in text:
        return "HTF_BLOCKED"
    if "CONTEXT" in text:
        return "CONTEXT_BLOCKED"
    if str(stage).lower() == "execution" or "REJECTED AT FILL" in text or "ORDER" in text \
            and "REJECT" in text:
        return "ORDER_REJECTED"
    if "EXECUTION" in text and ("FAIL" in text or "ERROR" in text):
        return "EXECUTION_FAILED"
    if any(word in text for word in ("RISK", "EXPOSURE", "DRAWDOWN", "DAILY", "SIZ",
                                     "CAPITAL", "STREAK", "MAX_OPEN", "OPEN POSITION",
                                     "HALT", "PAUSE", "KILL")):
        return "RISK_BLOCKED"
    if "WAIT" in text or "CONFIRM" in text:
        return "WAITING_CONFIRMATION"
    if str(stage).lower() in ("brain", "quality") or "SCORE" in text or "QUALITY" in text:
        return "QUALITY_BLOCKED"
    if fs == "GATE_REJECTED":
        return "SETUP_REJECTED"
    return "SIGNAL_GENERATED"


# ======================================================================
# ledger projector: Trading Instances, the legacy engine, the adaptive lab
# ======================================================================
class LedgerSource:
    """One SQLite paper ledger and how to label what it holds."""

    def __init__(self, name: str, ledger, *, instances: Optional[Callable[[], dict]] = None,
                 pending_intents: Optional[Callable[[str], Optional[set]]] = None,
                 decision_store=None, cycle_store=None, lab_id: Optional[str] = None):
        self.name = name
        self.ledger = ledger
        self.instances = instances or (lambda: {})
        self.pending_intents = pending_intents
        self.decision_store = decision_store
        self.cycle_store = cycle_store
        self.lab_id = lab_id

    def conn(self):
        return getattr(self.ledger, "_c", None), getattr(self.ledger, "_lock", None)


def _query(conn, lock, sql: str, args: Iterable = ()) -> list[dict]:
    if conn is None:
        return []
    if lock is not None:
        with lock:
            return [dict(r) for r in conn.execute(sql, list(args)).fetchall()]
    return [dict(r) for r in conn.execute(sql, list(args)).fetchall()]


def _chunks(items: list, size: int = 400):
    for start in range(0, len(items), size):
        yield items[start:start + size]


class LedgerProjector:
    CORE = ("decision_snapshot", "planned_stop_loss", "entry_fill", "risk_amount")

    def __init__(self, store: TradeRecordStore):
        self.store = store

    # ------------------------------------------------------------ helpers
    def _events_by_alert(self, conn, lock, alert_ids: list[str]) -> dict:
        out: dict = {}
        for chunk in _chunks([a for a in alert_ids if a]):
            for row in _query(conn, lock,
                              f"SELECT * FROM webhook_events WHERE alert_id IN "
                              f"({','.join('?' * len(chunk))}) ORDER BY received_at", chunk):
                out.setdefault((row.get("instance_id") or "", row["alert_id"]), []).append(row)
        return out

    def _fill_events(self, conn, lock, alert_id: str, instance_id: str) -> list[dict]:
        rows = _query(conn, lock,
                      "SELECT * FROM webhook_events WHERE alert_id >= ? AND alert_id < ? "
                      "ORDER BY received_at", (f"{alert_id}:fill:", f"{alert_id}:fill;"))
        return [r for r in rows if (r.get("instance_id") or "") == instance_id]

    # -------------------------------------------------------------- project
    def project(self, source: LedgerSource) -> dict:
        conn, lock = source.conn()
        if conn is None:
            return {"source": source.name, "skipped": "ledger is not a local SQLite ledger"}
        trade_cols = {r["name"] for r in _query(conn, lock, "PRAGMA table_info(paper_trades)")}
        if not trade_cols:
            return {"source": source.name, "skipped": "no paper_trades table"}
        trades = _query(conn, lock, "SELECT * FROM paper_trades ORDER BY opened_at")
        execs = _query(conn, lock, "SELECT * FROM paper_executions")
        reduce_targets = {e["trade_id"] for e in execs if e["action"] == "REDUCE"}
        close_by_trade = {e["trade_id"]: e for e in execs if e["action"] == "CLOSE"}
        # chain partial-exit remainders onto the trade they came from
        by_close: dict = {}
        for t in trades:
            if t.get("closed_at"):
                key = (t.get("instance_id") or "", t.get("simulation_session_id") or "",
                       t["symbol"], t["side"], _f(t["entry"]), t["closed_at"])
                by_close[key] = t
        parent: dict = {}
        for t in trades:
            if t["id"] in reduce_targets:
                key = (t.get("instance_id") or "", t.get("simulation_session_id") or "",
                       t["symbol"], t["side"], _f(t["entry"]), t["opened_at"])
                prev = by_close.get(key)
                if prev is not None and prev["id"] != t["id"]:
                    parent[t["id"]] = prev["id"]
        roots: dict = {}
        by_id = {t["id"]: t for t in trades}
        for t in trades:
            root = t["id"]
            seen = set()
            while root in parent and root not in seen:
                seen.add(root)
                root = parent[root]
            roots.setdefault(root, []).append(t)
        known = self.store.keys_with_status(("INSTANCE", "LEGACY_ENGINE", "ADAPTIVE_LAB"))
        results = {"source": source.name, "lifecycles": 0, "written": 0, "skipped_final": 0}
        alert_ids = [by_id[r].get("alert_id") for r in roots if by_id.get(r)]
        decision_events = self._events_by_alert(conn, lock, [a for a in alert_ids if a])
        close_alerts = [close_by_trade[t["id"]]["execution_id"]
                        for legs in roots.values() for t in legs if t["id"] in close_by_trade]
        close_events = self._events_by_alert(conn, lock, close_alerts)
        instances = source.instances() or {}
        for root_id, legs in roots.items():
            root = by_id[root_id]
            key = self._key(source, root)
            status, finalized = known.get(key, (None, 0))
            if finalized and all(l["status"] != "open" for l in legs):
                results["skipped_final"] += 1
                continue
            record = self._record(source, conn, lock, root, legs, key, decision_events,
                                  close_by_trade, close_events, instances)
            outcome = self.store.upsert_trade(record, events=_timeline(record))
            results["lifecycles"] += 1
            results["written"] += outcome["action"] != "unchanged"
        results["pending_intents"] = self._project_pending(source, conn, lock, known,
                                                           set(a for a in alert_ids if a),
                                                           instances)
        return results

    def _source_name(self, source: LedgerSource, instance_id: str) -> str:
        if source.name != "MAIN":
            return source.name
        return "INSTANCE" if instance_id else "LEGACY_ENGINE"

    def _key(self, source: LedgerSource, root: dict) -> str:
        instance = root.get("instance_id") or "-"
        session = root.get("simulation_session_id") or "-"
        name = self._source_name(source, root.get("instance_id") or "")
        if root.get("alert_id"):
            return f"{name}:{instance}:{session}:{root['alert_id']}"
        return f"{name}:{instance}:{session}:trade:{root['id']}"

    def _record(self, source, conn, lock, root, legs, key, decision_events, close_by_trade,
                close_events, instances) -> dict:
        instance_id = root.get("instance_id") or ""
        alert_id = root.get("alert_id") or ""
        rows = decision_events.get((instance_id, alert_id)) or []
        decision_row = rows[0] if rows else None
        payload = _json((decision_row or {}).get("payload_json"), {}) or {}
        fills = self._fill_events(conn, lock, alert_id, instance_id) if alert_id else []
        fill = _json(fills[0].get("payload_json"), {}) if fills else {}
        journal_exec = payload.get("journal_execution") or {}
        sizing = payload.get("journal_sizing") or {}
        gate = payload.get("journal_quality_gate") or {}
        inst = instances.get(instance_id) or {}
        missing: list[str] = []
        if not payload:
            missing.append("decision_snapshot")
        side = _side(root.get("side"))
        name = self._source_name(source, instance_id)
        forward = bool(fill)
        origin, origin_basis = _ledger_origin(forward, journal_exec, payload)
        # timeline
        signal_at = _ts(payload.get("timestamp") or fill.get("signal_timestamp"))
        decision_at = _ts((decision_row or {}).get("received_at"))
        order_at = _ts(fill.get("order_timestamp")) if forward else decision_at
        entry_fill_at = _ts(fill.get("fill_timestamp")) if forward else _ts(root.get("opened_at"))
        if not forward and alert_id:
            missing.append("fill_quote_evidence")
        # plan (frozen at decision time)
        entry_plan = _f(payload.get("entry")) if payload else _f(fill.get("requested_price"))
        stop_plan = _f(payload.get("stop")) if payload else _f(root.get("stop"))
        target_plan = _f(payload.get("target")) if payload else _f(root.get("target"))
        if stop_plan is None:
            stop_plan = _f(root.get("stop"))
            if stop_plan is None:
                missing.append("planned_stop_loss")
        if target_plan is None:
            missing.append("planned_take_profit")
        risk_amount = _f(root.get("risk_amount_at_entry")) or _f(sizing.get("risk_amount")) \
            or _f(fill.get("risk_amount"))
        if risk_amount is None:
            missing.append("risk_amount")
        legs_sorted = sorted(legs, key=lambda l: l["opened_at"])
        entry_size = sum(_f(l.get("size")) or 0 for l in legs_sorted if l["id"] == root["id"]) \
            or _f(root.get("size"))
        closed_legs = [l for l in legs_sorted if l["status"] == "closed"]
        open_legs = [l for l in legs_sorted if l["status"] == "open"]
        status = "OPEN" if open_legs else "CLOSED"
        close_payloads, close_sides = [], []
        for leg in closed_legs:
            exec_row = close_by_trade.get(leg["id"])
            if exec_row:
                evts = close_events.get((instance_id, exec_row["execution_id"])) or []
                if evts:
                    close_payloads.append(_json(evts[-1].get("payload_json"), {}) or {})
                    close_sides.append(str(evts[-1].get("side") or "").upper())
        last_close = close_payloads[-1] if close_payloads else {}
        exit_reason = last_close.get("exit_reason") if last_close else None
        if last_close and not exit_reason:
            # SignalPipeline's own rule for a close that names no reason: an
            # explicit CLOSE is an operator close, an opposite side is a flip.
            exit_reason = ("manual-close" if close_sides[-1] in _CLOSE_SIDES
                           else "opposite-signal")
        if status == "CLOSED" and not exit_reason:
            missing.append("exit_reason")
        exit_legs = [{"price": l.get("exit"), "size": l.get("size"),
                      "pnl": l.get("pnl"), "fees": l.get("fees")} for l in closed_legs]
        rec = {
            "execution_key": key, "record_source": name, "record_origin": origin,
            "verification": "VERIFIED", "operating_mode": "paper",
            "status": status,
            "trade_id": root["id"],
            "decision_id": str(payload.get("journal_decision_id")) if payload.get(
                "journal_decision_id") is not None else None,
            "signal_id": payload.get("decision_identity"),
            "intent_id": alert_id or None, "order_id": alert_id or None,
            "position_id": (close_by_trade.get(root["id"]) or {}).get("position_id"),
            "session_id": root.get("simulation_session_id") or None,
            "instance_id": instance_id or None, "lab_id": source.lab_id,
            "strategy_id": payload.get("strategy_id") or root.get("strategy_id")
            or journal_exec.get("strategy_id") or inst.get("strategy_key"),
            "strategy_name": journal_exec.get("strategy_name") or payload.get("strategy")
            or inst.get("strategy_label"),
            "strategy_version": journal_exec.get("strategy_version") or fill.get("strategy_version"),
            "symbol": root["symbol"], "exchange": journal_exec.get("exchange")
            or inst.get("exchange"),
            "market_type": journal_exec.get("instrument_type") or inst.get("instrument_type"),
            "timeframe": payload.get("timeframe") or fill.get("timeframe") or inst.get("timeframe"),
            "side": side,
            "signal_detected_at": signal_at, "decision_created_at": decision_at,
            "intent_created_at": order_at, "order_submitted_at": order_at,
            "order_acknowledged_at": order_at if forward else None,
            "entry_filled_at": entry_fill_at,
            "position_opened_at": _ts(root.get("opened_at")),
            "exit_signal_at": _ts(last_close.get("timestamp")) if last_close else None,
            "setup_type": gate.get("setup_type"),
            "market_regime": payload.get("regime") or gate.get("regime"),
            "trading_session": trading_session(signal_at),
            "htf_bias": gate.get("htf_bias"),
            "setup_json": {
                "setup_type": gate.get("setup_type"),
                "market_regime": payload.get("regime") or gate.get("regime"),
                "session": trading_session(signal_at),
                "htf_bias": gate.get("htf_bias"),
                "quality_score": gate.get("score"),
                "conditions": payload.get("brain_checklist"),
                "quality_blocks": gate.get("blocks"),
                "strategy_reason": payload.get("reason"),
                "session_basis": "UTC session bucket of the signal time",
            } if payload else None,
            "evidence_json": {
                "strategy_snapshot": payload.get("snapshot"),
                "quality_gate": gate or None,
                "engine": payload.get("journal_engine"),
                "candle_id": payload.get("decision_identity") or fill.get("candle_id"),
                "market_data_source": payload.get("market_data_source")
                or fill.get("market_data_source"),
            } if payload else None,
            "signal_price": _f(sizing.get("signal_price")) or _f(fill.get("signal_price"))
            or entry_plan,
            "planned_entry": entry_plan, "planned_stop_loss": stop_plan,
            "planned_take_profit": target_plan,
            "planned_rr": _rr(entry_plan, stop_plan, target_plan),
            "risk_percent": _f(root.get("risk_pct_at_entry")),
            "risk_amount": risk_amount,
            "quantity": _f(sizing.get("accepted_size")) or entry_size,
            "balance_before": _f(root.get("equity_before_trade")),
            "equity_before": _f(root.get("equity_before_trade")),
            "available_balance_before": _f(sizing.get("available_balance")),
            "risk_check_json": ({"result": "PASSED", "sizing": sizing or None,
                                 "engine_guardrails": payload.get("journal_engine"),
                                 "size_factors": {"context": payload.get("context_size_factor"),
                                                  "health": payload.get("health_size_factor")},
                                 "basis": "the order exists, so every pre-trade gate passed; "
                                          "values are the sizing receipt frozen at decision time"}
                                if payload else None),
            "requested_entry": _f(fill.get("requested_price")) or entry_plan,
            "actual_entry": _f(root.get("entry")),
            "requested_quantity": _f(sizing.get("accepted_size")) or entry_size,
            "filled_quantity": entry_size,
            "bid": _f(fill.get("fill_bid")), "ask": _f(fill.get("fill_ask")),
            "spread": _f(fill.get("spread")), "slippage": _f(fill.get("slippage")),
            "order_type": ("LIMIT" if payload.get("maker") else "MARKET") if payload else None,
            "execution_status": "FILLED",
            "fill_model": ("NEXT_QUOTE" if forward else "SIGNAL_PRICE"),
            "legs_json": [{"trade_id": l["id"], "status": l["status"], "size": l.get("size"),
                           "exit": l.get("exit"), "pnl": l.get("pnl"), "fees": l.get("fees"),
                           "opened_at": l["opened_at"], "closed_at": l.get("closed_at")}
                          for l in legs_sorted],
            "source_ref_json": {"ledger": source.name, "alert_id": alert_id or None,
                                "fill_event": (fills[0]["alert_id"] if fills else None),
                                "trade_ids": [l["id"] for l in legs_sorted],
                                "origin_basis": origin_basis},
        }
        if status == "CLOSED":
            last_leg = closed_legs[-1]
            exec_row = close_by_trade.get(last_leg["id"]) or {}
            rec.update(_result_fields(
                entry=rec["actual_entry"], stop=stop_plan, side=side, legs=exit_legs,
                risk_amount=risk_amount, exit_reason=exit_reason,
                mae_r=last_close.get("mae_r"), mfe_r=last_close.get("mfe_r")))
            rec["exit_submitted_at"] = _ts(exec_row.get("created_at"))
            rec["exit_filled_at"] = _ts(exec_row.get("created_at")) or _ts(last_leg.get("closed_at"))
            rec["position_closed_at"] = _ts(last_leg.get("closed_at"))
            rec["execution_status"] = "CLOSED"
        return _finish(rec, missing, self.CORE)

    def _project_pending(self, source, conn, lock, known, traded_alerts: set,
                         instances) -> int:
        """Forward intents that have not filled: PENDING, or CANCELLED once gone."""
        rows = _query(conn, lock,
                      "SELECT * FROM webhook_events WHERE status='pending' ORDER BY received_at")
        written = 0
        now = datetime.now(timezone.utc)
        for row in rows:
            alert_id = row["alert_id"]
            if alert_id in traded_alerts or ":fill:" in alert_id:
                continue
            instance_id = row.get("instance_id") or ""
            payload = _json(row.get("payload_json"), {}) or {}
            session = (payload.get("journal_execution") or {}).get("simulation_session_id") or "-"
            name = self._source_name(source, instance_id)
            key = f"{name}:{instance_id or '-'}:{session}:{alert_id}"
            if known.get(key, (None, 0))[1]:
                continue
            parked = source.pending_intents(instance_id) if source.pending_intents else None
            age = (now - (_dt(row.get("received_at")) or now)).total_seconds()
            status = "PENDING"
            if parked is not None and alert_id not in parked and age > INTENT_GRACE_S:
                status = "CANCELLED"
            journal_exec = payload.get("journal_execution") or {}
            gate = payload.get("journal_quality_gate") or {}
            signal_at = _ts(payload.get("timestamp"))
            rec = {
                "execution_key": key, "record_source": name, "record_origin": "FORWARD_PAPER",
                "verification": "VERIFIED", "status": status,
                "outcome": "CANCELLED" if status == "CANCELLED" else None,
                "intent_id": alert_id, "order_id": alert_id,
                "decision_id": str(payload.get("journal_decision_id"))
                if payload.get("journal_decision_id") is not None else None,
                "signal_id": payload.get("decision_identity"),
                "session_id": session if session != "-" else None,
                "instance_id": instance_id or None, "lab_id": source.lab_id,
                "strategy_id": payload.get("strategy_id"),
                "strategy_name": journal_exec.get("strategy_name") or payload.get("strategy"),
                "strategy_version": journal_exec.get("strategy_version"),
                "symbol": row["symbol"], "side": _side(row.get("side")),
                "timeframe": payload.get("timeframe"),
                "exchange": journal_exec.get("exchange"),
                "market_type": journal_exec.get("instrument_type"),
                "signal_detected_at": signal_at,
                "decision_created_at": _ts(row.get("received_at")),
                "intent_created_at": _ts(row.get("received_at")),
                "order_submitted_at": _ts(row.get("received_at")),
                "setup_type": gate.get("setup_type"),
                "market_regime": payload.get("regime") or gate.get("regime"),
                "trading_session": trading_session(signal_at), "htf_bias": gate.get("htf_bias"),
                "planned_entry": _f(payload.get("entry")),
                "planned_stop_loss": _f(payload.get("stop")),
                "planned_take_profit": _f(payload.get("target")),
                "planned_rr": _rr(payload.get("entry"), payload.get("stop"), payload.get("target")),
                "requested_entry": _f(payload.get("entry")),
                "execution_status": ("INTENT_NOT_FILLED" if status == "CANCELLED"
                                     else "AWAITING_NEXT_QUOTE"),
                "exit_reason": ("intent left the instance's parked orders without a fill"
                                if status == "CANCELLED" else None),
                "source_ref_json": {"ledger": source.name, "alert_id": alert_id},
            }
            self.store.upsert_trade(_finish(rec, [] if status == "CANCELLED" else
                                            ["entry_fill"], self.CORE),
                                    events=_timeline(rec))
            written += 1
        return written


def _ledger_origin(forward: bool, journal_exec: dict, payload: dict) -> tuple[str, str]:
    """FORWARD_PAPER only with evidence that the trade ran on live market data.

    A ledger trade with no decision-time evidence of its data source could
    have come from synthetic, demo or replayed candles; counting it as
    forward paper would let it into forward-paper statistics unproven.
    """
    mode = str(journal_exec.get("market_data_mode") or "").lower()
    source = str(payload.get("market_data_source") or journal_exec.get("market_data_source")
                 or "").lower()
    # An explicit simulated-data marker wins over everything else.
    if mode in ("synthetic", "demo", "replay", "backtest") or any(
            word in source for word in ("synthetic", "demo", "replay", "backtest")):
        return "SIMULATION", f"decision recorded market data as {mode or source}"
    if forward:
        return "FORWARD_PAPER", "filled from a live Binance quote after the decision"
    if mode == "live" or any(word in source for word in ("live", "binance", "websocket")):
        return "FORWARD_PAPER", f"decision recorded live market data ({mode or source})"
    return "LEGACY_MIGRATION", ("no decision-time evidence of the market data source; kept "
                                "out of forward-paper statistics")


# ======================================================================
# decision projector (instance decision stores)
# ======================================================================
class DecisionProjector:
    def __init__(self, store: TradeRecordStore):
        self.store = store

    def project(self, source_name: str, decision_store, *, lab_id: Optional[str] = None,
                since_id: int = 0) -> dict:
        conn = getattr(decision_store, "_c", None)
        lock = getattr(decision_store, "_lock", None)
        if conn is None:
            return {"source": source_name, "skipped": "no decision store"}
        rows = _query(conn, lock, "SELECT * FROM decisions WHERE id > ? ORDER BY id", (since_id,))
        linked = {}
        for rec in self.store.query_trades(
                where="decision_id IS NOT NULL AND record_source IN (?,?,?)",
                params=("INSTANCE", "LEGACY_ENGINE", "ADAPTIVE_LAB"), limit=100000):
            linked[(rec.get("instance_id") or "", str(rec["decision_id"]))] = rec
        written = 0
        last = since_id
        for row in rows:
            last = max(last, int(row["id"]))
            instance_id = row.get("instance_id") or ""
            name = source_name if source_name != "MAIN" else (
                "INSTANCE" if instance_id else "LEGACY_ENGINE")
            identity = row.get("decision_identity") or f"id:{row['id']}"
            key = f"{name}:decision:{instance_id or '-'}:{identity}"
            trade = linked.get((instance_id, str(row["id"])))
            dtype = classify_decision(row.get("final_state"), row.get("gate_stage"),
                                      row.get("blocker"), row.get("reason"))
            if trade is not None:
                dtype = "TRADE_OPENED"
            self.store.upsert_decision({
                "decision_key": key, "record_source": name, "record_origin": "FORWARD_PAPER",
                "instance_id": instance_id or None, "lab_id": lab_id,
                "strategy_name": row.get("strategy"), "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"), "side": _side(row.get("side")),
                "candle_time": _ts(row.get("ts")), "decided_at": _ts(row.get("ts")),
                "signal": (_side(row.get("side")) or "").upper() or None,
                "decision_type": dtype, "status": row.get("final_state") or None,
                "blocker": row.get("blocker") or None, "reason": row.get("reason"),
                "conditions_passed": _json(row.get("passed_json"), []),
                "conditions_missing": _json(row.get("failed_json"), []),
                "evidence": {"regime": row.get("regime"), "htf_bias": row.get("htf_bias"),
                             "setup_quality_score": row.get("setup_quality_score"),
                             "rr_score": row.get("rr_score"), "confidence": row.get("confidence"),
                             "components": _json(row.get("components_json"), {}),
                             "gate_stage": row.get("gate_stage")},
                "source_ref": {"decision_store": source_name, "decision_row_id": row["id"]},
                "journal_record_id": trade["journal_record_id"] if trade else None,
                "trade_id": trade.get("trade_id") if trade else None,
            })
            written += 1
        return {"source": source_name, "written": written, "last_id": last}

    def project_feed_incidents(self, source_name: str, cycle_store, *,
                               since_id: int = 0) -> dict:
        """One decision record per run of data-blocked candles, not one per candle."""
        conn = getattr(cycle_store, "_c", None)
        lock = getattr(cycle_store, "_lock", None)
        if conn is None:
            return {"source": source_name, "skipped": "no cycle store"}
        rows = _query(conn, lock,
                      "SELECT id, ts, symbol, timeframe, instance_id, decision, report_json "
                      "FROM cycle_reports WHERE id > ? ORDER BY instance_id, symbol, id",
                      (since_id,))
        runs: dict = {}
        last = since_id
        written = 0
        for row in rows:
            last = max(last, int(row["id"]))
            report = _json(row.get("report_json"), {}) or {}
            blocker = str(report.get("blocker") or (report.get("outcome") or {}).get("blocker")
                          or "").upper()
            material = next((kind for word, kind in (
                ("STALE", "STALE_DATA"), ("WARM", "STALE_DATA"),
                ("FEED", "FEED_UNAVAILABLE"), ("DISCONNECT", "FEED_UNAVAILABLE"),
                ("PIPELINE_ERROR", "EXECUTION_FAILED"), ("SIGNALS_ONLY", "SIGNALS_ONLY"))
                if word in blocker), None)
            scope = (row.get("instance_id") or "", row["symbol"])
            if material is None:
                runs.pop(scope, None)
                continue
            run = runs.get(scope)
            if run is None or run["type"] != material:
                run = runs[scope] = {"type": material, "first": row, "count": 0}
            run["count"] += 1
            instance_id = scope[0]
            name = source_name if source_name != "MAIN" else (
                "INSTANCE" if instance_id else "LEGACY_ENGINE")
            first = run["first"]
            self.store.upsert_decision({
                "decision_key": f"{name}:incident:{instance_id or '-'}:{row['symbol']}:"
                                f"{material}:{first['id']}",
                "record_source": name, "record_origin": "FORWARD_PAPER",
                "instance_id": instance_id or None, "symbol": row["symbol"],
                "timeframe": row.get("timeframe"), "candle_time": _ts(first["ts"]),
                "decided_at": _ts(first["ts"]), "decision_type": material,
                "status": "INCIDENT", "blocker": blocker,
                "reason": f"{run['count']} consecutive candle(s) blocked: {blocker}",
                "market_data_state": material,
                "evidence": {"first_cycle_id": first["id"], "last_cycle_id": row["id"],
                             "last_candle": _ts(row["ts"]), "candles": run["count"]},
                "source_ref": {"cycle_store": source_name},
            })
            written += 1
        return {"source": source_name, "written": written, "last_id": last}


# ======================================================================
# the recorder: one writer, a durable reconcile loop, cheap notifications
# ======================================================================
class JournalRecorder:
    def __init__(self, store: TradeRecordStore, *, interval_s: Optional[float] = None):
        self.store = store
        self.interval_s = max(5.0, float(interval_s if interval_s is not None else
                                         os.environ.get("HUB_JOURNAL_RECONCILE_S", "30")))
        self.ledgers: list[LedgerSource] = []
        self.labs: list = []                 # lab projectors (services/journal_labs.py)
        self.legacy = None                   # legacy journal migration
        self.after_pass: list[Callable[[], None]] = []
        self._ledger = LedgerProjector(store)
        self._decisions = DecisionProjector(store)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pass_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.last_report: dict = {}
        self.last_error: Optional[str] = None
        self.passes = 0

    def add_ledger(self, source: LedgerSource) -> None:
        self.ledgers.append(source)

    def notify(self, *_args, **_kwargs) -> None:
        """Called from the execution path; never blocks and never raises."""
        self._wake.set()

    def reconcile(self) -> dict:
        """One full, idempotent pass. Safe to run at any time, any number of times."""
        with self._pass_lock:
            started = time.monotonic()
            report: dict = {"at": utcnow(), "ledgers": [], "labs": [], "decisions": []}
            errors = []
            for source in self.ledgers:
                try:
                    report["ledgers"].append(self._ledger.project(source))
                except Exception as exc:  # noqa: BLE001 -- one source cannot stop the others
                    errors.append(f"ledger {source.name}: {type(exc).__name__}: {exc}")
            for lab in self.labs:
                try:
                    report["labs"].append(lab.project(self.store))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"lab {getattr(lab, 'name', lab)}: {type(exc).__name__}: {exc}")
            if self.legacy is not None:
                try:
                    report["legacy"] = self.legacy.migrate(self.store)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"legacy migration: {type(exc).__name__}: {exc}")
            for source in self.ledgers:
                try:
                    if source.decision_store is not None:
                        key = f"decisions_watermark:{source.name}"
                        since = int(self.store.state(key, 0) or 0)
                        # Decisions link to trades by id; re-read a window so a
                        # decision recorded just before its trade gets its link.
                        out = self._decisions.project(source.name, source.decision_store,
                                                      lab_id=source.lab_id,
                                                      since_id=max(0, since - 500))
                        self.store.set_state(key, out.get("last_id", since))
                        report["decisions"].append(out)
                    if source.cycle_store is not None:
                        key = f"cycles_watermark:{source.name}"
                        since = int(self.store.state(key, 0) or 0)
                        out = self._decisions.project_feed_incidents(
                            source.name, source.cycle_store, since_id=since)
                        self.store.set_state(key, out.get("last_id", since))
                        report["decisions"].append(out)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"decisions {source.name}: {type(exc).__name__}: {exc}")
            for hook in self.after_pass:
                try:
                    hook()
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"after-pass {getattr(hook, '__name__', hook)}: "
                                  f"{type(exc).__name__}: {exc}")
            report["errors"] = errors
            report["seconds"] = round(time.monotonic() - started, 3)
            self.last_report = report
            self.last_error = errors[-1] if errors else None
            self.passes += 1
            self.store.set_state("last_reconcile", {"at": report["at"], "errors": errors,
                                                    "seconds": report["seconds"]})
            return report

    # ---------------------------------------------------------------- loop
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="journal-recorder", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001 -- the loop must survive
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._wake.wait(self.interval_s)
            self._wake.clear()
            if self._wake.is_set():
                continue
            # coalesce a burst of notifications into one pass
            time.sleep(0.2)

    def status(self) -> dict:
        return {"running": self.running, "interval_s": self.interval_s, "passes": self.passes,
                "last_error": self.last_error,
                "last_reconcile": self.store.state("last_reconcile"),
                "sources": [s.name for s in self.ledgers] + [getattr(l, "name", "lab")
                                                             for l in self.labs]}


__all__ = ["JournalRecorder", "LedgerSource", "LedgerProjector", "DecisionProjector",
           "classify_outcome", "classify_decision", "trading_session", "BREAKEVEN_R",
           "record_id_for"]
