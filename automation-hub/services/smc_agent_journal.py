"""The agent's memory: what it decided, what happened, what it learned.

This is the record a disciplined trader keeps. It exists so the agent can look
back at its own behaviour the way a person reviews a trading journal -- what
was taken, what was skipped and why, what was missed, what went right, what
went wrong, and which mistakes keep repeating.

Three rules shape the schema:

  * **Every observation is recorded, not just the trades.** A journal holding
    only the trades that were taken cannot answer "what did I miss?" or "why
    did I skip that?", and those are the questions that change a trader's
    behaviour. Skips and misses are rows here, with the reason attached.

  * **It is append-only.** A journal that can be edited after the outcome is
    known is not evidence of anything -- it is a story written backwards.
    Triggers refuse UPDATE and DELETE on every table, so a correction is a new
    row that references the old one rather than a rewrite of it.

  * **It never touches the strategy.** Nothing in this module reads, writes,
    imports or parameterises the SMC decision path. It stores what the strategy
    already decided, alongside what the agent did about it. Improvement ideas
    aimed at the strategy land in `proposed_improvements` and stay there until
    a human acts on them.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

#: Outcome of one agent observation.
TAKEN = "TAKEN"            # the agent opened a trade on this signal
REJECTED = "REJECTED"      # SMC offered a trade; an AGENT gate refused it
NOT_READY = "NOT_READY"    # SMC itself was not ready — nothing was offered
MISSED = "MISSED"          # SMC offered a trade and the agent failed to act
EXECUTION_FAILED = "EXECUTION_FAILED"
EXECUTION_UNCERTAIN = "EXECUTION_UNCERTAIN"
RECONCILED = "RECONCILED"
DECISION_OUTCOMES = (TAKEN, REJECTED, NOT_READY, MISSED,
                     EXECUTION_FAILED, EXECUTION_UNCERTAIN, RECONCILED)

# Durable execution lifecycle.  These states belong to the execution intent,
# not to the strategy decision: the strategy remains read-only while the
# broker/journal boundary is recovered after partial failure.
DECISION_APPROVED = "DECISION_APPROVED"
EXECUTION_PENDING = "EXECUTION_PENDING"
EXECUTED = "EXECUTED"
EXECUTION_COMPLETE = "COMPLETE"
EXECUTION_STATES = (DECISION_APPROVED, EXECUTION_PENDING, EXECUTED,
                    EXECUTION_FAILED, EXECUTION_UNCERTAIN, RECONCILED,
                    EXECUTION_COMPLETE)

#: How a closed trade is judged. Deliberately separate from win/loss.
CORRECT = "CORRECT"                    # followed the rules
MISTAKE = "MISTAKE"                    # broke a rule the agent controls
CORRECT_BUT_LOST = "CORRECT_BUT_LOST"  # followed the rules, market disagreed
LUCKY = "LUCKY"                        # broke a rule and profited anyway
VERDICTS = (CORRECT, MISTAKE, CORRECT_BUT_LOST, LUCKY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid.uuid4().hex


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _load(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


_TABLES = ("agent_decisions", "agent_trades", "agent_reviews",
           "agent_lessons", "agent_weekly_reviews", "agent_proposed_improvements")


class SMCAgentJournal:
    """Append-only store for the agent's decisions, trades and reviews."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._depth = 0
        self._migrate()

    # -------------------------------------------------------- transactions
    def _commit(self) -> None:
        """Commit unless an enclosing transaction() owns the boundary."""
        if not self._depth:
            self._db.commit()

    @contextmanager
    def transaction(self):
        """Make several journal writes land together or not at all.

        Used where a journal row and a real side effect have to agree. The
        writes inside are ordered BEFORE the side effect so a journal that
        cannot accept them raises first and the side effect never happens;
        the commit comes after, so a side effect that fails takes its rows
        with it. Nothing here weakens append-only: rows that roll back were
        never visible, and committed rows remain immutable.
        """
        if self._depth:
            yield self
            return
        self._db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self
        except BaseException:
            self._depth = 0
            self._db.rollback()
            raise
        else:
            self._depth = 0
            self._db.commit()

    # ------------------------------------------------------------- schema
    def _migrate(self) -> None:
        c = self._db
        c.executescript("""
        CREATE TABLE IF NOT EXISTS agent_decisions(
            id TEXT PRIMARY KEY, at TEXT NOT NULL, candle_time TEXT,
            symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
            smc_state TEXT NOT NULL, outcome TEXT NOT NULL,
            reason_code TEXT NOT NULL, reason TEXT NOT NULL,
            setup_id TEXT, proposal_id TEXT,
            conditions_json TEXT, missing_json TEXT, plan_json TEXT,
            gates_json TEXT, market_json TEXT,
            strategy_fingerprint TEXT, trade_id TEXT);

        CREATE TABLE IF NOT EXISTS agent_trades(
            id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, opened_at TEXT NOT NULL,
            symbol TEXT NOT NULL, timeframe TEXT NOT NULL, direction TEXT NOT NULL,
            entry REAL NOT NULL, stop REAL NOT NULL, target REAL NOT NULL,
            planned_rr REAL NOT NULL, size REAL NOT NULL,
            -- What risk asked for before the position bound was applied, and
            -- whether the bound moved it. A journal showing only the executed
            -- size cannot tell a trade sized at 0.9 by choice from one capped
            -- there, and those are different trades to review.
            requested_size REAL, size_capped INTEGER NOT NULL DEFAULT 0,
            risk_amount REAL, setup_id TEXT, proposal_id TEXT,
            conditions_json TEXT, market_json TEXT, why TEXT NOT NULL,
            closed_at TEXT, exit_price REAL, realised_r REAL, result TEXT,
            close_reason TEXT, strategy_fingerprint TEXT, order_id TEXT);

        CREATE TABLE IF NOT EXISTS agent_reviews(
            id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, at TEXT NOT NULL,
            verdict TEXT NOT NULL, followed_rules INTEGER NOT NULL,
            result TEXT, realised_r REAL,
            did_well_json TEXT, did_badly_json TEXT,
            violations_json TEXT, why TEXT NOT NULL);

        CREATE TABLE IF NOT EXISTS agent_lessons(
            id TEXT PRIMARY KEY, at TEXT NOT NULL, pattern TEXT NOT NULL,
            occurrences INTEGER NOT NULL, detail TEXT NOT NULL,
            evidence_json TEXT, first_seen TEXT, last_seen TEXT);

        CREATE TABLE IF NOT EXISTS agent_weekly_reviews(
            id TEXT PRIMARY KEY, at TEXT NOT NULL,
            period_start TEXT NOT NULL, period_end TEXT NOT NULL,
            summary_json TEXT NOT NULL, agent_findings_json TEXT,
            lessons_json TEXT);

        CREATE TABLE IF NOT EXISTS agent_proposed_improvements(
            id TEXT PRIMARY KEY, at TEXT NOT NULL, target TEXT NOT NULL,
            title TEXT NOT NULL, rationale TEXT NOT NULL,
            evidence_json TEXT, status TEXT NOT NULL, applied INTEGER NOT NULL);

        CREATE INDEX IF NOT EXISTS ix_decisions_at ON agent_decisions(at);
        CREATE INDEX IF NOT EXISTS ix_decisions_outcome ON agent_decisions(outcome, at);
        CREATE INDEX IF NOT EXISTS ix_trades_opened ON agent_trades(opened_at);
        CREATE INDEX IF NOT EXISTS ix_reviews_trade ON agent_reviews(trade_id);

        -- The intent is written before the broker call.  It is deliberately
        -- separate from the append-only decision/trade tables because its
        -- current state must advance as the external paper broker responds.
        CREATE TABLE IF NOT EXISTS execution_intents(
            id TEXT PRIMARY KEY, execution_key TEXT NOT NULL UNIQUE,
            decision_id TEXT, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
            candle_time TEXT, proposal_id TEXT, state TEXT NOT NULL,
            broker_order_id TEXT, trade_id TEXT, error TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS execution_intent_events(
            id TEXT PRIMARY KEY, execution_key TEXT NOT NULL,
            state TEXT NOT NULL, broker_order_id TEXT, trade_id TEXT,
            error TEXT, payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_execution_intents_state
          ON execution_intents(state, updated_at);
        CREATE INDEX IF NOT EXISTS ix_execution_events_key
          ON execution_intent_events(execution_key, created_at);
        """)
        # A journal that can be rewritten after the outcome is known is a story,
        # not evidence. Closing a trade is the one legitimate update, so it goes
        # through a narrow trigger-exempt path rather than opening the table.
        for table in _TABLES:
            if table == "agent_trades":
                continue
            c.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_update
              BEFORE UPDATE ON {table}
              BEGIN SELECT RAISE(ABORT,
                'agent journal is append-only: {table} cannot be updated'); END;
            CREATE TRIGGER IF NOT EXISTS {table}_no_delete
              BEFORE DELETE ON {table}
              BEGIN SELECT RAISE(ABORT,
                'agent journal is append-only: {table} cannot be deleted'); END;
            """)
        # Columns added to agent_trades after its first version. A journal is
        # long-lived by definition, so opening a file an earlier build wrote
        # has to widen it rather than refuse it or lose what is in it.
        present = {row["name"] for row in c.execute("PRAGMA table_info(agent_trades)")}
        for column, definition in (("requested_size", "requested_size REAL"),
                                   ("size_capped", "size_capped INTEGER NOT NULL DEFAULT 0"),
                                   ("order_id", "order_id TEXT")):
            if column not in present:
                c.execute(f"ALTER TABLE agent_trades ADD COLUMN {definition}")

        # An open trade may be closed exactly once, and nothing else about it
        # may move — not the entry, not the stop, not the planned RR.
        c.executescript("""
        CREATE TRIGGER IF NOT EXISTS agent_trades_close_once
          BEFORE UPDATE ON agent_trades
          WHEN OLD.closed_at IS NOT NULL
          BEGIN SELECT RAISE(ABORT,
            'agent journal is append-only: a closed trade cannot be changed'); END;
        CREATE TRIGGER IF NOT EXISTS agent_trades_plan_is_fixed
          BEFORE UPDATE ON agent_trades
          WHEN OLD.entry <> NEW.entry OR OLD.stop <> NEW.stop
            OR OLD.target <> NEW.target OR OLD.planned_rr <> NEW.planned_rr
            OR OLD.size <> NEW.size OR OLD.direction <> NEW.direction
          BEGIN SELECT RAISE(ABORT,
            'agent journal is append-only: the plan a trade was opened on is fixed'); END;
        CREATE TRIGGER IF NOT EXISTS agent_trades_no_delete
          BEFORE DELETE ON agent_trades
          BEGIN SELECT RAISE(ABORT,
            'agent journal is append-only: agent_trades cannot be deleted'); END;
        """)
        c.commit()

    # ----------------------------------------------------- execution intents
    @staticmethod
    def _intent(row: sqlite3.Row | None) -> Optional[dict]:
        if row is None:
            return None
        out = dict(row)
        out["payload"] = _load(out.pop("payload_json", None)) or {}
        return out

    def create_execution_intent(self, *, execution_key: str, symbol: str,
                                timeframe: str, candle_time: str = "",
                                proposal_id: str = "", payload: Any = None,
                                decision_id: str = "") -> dict:
        """Durably claim one execution key before touching the broker."""
        key = str(execution_key or "").strip()
        if not key:
            raise ValueError("execution intent requires a stable execution key")
        existing = self._db.execute(
            "SELECT * FROM execution_intents WHERE execution_key=?", (key,)
        ).fetchone()
        if existing:
            return self._intent(existing)  # type: ignore[return-value]
        now, intent_id = _now(), _id()
        self._db.execute(
            "INSERT INTO execution_intents(id,execution_key,decision_id,symbol,"
            "timeframe,candle_time,proposal_id,state,payload_json,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (intent_id, key, decision_id or None, symbol, timeframe, candle_time,
             proposal_id, DECISION_APPROVED, _dump(payload or {}), now, now),
        )
        self._db.execute(
            "INSERT INTO execution_intent_events(id,execution_key,state,payload_json,created_at)"
            " VALUES (?,?,?,?,?)", (_id(), key, DECISION_APPROVED,
                                     _dump(payload or {}), now))
        self._commit()
        return self.execution_intent(key)  # type: ignore[return-value]

    def execution_intent(self, execution_key: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT * FROM execution_intents WHERE execution_key=?",
            (str(execution_key),)).fetchone()
        return self._intent(row)

    def execution_intents(self, *, states: Iterable[str] = ()) -> list[dict]:
        wanted = tuple(str(state) for state in states)
        if not wanted:
            rows = self._db.execute(
                "SELECT * FROM execution_intents ORDER BY created_at").fetchall()
        else:
            marks = ",".join("?" for _ in wanted)
            rows = self._db.execute(
                f"SELECT * FROM execution_intents WHERE state IN ({marks}) "
                "ORDER BY created_at", wanted).fetchall()
        return [self._intent(row) for row in rows]

    def transition_execution(self, execution_key: str, state: str, *,
                             broker_order_id: str = "", trade_id: str = "",
                             decision_id: str = "", error: str = "",
                             payload: Any = None) -> dict:
        """Record a durable state transition and its immutable event."""
        if state not in EXECUTION_STATES:
            raise ValueError(f"unknown execution state {state!r}")
        current = self.execution_intent(execution_key)
        if current is None:
            raise KeyError(execution_key)
        now = _now()
        order_id = broker_order_id or current.get("broker_order_id") or None
        linked_trade = trade_id or current.get("trade_id") or None
        linked_decision = decision_id or current.get("decision_id") or None
        detail = error or current.get("error") or None
        self._db.execute(
            "UPDATE execution_intents SET state=?,decision_id=?,broker_order_id=?,"
            "trade_id=?,error=?,updated_at=? WHERE execution_key=?",
            (state, linked_decision, order_id, linked_trade, detail, now,
             str(execution_key)),
        )
        self._db.execute(
            "INSERT INTO execution_intent_events(id,execution_key,state,"
            "broker_order_id,trade_id,error,payload_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (_id(), str(execution_key), state, order_id, linked_trade, detail,
             _dump(payload or {}), now),
        )
        self._commit()
        return self.execution_intent(execution_key)  # type: ignore[return-value]

    # ----------------------------------------------------------- decisions
    def record_decision(self, *, symbol: str, timeframe: str, smc_state: str,
                        outcome: str, reason_code: str, reason: str,
                        candle_time: str = "", setup_id: str = "",
                        proposal_id: str = "", conditions: Any = None,
                        missing: Any = None, plan: Any = None, gates: Any = None,
                        market: Any = None, strategy_fingerprint: str = "",
                        trade_id: str = "", at: Optional[str] = None) -> str:
        """Record one observation. Every look at the market leaves a row."""
        if outcome not in DECISION_OUTCOMES:
            raise ValueError(f"unknown decision outcome {outcome!r}")
        if not reason_code or not reason:
            raise ValueError("a decision must say why — reason_code and reason "
                             "are how the journal answers 'why did I skip that?'")
        row_id = _id()
        self._db.execute(
            "INSERT INTO agent_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row_id, at or _now(), candle_time, symbol, timeframe, smc_state,
             outcome, reason_code, reason, setup_id, proposal_id,
             _dump(conditions), _dump(missing), _dump(plan), _dump(gates),
             _dump(market), strategy_fingerprint, trade_id))
        self._commit()
        return row_id

    def decision_for_candle(self, *, symbol: str, timeframe: str,
                            candle_time: str) -> Optional[dict]:
        """The decision already recorded for this closed candle, if any.

        The runtime polls every few seconds and the evaluation only moves when
        a candle closes. Without this the journal would fill with thousands of
        identical "still watching" rows a day and stop being readable, and the
        agent would record the same entry decision on every poll.
        """
        if not candle_time:
            return None
        row = self._db.execute(
            "SELECT * FROM agent_decisions WHERE symbol=? AND timeframe=? "
            "AND candle_time=? ORDER BY at LIMIT 1",
            (symbol, timeframe, candle_time)).fetchone()
        return self._decision(row) if row else None

    def decision_for_proposal(self, proposal_id: str) -> Optional[dict]:
        """The decision already recorded against this SMC proposal, if any.

        A proposal is the strategy's unit of "this specific trade". Acting on
        one twice — across a restart, a replay, or two workers — would be two
        orders for one signal.
        """
        if not proposal_id:
            return None
        row = self._db.execute(
            "SELECT * FROM agent_decisions WHERE proposal_id=? AND outcome=? "
            "ORDER BY at LIMIT 1", (proposal_id, TAKEN)).fetchone()
        return self._decision(row) if row else None

    def decisions(self, *, outcome: str = "", since: str = "",
                  limit: int = 500) -> list[dict]:
        q = "SELECT * FROM agent_decisions WHERE 1=1"
        args: list = []
        if outcome:
            q += " AND outcome=?"
            args.append(outcome)
        if since:
            q += " AND at>=?"
            args.append(since)
        q += " ORDER BY at DESC LIMIT ?"
        args.append(int(limit))
        return [self._decision(r) for r in self._db.execute(q, args)]

    @staticmethod
    def _decision(row: sqlite3.Row) -> dict:
        out = dict(row)
        for key in ("conditions", "missing", "plan", "gates", "market"):
            out[key] = _load(out.pop(f"{key}_json", None))
        return out

    # -------------------------------------------------------------- trades
    def open_trade(self, *, decision_id: str, symbol: str, timeframe: str,
                   direction: str, entry: float, stop: float, target: float,
                   planned_rr: float, size: float, why: str,
                   requested_size: Optional[float] = None,
                   size_capped: bool = False,
                   risk_amount: Optional[float] = None, setup_id: str = "",
                   proposal_id: str = "", conditions: Any = None,
                   market: Any = None, strategy_fingerprint: str = "",
                   order_id: str = "", opened_at: Optional[str] = None) -> str:
        if not why:
            raise ValueError("a trade must record why it was taken")
        trade_id = _id()
        # Columns are named rather than positional: a journal outlives the
        # build that created it, and a column added by _migrate to an existing
        # file lands at the end of the table rather than where the CREATE
        # statement puts it. Naming them means an older file still writes.
        self._db.execute(
            "INSERT INTO agent_trades(id, decision_id, opened_at, symbol, timeframe,"
            " direction, entry, stop, target, planned_rr, size, requested_size,"
            " size_capped, risk_amount, setup_id, proposal_id, conditions_json,"
            " market_json, why, strategy_fingerprint, order_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, decision_id, opened_at or _now(), symbol, timeframe,
             direction, float(entry), float(stop), float(target),
             float(planned_rr), float(size),
             float(requested_size if requested_size is not None else size),
             int(bool(size_capped)),
             risk_amount, setup_id, proposal_id,
             _dump(conditions), _dump(market), why,
             strategy_fingerprint, order_id))
        self._commit()
        return trade_id

    def close_trade(self, trade_id: str, *, exit_price: float, realised_r: float,
                    result: str, close_reason: str,
                    closed_at: Optional[str] = None) -> dict:
        cur = self._db.execute(
            "UPDATE agent_trades SET closed_at=?, exit_price=?, realised_r=?, "
            "result=?, close_reason=? WHERE id=? AND closed_at IS NULL",
            (closed_at or _now(), float(exit_price), float(realised_r),
             result, close_reason, trade_id))
        self._commit()
        if not cur.rowcount:
            raise ValueError(f"no open trade {trade_id!r} to close")
        return self.trade(trade_id)

    def trade(self, trade_id: str) -> dict:
        row = self._db.execute("SELECT * FROM agent_trades WHERE id=?",
                               (trade_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown trade {trade_id!r}")
        return self._trade(row)

    def trades(self, *, open_only: bool = False, closed_only: bool = False,
               since: str = "", limit: int = 500) -> list[dict]:
        q = "SELECT * FROM agent_trades WHERE 1=1"
        args: list = []
        if open_only:
            q += " AND closed_at IS NULL"
        if closed_only:
            q += " AND closed_at IS NOT NULL"
        if since:
            q += " AND opened_at>=?"
            args.append(since)
        q += " ORDER BY opened_at DESC LIMIT ?"
        args.append(int(limit))
        return [self._trade(r) for r in self._db.execute(q, args)]

    @staticmethod
    def _trade(row: sqlite3.Row) -> dict:
        out = dict(row)
        out["conditions"] = _load(out.pop("conditions_json", None))
        out["market"] = _load(out.pop("market_json", None))
        out["open"] = out.get("closed_at") is None
        out["size_capped"] = bool(out.get("size_capped"))
        return out

    # ------------------------------------------------------------- reviews
    def record_review(self, *, trade_id: str, verdict: str, followed_rules: bool,
                      why: str, did_well: Iterable[str] = (),
                      did_badly: Iterable[str] = (),
                      violations: Iterable[dict] = (),
                      result: str = "", realised_r: Optional[float] = None,
                      at: Optional[str] = None) -> str:
        if verdict not in VERDICTS:
            raise ValueError(f"unknown review verdict {verdict!r}")
        row_id = _id()
        self._db.execute(
            "INSERT INTO agent_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (row_id, trade_id, at or _now(), verdict, int(bool(followed_rules)),
             result, realised_r, _dump(list(did_well)), _dump(list(did_badly)),
             _dump(list(violations)), why))
        self._commit()
        return row_id

    def reviews(self, *, trade_id: str = "", since: str = "",
                limit: int = 500) -> list[dict]:
        q = "SELECT * FROM agent_reviews WHERE 1=1"
        args: list = []
        if trade_id:
            q += " AND trade_id=?"
            args.append(trade_id)
        if since:
            q += " AND at>=?"
            args.append(since)
        q += " ORDER BY at DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for row in self._db.execute(q, args):
            item = dict(row)
            item["followed_rules"] = bool(item["followed_rules"])
            for key in ("did_well", "did_badly", "violations"):
                item[key] = _load(item.pop(f"{key}_json", None)) or []
            out.append(item)
        return out

    # ------------------------------------------------------------- lessons
    def record_lesson(self, *, pattern: str, occurrences: int, detail: str,
                      evidence: Any = None, first_seen: str = "",
                      last_seen: str = "", at: Optional[str] = None) -> str:
        row_id = _id()
        self._db.execute(
            "INSERT INTO agent_lessons VALUES (?,?,?,?,?,?,?,?)",
            (row_id, at or _now(), pattern, int(occurrences), detail,
             _dump(evidence), first_seen, last_seen))
        self._commit()
        return row_id

    def lessons(self, limit: int = 200) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM agent_lessons ORDER BY at DESC LIMIT ?", (int(limit),))
        out = []
        for row in rows:
            item = dict(row)
            item["evidence"] = _load(item.pop("evidence_json", None))
            out.append(item)
        return out

    # ------------------------------------------------------- weekly review
    def record_weekly_review(self, *, period_start: str, period_end: str,
                             summary: dict, agent_findings: Iterable[dict] = (),
                             lessons: Iterable[dict] = (),
                             at: Optional[str] = None) -> str:
        row_id = _id()
        self._db.execute(
            "INSERT INTO agent_weekly_reviews VALUES (?,?,?,?,?,?,?)",
            (row_id, at or _now(), period_start, period_end, _dump(summary),
             _dump(list(agent_findings)), _dump(list(lessons))))
        self._commit()
        return row_id

    def weekly_reviews(self, limit: int = 52) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM agent_weekly_reviews ORDER BY at DESC LIMIT ?",
            (int(limit),))
        out = []
        for row in rows:
            item = dict(row)
            for key in ("summary", "agent_findings", "lessons"):
                item[key] = _load(item.pop(f"{key}_json", None))
            out.append(item)
        return out

    # ------------------------------------------- proposed improvements only
    def propose_improvement(self, *, target: str, title: str, rationale: str,
                            evidence: Any = None, at: Optional[str] = None) -> str:
        """Record an idea. Recording is the ONLY thing that happens to it.

        ``target`` names what the idea is about. "STRATEGY" means it would
        change the SMC rules, and nothing in this system may act on it: it is
        filed, surfaced to a human, and left alone. ``applied`` exists so that
        an idea which somehow got applied would be visible; nothing here ever
        sets it.
        """
        row_id = _id()
        self._db.execute(
            "INSERT INTO agent_proposed_improvements VALUES (?,?,?,?,?,?,?,?)",
            (row_id, at or _now(), target, title, rationale, _dump(evidence),
             "PROPOSED", 0))
        self._commit()
        return row_id

    def proposed_improvements(self, *, target: str = "",
                              limit: int = 200) -> list[dict]:
        q = "SELECT * FROM agent_proposed_improvements WHERE 1=1"
        args: list = []
        if target:
            q += " AND target=?"
            args.append(target)
        q += " ORDER BY at DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for row in self._db.execute(q, args):
            item = dict(row)
            item["evidence"] = _load(item.pop("evidence_json", None))
            item["applied"] = bool(item["applied"])
            out.append(item)
        return out

    def close(self) -> None:
        self._db.close()
