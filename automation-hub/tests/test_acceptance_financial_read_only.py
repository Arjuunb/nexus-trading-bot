"""Real SQLite regression tests using only synthetic temporary financial data.

No application import, provider, runtime thread, order or execution loop is used.
These tests establish constructor/read-path preservation and fail-closed access;
they do not establish profitability or complete staging acceptance.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from data.account_store import AccountStore
from execution.paper_broker_v2 import PaperBrokerV2
from services.price_action_governance import PriceActionJournalStore
from services.price_action_lab import PriceActionPaperAccount
from services.smc_strategy_lab import SMCPaperAccount


MODE = "NEXUS_ACCEPTANCE_READ_ONLY"
KINDS = ("broker", "pa", "smc", "account", "journal")
INVALID_DATABASE = (RuntimeError, sqlite3.DatabaseError, ValueError)


def construct(kind, path, *, owner="alice", balance=1234.0):
    if kind == "broker":
        return PaperBrokerV2(path, starting_balance=balance, evidence_owner=owner)
    if kind == "pa":
        return PriceActionPaperAccount(path, starting_balance=balance, evidence_owner=owner)
    if kind == "smc":
        return SMCPaperAccount(path, starting_balance=balance, evidence_owner=owner)
    if kind == "account":
        return AccountStore(str(path))
    if kind == "journal":
        return PriceActionJournalStore(path)
    raise AssertionError(kind)


def connections(instance):
    found = []
    seen = set()
    for candidate in (instance, getattr(instance, "broker", None), getattr(instance, "journal", None)):
        for name in ("_c", "_db"):
            value = getattr(candidate, name, None)
            if isinstance(value, sqlite3.Connection) and id(value) not in seen:
                seen.add(id(value))
                found.append(value)
    return found


def close_instance(instance):
    for connection in connections(instance):
        connection.close()


def fingerprint(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        rows = {}
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            escaped = table.replace('"', '""')
            rows[table] = sorted(connection.execute('SELECT * FROM "' + escaped + '"').fetchall(), key=repr)
        proof = {
            "schema": schema,
            "rows": rows,
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        }
    encoded = json.dumps(
        proof,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: {"blob_hex": bytes(value).hex()},
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def read_state(kind, instance):
    if kind == "broker":
        return {
            "account": instance.account(),
            "orders": instance.orders(),
            "positions": instance.positions(),
            "fills": instance.fills(),
        }
    if kind in ("pa", "smc"):
        return instance.state()
    if kind == "account":
        return instance.get()
    if kind == "journal":
        return instance.list()
    raise AssertionError(kind)


class ExistingFinancialReadOnly(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {MODE: "0"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def seed(self, kind):
        path = self.root / (kind + ".sqlite")
        instance = construct(kind, path)
        try:
            if kind == "account":
                instance.seed_if_empty(1234.0)
                instance.update_snapshot(
                    current_equity=1220.25,
                    available_balance=1210.0,
                    realized_pnl=-13.75,
                    unrealized_pnl=0.0,
                )
            elif kind in ("pa", "smc"):
                session = instance.session()
                self.assertEqual(session["mode"], "LIVE_PAPER")
                self.assertEqual(session["status"], "active")
                saved = instance.broker.export_state()
                saved["synthetic_saved_session_note"] = "must survive acceptance restart"
                instance._db.execute(
                    "UPDATE " + kind + "_sessions SET state_json=?,updated_at=? WHERE id=?",
                    (json.dumps(saved, sort_keys=True), "2026-01-02T03:04:05+00:00", session["id"]),
                )
                instance._db.commit()
            elif kind == "broker":
                instance._c.execute("UPDATE v2_account SET peak_equity=1 WHERE id=1")
                instance._c.commit()
        finally:
            close_instance(instance)
        return path

    def test_existing_financial_schemas_rows_and_saved_session_snapshots_are_unchanged(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                os.environ[MODE] = "0"
                path = self.seed(kind)
                before = fingerprint(path)
                os.environ[MODE] = "1"
                instance = construct(kind, path, balance=99999.0)
                try:
                    self.assertIsNotNone(read_state(kind, instance))
                    if kind in ("pa", "smc"):
                        session = instance.session()
                        self.assertEqual(session["mode"], "LIVE_PAPER")
                        self.assertEqual(session["status"], "active")
                        self.assertEqual(session["updated_at"], "2026-01-02T03:04:05+00:00")
                    self.assertEqual(fingerprint(path), before)
                finally:
                    close_instance(instance)

    def test_every_financial_connection_rejects_direct_writes_even_if_query_only_is_disabled(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                os.environ[MODE] = "0"
                path = self.seed(kind)
                os.environ[MODE] = "1"
                instance = construct(kind, path)
                try:
                    financial_connections = connections(instance)
                    self.assertTrue(financial_connections)
                    for connection in financial_connections:
                        try:
                            connection.execute("PRAGMA query_only=OFF")
                        except sqlite3.DatabaseError:
                            pass
                        with self.assertRaisesRegex(sqlite3.DatabaseError, "readonly|read-only|not authorized"):
                            connection.execute("CREATE TABLE acceptance_write_probe(id INTEGER)")
                finally:
                    close_instance(instance)

    def test_missing_financial_databases_are_rejected_without_creation_or_seeding(self):
        os.environ[MODE] = "1"
        for kind in KINDS:
            for nested in (False, True):
                with self.subTest(kind=kind, nested=nested):
                    path = self.root / kind / "missing.sqlite" if nested else self.root / (kind + "-missing.sqlite")
                    with self.assertRaises(INVALID_DATABASE):
                        construct(kind, path)
                    self.assertFalse(path.exists())
                    if nested:
                        self.assertFalse(path.parent.exists())

    def test_incomplete_financial_schemas_are_rejected_without_automatic_migrations(self):
        os.environ[MODE] = "1"
        for kind in KINDS:
            with self.subTest(kind=kind):
                path = self.root / (kind + "-incomplete.sqlite")
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("CREATE TABLE existing_customer_marker(value TEXT)")
                    connection.execute("INSERT INTO existing_customer_marker VALUES ('preserve')")
                    connection.commit()
                before = fingerprint(path)
                with self.assertRaises(INVALID_DATABASE):
                    construct(kind, path)
                self.assertEqual(fingerprint(path), before)

    def test_absent_account_rows_are_not_reconstructed_from_default_balances(self):
        for kind in ("broker", "pa", "smc", "account"):
            with self.subTest(kind=kind):
                os.environ[MODE] = "0"
                path = self.seed(kind)
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("DELETE FROM " + ("account_state" if kind == "account" else "v2_account"))
                    connection.commit()
                before = fingerprint(path)
                os.environ[MODE] = "1"
                with self.assertRaises(INVALID_DATABASE):
                    construct(kind, path, balance=99999.0)
                self.assertEqual(fingerprint(path), before)

    def test_historical_lab_sessions_are_read_without_creating_new_live_paper_sessions(self):
        for kind in ("pa", "smc"):
            with self.subTest(kind=kind):
                os.environ[MODE] = "0"
                path = self.seed(kind)
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute(
                        "UPDATE " + kind + "_sessions SET status='closed',end_reason='synthetic-ended-session'"
                    )
                    connection.commit()
                before = fingerprint(path)
                os.environ[MODE] = "1"
                instance = construct(kind, path)
                try:
                    self.assertEqual(instance.session(), {})
                    self.assertEqual(read_state(kind, instance)["session"], {})
                    self.assertEqual(fingerprint(path), before)
                finally:
                    close_instance(instance)

    def test_acceptance_reads_cannot_reassign_existing_evidence_to_another_owner(self):
        for kind in ("broker", "pa", "smc"):
            with self.subTest(kind=kind):
                os.environ[MODE] = "0"
                path = self.seed(kind)
                before = fingerprint(path)
                os.environ[MODE] = "1"
                with self.assertRaisesRegex(ValueError, "OWNER_CONFLICT"):
                    construct(kind, path, owner="bob")
                self.assertEqual(fingerprint(path), before)

    def test_committed_wal_financial_state_is_read_without_checkpoint_or_rewrite(self):
        path = self.seed("account")
        with closing(sqlite3.connect(path)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "UPDATE account_state SET current_equity=1207.5,last_updated=?",
                ("2026-02-03T04:05:06+00:00",),
            )
            writer.commit()
            wal = Path(str(path) + "-wal")
            self.assertTrue(wal.is_file())
            wal_before = hashlib.sha256(wal.read_bytes()).hexdigest()
            before = fingerprint(path)
            os.environ[MODE] = "1"
            instance = construct("account", path)
            try:
                self.assertEqual(instance.get()["current_equity"], 1207.5)
                self.assertEqual(instance.get()["last_updated"], "2026-02-03T04:05:06+00:00")
                self.assertEqual(fingerprint(path), before)
                self.assertEqual(hashlib.sha256(wal.read_bytes()).hexdigest(), wal_before)
            finally:
                close_instance(instance)


if __name__ == "__main__":
    unittest.main()
