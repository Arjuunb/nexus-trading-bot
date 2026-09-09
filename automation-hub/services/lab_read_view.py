"""Committed lab snapshots on a separate read-only SQLite connection.

Requests never acquire worker locks or issue writes while hydrating the UI.
SQLite's WAL lets readers return the last committed session during a writer.
"""
from contextlib import contextmanager
from copy import copy
from pathlib import Path
import sqlite3
import threading
import time


@contextmanager
def lab_read_view(account):
    path = getattr(account, "path", None)
    if not path or path == ":memory:":
        yield account
        return
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=.25)
    connection.row_factory = sqlite3.Row
    deadline = time.monotonic() + 1
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        connection.execute("BEGIN")
        view = copy(account)
        view._db, view._lock = connection, threading.RLock()
        view.broker = copy(account.broker)
        view.broker._c, view.broker._lock = connection, threading.RLock()
        if hasattr(account, "journal") and not callable(account.journal):
            view.journal = copy(account.journal)
            view.journal._db, view.journal._lock = connection, threading.RLock()
        yield view
    finally:
        connection.close()


def saved_session(account):
    with lab_read_view(account) as view:
        row = view.session()
        decoder = getattr(view, "_decode_session", None) or view._decoded
        session = decoder(row) if row else {}
        # Identity/configuration hydration does not need archived account blobs.
        session.pop("state", None)
        session.pop("metrics", None)
        return {"session": session, "real_execution_allowed": False}


def saved_status(runtime):
    with lab_read_view(runtime.account) as account:
        view = copy(runtime)
        view.account = account
        return view.bot_status()
