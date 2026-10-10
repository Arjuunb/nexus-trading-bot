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
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from data.tenant_scope import ensure_tenant_column
from data.journal_evidence_migrations import HEADER_COLUMNS, apply_evidence_migrations
from data.journal_context_migrations import apply_context_migrations
from data.journal_context_store import JournalContextStore

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
        self._c.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._transaction_depth = 0
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
            apply_evidence_migrations(self._c)
            apply_context_migrations(self._c)
            self._commit()
        self.context = JournalContextStore(self)

    @contextmanager
    def transaction(self):
        """Serialize one complete journal capture, including evolution/timeline.

        Nested store methods defer their commits so a failed capture rolls back
        all journal effects together. This never includes ledger transactions.
        """
        with self._lock:
            outer = self._transaction_depth == 0
            if outer and not self._c.in_transaction:
                self._c.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
            except Exception:
                if outer:
                    self._c.rollback()
                raise
            else:
                if outer:
                    self._c.commit()
            finally:
                self._transaction_depth -= 1

    def _commit(self):
        if self._transaction_depth == 0:
            self._c.commit()

    # ------------------------------------------------------------- entry
    def record_entry(self, j: dict) -> bool:
        """Insert the journal at trade open. ``j`` carries the summary fields +
        a ``sections`` dict (entry_decision / checklist / market_snapshot /
        risk_check)."""
        with self.transaction():
            from services.strategy_evidence import evidence_json
            if not self._c.in_transaction:
                self._c.execute("BEGIN IMMEDIATE")
            fields = ("trade_id", "symbol", "side", "strategy", "timeframe", "entry", "stop",
                      "target", "size", "risk_amount", "planned_rr", "confidence", "brain_score",
                      "regime", "instance_id", "instance_name", "strategy_id", "strategy_name",
                      "strategy_version", "execution_mode", "market_data_mode", "market_data_source",
                      "exchange", "position_id", "simulation_session_id", *HEADER_COLUMNS)
            values = {key: j.get(key) for key in fields}
            for source, target in (("signal_timestamp", "signal_at"),
                                   ("decision_timestamp", "decision_at"),
                                   ("entry_timestamp", "executed_at")):
                values[target] = values[target] or j.get(source)
            values.update(created_at=j.get("created_at") or values.get("executed_at") or _now(),
                          mode=j.get("mode", "paper"), status="open",
                          sections_json=evidence_json(j.get("sections", {})))
            existing = self._c.execute("SELECT * FROM trade_decision_journal WHERE trade_id=?",
                                       (j["trade_id"],)).fetchone()
            if existing:
                # Narrative/close fields legitimately evolve; immutable entry
                # values must still agree when a producer retries the open.
                for key in fields:
                    if values[key] != existing[key]:
                        raise ValueError(f"journal entry conflict: {j['trade_id']} ({key})")
                return False
            columns = ",".join(values)
            markers = ",".join("?" for _ in values)
            self._c.execute(f"INSERT INTO trade_decision_journal ({columns}) VALUES ({markers})",
                            tuple(values.values()))
            self._commit()
            return True

    def add_event(self, trade_id: str, kind: str, detail: str = "",
                  ts: Optional[str] = None, event_id: Optional[str] = None) -> bool:
        with self.transaction():
            if not self._c.in_transaction:
                self._c.execute("BEGIN IMMEDIATE")
            if event_id:
                old = self._c.execute("SELECT * FROM trade_decision_events WHERE event_id=?",
                                      (event_id,)).fetchone()
                if old:
                    if (old["trade_id"], old["kind"], old["detail"]) != (trade_id, kind, detail):
                        raise ValueError(f"journal event conflict: {event_id}")
                    if ts and old["ts"] != ts:
                        raise ValueError(f"journal event timestamp conflict: {event_id}")
                    return False
            self._c.execute(
                "INSERT INTO trade_decision_events(trade_id, ts, kind, detail,event_id) VALUES (?,?,?,?,?)",
                (trade_id, ts or _now(), kind, detail, event_id))
            self._commit()
            return True

    # ------------------------------------------------------------- close
    def close_trade(self, trade_id: str, *, exit: float, pnl: float, actual_rr: float,
                    result: str, grade: str, extra_sections: dict,
                    instance_id: str = "", closed_at: Optional[str] = None) -> bool:
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
                (closed_at or _now(), exit, pnl, actual_rr, result, grade,
                 json.dumps(sections), trade_id, *([instance_id] if instance_id else [])))
            self._commit()
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
            self._commit()
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
        d["signal_timestamp"] = d.get("signal_at")
        d["decision_timestamp"] = d.get("decision_at")
        d["entry_timestamp"] = d.get("executed_at")
        d["config_fingerprint"] = d.get("strategy_config_hash")
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
            self._commit()
            return {"setup_key": setup_key, "trades": trades, "win_rate": wr,
                    "net_r": round(net_r, 2), "stage": stage, "note": note}

    def evolution(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT * FROM evolution_memory ORDER BY trades DESC")]

    # ------------------------------------------------ immutable evidence
    def save_strategy_identity(self, identity: dict) -> dict:
        """Store one observed configuration without guessing historical values."""
        import hashlib
        from services.strategy_evidence import evidence_json
        from services.strategy_identity import canonical_configuration_json
        identity = dict(identity)
        strategy_id = identity.get("strategy_id")
        version = identity.get("strategy_version")
        fingerprint = identity.get("strategy_config_hash") or identity.get("config_fingerprint")
        configuration = identity.get("configuration", identity.get("canonical_config"))
        if not strategy_id or not version or not fingerprint or configuration is None:
            raise ValueError("complete observed strategy identity required")
        canonical = canonical_configuration_json(configuration)
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != fingerprint:
            raise ValueError("configuration fingerprint mismatch")
        source_hash = identity.get("source_hash", identity.get("source_fingerprint"))
        # Full transient producer context must never become a strategy snapshot.
        snapshot = {key: identity.get(key) for key in (
            "strategy_id", "strategy_version", "observed_version", "declared_version",
            "identity_status", "source_manifest", "configuration_schema_version")}
        snapshot.update(strategy_id=strategy_id, strategy_version=version,
                        strategy_config_hash=fingerprint, source_hash=source_hash,
                        configuration=configuration)
        encoded = evidence_json(snapshot)
        with self.transaction():
            if not self._c.in_transaction:
                self._c.execute("BEGIN IMMEDIATE")
            old = self._c.execute("""SELECT * FROM strategy_evidence_versions
              WHERE strategy_id=? AND strategy_version=? AND strategy_config_hash=?""",
              (strategy_id, version, fingerprint)).fetchone()
            if old:
                if old["configuration_json"] != canonical or old["source_hash"] != source_hash:
                    raise ValueError("immutable strategy snapshot conflict")
                return json.loads(old["identity_json"])
            self._c.execute("""INSERT INTO strategy_evidence_versions
              (strategy_id,strategy_version,strategy_config_hash,configuration_json,
               source_hash,identity_json,captured_at) VALUES (?,?,?,?,?,?,?)""",
              (strategy_id, version, fingerprint, canonical, source_hash, encoded, _now()))
            self._commit()
        return snapshot

    def strategy_identity(self, strategy_id: str, strategy_version: str,
                          strategy_config_hash: str) -> Optional[dict]:
        with self._lock:
            row = self._c.execute("""SELECT identity_json FROM strategy_evidence_versions
              WHERE strategy_id=? AND strategy_version=? AND strategy_config_hash=?""",
              (strategy_id, strategy_version, strategy_config_hash)).fetchone()
            return json.loads(row[0]) if row else None

    def _insert_evidence_event(self, event_id: str, *, kind: str, payload: dict,
                               scope: dict) -> bool:
        from services.strategy_evidence import CORRELATION_FIELDS, SCOPE_FIELDS, evidence_json
        if not event_id or not kind:
            raise ValueError("durable event_id and event kind required")
        fields = (*SCOPE_FIELDS, *CORRELATION_FIELDS, "observed_at")
        normalized = {key: scope.get(key) for key in fields}
        normalized["strategy_config_hash"] = (scope.get("strategy_config_hash")
                                              or scope.get("config_fingerprint"))
        envelope = evidence_json({"kind": kind, "payload": payload, **normalized})
        old = self._c.execute("SELECT envelope_json FROM strategy_evidence_events WHERE event_id=?",
                              (event_id,)).fetchone()
        if old:
            if old[0] != envelope:
                raise ValueError(f"immutable evidence event conflict: {event_id}")
            return False
        columns = ("event_id", "kind", "payload_json", "envelope_json", "captured_at", *fields)
        self._c.execute(f"INSERT INTO strategy_evidence_events ({','.join(columns)}) "
                        f"VALUES ({','.join('?' for _ in columns)})",
                        (event_id, kind, evidence_json(payload), envelope, _now(),
                         *(normalized[key] for key in fields)))
        return True

    def record_evidence_event(self, event_id: str, *, kind: str, payload: dict,
                              **scope) -> bool:
        """Exact retries succeed without additional rows; conflicting facts fail."""
        with self.transaction():
            if not self._c.in_transaction:
                self._c.execute("BEGIN IMMEDIATE")
            return self._insert_evidence_event(event_id, kind=kind, payload=payload, scope=scope)

    def evidence_events(self, *, limit: Optional[int] = None, **filters) -> list[dict]:
        from services.strategy_evidence import CORRELATION_FIELDS, SCOPE_FIELDS
        allowed = {"event_id", "kind", *SCOPE_FIELDS, *CORRELATION_FIELDS}
        unknown = set(filters) - allowed
        if unknown:
            raise ValueError(f"unsupported evidence filters: {sorted(unknown)}")
        selected = {key: value for key, value in filters.items() if value is not None}
        query = "SELECT * FROM strategy_evidence_events"
        if selected:
            query += " WHERE " + " AND ".join(f"{key}=?" for key in selected)
        query += " ORDER BY rowid"
        args = list(selected.values())
        if limit is not None:
            query += " LIMIT ?"; args.append(int(limit))
        with self._lock:
            rows = [dict(row) for row in self._c.execute(query, args)]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
            row.pop("envelope_json")
        return rows

    def get_evidence_event(self, event_id: str) -> Optional[dict]:
        rows = self.evidence_events(event_id=event_id)
        return rows[0] if rows else None

    def execution_event(self, execution_id: str, *, instance_id: Optional[str] = None,
                        simulation_session_id: Optional[str] = None, **scope) -> Optional[dict]:
        """Resolve a ledger receipt ID within its actual account/session scope."""
        rows = [row for row in self.evidence_events(kind="execution_fill", instance_id=instance_id,
                                                   simulation_session_id=simulation_session_id, **scope)
                if row["payload"].get("execution_id") == execution_id]
        if len(rows) > 1:
            raise ValueError("ambiguous execution ID; explicit account/session scope required")
        return rows[0] if rows else None

    def episode_for_trade(self, trade_id: str, *, instance_id: Optional[str] = None,
                          simulation_session_id: Optional[str] = None,
                          owner_id: Optional[str] = None, account_id: Optional[str] = None) -> Optional[dict]:
        query = """SELECT l.* FROM strategy_episode_legs l
          JOIN strategy_position_episodes e ON e.episode_id=l.episode_id WHERE l.trade_id=?"""
        args = [trade_id]
        for key, value in (("instance_id", instance_id), ("simulation_session_id", simulation_session_id),
                           ("owner_id", owner_id), ("account_id", account_id)):
            if value is not None:
                query += f" AND e.{key}=?"; args.append(value)
        with self._lock:
            rows = self._c.execute(query, args).fetchall()
        if len(rows) > 1:
            raise ValueError("ambiguous trade lineage; explicit instance/session required")
        if not rows:
            return None
        row = dict(rows[0])
        row["metadata"] = json.loads(row.pop("metadata_json"))
        return row

    trade_evidence = episode_for_trade

    def record_execution_evidence(self, event_id: str, *, payload: dict, scope: dict,
                                  links: list[dict], create_episode: bool) -> bool:
        """Atomically capture immutable event and its journal-only lineage links."""
        import hashlib
        from services.strategy_evidence import SCOPE_FIELDS, evidence_json
        with self.transaction():
            if not self._c.in_transaction:
                self._c.execute("BEGIN IMMEDIATE")
            inserted = self._insert_evidence_event(event_id, kind="execution_fill", payload=payload, scope=scope)
            if not inserted:
                return False
            episode_id = scope.get("episode_id")
            if create_episode and episode_id:
                existing = self._c.execute("SELECT * FROM strategy_position_episodes WHERE episode_id=?",
                                           (episode_id,)).fetchone()
                if not existing:
                    columns = ("episode_id", "root_trade_id", "root_position_id", "metadata_json",
                               "opened_at", "initial_risk_amount_text", *SCOPE_FIELDS)
                    self._c.execute(f"INSERT INTO strategy_position_episodes ({','.join(columns)}) "
                                    f"VALUES ({','.join('?' for _ in columns)})",
                                    (episode_id, scope["trade_id"], scope["position_id"],
                                     evidence_json({"schema_version": 1}), scope.get("observed_at"),
                                     payload.get("initial_risk"), *(scope.get(key) for key in SCOPE_FIELDS)))
            for link in links:
                key = hashlib.sha256(evidence_json([
                    scope.get("owner_id"), scope.get("account_id"), scope.get("instance_id"),
                    scope.get("simulation_session_id"), link["trade_id"]]).encode()).hexdigest()
                metadata = evidence_json(link)
                old = self._c.execute("SELECT * FROM strategy_episode_legs WHERE leg_key=?", (key,)).fetchone()
                if old:
                    if old["episode_id"] != episode_id or old["metadata_json"] != metadata:
                        raise ValueError("immutable episode leg conflict")
                    continue
                self._c.execute("""INSERT INTO strategy_episode_legs
                  (leg_key,episode_id,trade_id,position_id,parent_trade_id,entry_event_id,metadata_json)
                  VALUES (?,?,?,?,?,?,?)""", (key, episode_id, link["trade_id"], link["position_id"],
                                            link.get("parent_trade_id"), event_id, metadata))
            return True

    def episodes(self, **filters) -> list[dict]:
        """Rebuild disposable episode views from original immutable capture facts."""
        from services.strategy_evidence import SCOPE_FIELDS
        allowed = {"episode_id", *SCOPE_FIELDS}
        if set(filters) - allowed:
            raise ValueError("unsupported episode filters")
        selected = {key: value for key, value in filters.items() if value is not None}
        query = "SELECT * FROM strategy_position_episodes"
        if selected:
            query += " WHERE " + " AND ".join(f"{key}=?" for key in selected)
        query += " ORDER BY rowid"
        with self._lock:
            return [self._episode_projection(dict(row)) for row in self._c.execute(query, list(selected.values()))]

    def get_episode(self, episode_id: Optional[str]) -> Optional[dict]:
        if not episode_id:
            return None
        episodes = self.episodes(episode_id=episode_id)
        return episodes[0] if episodes else None

    def completed_evidence_episodes(self, **filters) -> list[dict]:
        return [episode for episode in self.episodes(**filters) if episode["status"] == "closed"]

    def _episode_projection(self, episode: dict) -> dict:
        from services.strategy_evidence import build_episode
        events = self.evidence_events(episode_id=episode["episode_id"], kind="execution_fill")
        return build_episode(episode, events=events)

    def get_evidence_completeness_snapshot(self, max_records: int = 100_000) -> dict:
        """Read one consistent bounded view; overflow is never completeness.

        Each source collection has an explicit bound. The transaction includes
        episode projection and journal timelines, so callers cannot combine a
        newer fill with an older journal accidentally. This covers this journal
        only; it does not establish completeness of an external financial store.
        """
        import hashlib
        from services.strategy_evidence import build_episode, evidence_json
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        tables = {
            "events": "strategy_evidence_events", "episode_headers": "strategy_position_episodes",
            "legs": "strategy_episode_legs", "journals": "trade_decision_journal",
            "timeline": "trade_decision_events", "configurations": "strategy_evidence_versions",
        }
        with self.transaction():
            raw, counts = {}, {}
            for name, table in tables.items():
                counts[name] = self._c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                raw[name] = [dict(row) for row in self._c.execute(
                    f"SELECT * FROM {table} ORDER BY rowid LIMIT ?", (max_records + 1,))]
            complete = all(count <= max_records for count in counts.values())
            bounded = {name: rows[:max_records] for name, rows in raw.items()}
            events = []
            for original in bounded["events"]:
                event = dict(original)
                event["payload"] = json.loads(event.pop("payload_json"))
                event.pop("envelope_json")
                events.append(event)
            by_episode, by_trade = {}, {}
            for event in events:
                if event["kind"] == "execution_fill":
                    by_episode.setdefault(event.get("episode_id"), []).append(event)
            for event in bounded["timeline"]:
                by_trade.setdefault(event["trade_id"], []).append(
                    {key: event[key] for key in ("ts", "kind", "detail")})
            episodes = [build_episode(header, events=by_episode.get(header["episode_id"], []))
                        for header in bounded["episode_headers"]]
            journals = []
            for row in bounded["journals"]:
                journal = self._row(row)
                journal["events"] = by_trade.get(row["trade_id"], [])
                journals.append(journal)
            legs = []
            for original in bounded["legs"]:
                leg = dict(original)
                leg["metadata"] = json.loads(leg.pop("metadata_json"))
                legs.append(leg)
            # Include all original selected facts and source counts. The run
            # table is excluded so recording this assessment cannot invalidate
            # the assessment's own evidence watermark.
            watermark = hashlib.sha256(evidence_json(
                {"records": bounded, "source_counts": counts}).encode("utf-8")).hexdigest()
            return {"events": events, "episodes": episodes, "journals": journals,
                    "legs": legs, "configurations": [json.loads(row["identity_json"])
                                                      for row in bounded["configurations"]],
                    "source_complete": complete, "source_counts": counts,
                    "watermark": watermark, "calculated_at": _now(),
                    "bound": max_records,
                    "bound_exceeded": sorted(name for name, count in counts.items()
                                             if count > max_records)}

    def record_reconciliation_run(self, report: dict) -> bool:
        """Append immutable metadata for one actual assessment, never a fill.

        Delivery retries of the same run_id have one logical effect. A later
        assessment needs a new run_id and its actual calculation timestamp;
        changing a previous report under its ID is an explicit conflict.
        """
        from services.strategy_evidence import evidence_json
        run_id = report.get("run_id")
        timestamp = report.get("calculated_at") or report.get("reconciled_at")
        status = report.get("status") or report.get("reconciliation_status")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("reconciliation run_id required")
        if not isinstance(timestamp, str):
            raise ValueError("actual reconciliation calculated_at required")
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("reconciliation calculated_at must be an aware timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("reconciliation calculated_at must be an aware timestamp")
        if status not in {"COMPLETE", "PARTIAL", "UNKNOWN", "CONFLICTED", "RECOVERING"}:
            raise ValueError("unsupported reconciliation status")
        if (report.get("status") and report.get("reconciliation_status")
                and report["status"] != report["reconciliation_status"]):
            raise ValueError("conflicting reconciliation statuses")
        encoded = evidence_json(report)
        watermarks = report.get("watermarks", report.get("source_watermarks"))
        if watermarks is None:
            watermarks = {key: report[key] for key in (
                "source_watermark", "authoritative_watermark", "evidence_watermark",
                "metrics_evidence_watermark") if key in report}
        with self.transaction():
            previous = self._c.execute(
                "SELECT report_json FROM strategy_evidence_reconciliation_runs WHERE run_id=?",
                (run_id,)).fetchone()
            if previous is not None:
                if previous[0] != encoded:
                    raise ValueError(f"immutable reconciliation run conflict: {run_id}")
                return False
            self._c.execute(
                "INSERT INTO strategy_evidence_reconciliation_runs "
                "(run_id,calculated_at,status,scope_json,cohort_json,watermarks_json,report_json,captured_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, parsed.astimezone(timezone.utc).isoformat(), status,
                 evidence_json(report.get("scope")), evidence_json(report.get("cohort")),
                 evidence_json(watermarks),
                 encoded, _now()))
            return True

    def last_successful_reconciliation(self, scope=None, cohort=None) -> Optional[dict]:
        """Most recent COMPLETE assessment for exact supplied scope/cohort.

        A successful write of a PARTIAL report is not a successful
        reconciliation. Omitted filters inspect all runs; explicit mappings,
        including unknown/None-valued dimensions, match their canonical JSON.
        """
        from services.strategy_evidence import evidence_json
        query = "SELECT report_json FROM strategy_evidence_reconciliation_runs WHERE status='COMPLETE'"
        args = []
        for column, value in (("scope_json", scope), ("cohort_json", cohort)):
            if value is not None:
                query += f" AND {column}=?"
                args.append(evidence_json(value))
        query += " ORDER BY calculated_at DESC,rowid DESC LIMIT 1"
        with self._lock:
            row = self._c.execute(query, args).fetchone()
            return json.loads(row[0]) if row else None
