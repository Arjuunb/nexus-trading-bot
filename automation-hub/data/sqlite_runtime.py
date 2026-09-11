"""Shared SQLite connection policy for runtime-owned databases."""
from __future__ import annotations

import sqlite3
from pathlib import Path


SQLITE_BUSY_TIMEOUT_MS = 10_000


def runtime_connection(
        path: str | Path, *, autocommit: bool = False,
        busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a WAL connection with a consistent bounded lock wait."""
    value = max(1, int(busy_timeout_ms))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(path), timeout=value / 1000, check_same_thread=False,
        isolation_level=None if autocommit else "",
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={value}")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def is_sqlite_busy(exc: BaseException) -> bool:
    """Whether a SQLite error is transient contention worth retrying.

    "interrupted" belongs here with "locked" and "busy". SQLITE_INTERRUPT is
    raised when a read is cut short rather than because anything is wrong with
    the query or the schema, so the correct operator response is identical:
    retry. Leaving it out meant the one error the VPS actually produced under
    load reached callers as an unclassified failure, and the lab status routes
    reported a retryable condition as a hard 500.
    """
    message = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in message or "busy" in message or "interrupted" in message
    )
