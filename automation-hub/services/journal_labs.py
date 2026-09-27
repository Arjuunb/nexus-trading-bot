"""Canonical records for the PA lab, the SMC lab and the SMC agent -- read-only.

Both labs trade through ``PaperBrokerV2``, which stores every fill with its
decision-time evidence (signal/decision/order/fill timestamps, requested and
signal price, spread, slippage, commission, stop, target, risk, strategy
version, candle id). The strategy files that own these labs are frozen, so
nothing here calls into them or writes their databases: it reads the broker
tables and the labs' metadata tables and projects what they already hold.

A lifecycle is paired from the broker's own facts: a fill that takes the
symbol's position from flat opens it, fills that reduce it belong to it, and
it closes when the position is flat again -- whatever order id a protective
stop or target used. The entry order's metadata links the lifecycle to its
proposal, setup and frozen evaluation.

The SMC agent does not have its own broker: it decides whether the SMC lab's
proposals execute. Its trade is therefore the same execution as the SMC lab
lifecycle for that proposal, and gets merged into it (agent id, decision id,
imported review) rather than written twice. Agent intents that never produced
a fill become EXECUTION_FAILED / EXECUTION_UNCERTAIN records.
"""
from __future__ import annotations

from typing import Optional

from data.trade_record_store import TradeRecordStore
from services.journal_recorder import (_f, _finish, _json, _result_fields, _rr, _side, _timeline,
                                       _ts, classify_decision, trading_session)

_CORE = ("evaluation", "planned_stop_loss", "entry_fill", "risk_amount")
# Both labs stamp a decision with its signal candle's close time -- a candle
# time, not the moment the lab decided -- so a "decision latency" from the
# signal time would only restate the candle length (or zero).
_LAB_LATENCY = (None, "not measured: the lab stamps its decision with the signal candle's "
                      "close time, not the moment it decided")
_OPEN_ORDER = ("new", "open", "pending", "accepted", "triggered", "partially_filled")


def _pct_fraction(value) -> Optional[float]:
    number = _f(value)
    return round(number / 100, 10) if number is not None else None


def _rows(conn, lock, sql: str, args=()) -> list[dict]:
    if conn is None:
        return []
    try:
        if lock is not None:
            with lock:
                return [dict(r) for r in conn.execute(sql, list(args)).fetchall()]
        return [dict(r) for r in conn.execute(sql, list(args)).fetchall()]
    except Exception:  # noqa: BLE001 -- a table this build does not have
        return []


def _pair_lifecycles(fills: list[dict]) -> list[dict]:
    """Group broker fills into position lifecycles per symbol."""
    position: dict[str, float] = {}
    current: dict[str, dict] = {}
    lifecycles: list[dict] = []
    for fill in fills:
        symbol = fill["symbol"]
        qty = float(fill.get("quantity") or 0.0)
        signed = qty if str(fill.get("side")).lower() == "buy" else -qty
        before = position.get(symbol, 0.0)
        after = before + signed
        if abs(before) < 1e-12:
            life = {"symbol": symbol, "entry": fill, "entries": [fill], "exits": [],
                    "side": "long" if signed > 0 else "short", "closed": False}
            current[symbol] = life
            lifecycles.append(life)
        else:
            life = current[symbol]
            if (before > 0) == (signed > 0):
                life["entries"].append(fill)       # scale-in on the same side
            else:
                life["exits"].append(fill)
        position[symbol] = 0.0 if abs(after) < 1e-9 else after
        if abs(after) < 1e-9:
            current[symbol]["closed"] = True
            current.pop(symbol, None)
    return lifecycles


class V2LabProjector:
    """Shared projection for PaperBrokerV2 labs; subclasses add lab evidence."""

    name = "LAB"
    record_source = "LAB"
    lab_id = "lab"

    def __init__(self, account, *, agent_journal=None):
        self.account = account
        self.agent_journal = agent_journal

    # -------------------------------------------------------------- access
    def _broker(self):
        broker = getattr(self.account, "broker", None)
        return getattr(broker, "_c", None), getattr(broker, "_lock", None)

    def _meta(self):
        return getattr(self.account, "_db", None), getattr(self.account, "_lock", None)

    # ------------------------------------------------------------ overrides
    def order_meta(self) -> dict:
        return {}

    def evaluation(self, meta: dict) -> dict:
        return {}

    def session_origin(self, session_id: Optional[str], fill: dict) -> str:
        return "FORWARD_PAPER"

    # -------------------------------------------------------------- project
    def project(self, store: TradeRecordStore) -> dict:
        conn, lock = self._broker()
        if conn is None:
            return {"source": self.name, "skipped": "no broker"}
        fills = _rows(conn, lock, "SELECT * FROM v2_fills ORDER BY timestamp, rowid")
        orders = {o["id"]: o for o in _rows(conn, lock, "SELECT * FROM v2_orders")}
        metas = self.order_meta()
        known = store.keys_with_status((self.record_source,))
        written = seen = 0
        entry_orders = set()
        for life in _pair_lifecycles(fills):
            entry = life["entry"]
            meta = metas.get(entry["order_id"]) or {}
            entry_orders.add(entry["order_id"])
            key = self._key(meta, entry)
            if known.get(key, (None, 0))[1] and life["closed"]:
                seen += 1
                self._late_agent_link(store, key, meta)
                continue
            record = self._record(life, meta, orders, key)
            self._link_agent(record, meta)
            store.upsert_trade(record, events=_timeline(record))
            written += 1
        written += self._unfilled_orders(store, metas, orders, entry_orders, known)
        decisions = self.project_decisions(store)
        return {"source": self.name, "written": written, "skipped_final": seen,
                "decisions": decisions}

    def _key(self, meta: dict, entry: dict) -> str:
        session = meta.get("session_id") or "-"
        anchor = meta.get("proposal_id") or entry["order_id"]
        return f"{self.record_source}:{session}:{anchor}"

    def _record(self, life: dict, meta: dict, orders: dict, key: str) -> dict:
        entry = life["entry"]
        side = life["side"]
        evaluation = self.evaluation(meta)
        missing = [] if evaluation else ["evaluation"]
        entries = life["entries"]
        filled_qty = sum(float(f.get("quantity") or 0) for f in entries)
        avg_entry = (sum(float(f["price"]) * float(f.get("quantity") or 0) for f in entries)
                     / filled_qty) if filled_qty else _f(entry.get("price"))
        stop = _f(entry.get("stop_loss")) or _f(meta.get("stop"))
        target = _f(entry.get("take_profit")) or _f(meta.get("target_1") or meta.get("target"))
        planned_entry = _f(meta.get("entry")) or _f(entry.get("requested_price"))
        risk_amount = _f(entry.get("risk_amount"))
        if stop is None:
            missing.append("planned_stop_loss")
        if risk_amount is None:
            missing.append("risk_amount")
        signal_at = _ts(entry.get("signal_timestamp"))
        entry_order = orders.get(entry["order_id"]) or {}
        rec = {
            "execution_key": key, "record_source": self.record_source,
            "record_origin": self.session_origin(meta.get("session_id"), entry),
            "verification": "VERIFIED", "status": "CLOSED" if life["closed"] else "OPEN",
            "trade_id": f"{self.lab_id}:{entry['id']}",
            "signal_id": entry.get("candle_id"),
            "intent_id": entry_order.get("decision_key") or meta.get("proposal_id"),
            "order_id": entry["order_id"], "position_id": f"{self.lab_id}:{entry['id']}",
            "session_id": meta.get("session_id"), "lab_id": self.lab_id,
            "strategy_id": meta.get("strategy_id") or meta.get("model_id") or entry.get("strategy"),
            "strategy_name": entry.get("strategy") or meta.get("model_id") or meta.get("strategy_id"),
            "strategy_version": entry.get("strategy_version") or meta.get("model_version"),
            "symbol": life["symbol"], "exchange": "binance_usdm", "market_type": "perpetual",
            "timeframe": entry.get("timeframe"), "side": side,
            "signal_detected_at": signal_at,
            "decision_created_at": _ts(entry.get("decision_timestamp")),
            "intent_created_at": _ts(meta.get("created_at")) or _ts(entry.get("order_timestamp")),
            # The fill's order time is the broker's order-row time; the paper
            # broker sends no separate acknowledgement, so none is recorded.
            "order_submitted_at": _ts(entry.get("order_timestamp")),
            "order_acknowledged_at": None,
            "entry_filled_at": _ts(entry.get("fill_timestamp") or entry.get("timestamp")),
            "position_opened_at": _ts(entry.get("fill_timestamp") or entry.get("timestamp")),
            "trading_session": trading_session(signal_at),
            "setup_type": evaluation.get("setup_type"),
            "market_regime": evaluation.get("market_regime"),
            "htf_bias": evaluation.get("htf_bias"),
            "setup_json": evaluation.get("setup") or None,
            "evidence_json": evaluation.get("evidence") or None,
            "signal_price": _f(entry.get("signal_price")), "planned_entry": planned_entry,
            "planned_stop_loss": stop, "planned_take_profit": target,
            "planned_rr": _rr(planned_entry or avg_entry, stop, target),
            # The lab configures risk in percent (0.5 = 0.5%); the record keeps
            # a fraction of equity, as instance records do (0.01 = 1%).
            "risk_percent": _pct_fraction(meta.get("risk_pct")), "risk_amount": risk_amount,
            "quantity": _f(entry_order.get("quantity")) or filled_qty,
            "risk_check_json": evaluation.get("risk_check"),
            "requested_entry": _f(entry.get("requested_price")),
            "actual_entry": round(avg_entry, 10) if avg_entry is not None else None,
            "requested_quantity": _f(entry_order.get("quantity")),
            "filled_quantity": filled_qty,
            "spread": _f(entry.get("spread")), "slippage": _f(entry.get("slippage")),
            "order_type": (entry_order.get("type") or "").upper() or None,
            "execution_status": "FILLED", "fill_model": "NEXT_QUOTE",
            "legs_json": [{"fill_id": f["id"], "order_id": f["order_id"], "side": f["side"],
                           "quantity": f.get("quantity"), "price": f.get("price"),
                           "fee": f.get("fee"), "realized_pnl": f.get("realized_pnl"),
                           "at": f.get("timestamp")} for f in entries + life["exits"]],
            "source_ref_json": {"lab": self.name, "entry_fill_id": entry["id"],
                                "proposal_id": meta.get("proposal_id"),
                                "setup_id": meta.get("setup_id")},
        }
        if life["closed"] and life["exits"]:
            exits = life["exits"]
            entry_fees = sum(float(f.get("fee") or 0) for f in entries)
            legs = [{"price": f["price"], "size": f.get("quantity"),
                     "pnl": float(f.get("realized_pnl") or 0) - float(f.get("fee") or 0),
                     "fees": float(f.get("fee") or 0),
                     "funding": f.get("funding")} for f in exits]
            # The entry commission is a cost of this trade too.
            legs[0]["pnl"] -= entry_fees
            legs[0]["fees"] += entry_fees
            last = exits[-1]
            exit_order = orders.get(last["order_id"]) or {}
            reason = _exit_reason(exit_order)
            if reason is None and str(last["order_id"]).startswith("protective-"):
                reason = _protective_reason(_f(last.get("price")), stop, target)
                rec["source_ref_json"]["exit_reason_basis"] = (
                    "broker protective exit; classified by the nearer protection level")
            if reason is None:
                missing.append("exit_reason")
            rec.update(_result_fields(entry=avg_entry, stop=stop, side=side, legs=legs,
                                      risk_amount=risk_amount, exit_reason=reason))
            rec["exit_submitted_at"] = _ts(last.get("order_timestamp"))
            rec["exit_filled_at"] = _ts(last.get("fill_timestamp") or last.get("timestamp"))
            rec["position_closed_at"] = rec["exit_filled_at"]
            rec["execution_status"] = "CLOSED"
        return _finish(rec, missing, _CORE, latency_from=_LAB_LATENCY)

    def _unfilled_orders(self, store, metas, orders, filled_entry_orders, known) -> int:
        """Placed strategy orders that never filled: PENDING, CANCELLED or REJECTED."""
        written = 0
        for order_id, meta in metas.items():
            if order_id in filled_entry_orders or not meta.get("is_entry", True):
                continue
            order = orders.get(order_id)
            if order is None:
                continue
            key = self._key(meta, {"order_id": order_id})
            if known.get(key, (None, 0))[1]:
                continue
            state = str(order.get("status") or "").lower()
            status = ("PENDING" if state in _OPEN_ORDER else
                      "REJECTED" if state == "rejected" else "CANCELLED")
            evaluation = self.evaluation(meta)
            signal_at = _ts(order.get("signal_timestamp"))
            rec = {
                "execution_key": key, "record_source": self.record_source,
                "record_origin": self.session_origin(meta.get("session_id"), order),
                "verification": "VERIFIED", "status": status,
                "outcome": None if status == "PENDING" else status,
                "intent_id": order.get("decision_key") or meta.get("proposal_id"),
                "order_id": order_id, "session_id": meta.get("session_id"),
                "lab_id": self.lab_id,
                "strategy_id": meta.get("strategy_id") or meta.get("model_id"),
                "strategy_name": order.get("strategy") or meta.get("model_id"),
                "strategy_version": order.get("strategy_version") or meta.get("model_version"),
                "symbol": order.get("symbol"), "side": _side(order.get("side")),
                "timeframe": order.get("timeframe"),
                "signal_detected_at": signal_at,
                "decision_created_at": _ts(order.get("decision_timestamp")),
                "intent_created_at": _ts(meta.get("created_at")),
                "order_submitted_at": _ts(order.get("created_at")),
                "trading_session": trading_session(signal_at),
                "setup_json": evaluation.get("setup") or None,
                "evidence_json": evaluation.get("evidence") or None,
                "planned_entry": _f(meta.get("entry")) or _f(order.get("requested_price")),
                "planned_stop_loss": _f(order.get("protection_stop_loss")) or _f(meta.get("stop")),
                "planned_take_profit": _f(order.get("protection_take_profit"))
                or _f(meta.get("target_1")),
                "requested_entry": _f(order.get("requested_price")),
                "requested_quantity": _f(order.get("quantity")),
                "order_type": (order.get("type") or "").upper() or None,
                "execution_status": state.upper() or None,
                "exit_reason": (order.get("reason") or None) if status != "PENDING" else None,
                "source_ref_json": {"lab": self.name, "proposal_id": meta.get("proposal_id")},
            }
            rec["planned_rr"] = _rr(rec["planned_entry"], rec["planned_stop_loss"],
                                    rec["planned_take_profit"])
            store.upsert_trade(_finish(rec, ["entry_fill"] if status == "PENDING" else [],
                                       _CORE, latency_from=_LAB_LATENCY), events=_timeline(rec))
            written += 1
        return written

    # ------------------------------------------------------------ SMC agent
    def _link_agent(self, record: dict, meta: dict) -> None:
        return None

    def _late_agent_link(self, store, key: str, meta: dict) -> None:
        return None

    def project_decisions(self, store) -> int:
        return 0


#: SMC strategy condition outcomes (services/smc_strategy_ladder.ConditionStatus).
_SMC_FAILED = ("MISSING", "INVALIDATED", "EXPIRED")


def _smc_conditions(conditions: list) -> tuple[list, list, list]:
    """Required, passed and failed condition names from the SMC strategy's
    ordered results, which read {key, label, status, detail, object_id}.
    A NOT_REQUIRED condition is not part of this candidate's setup."""
    required, passed, failed = [], [], []
    for c in conditions:
        if not isinstance(c, dict):
            continue
        status = str(c.get("status") or "").upper()
        name = c.get("label") or c.get("key")
        if status == "NOT_REQUIRED" or not name:
            continue
        required.append(name)
        if status == "PASS":
            passed.append(name)
        elif status in _SMC_FAILED:
            failed.append(name)
    return required, passed, failed


def _candidate_type(status: str, reason, *, traded: bool) -> str:
    """A lab candidate's decision type. Both labs write the same statuses; two
    of them carry more than one meaning, which only the lab's reason tells
    apart: REJECTED is also the lab refusing to place an order, and
    DATA_PAUSED is also a fail-closed stop for a reason other than data."""
    if traded:
        return "TRADE_OPENED"
    text = str(reason or "").lower()
    if status == "REJECTED" and "placement rejected" in text:
        return "ORDER_REJECTED"
    if status == "DATA_PAUSED" and ("persistence" in text or "operating mode is invalid" in text):
        return "EXECUTION_FAILED"
    return classify_decision("", "", status, str(reason or ""))


def _protective_reason(price: Optional[float], stop: Optional[float],
                       target: Optional[float]) -> Optional[str]:
    if price is None or (stop is None and target is None):
        return None
    if target is None:
        return "stop-loss"
    if stop is None:
        return "take-profit"
    return "take-profit" if abs(price - target) < abs(price - stop) else "stop-loss"


def _exit_reason(order: dict) -> Optional[str]:
    text = " ".join(str(order.get(k) or "") for k in ("action_class", "type", "reason")).lower()
    if not text.strip():
        return None
    if "stop" in text and "take" not in text:
        return "stop-loss"
    if "take" in text or "target" in text or "tp" in text.split():
        return "take-profit"
    if "manual" in text or "user" in text or "remediation" in text:
        return "manual-close"
    if "time" in text or "expiry" in text:
        return "time-stop"
    return str(order.get("reason") or order.get("type") or "").strip() or None


# ======================================================================
# SMC lab (+ SMC agent)
# ======================================================================
class SMCLabProjector(V2LabProjector):
    name = "SMC_LAB"
    record_source = "SMC_LAB"
    lab_id = "smc_lab"

    def order_meta(self) -> dict:
        conn, lock = self._meta()
        out = {}
        for row in _rows(conn, lock, "SELECT * FROM smc_order_meta"):
            row["config"] = _json(row.get("config_json"), {}) or {}
            row["is_entry"] = row.get("ownership") == "strategy"
            out[row["order_id"]] = row
        return out

    def _sessions(self) -> dict:
        conn, lock = self._meta()
        return {r["id"]: r for r in _rows(conn, lock,
                                          "SELECT id, mode, symbol, timeframe FROM smc_sessions")}

    def session_origin(self, session_id, fill) -> str:
        session = self._sessions().get(session_id or "") or {}
        return "FORWARD_PAPER" if str(session.get("mode") or "LIVE_PAPER").upper() == \
            "LIVE_PAPER" else "SIMULATION"

    def evaluation(self, meta: dict) -> dict:
        proposal_id = meta.get("proposal_id")
        if not proposal_id:
            return {}
        conn, lock = self._meta()
        rows = _rows(conn, lock, "SELECT * FROM smc_candidates WHERE proposal_id=?", (proposal_id,))
        if not rows:
            return {}
        payload = _json(rows[0].get("payload"), {}) or {}
        ev = payload.get("evaluation") or {}
        conditions = ev.get("ordered_condition_results") or []
        required, passed, failed = _smc_conditions(conditions)
        mtf = ev.get("mtf_evidence") or {}
        return {
            "setup_type": meta.get("model_id"),
            "market_regime": (ev.get("context") or {}).get("regime") if isinstance(
                ev.get("context"), dict) else None,
            "htf_bias": (mtf.get("primary") or {}).get("bias") if isinstance(
                mtf.get("primary"), dict) else None,
            "setup": {"setup_type": meta.get("model_id"), "state": ev.get("state"),
                      "conditions_required": required,
                      "conditions_passed": passed, "conditions_failed": failed,
                      "missing_conditions": ev.get("missing_conditions") or [],
                      "trade_plan": ev.get("trade_plan")},
            "evidence": {"native_object_ids": ev.get("native_object_ids") or [],
                         "mtf_evidence": mtf, "ordered_conditions": conditions,
                         "proposal": ev.get("proposal"), "candidate_status": rows[0].get("status"),
                         "candidate_reason": rows[0].get("reason")},
            "risk_check": {"risk_pct": meta.get("risk_pct"), "result": "PASSED",
                           "basis": "the lab placed the order, so its risk gates passed"},
        }

    def _late_agent_link(self, store: TradeRecordStore, key: str, meta: dict) -> None:
        """A finished record whose agent row arrived after it was finalized.

        The agent writes its trade when it acts, but a pass can finalize the
        lab record first. The link is a previously unknown fact, so it fills
        in (logged as an enrichment) and never replaces a known one."""
        link: dict = {}
        self._link_agent(link, meta)
        if not link:
            return
        existing = store.by_key(key)
        if existing is None or existing.get("agent_id"):
            return
        store.upsert_trade({"execution_key": key, "agent_id": link["agent_id"],
                            "decision_id": link["decision_id"]})

    def _link_agent(self, record: dict, meta: dict) -> None:
        journal = self.agent_journal
        proposal_id = meta.get("proposal_id")
        if journal is None or not proposal_id:
            return
        conn, lock = getattr(journal, "_db", None), getattr(journal, "_lock", None)
        rows = _rows(conn, lock, "SELECT id, decision_id FROM agent_trades WHERE proposal_id=? "
                                 "ORDER BY opened_at", (proposal_id,))
        if rows:
            record["agent_id"] = "smc_agent"
            record["decision_id"] = rows[0]["decision_id"]
            record.setdefault("source_ref_json", {})["agent_trade_id"] = rows[0]["id"]

    def project(self, store: TradeRecordStore) -> dict:
        out = super().project(store)
        out["agent"] = self._project_agent(store)
        return out

    def _project_agent(self, store: TradeRecordStore) -> dict:
        """Agent intents with no fill, agent reviews, and agent decisions."""
        journal = self.agent_journal
        if journal is None:
            return {"skipped": "no agent journal"}
        conn, lock = getattr(journal, "_db", None), getattr(journal, "_lock", None)
        written = 0
        for intent in _rows(conn, lock, "SELECT * FROM execution_intents WHERE state IN "
                                        "('EXECUTION_FAILED','EXECUTION_UNCERTAIN','EXECUTION_PENDING')"):
            session = intent.get("session_id") or "-"
            key = (f"SMC_LAB:{session}:{intent['proposal_id']}" if intent.get("proposal_id")
                   else f"AGENT:{intent['execution_key']}")
            existing = store.by_key(key)
            if existing is not None and existing.get("actual_entry") is not None:
                continue                     # it did fill; the lab lifecycle owns it
            state = intent["state"]
            status = {"EXECUTION_FAILED": "EXECUTION_FAILED",
                      "EXECUTION_UNCERTAIN": "EXECUTION_UNCERTAIN"}.get(state, "PENDING")
            payload = _json(intent.get("payload_json"), {}) or {}
            rec = {
                "execution_key": key, "record_source": "SMC_LAB", "record_origin": "FORWARD_PAPER",
                "verification": "VERIFIED", "status": status,
                "outcome": status if status != "PENDING" else None,
                "intent_id": intent["execution_key"], "order_id": intent.get("broker_order_id"),
                "decision_id": intent.get("decision_id"), "session_id": intent.get("session_id"),
                "lab_id": self.lab_id, "agent_id": "smc_agent",
                "symbol": intent.get("symbol"), "timeframe": intent.get("timeframe"),
                "side": _side(payload.get("direction") or payload.get("side")),
                "strategy_name": payload.get("strategy") or payload.get("model_id"),
                "signal_detected_at": _ts(intent.get("candle_time")),
                "decision_created_at": _ts(intent.get("created_at")),
                "intent_created_at": _ts(intent.get("created_at")),
                "execution_status": state, "exit_reason": intent.get("error"),
                "source_ref_json": {"agent_intent": intent["execution_key"]},
            }
            store.upsert_trade(_finish(rec, [], _CORE), events=_timeline(rec))
            written += 1
        imported = 0
        for review in _rows(conn, lock, "SELECT r.*, t.proposal_id FROM agent_reviews r "
                                        "JOIN agent_trades t ON t.id = r.trade_id"):
            target = next(iter(store.query_trades(
                where="record_source='SMC_LAB' AND source_ref_json LIKE ?",
                params=(f'%"proposal_id":"{review["proposal_id"]}"%',), limit=1)), None)
            if target is None:
                continue
            store.add_review({
                "journal_record_id": target["journal_record_id"], "agent_id": "smc_agent",
                "review_version": 1, "reviewed_at": _ts(review.get("at")),
                "strategy_compliance": "COMPLIANT" if review.get("followed_rules") else "VIOLATION",
                "rule_violations": _json(review.get("violations_json"), []),
                "mistakes": _json(review.get("did_badly_json"), []),
                "positive_behaviours": _json(review.get("did_well_json"), []),
                "review_tags": [review.get("verdict")] if review.get("verdict") else [],
                "observations": [review.get("why")] if review.get("why") else [],
                "basis": {"imported_from": "smc_agent_journal.agent_reviews",
                          "agent_review_id": review["id"]},
            })
            imported += 1
        return {"intents": written, "reviews_imported": imported}

    def project_decisions(self, store) -> int:
        written = 0
        conn, lock = self._meta()
        sessions = self._sessions()
        for cand in _rows(conn, lock, "SELECT * FROM smc_candidates"):
            payload = _json(cand.get("payload"), {}) or {}
            ev = payload.get("evaluation") or {}
            session = sessions.get(cand["session_id"]) or {}
            status = str(cand.get("status") or "").upper()
            key = f"SMC_LAB:decision:{cand['session_id']}:{cand['proposal_id']}"
            trade = store.by_key(f"SMC_LAB:{cand['session_id']}:{cand['proposal_id']}")
            dtype = _candidate_type(status, cand.get("reason"), traded=trade is not None)
            proposal = ev.get("proposal") or {}
            store.upsert_decision({
                "decision_key": key, "record_source": "SMC_LAB",
                "record_origin": "FORWARD_PAPER" if str(session.get("mode") or "LIVE_PAPER").upper()
                == "LIVE_PAPER" else "SIMULATION",
                "lab_id": self.lab_id, "strategy_id": cand.get("model_id"),
                "strategy_name": cand.get("model_id"), "symbol": proposal.get("symbol")
                or session.get("symbol"), "timeframe": proposal.get("timeframe")
                or session.get("timeframe"), "side": _side(proposal.get("direction")),
                "candle_time": _ts(proposal.get("signal_timestamp")),
                "decided_at": _ts(cand.get("created_at")),
                "signal": (_side(proposal.get("direction")) or "").upper() or None,
                "decision_type": dtype, "status": status, "reason": cand.get("reason"),
                "conditions_passed": _smc_conditions(ev.get("ordered_condition_results") or [])[1],
                "conditions_missing": ev.get("missing_conditions") or [],
                "market_data_state": "UNRELIABLE" if status == "DATA_PAUSED" else "SYNCHRONIZED",
                "evidence": {"mtf_evidence": ev.get("mtf_evidence"),
                             "native_object_ids": ev.get("native_object_ids"),
                             "trade_plan": ev.get("trade_plan")},
                "source_ref": {"smc_candidate": cand["proposal_id"]},
                "journal_record_id": trade["journal_record_id"] if trade else None,
                "trade_id": trade.get("trade_id") if trade else None,
            })
            written += 1
        journal = self.agent_journal
        if journal is not None:
            jconn, jlock = getattr(journal, "_db", None), getattr(journal, "_lock", None)
            for d in _rows(jconn, jlock, "SELECT * FROM agent_decisions"):
                outcome = str(d.get("outcome") or "").upper()
                dtype = {"TAKEN": "TRADE_OPENED", "NOT_READY": "WAITING_CONFIRMATION",
                         "MISSED": "SETUP_REJECTED", "EXECUTION_FAILED": "EXECUTION_FAILED",
                         "EXECUTION_UNCERTAIN": "EXECUTION_UNCERTAIN",
                         "RECONCILED": "TRADE_OPENED"}.get(outcome) or classify_decision(
                    "", "", d.get("reason_code") or "", d.get("reason") or "")
                # Repeated NOT_READY for one setup is one waiting decision, not
                # a row per candle.
                anchor = (f"{d.get('setup_id')}:{outcome}:{d.get('reason_code')}"
                          if outcome == "NOT_READY" and d.get("setup_id") else d["id"])
                trade = None
                # The agent records the trade it took on agent_trades (keyed by
                # this decision), not on the decision row, which it writes first.
                rows = (_rows(jconn, jlock, "SELECT proposal_id FROM agent_trades WHERE id=?",
                              (d["trade_id"],)) if d.get("trade_id") else
                        _rows(jconn, jlock, "SELECT proposal_id FROM agent_trades WHERE decision_id=? "
                                            "ORDER BY opened_at LIMIT 1", (d["id"],)))
                if rows and rows[0].get("proposal_id"):
                    trade = next(iter(store.query_trades(
                        where="record_source='SMC_LAB' AND source_ref_json LIKE ?",
                        params=(f'%"proposal_id":"{rows[0]["proposal_id"]}"%',),
                        limit=1)), None)
                store.upsert_decision({
                    "decision_key": f"AGENT:decision:{anchor}", "record_source": "AGENT",
                    "record_origin": "FORWARD_PAPER", "agent_id": "smc_agent",
                    "lab_id": self.lab_id, "strategy_name": "SMC agent",
                    "strategy_version": d.get("strategy_fingerprint"),
                    "symbol": d.get("symbol"), "timeframe": d.get("timeframe"),
                    "candle_time": _ts(d.get("candle_time")), "decided_at": _ts(d.get("at")),
                    "decision_type": dtype, "status": outcome, "blocker": d.get("reason_code"),
                    "reason": d.get("reason"),
                    "conditions_passed": _json(d.get("conditions_json"), None),
                    "conditions_missing": _json(d.get("missing_json"), None),
                    "evidence": {"smc_state": d.get("smc_state"),
                                 "plan": _json(d.get("plan_json"), None),
                                 "gates": _json(d.get("gates_json"), None),
                                 "market": _json(d.get("market_json"), None)},
                    "source_ref": {"agent_decision_id": d["id"]},
                    "journal_record_id": trade["journal_record_id"] if trade else None,
                    "trade_id": trade.get("trade_id") if trade else None,
                })
                written += 1
        return written


# ======================================================================
# PA lab
# ======================================================================
class PALabProjector(V2LabProjector):
    name = "PA_LAB"
    record_source = "PA_LAB"
    lab_id = "pa_lab"

    def order_meta(self) -> dict:
        conn, lock = self._meta()
        out = {}
        for row in _rows(conn, lock, "SELECT * FROM pa_order_meta"):
            row["config"] = _json(row.get("config_json"), {}) or {}
            row["is_entry"] = True
            out[row["order_id"]] = row
        return out

    def _journal_record(self, meta: dict) -> Optional[dict]:
        journal = getattr(self.account, "journal", None)
        conn, lock = getattr(journal, "_db", None), getattr(journal, "_lock", None)
        if not meta.get("setup_id") or conn is None:
            return None
        rows = _rows(conn, lock, """
            SELECT r.payload_json FROM pa_journal_entries e
            JOIN pa_journal_revisions r ON r.journal_id = e.id
            WHERE e.session_id=? AND e.setup_id=? ORDER BY r.revision_no DESC LIMIT 1""",
                     (meta.get("session_id"), meta.get("setup_id")))
        return _json(rows[0]["payload_json"], None) if rows else None

    def session_origin(self, session_id, fill) -> str:
        record = None
        if session_id:
            conn, lock = self._meta()
            rows = _rows(conn, lock, "SELECT setup_id FROM pa_order_meta WHERE session_id=? LIMIT 1",
                         (session_id,))
            if rows:
                record = self._journal_record({"session_id": session_id,
                                               "setup_id": rows[0]["setup_id"]})
        partition = ((record or {}).get("identity") or {}).get("research_partition")
        if partition:
            return "FORWARD_PAPER" if partition == "paper_forward" else "SIMULATION"
        source = str(fill.get("market_data_source") or "").lower()
        return "FORWARD_PAPER" if ("binance" in source or "public" in source) else "SIMULATION"

    def evaluation(self, meta: dict) -> dict:
        record = self._journal_record(meta)
        if not record:
            return {}
        ctx = record.get("market_context") or {}
        setup = record.get("setup") or {}
        risk = record.get("order_risk") or {}
        return {
            "setup_type": setup.get("trigger_classification"),
            "market_regime": ctx.get("market_regime"),
            "htf_bias": (ctx.get("higher_timeframe_context") if isinstance(
                ctx.get("higher_timeframe_context"), str) else None),
            "setup": {"setup_type": setup.get("trigger_classification"),
                      "state": setup.get("state"),
                      "conditions_passed": setup.get("acceptance_reasons") or [],
                      "conditions_failed": setup.get("rejection_reasons") or [],
                      "location_reached_candle": setup.get("location_reached_candle"),
                      "rejection_reclaim_candle": setup.get("rejection_reclaim_candle"),
                      "confirmation_candle": setup.get("confirmation_candle"),
                      "confusion_candle_count": setup.get("confusion_candle_count"),
                      "invalidation_price": setup.get("invalidation_price")},
            "evidence": {"market_context": ctx, "pattern_metadata": setup.get("pattern_metadata"),
                         "state_transitions": setup.get("state_transitions"),
                         "identity": {k: (record.get("identity") or {}).get(k) for k in (
                             "strategy_id", "strategy_version", "configuration_fingerprint",
                             "engine_fingerprint", "research_partition")}},
            "risk_check": {"expected_risk_usdt": risk.get("expected_risk_usdt"),
                           "leverage": risk.get("leverage"), "margin": risk.get("margin"),
                           "result": "PASSED",
                           "basis": "the lab placed the order, so its risk gates passed"},
        }

    def project_decisions(self, store) -> int:
        """Every material Price Action decision, once.

        * candidates: signals-only, approval, rejected and paused proposals
          (pa_candidates);
        * orders the lab placed by itself: in automatic mode an accepted
          proposal goes straight to the broker and writes no candidate row;
        * setups the strategy formed and is waiting to confirm, one record per
          setup however many candles it waits -- a setup that became a
          proposal is covered by that proposal's decision instead.

        A candidate row names the proposal "{session}:{proposal}"; the trade
        record is keyed on the proposal itself, so the link uses the raw id.
        """
        written = 0
        conn, lock = self._meta()
        sessions = {r["id"]: r for r in _rows(conn, lock, "SELECT * FROM pa_sessions")}
        decided, setups_used = set(), set()
        for cand in _rows(conn, lock, "SELECT * FROM pa_candidates"):
            payload = _json(cand.get("payload"), {}) or {}
            status = str(cand.get("status") or "").upper()
            proposal = payload.get("proposal") or payload
            proposal_id = (cand.get("source_proposal_id")
                           or str(cand["proposal_id"]).split(":", 1)[-1])
            decided.add((cand["session_id"], proposal_id))
            setups_used.add((cand["session_id"], proposal.get("setup_id")))
            trade = store.by_key(f"PA_LAB:{cand['session_id']}:{proposal_id}")
            dtype = _candidate_type(status, payload.get("reason"), traded=trade is not None)
            store.upsert_decision({
                "decision_key": f"PA_LAB:decision:{cand['session_id']}:{proposal_id}",
                "record_source": "PA_LAB",
                "record_origin": self._session_origin(sessions.get(cand["session_id"])),
                "lab_id": self.lab_id, "strategy_id": proposal.get("strategy_id"),
                "strategy_name": proposal.get("strategy_id"),
                "symbol": proposal.get("symbol"), "timeframe": proposal.get("timeframe"),
                "side": _side(proposal.get("direction")),
                "candle_time": _ts(proposal.get("signal_at")),
                "decided_at": _ts(cand.get("created_at")),
                "signal": (_side(proposal.get("direction")) or "").upper() or None,
                "decision_type": dtype, "status": status, "reason": payload.get("reason"),
                "evidence": {"proposal": {k: proposal.get(k) for k in (
                    "entry", "stop", "target", "entry_model", "signal_at", "setup_id")}},
                "source_ref": {"pa_candidate": cand["proposal_id"]},
                "journal_record_id": trade["journal_record_id"] if trade else None,
                "trade_id": trade.get("trade_id") if trade else None,
            })
            written += 1
        for meta in _rows(conn, lock, "SELECT * FROM pa_order_meta ORDER BY created_at"):
            session_id = meta["session_id"]
            setups_used.add((session_id, meta.get("setup_id")))
            if (session_id, meta["proposal_id"]) in decided:
                continue                          # its candidate already says what happened
            decided.add((session_id, meta["proposal_id"]))
            trade = store.by_key(f"PA_LAB:{session_id}:{meta['proposal_id']}")
            status = str(meta.get("status") or "").upper()
            session = sessions.get(session_id) or {}
            store.upsert_decision({
                "decision_key": f"PA_LAB:decision:{session_id}:{meta['proposal_id']}",
                "record_source": "PA_LAB", "record_origin": self._session_origin(session),
                "lab_id": self.lab_id, "strategy_id": meta.get("strategy_id"),
                "strategy_name": meta.get("strategy_id"), "symbol": session.get("symbol"),
                "timeframe": session.get("timeframe"), "side": _side(meta.get("direction")),
                "decided_at": _ts(meta.get("created_at")),
                "signal": (_side(meta.get("direction")) or "").upper() or None,
                "decision_type": _candidate_type(status, meta.get("reason"),
                                                 traded=trade is not None),
                "status": status, "reason": meta.get("reason"),
                "evidence": {"setup_id": meta.get("setup_id"), "zone_id": meta.get("zone_id"),
                             "placed_by": "the lab's automatic mode (no candidate row)"},
                "source_ref": {"pa_order_meta": meta["order_id"]},
                "journal_record_id": trade["journal_record_id"] if trade else None,
                "trade_id": trade.get("trade_id") if trade else None,
            })
            written += 1
        return written + self._project_waiting_setups(store, conn, lock, sessions, setups_used)

    @staticmethod
    def _session_origin(session: Optional[dict]) -> str:
        return "FORWARD_PAPER" if str((session or {}).get("mode") or "LIVE_PAPER").upper() == \
            "LIVE_PAPER" else "SIMULATION"

    def _project_waiting_setups(self, store, conn, lock, sessions: dict, setups_used: set) -> int:
        """One WAITING_CONFIRMATION record per setup the strategy formed.

        Each closed-candle evaluation saves the strategy trace it judged on;
        a trace in ORDER_PENDING names a setup whose trigger has formed and is
        waiting for a later candle to confirm it. Candles where nothing formed
        (WATCHING, no setup) are not decisions and are never recorded.
        """
        runs: dict = {}
        latest: dict = {}
        for ev in _rows(conn, lock, "SELECT session_id, candle_time, strategy_id, payload_json "
                                    "FROM pa_evaluations ORDER BY session_id, candle_time"):
            trace = (_json(ev.get("payload_json"), {}) or {}).get("trace") or {}
            setup_id = trace.get("setup_id")
            latest[ev["session_id"]] = setup_id
            if not setup_id or trace.get("state") != "ORDER_PENDING":
                continue
            run = runs.setdefault((ev["session_id"], setup_id), {
                "first": ev["candle_time"], "candles": 0, "strategy_id": ev["strategy_id"]})
            run.update(last=ev["candle_time"], trace=trace)
            run["candles"] += 1
        written = 0
        for (session_id, setup_id), run in runs.items():
            if (session_id, setup_id) in setups_used:
                continue                          # it became a proposal; that decision covers it
            trace, session = run["trace"], sessions.get(session_id) or {}
            conditions = trace.get("conditions") or []
            still = latest.get(session_id) == setup_id
            store.upsert_decision({
                "decision_key": f"PA_LAB:setup:{session_id}:{setup_id}",
                "record_source": "PA_LAB", "record_origin": self._session_origin(session),
                "lab_id": self.lab_id, "strategy_id": run["strategy_id"],
                "strategy_name": run["strategy_id"], "symbol": session.get("symbol"),
                "timeframe": session.get("timeframe"), "side": _side(trace.get("direction")),
                "candle_time": _ts(run["first"]), "decided_at": _ts(run["first"]),
                "decision_type": "WAITING_CONFIRMATION",
                "status": "WAITING" if still else "NOT_SHOWN_SINCE",
                "reason": (f"{trace.get('next_required_event') or 'Waiting for confirmation'} "
                           f"(seen on {run['candles']} closed candle(s), {run['first']} to "
                           f"{run['last']}" + ("" if still else "; not shown in later evaluations")
                           + ")"),
                "conditions_passed": [c.get("key") for c in conditions
                                      if isinstance(c, dict) and c.get("status") == "PASS"],
                "conditions_missing": list(trace.get("missing_conditions") or []),
                "evidence": {"setup_id": setup_id, "first_candle": run["first"],
                             "last_candle": run["last"], "candles": run["candles"],
                             "conditions": conditions,
                             "supporting_object_ids": trace.get("supporting_object_ids")},
                "source_ref": {"pa_evaluations": {"session_id": session_id, "setup_id": setup_id}},
            })
            written += 1
        return written
