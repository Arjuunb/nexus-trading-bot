"""Decision-journal storage — the full, explainable record of every bot trade.

Three tables (linked to the ledger's paper_trades by trade_id):
    trade_decision_journal  one row per trade: summary + the rich decision
                            sections (entry reasoning, rule checklist, market
                            snapshot, risk check, exit, review, evolution) as
                            JSON, so every trade is explainable and searchable.
    trade_decision_events   the trade timeline — one row per event.
    evolution_memory        aggregated learning per setup (strategy·regime·side)
                            with the early-signal / evidence staging the bot
                            uses to decide how much a pattern can be trusted.

Everything stored here is REAL decision data captured at the moment it was
produced. Nothing is fabricated: a field the bot did not compute is stored as
"Not checked", never invented.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from data.sqlite_runtime import TransactionLock
from data.tenant_scope import ensure_tenant_column

EARLY_SIGNAL_MAX = 30       # < this many trades for a setup = early signal only
EVIDENCE_MIN = 50           # >= this = strong enough for stronger changes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JournalStore:
    def __init__(self, path: str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(self.path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        # journal.db is shared with the canonical trade journal: a failed write
        # must roll back, or it holds the file's write lock from that writer
        self._lock = TransactionLock(self._c)
        with self._lock:
            self._c.executescript("""
            CREATE TABLE IF NOT EXISTS trade_decision_journal (
                trade_id TEXT PRIMARY KEY, created_at TEXT, closed_at TEXT,
                mode TEXT, symbol TEXT, side TEXT, strategy TEXT, timeframe TEXT,
                entry REAL, stop REAL, target REAL, exit REAL, size REAL,
                risk_amount REAL, planned_rr REAL, actual_rr REAL, pnl REAL,
                result TEXT, confidence REAL, brain_score REAL, regime TEXT,
                grade TEXT, status TEXT, sections_json TEXT);
            CREATE TABLE IF NOT EXISTS trade_decision_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT, ts TEXT,
                kind TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS evolution_memory (
                setup_key TEXT PRIMARY KEY, strategy TEXT, regime TEXT, side TEXT,
                trades INTEGER, wins INTEGER, net_r REAL, updated_at TEXT,
                stage TEXT, note TEXT);
            CREATE INDEX IF NOT EXISTS idx_evt_trade ON trade_decision_events(trade_id);
            """)
            existing = {row[1] for row in self._c.execute("PRAGMA table_info(trade_decision_journal)")}
            for column in (
                "instance_id TEXT", "instance_name TEXT", "strategy_id TEXT",
                "strategy_name TEXT", "strategy_version TEXT", "execution_mode TEXT",
                "market_data_mode TEXT", "market_data_source TEXT", "exchange TEXT",
                "position_id TEXT", "simulation_session_id TEXT",
            ):
                if column.split()[0] not in existing:
                    self._c.execute(f"ALTER TABLE trade_decision_journal ADD COLUMN {column}")
            self._c.execute("CREATE INDEX IF NOT EXISTS idx_journal_instance ON trade_decision_journal(instance_id)")
            for _t in ("trade_decision_journal", "trade_decision_events", "evolution_memory"):
                ensure_tenant_column(self._c, _t)   # Phase C-3: schema-only, additive
            self._c.commit()

    # ------------------------------------------------------------- entry
    def record_entry(self, j: dict) -> None:
        """Insert the journal at trade open. ``j`` carries the summary fields +
        a ``sections`` dict (entry_decision / checklist / market_snapshot /
        risk_check)."""
        with self._lock:
            self._c.execute(
                """INSERT INTO trade_decision_journal
                (trade_id, created_at, mode, symbol, side, strategy, timeframe,
                 entry, stop, target, size, risk_amount, planned_rr, confidence,
                 brain_score, regime, status, sections_json, instance_id, instance_name,
                 strategy_id, strategy_name, strategy_version, execution_mode,
                 market_data_mode, market_data_source, exchange, position_id, simulation_session_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?,?,?,?,?,?,?,?,?,?,?,?)""",
                (j["trade_id"], _now(), j.get("mode", "paper"), j.get("symbol"),
                 j.get("side"), j.get("strategy"), j.get("timeframe"), j.get("entry"),
                 j.get("stop"), j.get("target"), j.get("size"), j.get("risk_amount"),
                 j.get("planned_rr"), j.get("confidence"), j.get("brain_score"),
                 j.get("regime"), json.dumps(j.get("sections", {})), j.get("instance_id"),
                 j.get("instance_name"), j.get("strategy_id"), j.get("strategy_name"),
                 j.get("strategy_version"), j.get("execution_mode"),
                 j.get("market_data_mode"), j.get("market_data_source"),
                 j.get("exchange"), j.get("position_id"), j.get("simulation_session_id")))
            self._c.commit()

    def add_event(self, trade_id: str, kind: str, detail: str = "",
                  ts: Optional[str] = None) -> None:
        with self._lock:
            self._c.execute(
                "INSERT INTO trade_decision_events(trade_id, ts, kind, detail) VALUES (?,?,?,?)",
                (trade_id, ts or _now(), kind, detail))
            self._c.commit()

    # ------------------------------------------------------------- close
    def close_trade(self, trade_id: str, *, exit: float, pnl: float, actual_rr: float,
                    result: str, grade: str, extra_sections: dict,
                    instance_id: str = "") -> bool:
        with self._lock:
            row = self._c.execute(
                "SELECT sections_json FROM trade_decision_journal WHERE trade_id=?"
                + (" AND instance_id=?" if instance_id else ""),
                (trade_id, instance_id) if instance_id else (trade_id,)).fetchone()
            if row is None:
                return False
            sections = json.loads(row["sections_json"] or "{}")
            sections.update(extra_sections)          # exit_decision / review / evolution
            self._c.execute(
                """UPDATE trade_decision_journal SET closed_at=?, exit=?, pnl=?,
                   actual_rr=?, result=?, grade=?, status='closed', sections_json=?
                   WHERE trade_id=?""" + (" AND instance_id=?" if instance_id else ""),
                (_now(), exit, pnl, actual_rr, result, grade,
                 json.dumps(sections), trade_id, *([instance_id] if instance_id else [])))
            self._c.commit()
            return True

    def cancel_open_for_instance(self, instance_id: str, *, reason: str) -> int:
        """Terminate open journal rows without fabricating an execution fill."""
        with self._lock:
            rows = self._c.execute(
                "SELECT trade_id FROM trade_decision_journal WHERE instance_id=? AND status='open'",
                (instance_id,)).fetchall()
            timestamp = _now()
            self._c.execute(
                """UPDATE trade_decision_journal
                   SET closed_at=?, status='cancelled', result='account_restart'
                   WHERE instance_id=? AND status='open'""",
                (timestamp, instance_id))
            self._c.executemany(
                "INSERT INTO trade_decision_events(trade_id,ts,kind,detail) VALUES (?,?,?,?)",
                [(row["trade_id"], timestamp, "simulation-account-restarted", reason)
                 for row in rows])
            self._c.commit()
            return len(rows)

    # ------------------------------------------------------------- queries
    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["sections"] = json.loads(d.pop("sections_json") or "{}")
        provenance = d["sections"].get("provenance") or {}
        # Existing rows are not guessed. Only explicit captured provenance is
        # promoted; otherwise execution truth remains visibly unverified.
        for key in ("instance_id", "instance_name", "strategy_id", "strategy_name",
                    "strategy_version", "market_data_mode", "market_data_source",
                    "exchange", "position_id", "simulation_session_id"):
            if d.get(key) is None and provenance.get(key) is not None:
                d[key] = provenance[key]
        d["strategy_name"] = d.get("strategy_name") or d.get("strategy")
        d["execution_mode"] = (d.get("execution_mode") or provenance.get("execution_mode")
                               or "LEGACY / UNVERIFIED")
        d["mode"] = d["execution_mode"]
        return d

    def get(self, trade_id: str, instance_id: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            query = "SELECT * FROM trade_decision_journal WHERE trade_id=?"
            args = [trade_id]
            if instance_id:
                query += " AND instance_id=?"
                args.append(instance_id)
            r = self._c.execute(query, args).fetchone()
            if r is None:
                return None
            j = self._row(r)
            j["events"] = [dict(e) for e in self._c.execute(
                "SELECT ts, kind, detail FROM trade_decision_events WHERE trade_id=? ORDER BY id",
                (trade_id,))]
            return j

    def list(self, limit: int = 100, mode: Optional[str] = None,
             symbol: Optional[str] = None, result: Optional[str] = None,
             instance_id: Optional[str] = None, strategy: Optional[str] = None,
             timeframe: Optional[str] = None) -> list[dict]:
        q = "SELECT * FROM trade_decision_journal"
        cond, args = [], []
        if mode:
            if mode.upper() == "LEGACY / UNVERIFIED":
                cond.append("(execution_mode IS NULL OR execution_mode='')")
            else:
                cond.append("LOWER(execution_mode)=?"); args.append(mode.lower())
        if symbol:
            cond.append("symbol=?"); args.append(symbol.upper())
        if result:
            cond.append("result=?"); args.append(result)
        if instance_id:
            cond.append("instance_id=?"); args.append(instance_id)
        if strategy:
            cond.append("(strategy_id=? OR strategy_name=? OR strategy=?)")
            args.extend((strategy, strategy, strategy))
        if timeframe:
            cond.append("timeframe=?"); args.append(timeframe)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [self._row(r) for r in self._c.execute(q, args)]

    # ------------------------------------------------------------- evolution
    def update_evolution(self, setup_key: str, strategy: str, regime: str,
                         side: str, r: float) -> dict:
        """Fold one closed trade into the setup's aggregated memory + restage."""
        with self._lock:
            row = self._c.execute("SELECT * FROM evolution_memory WHERE setup_key=?",
                                  (setup_key,)).fetchone()
            trades = (row["trades"] if row else 0) + 1
            wins = (row["wins"] if row else 0) + (1 if r > 0 else 0)
            net_r = (row["net_r"] if row else 0.0) + r
            stage = ("evidence" if trades >= EVIDENCE_MIN
                     else "building" if trades >= EARLY_SIGNAL_MAX else "early-signal")
            wr = round(100 * wins / trades, 1)
            note = (f"{trades} trades, {wr}% win, {net_r:+.1f}R net. "
                    + ("Strong enough to inform strategy changes." if stage == "evidence"
                       else "Early signal — do NOT change strategy on this alone."
                       if stage == "early-signal" else
                       "Building evidence — keep observing before changes."))
            self._c.execute(
                """INSERT OR REPLACE INTO evolution_memory
                (setup_key, strategy, regime, side, trades, wins, net_r, updated_at, stage, note)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (setup_key, strategy, regime, side, trades, wins, net_r, _now(), stage, note))
            self._c.commit()
            return {"setup_key": setup_key, "trades": trades, "win_rate": wr,
                    "net_r": round(net_r, 2), "stage": stage, "note": note}

    def evolution(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT * FROM evolution_memory ORDER BY trades DESC")]
