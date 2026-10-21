"""Primary-ledger acceptance guards with synthetic SQLite and fake transport.

Only disposable local fixtures and an in-memory Supabase client are used.
No real network, credentials, account execution or runtime thread is involved.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from data.ledger import SqliteLedger, SupabaseLedger, get_ledger
from services.trading_instances import InstanceStore, TradingInstance, TradingInstanceManager


MODE = "NEXUS_ACCEPTANCE_READ_ONLY"


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
    encoded = json.dumps(
        {"schema": schema, "rows": rows},
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: bytes(value).hex(),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class PrimarySqliteReadOnly(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {MODE: "0"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def seed(self, *, instances=False):
        path = self.root / "primary.sqlite"
        ledger = SqliteLedger(str(path))
        try:
            ledger.log(level="info", stage="synthetic-fixture", message="existing retained record")
            if instances:
                store = InstanceStore(ledger)
                for owner, symbol in (("alice", "BTCUSDT"), ("bob", "ETHUSDT")):
                    store.create(TradingInstance(
                        id="saved-" + owner,
                        owner_id=owner,
                        symbol=symbol,
                        strategy_key="synthetic-strategy",
                        strategy_label="Synthetic fixture",
                        strategy_version="frozen-fixture-1",
                        timeframe="5m",
                        risk_per_trade_pct=0.005,
                        capital_allocation=1234.0,
                        state="running",
                        desired_running=True,
                        config_revision=7,
                        sizing_mode="auto",
                        starting_equity=0.0,
                        created_at="2026-01-02T03:04:05+00:00",
                        updated_at="2026-01-02T03:04:05+00:00",
                    ))
        finally:
            ledger._c.close()
        return path

    def test_existing_primary_ledger_does_not_recreate_missing_optional_indexes(self):
        path = self.seed()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("DROP INDEX idx_paper_opened")
            connection.commit()
        before = fingerprint(path)
        os.environ[MODE] = "1"
        ledger = SqliteLedger(str(path))
        try:
            self.assertEqual(ledger.get_paper_trades(), [])
            self.assertEqual(ledger.get_positions(), [])
            self.assertEqual(ledger.get_logs()[0]["message"], "existing retained record")
            self.assertEqual(fingerprint(path), before)
        finally:
            ledger._c.close()

    def test_instance_constructor_preserves_owner_revision_saved_intent_and_financial_rows(self):
        path = self.seed(instances=True)
        before = fingerprint(path)
        os.environ[MODE] = "1"
        ledger = SqliteLedger(str(path))
        try:
            instances = InstanceStore(ledger).list()
            self.assertEqual({instance.owner_id for instance in instances}, {"alice", "bob"})
            self.assertTrue(all(instance.desired_running for instance in instances))
            self.assertTrue(all(instance.state == "running" for instance in instances))
            self.assertTrue(all(instance.config_revision == 7 for instance in instances))
            self.assertTrue(all(instance.strategy_version == "frozen-fixture-1" for instance in instances))
            self.assertEqual(fingerprint(path), before)
        finally:
            ledger._c.close()

    def test_primary_connection_is_read_only_even_after_query_only_is_disabled(self):
        path = self.seed(instances=True)
        os.environ[MODE] = "1"
        ledger = SqliteLedger(str(path))
        try:
            ledger._c.execute("PRAGMA query_only=OFF")
            with self.assertRaisesRegex(sqlite3.DatabaseError, "readonly|read-only|not authorized"):
                ledger._c.execute("CREATE TABLE acceptance_primary_write_probe(id INTEGER)")
        finally:
            ledger._c.close()

    def test_manager_constructor_does_not_create_sessions_from_saved_running_intent(self):
        path = self.seed(instances=True)
        before = fingerprint(path)
        os.environ[MODE] = "1"
        ledger = SqliteLedger(str(path))

        def forbidden_factory(*_args, **_kwargs):
            self.fail("Acceptance constructor attempted to instantiate an execution strategy")

        try:
            manager = TradingInstanceManager(
                ledger, strategy_factory=forbidden_factory, live=False, live_poll_s=60
            )
            self.assertEqual(set(manager._instances), {"saved-alice", "saved-bob"})
            self.assertEqual(manager._runtime, {})
            self.assertTrue(all(instance.desired_running for instance in manager._instances.values()))
            self.assertTrue(all(instance.simulation_session_id == "" for instance in manager._instances.values()))
            self.assertEqual(fingerprint(path), before)
        finally:
            ledger._c.close()

    def test_missing_primary_database_is_rejected_without_creation(self):
        os.environ[MODE] = "1"
        for path in (self.root / "missing.sqlite", self.root / "missing-parent" / "missing.sqlite"):
            with self.subTest(path=str(path)):
                with self.assertRaises((RuntimeError, sqlite3.DatabaseError, ValueError)):
                    SqliteLedger(str(path))
                self.assertFalse(path.exists())
                if path.parent != self.root:
                    self.assertFalse(path.parent.exists())

    def test_incomplete_primary_schema_is_rejected_without_migration(self):
        path = self.root / "incomplete.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE customer_marker(value TEXT)")
            connection.execute("INSERT INTO customer_marker VALUES ('preserve')")
            connection.commit()
        before = fingerprint(path)
        os.environ[MODE] = "1"
        with self.assertRaises((RuntimeError, sqlite3.DatabaseError, ValueError)):
            SqliteLedger(str(path))
        self.assertEqual(fingerprint(path), before)

    def test_missing_instance_schema_is_not_created_on_acceptance_open(self):
        path = self.seed()
        before = fingerprint(path)
        os.environ[MODE] = "1"
        ledger = SqliteLedger(str(path))
        try:
            try:
                store = InstanceStore(ledger)
            except (RuntimeError, sqlite3.DatabaseError, ValueError):
                pass
            else:
                self.assertFalse(store.available)
            self.assertEqual(fingerprint(path), before)
        finally:
            ledger._c.close()

    def test_missing_instance_ownership_or_revision_is_rejected_without_backfill(self):
        for column in ("owner_id", "config_revision"):
            with self.subTest(column=column):
                os.environ[MODE] = "0"
                path = self.seed(instances=True)
                with closing(sqlite3.connect(path)) as connection:
                    if column == "owner_id":
                        connection.execute("DROP INDEX idx_instance_owner")
                    connection.execute("ALTER TABLE trading_instances DROP COLUMN " + column)
                    connection.commit()
                before = fingerprint(path)
                os.environ[MODE] = "1"
                ledger = SqliteLedger(str(path))
                try:
                    with self.assertRaises((RuntimeError, sqlite3.DatabaseError, ValueError)):
                        InstanceStore(ledger)
                    self.assertEqual(fingerprint(path), before)
                finally:
                    ledger._c.close()
                    path.unlink()


class SyntheticQuery:
    def __init__(self, client, table):
        self.client = client
        self.table_name = table

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def neq(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def range(self, *_args, **_kwargs):
        return self

    def insert(self, *_args, **_kwargs):
        self.client.mutations.append(("insert", self.table_name))
        return self

    def update(self, *_args, **_kwargs):
        self.client.mutations.append(("update", self.table_name))
        return self

    def upsert(self, *_args, **_kwargs):
        self.client.mutations.append(("upsert", self.table_name))
        return self

    def delete(self, *_args, **_kwargs):
        self.client.mutations.append(("delete", self.table_name))
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class SyntheticPrimaryClient:
    def __init__(self):
        self.mutations = []
        self.rpc_calls = []

    def table(self, name):
        return SyntheticQuery(self, name)

    def from_(self, name):
        return self.table(name)

    def rpc(self, name, *_args, **_kwargs):
        self.rpc_calls.append(name)
        return SyntheticQuery(self, "rpc:" + name)


class PrimaryRemoteReadOnly(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {MODE: "1"})
        self.environment.start()
        self.client = SyntheticPrimaryClient()
        self.fake_supabase = ModuleType("supabase")
        self.fake_supabase.create_client = lambda *_args, **_kwargs: self.client
        self.modules = patch.dict(sys.modules, {"supabase": self.fake_supabase})
        self.modules.start()

    def tearDown(self):
        self.modules.stop()
        self.environment.stop()

    def ledger(self):
        return SupabaseLedger("https://synthetic.invalid", "synthetic-fixture-key")

    def test_remote_financial_reads_still_use_existing_adapter(self):
        ledger = self.ledger()
        self.assertEqual(ledger.get_paper_trades(), [])
        self.assertEqual(ledger.get_positions(), [])
        self.assertEqual(self.client.mutations, [])

    def test_remote_financial_mutations_are_rejected_before_transport(self):
        ledger = self.ledger()
        for method in ("insert", "update", "upsert", "delete"):
            with self.subTest(method=method):
                query = ledger._t("paper_trades").select("*")
                with self.assertRaises((RuntimeError, AttributeError)):
                    if method == "delete":
                        getattr(query, method)().execute()
                    else:
                        getattr(query, method)({"id": "synthetic"}).execute()
                self.assertEqual(self.client.mutations, [])

    def test_instance_store_cannot_bypass_the_central_client_guard(self):
        ledger = self.ledger()
        store = InstanceStore(ledger)
        with self.assertRaises((RuntimeError, AttributeError)):
            store._table("trading_instances").update({"desired_running": False}).eq("id", "saved").execute()
        self.assertEqual(self.client.mutations, [])

    def test_unknown_or_financial_mutation_rpcs_are_rejected_before_transport(self):
        ledger = self.ledger()
        for name in (
            "paper_open_atomic",
            "paper_close_atomic",
            "paper_reduce_atomic",
            "restart_simulation_account",
            "factory_reset_application_data",
            "unknown_future_rpc",
        ):
            with self.subTest(rpc=name):
                with self.assertRaises((RuntimeError, AttributeError)):
                    ledger._db.rpc(name, {"synthetic": True}).execute()
                self.assertEqual(self.client.rpc_calls, [])

    def test_only_explicit_read_evidence_rpcs_are_permitted(self):
        ledger = self.ledger()
        for name in ("paper_evidence_capabilities", "paper_evidence_snapshot"):
            ledger._db.rpc(name, {}).execute()
        self.assertEqual(self.client.rpc_calls, ["paper_evidence_capabilities", "paper_evidence_snapshot"])
        self.assertEqual(self.client.mutations, [])

    def test_configured_primary_failure_does_not_create_local_diagnostic_fallback(self):
        def unavailable(*_args, **_kwargs):
            raise RuntimeError("synthetic primary unavailable")

        self.fake_supabase.create_client = unavailable
        with tempfile.TemporaryDirectory() as temporary:
            fallback = Path(temporary) / "must-not-become-primary.sqlite"
            with patch.dict(os.environ, {
                "SUPABASE_URL": "https://synthetic.invalid",
                "SUPABASE_KEY": "synthetic-fixture-key",
            }):
                with self.assertRaisesRegex(RuntimeError, "ACCEPTANCE_PRIMARY_LEDGER_UNAVAILABLE"):
                    get_ledger(str(fallback))
            self.assertFalse(fallback.exists())


if __name__ == "__main__":
    unittest.main()
