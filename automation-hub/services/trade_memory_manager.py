"""Wires the trade-memory store, composer and insights into the running bot.

Responsibilities:
  * ``remember(trade_id)`` — called right after the decision journal closes a
    trade; composes the permanent 8-category memory and upserts it. Never
    raises into the trading path.
  * ``run_reviews(now)`` — nightly pattern review, plus weekly (Mondays),
    monthly (1st) and yearly (Jan 1) rollups, each persisted.
  * ``backfill()`` — one-shot import of already-closed journal trades so the
    memory isn't empty on first deploy.
  * ``rebuild()`` — explicitly recompose derived memories from the journal
    while preserving manual notes.
  * thin ``insights`` / ``ask`` / ``similar`` pass-throughs for the router.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from services import trade_memory as tm
from services import memory_insights as mi


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class TradeMemoryManager:
    def __init__(self, store, journal_store, decision_store=None,
                 exchange: Optional[str] = None,
                 starting_balance: float = 10000.0) -> None:
        self.store = store
        self.journal_store = journal_store          # data.journal_store.JournalStore
        self.decision_store = decision_store
        self.exchange = exchange or os.environ.get("HUB_EXCHANGE", "paper")
        self.starting_balance = starting_balance

    # ------------------------------------------------------------- write path
    def remember(self, trade_id: str, notes: str = "") -> Optional[dict]:
        """Compose + persist the memory for one closed trade. Idempotent."""
        try:
            journal = self.journal_store.get(trade_id)
            if journal is None or journal.get("status") != "closed":
                return None
            existing = self.store.get(trade_id)
            if existing and not tm._memory_scope_matches(journal, existing):
                return None
            # keep any note already attached to the memory across recomposition
            if not notes:
                if existing:
                    notes = existing.get("notes") or ""
            decision = self._match_decision(journal)
            mem = tm.compose_memory(journal, decision=decision,
                                    exchange=self.exchange, notes=notes)
            self.store.upsert(mem)
            return mem
        except Exception as e:  # noqa: BLE001 — memory must never break trading
            print(f"[trade-memory] remember({trade_id}) failed: {type(e).__name__}: {e}")
            return None

    def _match_decision(self, journal: dict) -> Optional[dict]:
        """Resolve explicit persisted IDs before any unverified legacy match.

        A broken explicit reference never falls back to symbol/time. Every
        candidate must respect captured account, instance and strategy scope.
        The composer labels the legacy heuristic UNVERIFIED.
        """
        if self.decision_store is None:
            return None
        try:
            reference, _basis = tm._decision_reference(journal)
            if reference is not None:
                decision = self.decision_store.get(reference)
                return decision if decision and tm._decision_scope_matches(journal, decision) else None
            symbol = journal.get("symbol")
            created = _parse(journal.get("created_at"))
            cands = [d for d in self.decision_store.list(limit=200, decision="accepted",
                                                         symbol=symbol)
                     if tm._decision_scope_matches(journal, d, legacy=True)]
            if not cands or created is None:
                return None
            best, best_gap = None, None
            for d in cands:
                dt = _parse(d.get("ts"))
                if dt is None:
                    continue
                gap = abs((created - dt).total_seconds())
                if best_gap is None or gap < best_gap:
                    best, best_gap = d, gap
            # Legacy context only: proximity cannot establish a verified link.
            return best if (best_gap is not None and best_gap <= 3600) else None
        except Exception:  # noqa: BLE001
            return None

    def set_notes(self, trade_id: str, notes: str) -> bool:
        return self.store.set_notes(trade_id, notes)

    def delete(self, trade_id: str) -> bool:
        return self.store.delete(trade_id)

    # ----------------------------------------------------------- backfill
    def backfill(self) -> dict:
        """Import every already-closed journal trade not yet remembered."""
        added = 0
        try:
            existing = self.store.existing_ids()
            for j in self.journal_store.list(limit=100000):
                if j.get("status") != "closed" or j.get("trade_id") in existing:
                    continue
                if self.remember(j["trade_id"]):
                    added += 1
        except Exception as e:  # noqa: BLE001
            print(f"[trade-memory] backfill failed: {type(e).__name__}: {e}")
        return {"backfilled": added, "total": self.store.count()}

    def rebuild(self) -> dict:
        """Recompose all closed memories from their authoritative journals.

        Memory rows are derived records, so this is safe and idempotent. The
        normal ``remember`` path preserves any manual notes already attached.
        Nothing is deleted when a journal row cannot be recomposed.
        """
        rebuilt = failed = 0
        for journal in self.journal_store.list(limit=100000):
            if journal.get("status") != "closed" or not journal.get("trade_id"):
                continue
            if self.remember(journal["trade_id"]):
                rebuilt += 1
            else:
                failed += 1
        return {"rebuilt": rebuilt, "failed": failed, "total": self.store.count()}

    # -------------------------------------------------------------- reviews
    def insights(self) -> dict:
        rows = self.store.list(limit=100000)
        return mi.build_review(rows, self.starting_balance)

    def run_reviews(self, now: Optional[datetime] = None) -> dict:
        """Nightly review + conditional weekly/monthly/yearly rollups."""
        now = now or datetime.now(timezone.utc)
        rows = self.store.list(limit=100000)
        ran = []

        # nightly — trades that closed on the just-finished UTC day
        day = now.date()
        day_key = day.isoformat()
        day_rows = [r for r in rows if (r.get("closed_at") or "")[:10] == day_key]
        self.store.save_review("nightly", day_key,
                               {**mi.build_review(day_rows, self.starting_balance),
                                "generated_at": now.isoformat()})
        ran.append(f"nightly:{day_key}")

        iso = now.isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        self.store.save_review("weekly", week_key,
                               {**mi.build_review(_recent(rows, now, 7), self.starting_balance),
                                "generated_at": now.isoformat()})
        ran.append(f"weekly:{week_key}")

        month_key = f"{now.year}-{now.month:02d}"
        month_rows = [r for r in rows if (r.get("closed_at") or "")[:7] == month_key]
        self.store.save_review("monthly", month_key,
                               {**mi.build_review(month_rows, self.starting_balance),
                                "generated_at": now.isoformat()})
        ran.append(f"monthly:{month_key}")

        year_key = str(now.year)
        year_rows = [r for r in rows if (r.get("closed_at") or "")[:4] == year_key]
        self.store.save_review("yearly", year_key,
                               {**mi.build_review(year_rows, self.starting_balance),
                                "generated_at": now.isoformat()})
        ran.append(f"yearly:{year_key}")

        return {"ran": ran, "memories": len(rows)}

    # ------------------------------------------------------------- queries
    def ask(self, q: str, limit: int = 50) -> dict:
        return tm.ask(self.store, self.insights, q, limit)

    def similar(self, trade_id: str, limit: int = 5) -> list[dict]:
        return tm.similar(self.store, trade_id, limit)

    def reviews(self, period: Optional[str] = None, limit: int = 12) -> list[dict]:
        return self.store.get_reviews(period, limit)


def _recent(rows: list[dict], now: datetime, days: int) -> list[dict]:
    cutoff = now.timestamp() - days * 86400
    out = []
    for r in rows:
        dt = _parse(r.get("closed_at"))
        if dt and dt.timestamp() >= cutoff:
            out.append(r)
    return out
