"""Opt-in acceptance boundary; never grants execution or changes saved intent.

This mode requires existing financial databases. It cannot initialize or repair
them, and it is deliberately independent of every legacy autostart switch.
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3


def enabled() -> bool:
    value = os.environ.get("HUB_ACCEPTANCE_READ_ONLY", "0").strip().lower()
    if value not in {"0", "1", "false", "true", "no", "yes", "off", "on", ""}:
        raise RuntimeError("Invalid HUB_ACCEPTANCE_READ_ONLY setting; refusing startup")
    return value in {"1", "true", "yes", "on"}


def existing_connection(path: str | Path, *, autocommit: bool = False) -> sqlite3.Connection:
    """Read committed WAL data using SQLite's supported read-only connection.

    Do not use immutable=1: a live writer may have committed records in WAL.
    No parent directory, database, schema or account is created here.
    """
    if str(path) == ":memory:" or not Path(path).is_file():
        raise RuntimeError("Existing financial database required for acceptance")
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                         check_same_thread=False, timeout=10,
                         isolation_level=None if autocommit else "")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    denied = {getattr(sqlite3, name) for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_ATTACH", "SQLITE_DETACH",
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
        "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW", "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_ALTER_TABLE",
        "SQLITE_REINDEX", "SQLITE_ANALYZE", "SQLITE_CREATE_VTABLE", "SQLITE_DROP_VTABLE")}

    def authorize(action, first, second, _database, _trigger):
        if action in denied:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and second is not None:
            read_arguments = {"table_info", "table_xinfo", "index_info", "index_xinfo",
                              "index_list", "foreign_key_list", "foreign_key_check",
                              "integrity_check", "quick_check"}
            if first not in read_arguments and not (
                    first in {"query_only", "foreign_keys"} and str(second).lower() in {"1", "on"}):
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    db.set_authorizer(authorize)
    return db


def require_row(db: sqlite3.Connection, sql: str):
    row = db.execute(sql).fetchone()
    if row is None:
        raise RuntimeError("Existing financial account required for acceptance")
    return row


def request_allowed(method: str, path: str) -> bool:
    """Authentication remains usable; execution and mutations stay closed.

    Research writes need separate target validation. This first acceptance
    mode permits reads of existing evidence, not new research submissions.
    """
    if method == "OPTIONS":
        return True
    if method in {"GET", "HEAD"}:
        # Some legacy GET handlers start or repair native runtimes. Keep the
        # acceptance surface explicit instead of trusting HTTP method alone.
        return path in {"/", "/app", "/health", "/version", "/api/version", "/api/v1",
                        "/login", "/user/settings"} or any(
            path == prefix or path.startswith(prefix + "/") for prefix in (
                "/auth", "/settings", "/app", "/static", "/assets", "/webui",
                "/api/v2/strategy-intelligence"))
    return method == "POST" and path in {
        "/auth/supabase/session", "/auth/supabase/refresh", "/auth/logout",
        "/auth/login", "/auth/two-factor", "/login", "/logout"}
