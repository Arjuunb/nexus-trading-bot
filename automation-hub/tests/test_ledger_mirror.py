"""The journal reads a Supabase ledger through a local mirror, and gets the
same records it would get from the ledger itself.

Supabase cannot run here, so the "remote" is a real SqliteLedger answering
exactly the two queries the PostgREST adapter makes (rows at or after a
timestamp, ordered and paged; rows by key). The trades in it are real: the
3-Candle Rejection strategy through AutoStrategyEngine, the pipeline and the
paper engines.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from data.decision_store import DecisionStore
from data.trade_record_store import TradeRecordStore
from services.journal_recorder import JournalRecorder, LedgerSource
from services.ledger_mirror import LedgerMirror, PostgrestTables

_FACTS = ("trade_id", "status", "record_origin", "symbol", "side", "actual_entry", "actual_exit",
          "planned_stop_loss", "planned_take_profit", "risk_amount", "net_pnl", "realized_r",
          "exit_reason", "entry_filled_at", "position_closed_at", "position_id", "order_id",
          "decision_id", "instance_id", "session_id", "execution_key", "journal_record_id")


class SqliteTables:
    """Stand-in for Supabase: a real ledger answering the PostgREST queries."""

    def __init__(self, ledger):
        from services.ledger_mirror import _ENGINE_LOGS_DDL
        self.ledger = ledger
        self.calls = 0
        with ledger._lock:                    # the instance store creates it in production
            ledger._c.execute(_ENGINE_LOGS_DDL)
            ledger._c.commit()

    def page(self, table, *, order, since, offset, limit):
        self.calls += 1
        sql = f"SELECT * FROM {table}" + (f" WHERE {order} >= ?" if since else "")
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        args = ([since] if since else []) + [limit, offset]
        with self.ledger._lock:
            return [dict(r) for r in self.ledger._c.execute(sql, args)]

    def by_ids(self, table, key, ids):
        with self.ledger._lock:
            return [dict(r) for r in self.ledger._c.execute(
                f"SELECT * FROM {table} WHERE {key} IN ({','.join('?' for _ in ids)})", ids)]


def _journal(ledger, path, decisions=None) -> list[dict]:
    store = TradeRecordStore(str(path))
    recorder = JournalRecorder(store)
    recorder.add_ledger(LedgerSource("MAIN", ledger, decision_store=decisions))
    report = recorder.reconcile()
    assert not report["errors"], report["errors"]
    assert not [r for r in report["ledgers"] if r.get("skipped")], report["ledgers"]
    return sorted(({k: row.get(k) for k in _FACTS} for row in store.query_trades()),
                  key=lambda r: r["execution_key"])


def test_a_forward_trade_journals_the_same_from_the_mirror(tmp_path):
    from tests.test_journal_record_timing import _forward_trade
    _, remote, _ = _forward_trade(tmp_path)
    decisions = DecisionStore(str(tmp_path / "decisions.db"))
    mirror = LedgerMirror(SqliteTables(remote), tmp_path / "mirror.db", page_size=3)  # forces paging
    changes_before = remote._c.total_changes
    report = mirror.sync()
    assert remote._c.total_changes == changes_before          # the remote is only read
    assert report["tables"]["webhook_events"]["copied"] > 0
    direct = _journal(remote, tmp_path / "direct.db", decisions)
    mirrored = _journal(mirror.ledger, tmp_path / "mirrored.db", decisions)
    assert direct and mirrored == direct
    assert direct[0]["status"] == "OPEN" and direct[0]["record_origin"] == "FORWARD_PAPER"


def test_later_closes_new_trades_and_released_claims_reach_the_mirror(tmp_path):
    from data.ledger import SqliteLedger
    from tests.test_loss_streak_order import _attempt, _engine
    ledger, engine, paper = _engine(tmp_path)
    mirror = LedgerMirror(SqliteTables(ledger), tmp_path / "mirror.db")
    assert _attempt(engine, paper, 0, "win")
    mirror.sync()
    first = _journal(mirror.ledger, tmp_path / "j1.db")
    assert [r["status"] for r in first] == ["CLOSED"]

    # A second trade, and a claim that was taken and then released.
    assert _attempt(engine, paper, 3, "loss")
    ledger.insert_webhook_event(alert_id="claim-1", symbol="BTCUSDT", side="BUY", entry=100.0,
                                stop=99.0, payload={}, status="claimed", instance_id="inst-s")
    mirror.sync()
    assert mirror.ledger._c.execute(
        "SELECT COUNT(*) FROM webhook_events WHERE alert_id='claim-1'").fetchone()[0] == 1
    ledger.release_webhook_claim("claim-1", instance_id="inst-s")
    mirror.sync()
    assert mirror.ledger._c.execute(
        "SELECT COUNT(*) FROM webhook_events WHERE alert_id='claim-1'").fetchone()[0] == 0
    assert _journal(mirror.ledger, tmp_path / "j2.db") == _journal(ledger, tmp_path / "j3.db")
    assert isinstance(ledger, SqliteLedger)


def test_a_trade_that_closes_after_a_sync_is_updated(tmp_path):
    """Opened before one sync, closed before the next: the close reaches the copy."""
    from bot.types import Bar
    from datetime import timedelta
    from tests.test_loss_streak_order import T0, TF, _engine
    from tests.test_three_candle_rejection import _history, _long_pattern
    ledger, engine, paper = _engine(tmp_path)
    mirror = LedgerMirror(SqliteTables(ledger), tmp_path / "mirror.db")
    rows, i = _history()
    series = rows + _long_pattern(i)
    bars = [Bar(T0 + TF * k, r.open, r.high, r.low, r.close, r.volume) for k, r in enumerate(series)]
    strategy = engine.strategy_factory("BTCUSDT")
    strategy.bars.extend(bars[:-3])
    for bar in bars[-3:]:
        engine._process_bar("BTCUSDT", bar, strategy)
    pos = paper.open_position("BTCUSDT")
    assert pos is not None
    mirror.sync()
    assert [r["status"] for r in _journal(mirror.ledger, tmp_path / "a.db")] == ["OPEN"]
    engine._process_bar("BTCUSDT", Bar(bars[-1].timestamp + TF, pos["entry"], pos["target"] + 0.2,
                                       pos["entry"] - 0.1, pos["target"], 1.0), strategy)
    mirror.sync()
    [closed] = _journal(mirror.ledger, tmp_path / "b.db")
    assert closed["status"] == "CLOSED" and closed["exit_reason"] == "take-profit"
    assert [closed] == _journal(ledger, tmp_path / "c.db")
    assert timedelta  # noqa: B018


def test_a_column_only_the_remote_has_is_kept(tmp_path):
    from tests.test_loss_streak_order import _attempt, _engine
    ledger, engine, paper = _engine(tmp_path)
    assert _attempt(engine, paper, 0, "win")
    ledger._c.execute("ALTER TABLE paper_trades ADD COLUMN remote_only TEXT")
    ledger._c.execute("UPDATE paper_trades SET remote_only='kept'")
    ledger._c.commit()
    mirror = LedgerMirror(SqliteTables(ledger), tmp_path / "mirror.db")
    mirror.sync()
    assert mirror.ledger._c.execute("SELECT remote_only FROM paper_trades").fetchone()[0] == "kept"


def test_a_failing_remote_is_reported_and_the_copy_keeps_what_it_had(tmp_path):
    from tests.test_loss_streak_order import _attempt, _engine
    ledger, engine, paper = _engine(tmp_path)
    assert _attempt(engine, paper, 0, "win")
    tables = SqliteTables(ledger)
    mirror = LedgerMirror(tables, tmp_path / "mirror.db")
    mirror.sync()
    tables.page = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("unreachable"))
    try:
        mirror.sync()
        raise AssertionError("a failing remote must be reported")
    except RuntimeError as exc:
        assert "unreachable" in str(exc)
    assert mirror.counts()["paper_trades"] == 1


class _Query:
    def __init__(self, log):
        self.log = log

    def __getattr__(self, name):
        def call(*args):
            self.log.append((name, args))
            return self
        return call

    def execute(self):
        self.log.append(("execute", ()))
        return type("R", (), {"data": [{"id": "x"}]})()


def test_the_postgrest_adapter_only_reads():
    """The adapter's query shape (it cannot run against Supabase here)."""
    log: list = []

    class Ledger:
        def _t(self, name):
            log.append(("table", (name,)))
            return _Query(log)
    tables = PostgrestTables(Ledger())
    assert tables.page("paper_trades", order="opened_at", since="2026-01-01T00:00:00+00:00",
                       offset=1000, limit=1000) == [{"id": "x"}]
    assert ("gte", ("opened_at", "2026-01-01T00:00:00+00:00")) in log
    assert ("range", (1000, 1999)) in log and ("order", ("opened_at",)) in log
    log.clear()
    tables.by_ids("webhook_events", "id", ["a", "b"])
    assert ("in_", ("id", ["a", "b"])) in log
    assert not {name for name, _ in log} & {"insert", "update", "upsert", "delete"}
    assert datetime.now(timezone.utc)
