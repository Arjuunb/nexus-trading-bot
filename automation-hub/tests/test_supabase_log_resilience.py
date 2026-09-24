"""A lost log line must never stop a Trading Instance.

On 2026-09-24 at 12:19:34 two instances (SOLUSDT, LINKUSDT) stopped with
"Unknown internal error: ReadError: [Errno 32] Broken pipe". The traceback
ended in SupabaseLedger.log -> bot_logs insert: a pooled Supabase connection
the server had closed failed the engine's routine "candle_processed" log
line, and nothing between that line and the worker loop caught it.

These tests drive the real SupabaseLedger.log against a fake PostgREST
client that fails the way production did.
"""
import pytest

from data.ledger import SupabaseLedger
from services.trading_instances import InstanceLedger


class ReadError(Exception):
    """Named like httpx.ReadError, which the ledger treats as transient."""


class APIError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class _Table:
    def __init__(self, db, name):
        self.db, self.name, self.row = db, name, None

    def insert(self, row):
        self.row = row
        return self

    def execute(self):
        return self.db.execute(self.name, self.row)


class _PostgREST:
    """Stores rows by primary key; fails the next calls as scripted.

    "lost_reply": the server stores the row, then the reply is lost -- the
    case where a naive retry would write a second row.
    """

    def __init__(self, *failures):
        self.failures = list(failures)
        self.rows: dict[str, dict[str, dict]] = {}
        self.calls = 0

    def table(self, name):
        return _Table(self, name)

    def _store(self, name, row):
        table = self.rows.setdefault(name, {})
        if row["id"] in table:
            raise APIError('duplicate key value violates unique constraint "bot_logs_pkey"', "23505")
        table[row["id"]] = dict(row)

    def execute(self, name, row):
        self.calls += 1
        failure = self.failures.pop(0) if self.failures else None
        if failure == "lost_reply":
            self._store(name, row)
            raise ReadError("[Errno 32] Broken pipe")
        if failure is not None:
            raise failure
        self._store(name, row)
        return type("Response", (), {"data": [row]})()


def _ledger(db):
    ledger = object.__new__(SupabaseLedger)  # no network: the client is the fake
    ledger._db = db
    return ledger


def _engine_log_line(ledger):
    """Exactly the call in the production traceback (auto_engine._ingest)."""
    InstanceLedger(ledger, "a47d2f3d").log(
        level="info", stage="engine", symbol="SOLUSDT",
        message="candle_processed timestamp=2026-09-24T08:05:00+00:00")


def test_a_broken_pipe_on_a_log_line_is_retried_and_written_once():
    db = _PostgREST(ReadError("[Errno 32] Broken pipe"))
    _engine_log_line(_ledger(db))                       # no exception reaches the engine
    rows = list(db.rows["bot_logs"].values())
    assert len(rows) == 1 and db.calls == 2
    assert rows[0]["instance_id"] == "a47d2f3d" and rows[0]["symbol"] == "SOLUSDT"


def test_a_row_that_landed_before_the_reply_was_lost_is_not_written_twice():
    db = _PostgREST("lost_reply")
    _engine_log_line(_ledger(db))
    assert len(db.rows["bot_logs"]) == 1               # the retry met the primary key
    assert db.calls == 2


def test_a_supabase_outage_costs_log_lines_not_the_worker(capsys):
    db = _PostgREST(*[ReadError("[Errno 32] Broken pipe")] * 10)
    for _ in range(3):
        _engine_log_line(_ledger(db))                   # the engine keeps processing candles
    assert db.rows.get("bot_logs", {}) == {}
    assert "log line not written" in capsys.readouterr().out


def test_a_configuration_error_is_not_retried_and_still_does_not_stop_the_worker(capsys):
    db = _PostgREST(APIError('relation "bot_logs" does not exist', "42P01"))
    _engine_log_line(_ledger(db))
    assert db.calls == 1                                # not transient: no retry
    assert "does not exist" in capsys.readouterr().out  # visible, not silent


def test_trading_state_writes_still_fail_closed():
    # Only log rows are best-effort. A position write that fails must still
    # raise, so an order is never believed to exist when it may not.
    db = _PostgREST(ReadError("[Errno 32] Broken pipe"))
    with pytest.raises(ReadError):
        _ledger(db).open_position(symbol="SOLUSDT", side="long", size=1, entry=100.0, stop=99.0)
