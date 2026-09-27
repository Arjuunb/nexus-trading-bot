"""Migrate the old decision journal into canonical records -- without inventing.

``trade_decision_journal`` (journal.db) holds one row per trade journaled by
``SignalPipeline`` before forward-paper fills made that path unreachable. The
rows are not deleted or edited. For each:

* if the ledger still has the trade, the ledger projection already made a
  VERIFIED record for it; the old row only fills fields that record does not
  know (its decision sections as evidence), logged as enrichments;
* otherwise it becomes a LEGACY_MIGRATION record marked UNVERIFIED, carrying
  only what the row actually holds. Missing SL, TP, exit or P&L stay NULL.

Stats default to FORWARD_PAPER, so none of this can blend into forward-paper
performance unless a view asks for it by name.
"""
from __future__ import annotations


from data.trade_record_store import TradeRecordStore
from services.journal_recorder import (_f, _finish, _json, _rr, _side,
                                       _timeline, _ts, classify_outcome, trading_session)

_CORE = ("ledger_trade", "planned_stop_loss", "actual_exit", "net_pnl")


class LegacyJournalMigration:
    name = "LEGACY_JOURNAL"

    def __init__(self, journal_store):
        self.journal_store = journal_store

    def _rows(self) -> list[dict]:
        conn = getattr(self.journal_store, "_c", None)
        lock = getattr(self.journal_store, "_lock", None)
        if conn is None:
            return []
        with lock:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM trade_decision_journal ORDER BY created_at")]

    def migrate(self, store: TradeRecordStore) -> dict:
        rows = self._rows()
        matched = created = 0
        for row in rows:
            sections = _json(row.get("sections_json"), {}) or {}
            existing = store.get(row["trade_id"])
            # A record the ledger projection built (its key is not a legacy
            # journal key) is backed by execution facts: only fill its gaps.
            if existing is not None and not existing["execution_key"].startswith("LEGACY_JOURNAL:"):
                # A verified ledger record: only fill what it does not know.
                enrich = {"execution_key": existing["execution_key"], "trade_id": row["trade_id"]}
                if existing.get("setup") is None and sections:
                    enrich["setup_json"] = {
                        "market_regime": row.get("regime"),
                        "checklist": sections.get("checklist"),
                        "entry_decision": sections.get("entry_decision"),
                        "source": "legacy decision journal (captured at entry)"}
                if existing.get("evidence") is None and sections.get("market_snapshot"):
                    enrich["evidence_json"] = {"market_snapshot": sections.get("market_snapshot")}
                if existing.get("market_regime") is None and row.get("regime"):
                    enrich["market_regime"] = row.get("regime")
                store.upsert_trade(enrich)
                matched += 1
                continue
            record = self._legacy_record(row, sections)
            store.upsert_trade(record, events=_timeline(record))
            created += 1
        return {"source": self.name, "rows": len(rows), "matched_to_ledger": matched,
                "legacy_unverified": created}

    def _legacy_record(self, row: dict, sections: dict) -> dict:
        provenance = sections.get("provenance") or {}
        instance_id = row.get("instance_id") or provenance.get("instance_id") or ""
        side = _side(row.get("side"))
        closed = row.get("status") == "closed"
        missing = ["ledger_trade"]
        entry, stop, target = _f(row.get("entry")), _f(row.get("stop")), _f(row.get("target"))
        if stop is None:
            missing.append("planned_stop_loss")
        opened_at = _ts(row.get("created_at"))
        rec = {
            "execution_key": f"LEGACY_JOURNAL:{row['trade_id']}",
            "record_source": "INSTANCE" if instance_id else "LEGACY_ENGINE",
            "record_origin": "LEGACY_MIGRATION", "verification": "UNVERIFIED",
            "operating_mode": str(row.get("execution_mode") or row.get("mode") or "paper").lower(),
            "status": ("CLOSED" if closed else "CANCELLED" if row.get("status") == "cancelled"
                       else "OPEN"),
            "trade_id": row["trade_id"],
            "position_id": row.get("position_id") or provenance.get("position_id"),
            "session_id": row.get("simulation_session_id") or provenance.get("simulation_session_id"),
            "instance_id": instance_id or None,
            "strategy_id": row.get("strategy_id") or provenance.get("strategy_id"),
            "strategy_name": row.get("strategy_name") or row.get("strategy"),
            "strategy_version": row.get("strategy_version") or provenance.get("strategy_version"),
            "symbol": row.get("symbol"), "timeframe": row.get("timeframe"), "side": side,
            "exchange": row.get("exchange") or provenance.get("exchange"),
            "position_opened_at": opened_at,
            "position_closed_at": _ts(row.get("closed_at")) if closed else None,
            "market_regime": row.get("regime"), "trading_session": trading_session(opened_at),
            "setup_json": {"market_regime": row.get("regime"),
                           "checklist": sections.get("checklist"),
                           "entry_decision": sections.get("entry_decision"),
                           "source": "legacy decision journal (captured at entry)"} if sections else None,
            "evidence_json": {"market_snapshot": sections.get("market_snapshot")}
            if sections.get("market_snapshot") else None,
            "planned_entry": entry, "planned_stop_loss": stop, "planned_take_profit": target,
            "planned_rr": _f(row.get("planned_rr")) or _rr(entry, stop, target),
            "risk_amount": _f(row.get("risk_amount")), "quantity": _f(row.get("size")),
            "actual_entry": entry, "filled_quantity": _f(row.get("size")),
            "risk_check_json": sections.get("risk_check"),
            "source_ref_json": {
                "journal_db_trade_id": row["trade_id"],
                "timestamps": "journal write times, recorded in the same call as the fill",
                "why_unverified": "no ledger trade with this id remains to verify it against"},
        }
        if closed:
            pnl, rr = _f(row.get("pnl")), _f(row.get("actual_rr"))
            exit_price = _f(row.get("exit"))
            if exit_price is None:
                missing.append("actual_exit")
            if pnl is None:
                missing.append("net_pnl")
            exit_decision = sections.get("exit_decision") or {}
            rec.update({"actual_exit": exit_price, "net_pnl": pnl,
                        "exit_reason": exit_decision.get("exit_reason"),
                        "realized_r": rr, "outcome": classify_outcome(pnl, rr),
                        "exit_filled_at": _ts(row.get("closed_at"))})
            if exit_price is not None and entry is not None and stop is not None and entry != stop:
                sign = 1.0 if side == "long" else -1.0
                rec["achieved_rr"] = round(sign * (exit_price - entry) / abs(entry - stop), 4)
        return _finish(rec, missing, _CORE)


#: Market data modes a decision records for candles that were not live.
_SIMULATED_MODES = ("replay", "synthetic", "demo", "backtest")


def evolution_provenance(journal_store, store: TradeRecordStore) -> list[dict]:
    """Label each old evolution counter by what can be proven behind it."""
    conn = getattr(journal_store, "_c", None)
    lock = getattr(journal_store, "_lock", None)
    if conn is None:
        return []
    with lock:
        counters = [dict(r) for r in conn.execute("SELECT * FROM evolution_memory")]
        closed = [dict(r) for r in conn.execute(
            "SELECT trade_id, strategy, regime, side, closed_at, actual_rr, result, sections_json "
            "FROM trade_decision_journal WHERE status='closed'")]
    out = []
    for c in counters:
        rows = [r for r in closed if (r.get("strategy"), r.get("regime"), r.get("side")) ==
                (c.get("strategy"), c.get("regime"), c.get("side"))]
        ids = [r["trade_id"] for r in rows]
        verified, origins = [], {}
        for trade_id in ids:
            rec = store.get(trade_id)
            if rec is not None and rec.get("verification") == "VERIFIED":
                verified.append(rec["journal_record_id"])
                origin = rec.get("record_origin") or "UNKNOWN"
                origins[origin] = origins.get(origin, 0) + 1
        # The data mode each surviving journal row recorded at decision time.
        # A replay-mode Trading Instance fills through the same pipeline, so
        # its trades reach this counter too (docs/JOURNAL_AUDIT.md, correction).
        modes: dict = {}
        for r in rows:
            sections = _json(r.get("sections_json"), {})
            prov = (sections.get("provenance") if isinstance(sections, dict) else None) or {}
            mode = str(prov.get("market_data_mode") or "not recorded")
            modes[mode] = modes.get(mode, 0) + 1
        simulated_rows = sum(n for mode, n in modes.items() if mode in _SIMULATED_MODES)
        backed = len(rows)
        trades = int(c.get("trades") or 0)
        if trades and len(verified) == trades:
            # Every increment is a real ledger trade. Only forward paper is
            # verified trading history: a counter built on replayed candles
            # is verified to exist and is still a simulation, and a trade whose
            # decision never recorded its market data is not proven either way.
            label = ("VERIFIED" if set(origins) == {"FORWARD_PAPER"} else
                     "SIMULATION" if set(origins) == {"SIMULATION"} else
                     "MIXED" if "SIMULATION" in origins else "LEGACY")
        elif backed == trades and trades:
            label = "LEGACY"        # every increment has its journal row, not all a ledger trade
        else:
            label = "UNVERIFIED"    # increments with no surviving record at all
        note = ("Counter from the old evolution memory. It stores no trade ids; "
                f"{backed} of its {trades} increments have a journal row behind them"
                + (f", {len(verified)} verified against the ledger" if backed else "")
                + (" (" + ", ".join(f"{n} {o.replace('_', ' ').lower()}"
                                    for o, n in sorted(origins.items())) + ")" if origins else "")
                + ".")
        if simulated_rows:
            note += (f" {simulated_rows} of its journal rows recorded simulated market data "
                     "(replay), so they are not forward-paper history.")
        out.append({
            "setup_key": c["setup_key"], "strategy": c.get("strategy"), "regime": c.get("regime"),
            "side": c.get("side"), "trades": trades, "wins": int(c.get("wins") or 0),
            "net_r": round(float(c.get("net_r") or 0), 2), "stage": c.get("stage"),
            "updated_at": c.get("updated_at"),
            "provenance": label,
            "record_origin": "LEGACY_MIGRATION",
            "backing_origins": origins, "recorded_market_data": modes,
            "evidence_rows": backed, "verified_records": len(verified),
            "unbacked_increments": max(0, trades - backed),
            "period": {"start": min((r["closed_at"] for r in rows if r.get("closed_at")), default=None),
                       "end": max((r["closed_at"] for r in rows if r.get("closed_at")), default=None)},
            "journal_record_ids": verified,
            "legacy_trade_ids": ids,
            "note": note,
        })
    return out
