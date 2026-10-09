"""Compatibility for bounded Guardian diagnostic reads on Python 3.10+.

The readers must still use SQL CASE byte bounds, row limits, read-only/query-only
connections and deadlines. setlimit (Python 3.11+) is an additional native cap,
not a replacement for those mandatory query-level bounds.
"""
from __future__ import annotations

import sqlite3

# Stable SQLite C API values, not authorizer action codes. Python 3.10 does not
# export these result/limit constants: https://sqlite.org/rescode.html
SQLITE_INTERRUPT = 9
_BLOCKED_CODES = frozenset((5, 6, SQLITE_INTERRUPT))
_LIMIT_LENGTH = 0
_LEGACY_BLOCKED_MESSAGES = frozenset((
    "database is locked", "database table is locked", "database schema is locked", "interrupted",
))


def bound_read_values(conn: sqlite3.Connection, maximum_bytes: int) -> None:
    """Apply the extra native length cap where the stdlib exposes it.

    On 3.10, the caller's bounded SQL projections remain authoritative. Errors
    from an available setter must propagate into the reader's failure state.
    """
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("invalid diagnostic read limit")
    setter = getattr(conn, "setlimit", None)
    if setter is not None:
        setter(_LIMIT_LENGTH, maximum_bytes)


def read_is_blocked(exc: sqlite3.Error) -> bool:
    """Classify numeric codes, or exact legacy SQLite messages; expose no prose."""
    code = getattr(exc, "sqlite_errorcode", None)
    if type(code) is int:
        return (code & 255) in _BLOCKED_CODES
    # Python 3.10 exceptions lack sqlite_errorcode. Unknown/corrupt/full errors
    # are still failed reads, never healthy or eligible for trading.
    return isinstance(exc, sqlite3.OperationalError) and str(exc) in _LEGACY_BLOCKED_MESSAGES
