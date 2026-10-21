"""Persistent paper-account state.

Fixes capital/equity resetting to the default after a restart. Previously the
paper balance was only ever *derived* (`starting_cash` env default + realized
P&L from the ledger), so a backend restart on an ephemeral disk reverted it to
the default. This store keeps the account's own record under HUB_DATA_DIR:

    initial_capital    the original starting value (set once, edited only with
                       explicit confirmation)
    current_equity     latest account value = initial_capital + realized + unrealized
    available_balance  funds not committed to open positions
    realized_pnl       closed-trade P&L
    unrealized_pnl     open-trade P&L (against last marks)
    last_updated       timestamp of the latest snapshot

The ledger's closed trades remain the source of truth for realized P&L; this
store persists initial_capital and a snapshot so the numbers survive restart
(when HUB_DATA_DIR points at a persistent disk) and are never reset by logout.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        from services.acceptance_mode import enabled, existing_connection, require_row
        if enabled():
            self._c = existing_connection(path)
            require_row(self._c, "SELECT * FROM account_state WHERE id=1")
            return
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.execute(
            """CREATE TABLE IF NOT EXISTS account_state (
                   id INTEGER PRIMARY KEY CHECK (id = 1),
                   initial_capital REAL NOT NULL,
                   current_equity REAL NOT NULL,
                   available_balance REAL NOT NULL,
                   realized_pnl REAL NOT NULL DEFAULT 0,
                   unrealized_pnl REAL NOT NULL DEFAULT 0,
                   last_updated TEXT
               )""")
        self._c.commit()

    def _row(self) -> Optional[dict]:
        r = self._c.execute("SELECT * FROM account_state WHERE id = 1").fetchone()
        return dict(r) if r else None

    def get(self) -> Optional[dict]:
        with self._lock:
            return self._row()

    def seed_if_empty(self, initial_capital: float) -> dict:
        """Create the account on first run from the configured starting cash.
        Never overwrites an existing (persisted) account — that is the whole
        point: the saved value wins over the default on restart."""
        with self._lock:
            from services.acceptance_mode import enabled
            if self._row() is None and enabled():
                raise RuntimeError("Existing financial account required for acceptance")
            if self._row() is None:
                self._c.execute(
                    "INSERT INTO account_state (id, initial_capital, current_equity, "
                    "available_balance, realized_pnl, unrealized_pnl, last_updated) "
                    "VALUES (1, ?, ?, ?, 0, 0, ?)",
                    (initial_capital, initial_capital, initial_capital, _utcnow()))
                self._c.commit()
            return self._row()

    def initial_capital(self) -> float:
        with self._lock:
            r = self._row()
            return float(r["initial_capital"]) if r else 0.0

    def update_snapshot(self, *, current_equity: float, available_balance: float,
                        realized_pnl: float, unrealized_pnl: float = 0.0) -> dict:
        """Persist the latest account values (called on every trade close so the
        state survives a restart)."""
        with self._lock:
            self._c.execute(
                "UPDATE account_state SET current_equity=?, available_balance=?, "
                "realized_pnl=?, unrealized_pnl=?, last_updated=? WHERE id=1",
                (current_equity, available_balance, realized_pnl, unrealized_pnl, _utcnow()))
            self._c.commit()
            return self._row()

    def set_initial_capital(self, amount: float, *, reset_account: bool) -> dict:
        """Change the starting capital. This is an intentional, confirmed action.
        When ``reset_account`` is true the paper account is reset to the new
        initial capital (equity/available = initial, P&L zeroed) — the caller is
        responsible for clearing trade history to match."""
        with self._lock:
            if self._row() is None:
                self._c.execute(
                    "INSERT INTO account_state (id, initial_capital, current_equity, "
                    "available_balance, realized_pnl, unrealized_pnl, last_updated) "
                    "VALUES (1, ?, ?, ?, 0, 0, ?)",
                    (amount, amount, amount, _utcnow()))
            elif reset_account:
                self._c.execute(
                    "UPDATE account_state SET initial_capital=?, current_equity=?, "
                    "available_balance=?, realized_pnl=0, unrealized_pnl=0, last_updated=? "
                    "WHERE id=1", (amount, amount, amount, _utcnow()))
            else:
                self._c.execute(
                    "UPDATE account_state SET initial_capital=?, last_updated=? WHERE id=1",
                    (amount, _utcnow()))
            self._c.commit()
            return self._row()
