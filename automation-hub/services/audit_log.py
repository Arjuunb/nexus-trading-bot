"""The security audit log: append-only, hash-chained, and checkable.

Every state-changing request that reaches the API -- configuration changes,
instance start/stop/pause, halts and resumes, key attach/rotate/revoke,
manual closes, sign-in attempts, webhook alerts -- is recorded with who made
it (actor and how they authenticated), where from (source address, user
agent), what (method, path, a redacted copy of the request body, and for
explicit change events the previous and new values) and the outcome (status,
duration).

Why it can be trusted after an incident:

* **Append-only.** SQLite triggers abort any UPDATE or DELETE on the entries
  table, so nothing in the product -- and no bug in it -- can amend a row.
* **Chained.** Each entry stores the SHA-256 of the previous entry's hash
  plus its own canonical content. Editing, deleting or reordering any row
  outside the application (e.g. with a SQLite shell) breaks every hash after
  it, and ``verify()`` names the first entry that no longer matches.
* **Exportable.** ``export_jsonl()`` writes the chain, hashes included, so a
  copy kept elsewhere pins the head: the one thing a chain alone cannot show
  is its newest rows being truncated, and an external copy of the head hash
  does.

Secrets never enter the log: every value passes through
``services.redaction`` before it is hashed and stored.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from services.redaction import redact

GENESIS = "0" * 64
_MAX_DETAIL_CHARS = 8000

# The fields covered by the hash, in a fixed order. Adding a field later means
# a new chain version, never a silent change to what old hashes cover.
HASHED_FIELDS = ("seq", "ts", "kind", "actor", "auth", "ip", "user_agent", "method",
                 "path", "query", "status", "duration_ms", "action", "detail", "prev_hash")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_entries (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    actor       TEXT NOT NULL,
    auth        TEXT NOT NULL,
    ip          TEXT NOT NULL,
    user_agent  TEXT NOT NULL,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    query       TEXT NOT NULL,
    status      INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS audit_entries_no_update
BEFORE UPDATE ON audit_entries
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_entries_no_delete
BEFORE DELETE ON audit_entries
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE INDEX IF NOT EXISTS audit_entries_actor ON audit_entries(actor);
CREATE INDEX IF NOT EXISTS audit_entries_path ON audit_entries(path);
-- Bookkeeping about the log (e.g. how far the external export has got). Not
-- part of the chain and deliberately not append-only: it records progress,
-- not events.
CREATE TABLE IF NOT EXISTS audit_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def entry_hash(entry: dict) -> str:
    """SHA-256 over the canonical JSON of the hashed fields."""
    canonical = json.dumps({k: entry[k] for k in HASHED_FIELDS}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _detail_text(detail: Any) -> str:
    if detail is None or detail == "":
        return ""
    text = detail if isinstance(detail, str) else json.dumps(
        redact(detail), sort_keys=True, ensure_ascii=False, default=str)
    if isinstance(detail, str):
        text = redact(text)
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[:_MAX_DETAIL_CHARS] + f"...[truncated {len(text) - _MAX_DETAIL_CHARS} chars]"
    return text


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: explicit transactions, so BEGIN IMMEDIATE below
        # serialises writers -- including a second process on the same file.
        self._c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._c.executescript(_SCHEMA)

    # ------------------------------------------------------------------ write
    def append(self, *, kind: str, actor: str, auth: str = "none", ip: str = "",
               user_agent: str = "", method: str = "", path: str = "", query: str = "",
               status: int = 0, duration_ms: int = 0, action: str = "",
               detail: Any = None) -> dict:
        """Add one entry to the chain and return it (hash included)."""
        entry = {
            "ts": _now(), "kind": str(kind)[:40], "actor": redact(str(actor or "anonymous"))[:200],
            "auth": str(auth or "none")[:40], "ip": str(ip or "")[:64],
            "user_agent": redact(str(user_agent or ""))[:300], "method": str(method or "")[:10],
            "path": redact(str(path or ""))[:500], "query": redact(str(query or ""))[:1000],
            "status": int(status or 0), "duration_ms": int(duration_ms or 0),
            "action": redact(str(action or ""))[:200], "detail": _detail_text(detail),
        }
        with self._lock:
            self._c.execute("BEGIN IMMEDIATE")
            try:
                row = self._c.execute(
                    "SELECT seq, hash FROM audit_entries ORDER BY seq DESC LIMIT 1").fetchone()
                entry["seq"] = (row["seq"] + 1) if row else 1
                entry["prev_hash"] = row["hash"] if row else GENESIS
                entry["hash"] = entry_hash(entry)
                cols = ("seq",) + tuple(k for k in HASHED_FIELDS if k != "seq") + ("hash",)
                self._c.execute(
                    f"INSERT INTO audit_entries({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    tuple(entry[c] for c in cols))
                self._c.execute("COMMIT")
            except BaseException:
                self._c.execute("ROLLBACK")
                raise
        return entry

    # ------------------------------------------------------------------- read
    def list(self, *, limit: int = 100, before_seq: Optional[int] = None,
             actor: str = "", path_prefix: str = "", kind: str = "") -> list[dict]:
        where, args = [], []
        if before_seq:
            where.append("seq < ?"); args.append(int(before_seq))
        if actor:
            where.append("actor = ?"); args.append(actor)
        if path_prefix:
            where.append("path LIKE ?"); args.append(path_prefix.replace("%", "") + "%")
        if kind:
            where.append("kind = ?"); args.append(kind)
        sql = "SELECT * FROM audit_entries"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            return [dict(r) for r in self._c.execute(sql, args)]

    def entries_after(self, seq: int, *, limit: int = 500) -> list[dict]:
        """Entries with ``seq`` greater than the given one, oldest first."""
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT * FROM audit_entries WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                (int(seq), max(1, min(int(limit), 5000))))]

    def get_state(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._c.execute("SELECT value FROM audit_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._c.execute("INSERT INTO audit_state(key,value) VALUES (?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def _iter_all(self) -> Iterator[dict]:
        with self._lock:
            rows = [dict(r) for r in self._c.execute("SELECT * FROM audit_entries ORDER BY seq ASC")]
        yield from rows

    def head(self) -> dict:
        with self._lock:
            row = self._c.execute(
                "SELECT seq, hash, ts FROM audit_entries ORDER BY seq DESC LIMIT 1").fetchone()
        return dict(row) if row else {"seq": 0, "hash": GENESIS, "ts": None}

    def verify(self) -> dict:
        """Recompute the whole chain. ``ok`` is False at the first entry whose
        stored hash, link or sequence no longer matches."""
        prev, expected_seq, count = GENESIS, 1, 0
        for row in self._iter_all():
            count += 1
            problem = None
            if row["seq"] != expected_seq:
                problem = f"sequence gap: expected {expected_seq}, found {row['seq']}"
            elif row["prev_hash"] != prev:
                problem = "link broken: prev_hash does not match the previous entry"
            elif entry_hash(row) != row["hash"]:
                problem = "content altered: stored hash does not match the entry"
            if problem:
                return {"ok": False, "entries": count, "first_bad_seq": row["seq"],
                        "reason": problem, "head_hash": prev}
            prev, expected_seq = row["hash"], expected_seq + 1
        return {"ok": True, "entries": count, "first_bad_seq": None, "reason": "",
                "head_hash": prev}

    def export_jsonl(self) -> Iterator[str]:
        for row in self._iter_all():
            yield json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"

    def close(self) -> None:
        with self._lock:
            self._c.close()


# ------------------------------------------------------------ process default
_default: Optional[AuditLog] = None
_default_lock = threading.Lock()


def default_log() -> Optional[AuditLog]:
    """The process-wide audit log at ``settings.audit_path``, opened on first
    use. ``None`` (and a message on stderr) if it cannot be opened, so a disk
    problem degrades auditing rather than taking the API down."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                try:
                    from config import settings
                    _default = AuditLog(settings.audit_path)
                except Exception as exc:  # noqa: BLE001
                    import sys
                    print(f"[audit] log unavailable: {type(exc).__name__}: {exc}"[:400],
                          file=sys.stderr, flush=True)
                    return None
    return _default


def set_default_log(log: Optional[AuditLog]) -> None:
    """Swap the process default (tests)."""
    global _default
    _default = log


def record_change(*, action: str, actor: str, auth: str = "session", before: Any = None,
                  after: Any = None, ip: str = "", note: str = "") -> Optional[dict]:
    """An explicit change event with the previous and the new value -- the
    request entry says a change was asked for; this says what it changed."""
    log = default_log()
    if log is None:
        return None
    try:
        return log.append(kind="change", actor=actor, auth=auth, ip=ip, action=action,
                          status=200, detail={"before": before, "after": after, "note": note})
    except Exception as exc:  # noqa: BLE001
        import sys
        print(f"[audit] change not written: {type(exc).__name__}: {exc}"[:400],
              file=sys.stderr, flush=True)
        return None
