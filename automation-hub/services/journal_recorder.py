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
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from bot.data.resample import TF_SECONDS
from data.ledger import SqliteLedger
from data.trade_record_store import TradeRecordStore, record_id_for, utcnow

#: |realized R| at or below this is a breakeven, not a win or a loss. A trade
#: stopped at entry loses its fees; calling that a loss would hide stop
#: management in the loss count. Documented on every record as outcome_basis.
BREAKEVEN_R = 0.05
#: SignalPipeline's close-side vocabulary (services/signal_pipeline.py).
_CLOSE_SIDES = {"REDUCE", "CLOSE", "EXIT", "FLAT", "FLATTEN"}
#: Why a position ended when an account restart cancelled what was still open.
RESTART_EXIT_REASON = "account-restart"
#: Why a position ended when a paper-account reset deleted it from the ledger.
PAPER_RESET_EXIT_REASON = "paper-reset"
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
    def total(field):                    # unknown stays unknown: one missing leg value
        values = [_f(l.get(field)) for l in legs]
        return None if None in values else sum(values)

    prices = [_f(l.get("price")) for l in legs]
    exit_price = (None if None in prices else
                  sum(p * (_f(l.get("size")) or 0.0) for p, l in zip(prices, legs)) / size)
    net, fees = total("pnl"), total("fees")
    funding = None
    if any(l.get("funding") is not None for l in legs):
        funding = sum(_f(l.get("funding")) or 0.0 for l in legs)
    risk = _f(risk_amount)
    realized_r = round(net / risk, 4) if net is not None and risk and risk > 0 else None
    entry_f, stop_f = _f(entry), _f(stop)
    achieved = None
    if entry_f is not None and stop_f is not None and entry_f != stop_f and exit_price is not None:
        sign = 1.0 if side == "long" else -1.0
        achieved = round(sign * (exit_price - entry_f) / abs(entry_f - stop_f), 4)
    mae = _f(mae_r)
    return {
        "actual_exit": round(exit_price, 10) if exit_price is not None else None,
        "exit_reason": exit_reason,
        # net is after fees and funding, so the price P&L adds both back
        "gross_pnl": (round(net + fees + (funding or 0.0), 10)
                      if net is not None and fees is not None else None),
        "fees": round(fees, 10) if fees is not None else None,
        "funding": funding,
        "net_pnl": round(net, 10) if net is not None else None,
        "realized_r": realized_r, "achieved_rr": achieved,
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
            # A cancelled, rejected or failed record never exits: nothing is pending.
            state = ("NOT_REACHED" if status in ("CANCELLED", "REJECTED", "EXECUTION_FAILED")
                     else "PENDING")
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


def _finish(rec: dict, missing: list[str], core: Iterable[str], *,
            latency_from: Optional[tuple[Optional[str], str]] = None) -> dict:
    """Derived fields. ``latency_from`` is (start, basis) for the decision
    latency, when it does not simply run from the signal time; a None start
    means there is nothing to measure."""
    start, basis = latency_from or (rec.get("signal_detected_at"), "from the signal time")
    rec["decision_latency_ms"] = _ms_between(start, rec.get("decision_created_at"))
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
    rec["source_ref_json"]["decision_latency_basis"] = basis
    rec["source_ref_json"]["outcome_basis"] = (
        f"realized R within ±{BREAKEVEN_R} is BREAKEVEN; otherwise the sign of net P&L")
    return rec


# ======================================================================
# decision classification
# ======================================================================
#: What each structured code the producers write means. Looked up before
#: anything else: a code says what happened, a reason only describes it.
#: Sources: gate_blocker() in services/signal_pipeline.py, the engine's
#: operating-mode and order codes (services/auto_engine.py), the SMC lab's
#: candidate statuses (services/smc_strategy_lab.py) and the SMC agent's gate
#: names (services/smc_agent*.py).
_CODE_TYPES = {
    # the Decision Brain and the market-quality gate judged the setup
    "BRAIN": "QUALITY_BLOCKED", "QUALITY_SCORE": "QUALITY_BLOCKED",
    "MARKET_QUALITY": "QUALITY_BLOCKED", "PATTERN_EXPECTANCY": "QUALITY_BLOCKED",
    # the plan itself fell short
    "INSUFFICIENT_RR": "SETUP_REJECTED", "NET_RR_TOO_LOW": "SETUP_REJECTED",
    "MINIMUM_REWARD_TO_RISK": "SETUP_REJECTED", "NOT_AN_SMC_SIGNAL": "SETUP_REJECTED",
    "REJECTED": "SETUP_REJECTED", "EXPIRED": "SETUP_REJECTED", "CANCELLED": "SETUP_REJECTED",
    # market context
    "CONTEXT": "CONTEXT_BLOCKED", "MINIMUM_VOLATILITY": "CONTEXT_BLOCKED",
    # data
    "STALE_CANDLE": "STALE_DATA", "STALE_CANDLES": "STALE_DATA", "WARMUP": "STALE_DATA",
    "DATA_PAUSED": "STALE_DATA", "DATA_STALE": "STALE_DATA",
    # risk, account and operator controls
    "PAUSED": "RISK_BLOCKED", "INVALID_RISK": "RISK_BLOCKED", "RISK_LIMIT": "RISK_BLOCKED",
    "DAILY_LOSS_LIMIT": "RISK_BLOCKED", "WEEKLY_LOSS_LIMIT": "RISK_BLOCKED",
    "LOSS_COOLDOWN": "RISK_BLOCKED", "TRADE_LIMIT": "RISK_BLOCKED",
    "CORRELATED_EXPOSURE": "RISK_BLOCKED", "PORTFOLIO_EXPOSURE": "RISK_BLOCKED",
    "MAX_OPEN_POSITIONS": "RISK_BLOCKED", "DAILY_LOSS_CAP": "RISK_BLOCKED",
    "CONSECUTIVE_LOSSES": "RISK_BLOCKED", "POSITION_SIZE_CAPPED": "RISK_BLOCKED",
    "POSITION_SIZE_WITHIN_BOUNDS": "RISK_BLOCKED",
    # when and what news
    "OUTSIDE_SESSION": "SESSION_BLOCKED", "TRADING_DAY_DISABLED": "SESSION_BLOCKED",
    "SESSION_HOURS": "SESSION_BLOCKED", "EVENT_BLACKOUT": "NEWS_BLACKOUT",
    # the operating mode held the order back
    "SIGNALS_ONLY": "SIGNALS_ONLY", "SIGNAL_ONLY": "SIGNALS_ONLY",
    "APPROVAL_REQUIRED": "APPROVAL_REQUIRED", "PENDING_APPROVAL": "APPROVAL_REQUIRED",
    # duplicates and execution
    "DUPLICATE_SIGNAL": "DUPLICATE_PREVENTED", "EXECUTION": "ORDER_REJECTED",
    "INSUFFICIENT_PAPER_CAPITAL": "ORDER_REJECTED", "PIPELINE_ERROR": "EXECUTION_FAILED",
    "EXECUTION_FAILED": "EXECUTION_FAILED", "INTENT_PERSISTENCE_FAILED": "EXECUTION_FAILED",
    "EXECUTION_UNCERTAIN": "EXECUTION_UNCERTAIN",
    # an order exists (a linked trade record overrides these anyway)
    "ORDER_PENDING": "TRADE_OPENED", "APPROVED_AUTOMATIC": "TRADE_OPENED",
    "ORDER_CREATED": "TRADE_OPENED", "ENTERED": "TRADE_OPENED", "PLACED": "TRADE_OPENED",
    "FILLED": "TRADE_OPENED", "COMPLETED": "TRADE_OPENED", "OPEN": "TRADE_OPENED",
    # the SMC agent was waiting for the setup to complete
    "SMC_NOT_READY": "WAITING_CONFIRMATION",
}

#: The gate a decision stopped at, when its code is not one of the above.
_STAGE_TYPES = {
    "brain": "QUALITY_BLOCKED", "quality": "QUALITY_BLOCKED", "market_quality": "QUALITY_BLOCKED",
    "strategy": "SETUP_REJECTED", "context": "CONTEXT_BLOCKED",
    "controls": "RISK_BLOCKED", "risk": "RISK_BLOCKED", "risk_guard": "RISK_BLOCKED",
    "daily_loss": "RISK_BLOCKED", "weekly_loss": "RISK_BLOCKED", "cooldown": "RISK_BLOCKED",
    "max_trades": "RISK_BLOCKED", "correlation": "RISK_BLOCKED",
    "portfolio_exposure": "RISK_BLOCKED", "session": "SESSION_BLOCKED",
    "trading_day": "SESSION_BLOCKED", "event_risk": "NEWS_BLACKOUT",
    "dedup": "DUPLICATE_PREVENTED", "execution": "ORDER_REJECTED",
}

_HTF_WORDS = ("HTF", "HIGHER-TIMEFRAME", "HIGHER TIMEFRAME")


def _code(blocker: str) -> str:
    text = str(blocker or "").strip().upper()
    return text.split(":", 1)[1].strip() if text.startswith("GATE_REJECTED:") else text


def classify_decision(final_state: str, stage: str, blocker: str, reason: str) -> str:
    """What kind of decision this was, from the codes its producer wrote.

    Order: the terminal state, then the blocker code, then the gate stage.
    Words in the free-text reason are consulted only to refine a context
    block into an HTF block, a market-quality block into stale data, or --
    for a code nobody has mapped yet -- as a last resort. Reading the reason
    first labelled a Decision Brain block whose reason said "HTF context
    unavailable" as a feed outage.
    """
    fs = str(final_state or "").upper()
    if fs in ("FILLED", "PENDING_INTENT"):
        return "TRADE_OPENED"
    if fs in ("SIGNALS_ONLY", "APPROVAL_REQUIRED"):
        return fs
    text = str(reason or "").upper()
    kind = _CODE_TYPES.get(_code(blocker)) or _STAGE_TYPES.get(str(stage or "").lower())
    if kind == "CONTEXT_BLOCKED" and any(word in text for word in _HTF_WORDS):
        return "HTF_BLOCKED"
    if kind == "QUALITY_BLOCKED" and str(stage or "").lower() == "market_quality" \
            and ("STALE" in text or " AGE" in text):
        return "STALE_DATA"
    if kind:
        return kind
    return _classify_by_words(fs, stage, blocker, reason)


def _classify_by_words(fs: str, stage: str, blocker: str, reason: str) -> str:
    """Last resort for a code no table above knows."""
    text = f"{stage} {blocker} {reason}".upper()
    if "DUPLICATE" in text or "DEDUP" in text:
        return "DUPLICATE_PREVENTED"
    if "UNCERTAIN" in text:
        return "EXECUTION_UNCERTAIN"
    if "NEWS" in text or "BLACKOUT" in text or "EVENT_GUARD" in text:
        return "NEWS_BLACKOUT"
    if "STALE" in text or "WARM" in text:
        return "STALE_DATA"
    if "FEED" in text or "DISCONNECT" in text:
        return "FEED_UNAVAILABLE"
    if "SESSION" in text or "TRADING_DAY" in text:
        return "SESSION_BLOCKED"
    if any(word in text for word in _HTF_WORDS):
        return "HTF_BLOCKED"
    if "CONTEXT" in text:
        return "CONTEXT_BLOCKED"
    if "EXECUTION" in text and ("FAIL" in text or "ERROR" in text):
        return "EXECUTION_FAILED"
    if "ORDER" in text and "REJECT" in text:
        return "ORDER_REJECTED"
    if any(word in text for word in ("RISK", "EXPOSURE", "DRAWDOWN", "DAILY", "SIZ",
                                     "CAPITAL", "STREAK", "MAX_OPEN", "OPEN POSITION",
                                     "HALT", "PAUSE", "KILL")):
        return "RISK_BLOCKED"
    if "WAIT" in text or "CONFIRM" in text:
        return "WAITING_CONFIRMATION"
    if "SCORE" in text or "QUALITY" in text:
        return "QUALITY_BLOCKED"
    if fs == "GATE_REJECTED":
        return "SETUP_REJECTED"
    return "SIGNAL_GENERATED"


def _risk_taken(sizing: dict, risk_amount: Optional[float], equity: Optional[float]) -> dict:
    """What the sizer aimed to risk, and what the trade risked once sized.

    The sizer targets a share of equity; the exposure caps applied after it
    can only shrink the size. A 1.00% target could leave a trade risking
    0.15%, and the record showed the 1.00% beside the smaller dollar amount.
    Percentages here are in percent, like the sizing receipt next to them.
    """
    computed, accepted = _f(sizing.get("computed_size")), _f(sizing.get("accepted_size"))
    return {
        "target_pct": _f(sizing.get("effective_risk_pct")),
        "taken_pct": (round(risk_amount / equity * 100, 4)
                      if risk_amount is not None and equity else None),
        "reduced_after_sizing": (accepted < computed
                                 if accepted is not None and computed is not None else None),
        "basis": "taken = risk amount at entry / equity before the trade; a cap on the size "
                 "applied after sizing lowers it below the target",
    }


def _risk_check(gate: dict, sizing: dict, payload: dict, *, risk_amount: Optional[float] = None,
                equity: Optional[float] = None) -> dict:
    """The pre-trade receipt, frozen at decision time.

    An order exists only if every gate let it through, with one exception an
    owner can choose: the per-instance switch that turns the Decision Brain
    quality gate off (services/auto_engine.py). The Brain's verdict is frozen
    with the payload either way, so a trade whose verdict says it was not
    allowed was let through by that switch -- and the record says so rather
    than claiming every gate passed. (A score below the minimum without a hard
    block cannot be told apart here: the minimum is not frozen with the trade.)
    """
    bypassed = bool(gate) and gate.get("allowed") is False
    out = {"result": "PASSED_WITH_QUALITY_GATE_OFF" if bypassed else "PASSED",
           "sizing": sizing or None,
           "engine_guardrails": payload.get("journal_engine"),
           "size_factors": {"context": payload.get("context_size_factor"),
                            "health": payload.get("health_size_factor")},
           "risk": _risk_taken(sizing or {}, risk_amount, equity)}
    if bypassed:
        out["quality_gate"] = {"bypassed_by_owner": True, "score": gate.get("score"),
                               "grade": gate.get("grade"),
                               "would_have_blocked_for": gate.get("blocks") or []}
        out["basis"] = ("the Decision Brain would have blocked this trade, and the quality gate was "
                        "off for this instance by its owner's choice; every other pre-trade gate "
                        "passed. Values are the sizing receipt frozen at decision time")
    else:
        out["basis"] = ("the order exists, so every pre-trade gate passed; "
                        "values are the sizing receipt frozen at decision time")
    return out


# ======================================================================
# ledger projector: Trading Instances, the legacy engine, the adaptive lab
# ======================================================================
class LedgerSource:
    """One SQLite paper ledger and how to label what it holds."""

    def __init__(self, name: str, ledger, *, instances: Optional[Callable[[], dict]] = None,
                 pending_intents: Optional[Callable[[str], Optional[set]]] = None,
                 decision_store=None, lab_id: Optional[str] = None):
        self.name = name
        self.ledger = ledger
        self.instances = instances or (lambda: {})
        self.pending_intents = pending_intents
        self.decision_store = decision_store
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
        # The OPEN row names the position from the fill on; the CLOSE row only
        # exists once it is closed, so an open record read from it had none.
        position_by_trade = {e["trade_id"]: e.get("position_id") for e in execs
                             if e["action"] == "OPEN" and e.get("position_id")}
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
        # A finished trade this projection already wrote may be stored under
        # another key (imported from the legacy journal first): skip it too.
        projected = self.store.projected_trades(source.name)
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
            status, finalized = known.get(key) or projected.get(root_id) or (None, 0)
            if finalized and all(l["status"] != "open" for l in legs):
                results["skipped_final"] += 1
                continue
            record = self._record(source, conn, lock, root, legs, key, decision_events,
                                  close_by_trade, close_events, instances)
            record["position_id"] = (position_by_trade.get(root["id"])
                                     or record.get("position_id"))
            outcome = self.store.upsert_trade(record, events=_timeline(record))
            results["lifecycles"] += 1
            results["written"] += outcome["action"] != "unchanged"
        results["pending_intents"] = self._project_pending(source, conn, lock, known,
                                                           set(a for a in alert_ids if a),
                                                           instances)
        results["ended_by_reset"] = self._end_reset_records(source, conn, lock, by_id)
        return results

    def _end_reset_records(self, source: LedgerSource, conn, lock, trades: dict) -> int:
        """End OPEN records whose position a logged paper reset deleted.

        An initial-capital change runs ``SqliteLedger.reset_paper``, which
        deletes every paper trade and position, and logs the reset. A record
        still OPEN whose trade is gone, and which opened before such a
        logged reset, ends CANCELLED: nothing was sold or bought back, so no
        exit price or result is invented. A missing row without that log is
        left alone, since it could be an incomplete read."""
        names = ("INSTANCE", "LEGACY_ENGINE") if source.name == "MAIN" else (source.name,)
        missing = [rec for rec in self.store.unfinished(names)
                   if rec["status"] == "OPEN" and rec.get("trade_id") not in trades
                   and _dt(rec.get("position_opened_at")) is not None
                   and (rec.get("source_ref") or {}).get("ledger") == source.name]
        if not missing:                  # the usual pass: no log read, no ledger lock
            return 0
        earliest = min(_ts(rec["position_opened_at"]) for rec in missing)
        try:
            resets = [r["ts"] for r in _query(
                conn, lock, "SELECT ts FROM bot_logs WHERE stage='account' AND ts > ? "
                            "AND message LIKE ? ORDER BY ts",
                (earliest, f"%{SqliteLedger.PAPER_RESET_LOG}%"))]
        except sqlite3.Error:            # a ledger copy without bot logs (the mirror)
            return 0
        ended = 0
        for rec in missing:
            opened = _dt(rec.get("position_opened_at"))
            reset_at = next((_ts(ts) for ts in resets if (_dt(ts) or opened) > opened), None)
            if reset_at is None:
                continue
            rec.update({"status": "CANCELLED", "outcome": "CANCELLED",
                        "exit_reason": PAPER_RESET_EXIT_REASON,
                        "execution_status": "POSITION_REMOVED", "position_closed_at": reset_at})
            rec["source_ref"]["ended_without_exit"] = {
                "basis": "a paper-account reset (initial-capital change) deleted the position "
                         "from the ledger; no exit fill exists", "reset_logged_at": reset_at}
            self.store.upsert_trade(
                {"execution_key": rec["execution_key"], "status": "CANCELLED",
                 "outcome": "CANCELLED", "exit_reason": PAPER_RESET_EXIT_REASON,
                 "execution_status": "POSITION_REMOVED", "position_closed_at": reset_at,
                 "source_ref_json": rec["source_ref"]}, events=_timeline(rec))
            ended += 1
        return ended

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
        # A scale-out rewrites the trade row to the size it closed and puts the
        # rest on a remainder row, so the filled size is the sum of every leg.
        entry_size = sum(_f(l.get("size")) or 0 for l in legs_sorted) or _f(root.get("size"))
        closed_legs = [l for l in legs_sorted if l["status"] == "closed"]
        open_legs = [l for l in legs_sorted if l["status"] == "open"]
        # An account restart marks a position's open rows 'cancelled' (P&L 0):
        # nothing was sold or bought back for them, so they have no exit fill
        # and no result. Without any closed leg the trade was cancelled.
        ended_legs = [l for l in legs_sorted if l["status"] not in ("open", "closed")]
        status = "OPEN" if open_legs else "CLOSED" if closed_legs else "CANCELLED"
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
        if not open_legs and ended_legs and (
                not closed_legs or (ended_legs[-1].get("closed_at") or "")
                >= (closed_legs[-1].get("closed_at") or "")):
            exit_reason = RESTART_EXIT_REASON
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
            # The paper engine parks the intent as the order: one event, one
            # time. Nothing acknowledges it afterwards, so no ack time exists
            # to record (it used to repeat the intent time).
            "intent_created_at": order_at, "order_submitted_at": order_at,
            "order_acknowledged_at": None,
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
                "strategy_report": payload.get("strategy_report"),
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
            "risk_check_json": (_risk_check(gate, sizing, payload, risk_amount=risk_amount,
                                            equity=_f(root.get("equity_before_trade")))
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
        if ended_legs and not open_legs:
            last_end = max(_ts(l.get("closed_at")) or "" for l in closed_legs + ended_legs)
            rec["position_closed_at"] = last_end or rec.get("position_closed_at")
            rec["source_ref_json"]["ended_without_exit"] = {
                "trade_ids": [l["id"] for l in ended_legs],
                "ledger_status": sorted({str(l["status"]) for l in ended_legs}),
                "basis": "the ledger ended these rows without an exit fill (an account "
                         "restart marks open rows cancelled); no exit price or result exists "
                         "for them"}
        if status == "CANCELLED":
            rec.update({"outcome": "CANCELLED", "exit_reason": exit_reason,
                        "execution_status": "POSITION_CANCELLED"})
        return _finish(rec, missing, self.CORE,
                       latency_from=_ledger_latency_start(origin, signal_at, rec["timeframe"], payload))

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


def _ledger_latency_start(origin: str, signal_at: Optional[str], timeframe: Optional[str],
                          payload: dict) -> tuple[Optional[str], str]:
    """Where a ledger decision's latency starts, and why.

    The engine stamps a signal with its candle's open time, but nothing about
    that candle is known until it closes; measured from the open, every
    decision on 5m candles looked five minutes slow. A replay decides on
    replayed candle times while the decision row carries the wall clock, and
    there is no latency between two different clocks.
    """
    if origin == "SIMULATION":
        return None, ("not measured: the signal time is a replayed candle's and the decision "
                      "time is the wall clock")
    seconds, opened = TF_SECONDS.get(str(timeframe or "")), _dt(signal_at)
    if payload.get("decision_identity") and seconds and opened is not None:
        return ((opened + timedelta(seconds=seconds)).isoformat(),
                f"from the close of the {timeframe} signal candle")
    return signal_at, "from the signal time"


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
                since_id: int = 0, instances: Optional[Callable[[], dict]] = None) -> dict:
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
        meta = (instances() if instances else {}) or {}
        for row in rows:
            last = max(last, int(row["id"]))
            instance_id = row.get("instance_id") or ""
            name = source_name if source_name != "MAIN" else (
                "INSTANCE" if instance_id else "LEGACY_ENGINE")
            identity = row.get("decision_identity") or f"id:{row['id']}"
            key = f"{name}:decision:{instance_id or '-'}:{identity}"
            trade = linked.get((instance_id, str(row["id"])))
            # A forward intent's record exists from the moment it is parked.
            # Only a filled one is a trade; an unfilled order keeps the
            # decision's own outcome and is referenced, not linked as a trade.
            order_record = None
            if trade is not None and not (trade.get("entry_filled_at")
                                          or trade.get("status") in ("OPEN", "CLOSED")):
                order_record, trade = trade, None
            dtype = classify_decision(row.get("final_state"), row.get("gate_stage"),
                                      row.get("blocker"), row.get("reason"))
            status, blocker, reason = (row.get("final_state") or None, row.get("blocker") or None,
                                       row.get("reason"))
            later = None
            if trade is not None:
                dtype = "TRADE_OPENED"
                # A trade exists only after its order filled. The decision
                # store keeps one row per candle, and a worker that re-evaluates
                # that candle after a restart finalizes the SAME row with its
                # refusal ("duplicate"), overwriting the decision that opened
                # the trade. The trade link proves what happened first; the
                # refused re-evaluation is kept beside it, not in its place.
                if str(row.get("gate_stage") or "").lower() == "dedup":
                    later = {"final_state": status, "gate_stage": row.get("gate_stage"),
                             "blocker": blocker, "reason": reason}
                    reason = ("Signal accepted and its order filled. A later evaluation of the same "
                              "candle was refused as a duplicate.")
                status, blocker = "FILLED", None
            origin = self._decision_origin(key, trade, meta.get(instance_id))
            self.store.upsert_decision({
                "decision_key": key, "record_source": name, "record_origin": origin,
                "instance_id": instance_id or None, "lab_id": lab_id,
                "strategy_name": row.get("strategy"), "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"), "side": _side(row.get("side")),
                "candle_time": _ts(row.get("ts")), "decided_at": _ts(row.get("ts")),
                "signal": (_side(row.get("side")) or "").upper() or None,
                "decision_type": dtype, "status": status, "blocker": blocker, "reason": reason,
                "conditions_passed": _json(row.get("passed_json"), []),
                "conditions_missing": _json(row.get("failed_json"), []),
                "evidence": {"regime": row.get("regime"), "htf_bias": row.get("htf_bias"),
                             "setup_quality_score": row.get("setup_quality_score"),
                             "rr_score": row.get("rr_score"), "confidence": row.get("confidence"),
                             "components": _json(row.get("components_json"), {}),
                             "gate_stage": row.get("gate_stage"),
                             **({"duplicate_attempt": later} if later else {})},
                "source_ref": {"decision_store": source_name, "decision_row_id": row["id"],
                               **({"order_record_id": order_record["journal_record_id"],
                                   "order_record_status": order_record.get("status")}
                                  if order_record else {})},
                "journal_record_id": trade["journal_record_id"] if trade else None,
                "trade_id": trade.get("trade_id") if trade else None,
            }, overwrite=("journal_record_id", "trade_id"))   # every link is known here
            written += 1
        return {"source": source_name, "written": written, "last_id": last}

    def _decision_origin(self, key: str, trade: Optional[dict], instance: Optional[dict]) -> str:
        """Which market data a decision ran on, from what can show it.

        A decision that became a trade takes that trade's origin, which the
        ledger projector proved from the fill. Otherwise the instance's mode
        says it: a "trading" instance runs on the live forward feed, any other
        mode replays candles. A decision whose instance is no longer known keeps
        the origin it was first recorded with; one never recorded before, with
        nothing to show its data, is not called forward paper.
        """
        if trade is not None and trade.get("record_origin"):
            return trade["record_origin"]
        mode = (instance or {}).get("mode")
        if mode:
            return "FORWARD_PAPER" if mode == "trading" else "SIMULATION"
        existing = self.store.get_decision(key)
        return existing["record_origin"] if existing else "LEGACY_MIGRATION"

    def project_outages(self, source_name: str, ledger, *, since_ts: str = "") -> dict:
        """One decision record per market-data outage of a Trading Instance.

        A worker whose closed candles go stale does not evaluate a candle at
        all: the engine raises before any decision or cycle report exists, so
        the outage is visible only in the lifecycle events the instance
        manager writes to ``instance_engine_logs`` (``instance_event {json}``,
        services/instance_telemetry.py). The engine emits MARKET_STALE, then
        MARKET_DISCONNECTED / MARKET_CONNECTING for each recovery attempt, and
        MARKET_CONNECTED when fresh data returns -- so the outage is the span
        from the first MARKET_STALE to MARKET_CONNECTED (or to the worker
        stopping), and it is one record however many retries it took.

        ``since_ts`` is where the previous pass left off: the start of the
        earliest outage still open, or the last event it read.
        """
        conn = getattr(ledger, "_c", None)
        lock = getattr(ledger, "_lock", None)
        if conn is None:
            return {"source": source_name, "skipped": "no sqlite ledger"}
        try:
            rows = _query(conn, lock,
                          "SELECT id, instance_id, ts, message FROM instance_engine_logs "
                          "WHERE message LIKE 'instance_event %' AND ts >= ? "
                          "ORDER BY instance_id, ts, id", (since_ts or "",))
        except Exception as exc:  # noqa: BLE001 -- a ledger without instances
            if "no such table" in str(exc):
                return {"source": source_name, "skipped": "no instance_engine_logs"}
            raise
        name = "INSTANCE" if source_name == "MAIN" else source_name
        open_by_instance: dict = {}
        outages: list[dict] = []
        last_ts = since_ts or ""
        for row in rows:
            last_ts = max(last_ts, str(row["ts"]))
            try:
                event = json.loads(str(row["message"])[len("instance_event "):])
            except ValueError:
                continue
            kind = str(event.get("event") or "")
            instance_id = row["instance_id"]
            current = open_by_instance.get(instance_id)
            if kind in _OUTAGE_OPENS:
                if current is None:
                    current = open_by_instance[instance_id] = {
                        "first": row, "first_event": event, "last_event": event,
                        "attempts": 0, "end": None, "resolution": None}
                    outages.append(current)
                else:
                    current["last_event"] = event
                if kind == "MARKET_DISCONNECTED":
                    current["attempts"] += 1
            elif current is not None and kind in _OUTAGE_ENDS:
                current["end"], current["resolution"] = row, _OUTAGE_ENDS[kind]
                open_by_instance.pop(instance_id, None)
        for outage in outages:
            self._upsert_outage(name, outage)
        still_open = [str(o["first"]["ts"]) for o in open_by_instance.values()]
        return {"source": source_name, "written": len(outages), "open": len(still_open),
                "watermark": min(still_open) if still_open else last_ts}

    def _upsert_outage(self, name: str, outage: dict) -> None:
        first, event, last = outage["first"], outage["first_event"], outage["last_event"]
        stale = "stale" in str(event.get("detail") or "").lower()
        dtype = "STALE_DATA" if stale else "FEED_UNAVAILABLE"
        started = _ts(first["ts"])
        ended = _ts(outage["end"]["ts"]) if outage["end"] is not None else None
        seconds = round((_dt(ended) - _dt(started)).total_seconds(), 1) if ended else None
        resolution = outage["resolution"] or "OPEN"
        what = "Closed candles were stale" if stale else "The market data feed was unavailable"
        if ended:
            reason = (f"{what} from {started} to {ended} ({seconds:.0f}s, "
                      f"{outage['attempts']} recovery attempt(s)); the worker evaluated no candle "
                      f"in that time. Ended: {resolution.replace('_', ' ').lower()}.")
        else:
            reason = (f"{what} since {started} ({outage['attempts']} recovery attempt(s) so far); "
                      "the worker evaluates no candle until fresh data returns.")
        self.store.upsert_decision({
            "decision_key": f"{name}:outage:{first['instance_id']}:{first['id']}",
            "record_source": name, "record_origin": "FORWARD_PAPER",
            "instance_id": first["instance_id"],
            "strategy_id": event.get("strategy_id"), "strategy_name": event.get("strategy_id"),
            "strategy_version": event.get("strategy_version"),
            "symbol": event.get("symbol"), "timeframe": event.get("timeframe"),
            "candle_time": started, "decided_at": started,
            "decision_type": dtype, "status": resolution,
            "blocker": "MARKET_STALE" if stale else "MARKET_DISCONNECTED",
            "reason": reason, "market_data_state": "STALE" if stale else "UNAVAILABLE",
            "evidence": {"started_at": started, "ended_at": ended, "duration_s": seconds,
                         "resolution": resolution, "recovery_attempts": outage["attempts"],
                         "first_detail": event.get("detail"), "last_detail": last.get("detail"),
                         "basis": "instance lifecycle events (instance_engine_logs)"},
            "source_ref": {"instance_engine_logs": {"first": first["id"],
                                                    "last": (outage["end"] or {}).get("id")}},
        })


#: Lifecycle events that open or continue a market-data outage, and those
#: that end one (services/trading_instances._LIFECYCLE_EVENTS).
_OUTAGE_OPENS = ("MARKET_STALE", "MARKET_DISCONNECTED")
_OUTAGE_ENDS = {"MARKET_CONNECTED": "RESOLVED", "INSTANCE_ERROR": "WORKER_STOPPED_ON_ERROR",
                "INSTANCE_STOPPED": "INSTANCE_STOPPED"}


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
        self.before_pass: list[Callable[[], None]] = []
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
            for hook in self.before_pass:
                # e.g. the Supabase ledger mirror catching up before it is read
                try:
                    hook()
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"before-pass {getattr(hook, '__name__', hook)}: "
                                  f"{type(exc).__name__}: {exc}")
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
                                                      since_id=max(0, since - 500),
                                                      instances=source.instances)
                        self.store.set_state(key, out.get("last_id", since))
                        report["decisions"].append(out)
                    key = f"outages_watermark:{source.name}"
                    since = str(self.store.state(key, "") or "")
                    out = self._decisions.project_outages(source.name, source.ledger,
                                                          since_ts=since)
                    if "watermark" in out:
                        self.store.set_state(key, out["watermark"])
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
