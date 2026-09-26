"""Evolution Memory with provenance.

Two kinds of memory are shown, never blended:

* VERIFIED memory is computed from canonical FORWARD_PAPER records. Every
  claim carries the journal_record_ids behind it, its period and the last
  weekly review that covered it, so "59 trades" can be opened to the 59.
* LEGACY counters are the old ``evolution_memory`` rows. They store no trade
  ids; services/journal_legacy.evolution_provenance labels each one by what
  can still be proven behind it (VERIFIED / LEGACY / UNVERIFIED).

Staging mirrors the old thresholds: under 30 trades is an early signal,
30-49 is building, 50+ is evidence. A stage describes sample size only; it
never changes a strategy.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from data.trade_record_store import TradeRecordStore
from services import journal_stats as stats
from services.journal_legacy import evolution_provenance

EARLY_SIGNAL_MAX = 30
EVIDENCE_MIN = 50


def _stage(n: int) -> str:
    return "evidence" if n >= EVIDENCE_MIN else "building" if n >= EARLY_SIGNAL_MAX else "early-signal"


def verified_memory(store: TradeRecordStore) -> list[dict]:
    records = store.query_trades(where="record_origin='FORWARD_PAPER' AND status='CLOSED'",
                                 limit=200000, order="position_closed_at ASC")
    groups: dict = defaultdict(list)
    for r in stats.completed(records):
        key = (r.get("strategy_name") or r.get("strategy_id") or "UNKNOWN",
               r.get("market_regime") or "UNKNOWN", r.get("side") or "UNKNOWN")
        groups[key].append(r)
    reviewed = {}
    with store._lock:
        for row in store._c.execute(
                "SELECT key, value_json FROM recorder_state WHERE key LIKE 'memory_last_reviewed:%'"):
            reviewed[row["key"]] = row["value_json"]
    last_review = None
    if reviewed:
        import json
        last_review = max((json.loads(v).get("at") for v in reviewed.values()), default=None)
    out = []
    for (strategy, regime, side), rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        s = stats.summarize(rows)
        sources = sorted({r.get("record_source") for r in rows})
        out.append({
            "setup_key": f"{strategy}|{regime}|{side}", "strategy": strategy, "regime": regime,
            "side": side, "trades": s["trades"], "wins": s["wins"], "losses": s["losses"],
            "net_r": s["total_r"], "net_pnl": s["net_pnl"], "win_rate": s["win_rate"],
            "stage": _stage(s["trades"]), "provenance": "VERIFIED",
            "record_origin": "FORWARD_PAPER", "record_sources": sources,
            "period": s["period"], "evidence_count": len(rows),
            "journal_record_ids": s["journal_record_ids"],
            "last_reviewed": last_review,
            "note": ("Computed from canonical forward-paper records; each one is listed."
                     + ("" if s["trades"] >= EARLY_SIGNAL_MAX else
                        " Early signal: do not change a strategy on this alone.")),
        })
    return out


def memory(store: TradeRecordStore, journal_store=None) -> dict:
    legacy = evolution_provenance(journal_store, store) if journal_store is not None else []
    return {"verified": verified_memory(store), "legacy": legacy,
            "explanation": ("VERIFIED rows are built from forward-paper trade records with ids. "
                            "LEGACY rows are counters from the old evolution memory; they are "
                            "labelled by how many of their increments still have a record behind "
                            "them and are never mixed into verified statistics.")}


def evidence(store: TradeRecordStore, setup_key: str, journal_store=None) -> Optional[dict]:
    for row in verified_memory(store):
        if row["setup_key"] == setup_key:
            return {**row, "records": [store.get(i) for i in row["journal_record_ids"]]}
    if journal_store is not None:
        for row in evolution_provenance(journal_store, store):
            if row["setup_key"] == setup_key:
                return {**row, "records": [store.get(i) for i in row["journal_record_ids"]],
                        "legacy_records": [store.get(t) for t in row["legacy_trade_ids"]
                                           if store.get(t) is not None]}
    return None
