"""Shared SQLite connection policy for runtime-owned databases."""
from __future__ import annotations

import sqlite3
import threading
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


class TransactionLock:
    """The reentrant lock around one shared connection. When an exception
    leaves the outermost hold while the connection is in a transaction, the
    transaction is rolled back. Left open, it would keep the database's write
    lock from every other connection or, in WAL mode, pin a stale snapshot
    that refuses this connection's own later writes."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._lock = threading.RLock()
        self._depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if not self._lock.acquire(blocking, timeout):
            return False
        self._depth += 1
        return True

    def release(self) -> None:
        self._depth -= 1
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is not None and self._depth == 1 and self._connection.in_transaction:
                try:
                    self._connection.rollback()
                except sqlite3.Error:
                    pass
        finally:
            self.release()


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
