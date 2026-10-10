"""Transactional metadata recovery without alternate accounting writes."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from types import SimpleNamespace
import uuid

import pytest

from data.ledger import SqliteLedger
from services.trading_instances import InstanceLedger


def open_pair(ledger, *, key="open-a", instance="one", session="session-a", evidence=True):
    position = {"symbol": "XRPUSDT", "side": "long", "size": 2, "entry": 1,
                "stop": .9, "target": 1.2, "instance_id": instance,
                "simulation_session_id": session}
    trade = {**position, "alert_id": key, "strategy_id": "adaptive_trend_pullback",
             "risk_amount_at_entry": .2}
    if evidence:
        trade["_evidence"] = {"context": {"decision_id": "decision-a"},
                              "observed_at": "2026-10-01T00:00:00+00:00",
                              "receipt": {"funding": None, "funding_coverage": "not_modeled"}}
    return ledger.open_position_and_trade(position=position, trade=trade, execution_id=key)


def reduce_pair(ledger, pid, tid, *, key="reduce-a", evidence=None):
    position = next(row for row in ledger.get_positions() if row["id"] == pid)
    return ledger.reduce_position_and_trade(
        position=position, trade_id=tid,
        remainder_position={**position, "size": 1},
        remainder_trade={**position, "size": 1, "risk_amount_at_entry": .2},
        exit_price=1.1, pnl=.098, rr=.5, closed_size=1, fees=.002,
        equity_after_close=10000.098, instance_id=position["instance_id"],
        execution_id=key, evidence=evidence)


def close_pair(ledger, pid, tid, *, key="close-a", evidence=None):
    return ledger.close_position_and_trade(
        position_id=pid, trade_id=tid, exit_price=1.2, pnl=.196, rr=1,
        fees=.004, equity_after_close=10000.294, execution_id=key, evidence=evidence)


def test_open_reduce_close_publish_exact_parent_remainder_ids_atomically():
    ledger = SqliteLedger()
    pid, tid = open_pair(ledger)
    new_pid, new_tid = reduce_pair(ledger, pid, tid, evidence={"observed_at": "2026-10-01T01:00:00+00:00"})
    close_pair(ledger, new_pid, new_tid, evidence={"observed_at": "2026-10-01T02:00:00+00:00"})
    events = ledger.get_evidence_outbox(instance_id="one", simulation_session_id="session-a")
    assert [row["action"] for row in events] == ["OPEN", "REDUCE", "CLOSE"]
    opened, reduced, closed = events
    assert opened["trade_id"] == tid and opened["position_id"] == pid
    assert opened["context"]["decision_id"] == "decision-a"
    assert opened["observed_at"] == "2026-10-01T00:00:00+00:00"
    assert reduced["parent_trade_id"] == tid and reduced["parent_position_id"] == pid
    assert reduced["remainder_trade_id"] == new_tid and reduced["remainder_position_id"] == new_pid
    assert reduced["trade_id"] == tid and reduced["position_id"] == pid
    assert reduced["receipt"]["net_pnl"] == .098
    assert reduced["receipt"]["booked_fees"] == .002
    assert reduced["receipt"]["initial_risk_amount"] == .2
    assert closed["trade_id"] == new_tid and closed["position_id"] == new_pid
    assert closed["receipt"]["net_pnl"] == .196
    assert closed["receipt"]["booked_fees"] == .004
    assert closed["receipt"]["initial_risk_amount"] == .2
    assert ledger.supports_evidence_outbox is True


@pytest.mark.parametrize("action", ["open", "reduce", "close"])
def test_outbox_failure_rolls_back_the_existing_accounting_unit(action):
    ledger = SqliteLedger()
    pid, tid = open_pair(ledger)
    before = ledger.get_authoritative_evidence_snapshot()
    ledger._c.execute("CREATE TRIGGER fail_evidence BEFORE INSERT ON paper_evidence_outbox "
                      "BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected outbox failure"):
        if action == "open":
            open_pair(ledger, key="other-open")
        elif action == "reduce":
            reduce_pair(ledger, pid, tid)
        else:
            close_pair(ledger, pid, tid)
    assert ledger.get_authoritative_evidence_snapshot() == before


def test_duplicate_execution_cannot_duplicate_outbox_or_accounting():
    ledger = SqliteLedger()
    open_pair(ledger)
    before = ledger.get_authoritative_evidence_snapshot()
    with pytest.raises(sqlite3.IntegrityError):
        open_pair(ledger)
    assert ledger.get_authoritative_evidence_snapshot() == before
    assert len(ledger.get_evidence_outbox()) == 1


def test_immutable_outbox_and_unknown_context_are_preserved():
    ledger = SqliteLedger()
    open_pair(ledger, evidence=False)
    event = ledger.get_evidence_outbox()[0]
    assert event["context"] is None
    assert event["observed_at"] is None
    with pytest.raises(sqlite3.IntegrityError):
        ledger._c.execute("UPDATE paper_evidence_outbox SET receipt_json='{}'")
    with pytest.raises(sqlite3.IntegrityError):
        ledger._c.execute("DELETE FROM paper_evidence_outbox")


def test_snapshot_is_consistent_read_only_and_reproducible():
    ledger = SqliteLedger()
    open_pair(ledger)
    statements = []
    ledger._c.set_trace_callback(statements.append)
    first = ledger.get_authoritative_evidence_snapshot(instance_id="one", simulation_session_id="session-a")
    second = ledger.get_authoritative_evidence_snapshot(instance_id="one", simulation_session_id="session-a")
    assert first == second
    assert first["source_complete"] is True
    assert first["outbox_supported"] is True
    assert len(first["trades"]) == len(first["positions"]) == len(first["executions"]) == len(first["outbox"]) == 1
    assert all(sql.lstrip().split()[0].upper() in ("BEGIN", "SELECT", "ROLLBACK") for sql in statements)


def test_instance_session_forwarding_isolates_same_symbol():
    ledger = SqliteLedger()
    open_pair(ledger, key="old", instance="one", session="old")
    open_pair(ledger, key="new", instance="one", session="new")
    open_pair(ledger, key="other", instance="two", session="new")
    scoped = InstanceLedger(ledger, "one", "new")
    assert scoped.supports_evidence_outbox is True
    assert [row["execution_id"] for row in scoped.get_evidence_outbox()] == ["new"]
    snapshot = scoped.get_authoritative_evidence_snapshot()
    assert [row["execution_id"] for row in snapshot["executions"]] == ["new"]
    assert [row["execution_id"] for row in snapshot["outbox"]] == ["new"]


def test_primary_payload_is_captured_before_observer_mutation(tmp_path):
    ledger = SqliteLedger(tmp_path / "ledger.db")
    pid, tid = open_pair(ledger)
    event = ledger.get_evidence_outbox()[0]
    event["context"]["decision_id"] = "mutated"
    assert ledger.get_evidence_outbox()[0]["context"]["decision_id"] == "decision-a"
    reopened = SqliteLedger(tmp_path / "ledger.db")
    assert reopened.get_evidence_outbox()[0]["trade_id"] == tid
    assert reopened.get_evidence_outbox()[0]["position_id"] == pid


def test_snapshot_proves_legacy_unscoped_close_membership_by_exact_primary_ids():
    ledger = SqliteLedger()
    pid, tid = open_pair(ledger)
    close_pair(ledger, pid, tid)
    # Preserve the original accounting RPC behavior and its empty scope.
    assert ledger.get_execution_receipts()[-1]["instance_id"] == ""
    snapshot = ledger.get_authoritative_evidence_snapshot("one", "session-a")
    assert [row["action"] for row in snapshot["executions"]] == ["OPEN", "CLOSE"]
    assert snapshot["source_complete"] is True


def test_exact_outbox_reader_uses_primary_key_and_isolates_account_session():
    ledger = SqliteLedger()
    open_pair(ledger)
    statements = []
    ledger._c.set_trace_callback(statements.append)
    scoped = InstanceLedger(ledger, "one", "session-a")
    row = scoped.get_evidence_outbox_event("open-a")
    assert row["execution_id"] == "open-a"
    assert len(statements) == 1 and "WHERE execution_id='open-a'" in statements[0]
    assert InstanceLedger(ledger, "one", "other-session").get_evidence_outbox_event("open-a") is None
    assert InstanceLedger(ledger, "other", "session-a").get_evidence_outbox_event("open-a") is None
    assert scoped.get_evidence_outbox_event("missing") is None
    row["context"]["decision_id"] = "mutated"
    assert scoped.get_evidence_outbox_event("open-a")["context"]["decision_id"] == "decision-a"


def test_remote_capability_is_discovered_by_recovery_without_order_path_network_calls():
    from data.ledger import SupabaseLedger
    calls = []
    snapshot = {"trades": [], "positions": [], "executions": [], "outbox": [],
                "unscoped_executions": [], "source_complete": True,
                "consistent_snapshot": True, "outbox_supported": True}
    class Client:
        def rpc(self, name, arguments):
            calls.append((name, arguments))
            data = ({"schema_version": 1, "atomic_outbox": True, "consistent_snapshot": True}
                    if name == "paper_evidence_capabilities" else snapshot)
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
    remote = SupabaseLedger.__new__(SupabaseLedger)
    remote._db = Client()
    assert remote.supports_evidence_outbox is False
    assert calls == []
    result = remote.get_authoritative_evidence_snapshot("one", "new")
    assert remote.supports_evidence_outbox is True
    assert result["consistent_snapshot"] is True and result["source_complete"] is True
    assert calls == [("paper_evidence_capabilities", {}), ("paper_evidence_snapshot", {
        "p_instance_id": "one", "p_simulation_session_id": "new"})]
    assert len(result["source_watermark"]) == 64


def test_legacy_remote_snapshot_cannot_certify_completeness_and_does_not_truncate():
    from data.ledger import SupabaseLedger
    remote = SupabaseLedger.__new__(SupabaseLedger)
    remote._evidence_capability = {}
    remote._db = SimpleNamespace(rpc=lambda name, arguments: SimpleNamespace(
        execute=lambda: SimpleNamespace(data={})))
    ranges = []
    class Query:
        def __init__(self, table):
            self.table = table
        def select(self, columns):
            return self
        def eq(self, key, value):
            assert (key, value) in (("instance_id", "one"), ("simulation_session_id", "new"))
            return self
        def order(self, key):
            assert key == "id"
            return self
        def range(self, start, end):
            self.start, self.end = start, end
            ranges.append((self.table, start, end))
            return self
        def execute(self):
            rows = [{"id": str(i)} for i in range(1001)]
            return SimpleNamespace(data=rows[self.start:self.end + 1])
    remote._t = lambda name: Query(name)
    remote.get_execution_receipts = lambda instance: []
    result = remote.get_authoritative_evidence_snapshot("one", "new")
    assert len(result["trades"]) == len(result["positions"]) == 1001
    assert result["source_complete"] is False
    assert result["consistent_snapshot"] is False
    assert result["outbox_supported"] is False
    assert ranges == [("paper_trades", 0, 999), ("paper_trades", 1000, 1999),
                      ("positions", 0, 999), ("positions", 1000, 1999)]


def test_remote_missing_migration_is_legacy_but_permission_failure_is_not():
    from data.ledger import SupabaseLedger
    class Failure(Exception):
        def __init__(self, code):
            self.code = code
    def remote(code):
        result = SupabaseLedger.__new__(SupabaseLedger)
        def execute():
            raise Failure(code)
        result._db = SimpleNamespace(rpc=lambda name, arguments: SimpleNamespace(execute=execute))
        return result
    assert remote("PGRST202")._discover_evidence_capability() == {}
    with pytest.raises(Failure):
        remote("42501")._discover_evidence_capability()


def test_remote_malformed_or_unproven_snapshot_is_rejected():
    from data.ledger import SupabaseLedger
    remote = SupabaseLedger.__new__(SupabaseLedger)
    remote._evidence_capability = {"atomic_outbox": True, "consistent_snapshot": True}
    remote._db = SimpleNamespace(rpc=lambda name, arguments: SimpleNamespace(
        execute=lambda: SimpleNamespace(data=remote._evidence_capability
            if name == "paper_evidence_capabilities" else {"trades": []})))
    with pytest.raises(ValueError, match="invalid authoritative"):
        remote.get_authoritative_evidence_snapshot()


def test_reconciliation_refreshes_remote_capability_after_rpc_schema_changes():
    from data.ledger import SupabaseLedger
    remote = SupabaseLedger.__new__(SupabaseLedger)
    remote._evidence_capability = {"atomic_outbox": True, "consistent_snapshot": True}
    remote._db = SimpleNamespace(rpc=lambda name, arguments: SimpleNamespace(
        execute=lambda: SimpleNamespace(data={"atomic_outbox": False, "consistent_snapshot": True})))
    remote._read_evidence_rows = lambda *args, **kwargs: []
    remote.get_execution_receipts = lambda instance: []
    assert remote.supports_evidence_outbox is True
    result = remote.get_authoritative_evidence_snapshot()
    assert remote.supports_evidence_outbox is False
    assert result["source_complete"] is False and result["outbox_supported"] is False


@pytest.fixture(scope="module")
def isolated_postgres():
    """Opt-in local server: no published port, no network, no production URL.

    Run NEXUS_PG_OUTBOX_TEST=1 ../.venv/bin/python -m pytest -q
    tests/test_paper_evidence_outbox.py. Docker must have postgres:16-alpine.
    """
    if os.environ.get("NEXUS_PG_OUTBOX_TEST") != "1":
        pytest.skip("isolated PostgreSQL migration validation requires NEXUS_PG_OUTBOX_TEST=1")
    name = "nexus-evidence-validation-" + uuid.uuid4().hex[:12]
    subprocess.run(["docker", "run", "-d", "--rm", "--network", "none", "--name", name,
                    "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "postgres:16-alpine"],
                   check=True, capture_output=True, text=True)
    try:
        for _ in range(100):
            # The image briefly starts a socket-only server for initdb. TCP
            # readiness proves that initialization ended and the final server
            # is available; otherwise shutdown can race the first psql call.
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                                   capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(.1)
        else:
            pytest.fail("isolated PostgreSQL did not become ready")
        subprocess.run(["docker", "exec", name, "psql", "-U", "postgres", "-q", "-c",
                        "CREATE ROLE service_role; CREATE ROLE anon; CREATE ROLE authenticated;"],
                       check=True, capture_output=True, text=True)
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)


@pytest.fixture
def postgres_outbox(isolated_postgres):
    database = "validation_" + uuid.uuid4().hex[:12]
    subprocess.run(["docker", "exec", isolated_postgres, "createdb", "-U", "postgres", database],
                   check=True, capture_output=True)
    def sql(statement, *, check=True):
        return subprocess.run(["docker", "exec", "-i", isolated_postgres, "psql", "-X", "-qAt",
                               "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database],
                              input=statement, text=True, capture_output=True, check=check)
    def value(statement):
        return json.loads(sql(statement).stdout.strip().splitlines()[-1])
    hub = Path(__file__).resolve().parents[1]
    base_schema = (hub / "data/ledger_schema.sql").read_text().split(
        "-- Permanent Trading Memory")[0]
    rpc_schema = (hub / "data/trading_instances_schema.sql").read_text().split(
        "CREATE TABLE IF NOT EXISTS trading_instances")[0]
    migration = (hub.parent / "supabase/migrations/0005_paper_evidence_outbox.sql").read_text()
    sql(base_schema + "\n" + rpc_schema)
    # A preexisting historical row must survive untouched and remain unknown.
    sql("INSERT INTO paper_trades(id,symbol,side,size,entry,status,opened_at) "
        "VALUES ('legacy','XRPUSDT','long',1,1,'open','2025-01-01T00:00:00Z');")
    original = value("SELECT to_jsonb(t) FROM paper_trades t WHERE id='legacy';")
    original_bodies = {action: sql(
        "SELECT pg_get_functiondef('public.paper_" + action + "_atomic(jsonb)'::regprocedure);").stdout
        .split("AS $function$")[1] for action in ("open", "reduce", "close")}
    sql(migration)
    yield SimpleNamespace(sql=sql, value=value, migration=migration,
                          original=original, original_bodies=original_bodies)


def pg_open(db, *, key="open", trade="trade", position="position", session="session", evidence=True):
    row = {"symbol": "XRPUSDT", "side": "long", "size": 2, "entry": 1, "stop": .9,
           "target": 1.2, "instance_id": "one", "simulation_session_id": session}
    payload = {"execution_id": key, "position": {**row, "id": position},
               "trade": {**row, "id": trade, "alert_id": key, "risk_amount_at_entry": .2}}
    if evidence:
        payload["trade"]["_evidence"] = {"context": {"decision_id": "decision"},
            "observed_at": "2026-10-01T00:00:00Z", "receipt": {"funding": None}}
    return "SELECT public.paper_open_atomic('" + json.dumps(payload) + "'::JSONB);"


def pg_reduce():
    row = {"symbol": "XRPUSDT", "side": "long", "size": 1, "entry": 1, "stop": .9,
           "target": 1.2, "instance_id": "one", "simulation_session_id": "session"}
    payload = {"execution_id": "reduce", "position": {"id": "position"}, "trade_id": "trade",
        "remainder_position": {**row, "id": "remainder-position"},
        "remainder_trade": {**row, "id": "remainder-trade", "risk_amount_at_entry": .2},
        "exit_price": 1.1, "pnl": .098, "rr": .5, "closed_size": 1, "fees": .002,
        "equity_after_close": 10000.098, "instance_id": "one",
        "evidence": {"observed_at": "2026-10-01T01:00:00Z", "receipt": {"funding": None}}}
    return "SELECT public.paper_reduce_atomic('" + json.dumps(payload) + "'::JSONB);"


def pg_close(*, remainder=False):
    payload = {"execution_id": "close", "position_id": "remainder-position" if remainder else "position",
        "trade_id": "remainder-trade" if remainder else "trade", "exit_price": 1.2,
        "pnl": .196, "rr": 1, "fees": .004, "equity_after_close": 10000.294,
        "instance_id": "one", "evidence": {"observed_at": "2026-10-01T02:00:00Z"}}
    return "SELECT public.paper_close_atomic('" + json.dumps(payload) + "'::JSONB);"


def test_postgres_migration_preserves_history_and_original_financial_bodies(postgres_outbox):
    db = postgres_outbox
    assert db.value("SELECT to_jsonb(t) FROM paper_trades t WHERE id='legacy';") == db.original
    assert db.value("SELECT COUNT(*) FROM paper_evidence_outbox;") == 0
    for action in ("open", "reduce", "close"):
        body = db.sql("SELECT pg_get_functiondef('public.paper_" + action +
                      "_accounting_v1(jsonb)'::regprocedure);").stdout.split("AS $function$")[1]
        assert body == db.original_bodies[action]
    db.sql(db.migration)
    assert db.value("SELECT to_jsonb(t) FROM paper_trades t WHERE id='legacy';") == db.original
    assert db.value("SELECT public.paper_evidence_capabilities();")["atomic_outbox"] is True


def test_postgres_exact_episode_links_financial_totals_and_snapshot_scope(postgres_outbox):
    db = postgres_outbox
    db.sql(pg_open(db) + pg_reduce() + pg_close(remainder=True))
    db.sql(pg_open(db, key="other", trade="other-trade", position="other-position", session="other"))
    snapshot = db.value("SELECT public.paper_evidence_snapshot('one','session');")
    assert snapshot["source_complete"] is True and snapshot["consistent_snapshot"] is True
    assert [row["action"] for row in snapshot["outbox"]] == ["OPEN", "REDUCE", "CLOSE"]
    opened, reduced, closed = snapshot["outbox"]
    assert opened["context_json"] == {"decision_id": "decision"}
    assert reduced["parent_trade_id"] == "trade" and reduced["parent_position_id"] == "position"
    assert reduced["remainder_trade_id"] == "remainder-trade"
    assert reduced["remainder_position_id"] == "remainder-position"
    assert closed["trade_id"] == "remainder-trade"
    from decimal import Decimal
    assert sum(Decimal(str(row["receipt_json"].get("net_pnl", 0))) for row in snapshot["outbox"]) == Decimal(".294")
    assert sum(Decimal(str(row["receipt_json"].get("booked_fees", 0))) for row in snapshot["outbox"]) == Decimal(".006")
    assert sum(Decimal(str(row["pnl"])) for row in snapshot["trades"]) == Decimal(".294")
    assert sum(Decimal(str(row["fees"])) for row in snapshot["trades"]) == Decimal(".006")


@pytest.mark.parametrize("action", ["open", "reduce", "close"])
def test_postgres_outbox_failure_rolls_back_accounting_rpc(postgres_outbox, action):
    db = postgres_outbox
    db.sql(pg_open(db))
    before = db.value("SELECT public.paper_evidence_snapshot('one','session');")
    db.sql("CREATE FUNCTION injected_failure() RETURNS TRIGGER LANGUAGE plpgsql AS $$ "
           "BEGIN RAISE EXCEPTION 'injected outbox failure'; END $$; "
           "CREATE TRIGGER fail_outbox BEFORE INSERT ON paper_evidence_outbox "
           "FOR EACH ROW EXECUTE FUNCTION injected_failure();")
    statement = {"open": pg_open(db, key="other", trade="other", position="other"),
                 "reduce": pg_reduce(), "close": pg_close()}[action]
    result = db.sql(statement, check=False)
    assert result.returncode != 0 and "injected outbox failure" in result.stderr
    assert db.value("SELECT public.paper_evidence_snapshot('one','session');") == before


def test_postgres_immutable_permissions_and_duplicate_execution(postgres_outbox):
    db = postgres_outbox
    db.sql("SET ROLE service_role; " + pg_open(db))
    before = db.value("SELECT public.paper_evidence_snapshot('one','session');")
    for statement in ("UPDATE paper_evidence_outbox SET context_json='{}';",
                      "DELETE FROM paper_evidence_outbox;",
                      "SET ROLE anon; SELECT public.paper_evidence_snapshot('one','session');",
                      "SET ROLE service_role; SELECT public.paper_open_accounting_v1('{}');",
                      pg_open(db, key="open", trade="duplicate", position="duplicate")):
        assert db.sql(statement, check=False).returncode != 0
    assert db.value("SELECT public.paper_evidence_snapshot('one','session');") == before


def test_postgres_snapshot_is_not_subject_to_ordinary_table_row_cap(postgres_outbox):
    db = postgres_outbox
    db.sql("INSERT INTO paper_trades(id,symbol,side,size,entry,status,opened_at,instance_id,simulation_session_id) "
        "SELECT 'historical-'||g,'XRPUSDT','long',1,1,'open','2025-01-01','one','session' "
        "FROM generate_series(1,1001) g;")
    snapshot = db.value("SELECT public.paper_evidence_snapshot('one','session');")
    assert len(snapshot["trades"]) == 1001
    assert snapshot["outbox"] == []  # historical metadata is never fabricated


def test_postgres_base_rpc_overwrite_is_detected_and_forward_recovery_restores_wrapper(postgres_outbox):
    db = postgres_outbox
    hub = Path(__file__).resolve().parents[1]
    base = (hub / "data/trading_instances_schema.sql").read_text().split(
        "CREATE TABLE IF NOT EXISTS trading_instances")[0]
    db.sql(base)
    assert db.value("SELECT public.paper_evidence_capabilities();")["atomic_outbox"] is False
    db.sql(db.migration)
    assert db.value("SELECT public.paper_evidence_capabilities();")["atomic_outbox"] is True
    db.sql(pg_open(db))
    assert db.value("SELECT COUNT(*) FROM paper_evidence_outbox;") == 1


@pytest.fixture(scope="module")
def isolated_postgrest():
    """Actual supabase-py RPC transport through an isolated local gateway.

    Only a temporary generated token is used. PostgreSQL has no published
    port; the HTTP gateway binds a random loopback-only port. Their Docker
    network is dedicated and is deleted with both containers after validation.
    """
    if os.environ.get("NEXUS_PG_OUTBOX_TEST") != "1":
        pytest.skip("isolated PostgREST validation requires NEXUS_PG_OUTBOX_TEST=1")
    import base64
    import hashlib
    import hmac
    import secrets
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    label = "nexus-gateway-validation-" + uuid.uuid4().hex[:12]
    network, postgres, gateway = label + "-network", label + "-pg", label + "-http"
    token_secret = secrets.token_hex(32)
    proxy, proxy_thread = None, None
    def command(arguments, **kwargs):
        return subprocess.run(["docker", *arguments], check=True, capture_output=True,
                              text=True, **kwargs)
    command(["network", "create", network])
    try:
        command(["run", "-d", "--rm", "--network", network, "--network-alias", "postgres",
                 "--name", postgres, "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "postgres:16-alpine"])
        for _ in range(100):
            if subprocess.run(["docker", "exec", postgres, "pg_isready", "-h", "127.0.0.1",
                               "-U", "postgres"], capture_output=True).returncode == 0:
                break
            time.sleep(.1)
        else:
            pytest.fail("isolated PostgreSQL gateway backend did not become ready")
        def sql(statement):
            return command(["exec", "-i", postgres, "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                            "-U", "postgres"], input=statement)
        hub = Path(__file__).resolve().parents[1]
        base = (hub / "data/ledger_schema.sql").read_text().split("-- Permanent Trading Memory")[0]
        rpc = (hub / "data/trading_instances_schema.sql").read_text().split(
            "CREATE TABLE IF NOT EXISTS trading_instances")[0]
        migration = (hub.parent / "supabase/migrations/0005_paper_evidence_outbox.sql").read_text()
        sql("CREATE ROLE service_role NOLOGIN BYPASSRLS; CREATE ROLE anon; CREATE ROLE authenticated;" +
            base + rpc + migration +
            "GRANT USAGE ON SCHEMA public TO service_role; "
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO service_role;")
        command(["run", "-d", "--rm", "--network", network, "--name", gateway,
                 "-p", "127.0.0.1::3000", "-e", "PGRST_DB_URI=postgres://postgres@postgres:5432/postgres",
                 "-e", "PGRST_DB_SCHEMAS=public", "-e", "PGRST_DB_ANON_ROLE=anon",
                 "-e", "PGRST_DB_MAX_ROWS=1000", "-e", "PGRST_JWT_SECRET=" + token_secret,
                 "postgrest/postgrest:v12.2.12"])
        try:
            address = command(["port", gateway, "3000/tcp"]).stdout.strip()
        except subprocess.CalledProcessError:
            details = command(["logs", gateway]).stderr
            raise RuntimeError("isolated PostgREST startup failed: " + details) from None
        assert address.startswith("127.0.0.1:")
        url = "http://" + address
        def encoded(value):
            return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
        head = encoded({"alg": "HS256", "typ": "JWT"})
        body = encoded({"role": "service_role", "iss": "supabase", "iat": int(time.time()),
                        "exp": int(time.time()) + 900})
        message = head + "." + body
        signature = base64.urlsafe_b64encode(hmac.new(token_secret.encode(), message.encode(),
                                                     hashlib.sha256).digest()).decode().rstrip("=")
        token = message + "." + signature
        for _ in range(100):
            try:
                request = Request(url + "/rpc/paper_evidence_capabilities", data=b"{}",
                                  headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                with urlopen(request, timeout=1) as response:
                    assert json.load(response)["atomic_outbox"] is True
                break
            except OSError:
                time.sleep(.1)
        else:
            pytest.fail("isolated PostgREST gateway did not become ready")
        # Supabase's SDK uses /rest/v1; the local standalone PostgREST image
        # serves /. This local proxy performs only the gateway path mapping.
        class GatewayPath(BaseHTTPRequestHandler):
            def forward(self):
                if not self.path.startswith("/rest/v1/"):
                    self.send_error(404)
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                headers = {key: value for key, value in self.headers.items()
                           if key.lower() not in {"host", "content-length"}}
                request = Request(url + self.path[len("/rest/v1"):],
                                  data=body if self.command != "GET" else None,
                                  headers=headers, method=self.command)
                try:
                    response = urlopen(request, timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    result = response.read()
                    self.send_response(response.status)
                    for key, value in response.headers.items():
                        if key.lower() not in {"content-length", "transfer-encoding", "connection"}:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(result)))
                    self.end_headers()
                    self.wfile.write(result)
            do_GET = forward
            do_POST = forward
            do_PATCH = forward
            do_DELETE = forward
            def log_message(self, *args):
                pass
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), GatewayPath)
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        proxy_thread.start()
        yield SimpleNamespace(url="http://127.0.0.1:" + str(proxy.server_port), token=token,
                              sql=sql, rpc_schema=rpc, migration=migration)
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        if proxy_thread is not None:
            proxy_thread.join(timeout=2)
        for name in (gateway, postgres):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        command(["network", "rm", network])


def test_actual_supabase_rpc_transport_preserves_ids_costs_and_legacy_pagination(isolated_postgrest):
    from data.ledger import SupabaseLedger
    from execution.paper_engine import PaperExecutionEngine
    from decimal import Decimal
    db = isolated_postgrest
    remote = SupabaseLedger(db.url, db.token)
    scoped = InstanceLedger(remote, "one", "session")
    empty = scoped.get_authoritative_evidence_snapshot()
    assert empty["source_complete"] is True
    assert scoped.supports_evidence_outbox is True
    paper = PaperExecutionEngine(scoped, 10000)
    opened = paper.open(symbol="XRPUSDT", side="BUY", size=2, entry=1, stop=.9, target=1.2,
        alert_id="actual-open", sizing_context={"evidence_context": {"decision_identity": "original"}})
    reduced = paper.reduce(symbol="XRPUSDT", exit_price=1.1, fraction=.5, execution_id="actual-reduce")
    closed = paper.close(symbol="XRPUSDT", exit_price=1.2, execution_id="actual-close")
    snapshot = scoped.get_authoritative_evidence_snapshot()
    assert snapshot["source_complete"] is True and snapshot["consistent_snapshot"] is True
    assert [row["action"] for row in snapshot["executions"]] == ["OPEN", "REDUCE", "CLOSE"]
    root, partial, final = snapshot["outbox"]
    assert root["trade_id"] == opened.trade_id and root["position_id"] == opened.position_id
    assert root["context"] == {"decision_identity": "original"}
    assert partial["parent_trade_id"] == opened.trade_id
    assert partial["parent_position_id"] == opened.position_id
    assert partial["remainder_trade_id"] == reduced.remainder_trade_id
    assert partial["remainder_position_id"] == reduced.remainder_position_id
    assert final["trade_id"] == reduced.remainder_trade_id
    assert final["position_id"] == reduced.remainder_position_id
    # Authority may use an older REAL schema; compare its own persisted totals,
    # then require the producer's double-precision values to agree within that
    # declared storage precision instead of pretending unknown bits persisted.
    authoritative_net = sum(Decimal(str(row["pnl"])) for row in snapshot["trades"])
    authoritative_fees = sum(Decimal(str(row["fees"])) for row in snapshot["trades"])
    outbox_net = sum(Decimal(str(row["receipt"].get("net_pnl", 0))) for row in snapshot["outbox"])
    outbox_fees = sum(Decimal(str(row["receipt"].get("booked_fees", 0))) for row in snapshot["outbox"])
    assert outbox_net == authoritative_net and outbox_fees == authoritative_fees
    assert abs(authoritative_net - Decimal(str(reduced.pnl)) - Decimal(str(closed.pnl))) < Decimal("1e-7")
    assert len(scoped.get_evidence_outbox()) == 3
    before = scoped.get_authoritative_evidence_snapshot()
    with pytest.raises(Exception):
        paper.open(symbol="XRPUSDT", side="BUY", size=1, entry=1, stop=.9, alert_id="actual-open")
    assert scoped.get_authoritative_evidence_snapshot() == before
    # Simulate legacy RPC schema overwrite and more than one HTTP page. The
    # fallback must return every readable row and retain UNKNOWN completeness.
    db.sql(db.rpc_schema +
        "INSERT INTO paper_trades(id,symbol,side,size,entry,status,opened_at,instance_id,simulation_session_id) "
        "SELECT 'legacy-'||g,'XRPUSDT','long',1,1,'cancelled','2025-01-01','one','session' "
        "FROM generate_series(1,1001) g;")
    fallback = scoped.get_authoritative_evidence_snapshot()
    assert len(fallback["trades"]) == 1003
    assert fallback["source_complete"] is False and fallback["consistent_snapshot"] is False
    assert scoped.supports_evidence_outbox is False
    db.sql(db.migration)
    restored = scoped.get_authoritative_evidence_snapshot()
    assert len(restored["trades"]) == 1003
    assert scoped.supports_evidence_outbox is True


def test_actual_gateway_real_precision_live_journal_uses_booked_authority(isolated_postgrest, tmp_path):
    from data.ledger import SupabaseLedger
    from datetime import datetime, timezone
    from decimal import Decimal
    from execution.paper_engine import PaperExecutionEngine
    from services.fill_model import RealisticFill
    from services.strategy_identity import observed_strategy_identity
    from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy
    from test_strategy_evidence_runtime import runtime
    _, store, decisions, _, pipeline, capture = runtime(tmp_path)
    remote = SupabaseLedger(isolated_postgrest.url, isolated_postgrest.token)
    scoped = InstanceLedger(remote, "precision", "precision-session")
    scoped.get_authoritative_evidence_snapshot()
    identity = observed_strategy_identity(AdaptiveTrendPullbackStrategy("XRPUSDT"),
        strategy_id="adaptive_trend_pullback", timeframe="5m")
    provenance = {**pipeline.journal_context, "instance_id": "precision",
                  "simulation_session_id": "precision-session", "account_id": "precision-account"}
    timestamp = datetime.now(timezone.utc).isoformat()
    decision_id = decisions.record({**provenance, "symbol": "XRPUSDT", "side": "long",
        "strategy": "Adaptive MTF", "decision": "accepted", "ts": timestamp,
        "decision_identity": "precision-signal"})
    context = capture.order_context({"alert_id": "precision-open", "symbol": "XRPUSDT", "side": "BUY",
        "strategy": "Adaptive MTF", "timeframe": "5m", "timestamp": timestamp,
        "decision_observed_at": decisions.get(decision_id)["decided_at"], "order_observed_at": timestamp,
        "decision_identity": "precision-signal", "journal_decision_id": decision_id,
        "journal_execution": provenance, "strategy_identity": identity}, [], 10000)
    capture.persist_order_context(context)
    paper = PaperExecutionEngine(scoped, 10000, fill_model=RealisticFill(
        spread_pct=.0007, slippage_pct=.00031, latency_pct=.00011, taker_fee_pct=.00037))
    paper.strategy_id = "adaptive_trend_pullback"
    observed = []
    def observe(fill):
        observed.append(fill)
        capture.observe_fill(fill)
    paper.evidence_listener = observe
    paper.evidence_prepare_listener = capture.prepare_exit
    opened = paper.open(symbol="XRPUSDT", side="BUY", size=3.456789, entry=1.23456789,
        stop=1.19999991, target=1.45678901, alert_id="precision-open",
        sizing_context={"evidence_context": context})
    reduced = paper.reduce(symbol="XRPUSDT", exit_price=1.36789012, fraction=.271828,
        execution_id="precision-reduce")
    closed = paper.close(symbol="XRPUSDT", exit_price=1.45678901, execution_id="precision-close")
    before = scoped.get_authoritative_evidence_snapshot()
    assert [fill.action for fill in observed] == ["opened", "reduced", "closed"]
    # The public returned fills preserve producer precision. Only the copied
    # observer matches persisted REAL values; neither primary rows nor existing
    # financial arithmetic are rewritten to make the evidence reconcile.
    assert observed[0] is not opened and observed[1] is not reduced and observed[2] is not closed
    assert any(Decimal(str(fill.pnl)) != Decimal(str(copied.pnl))
               for fill, copied in ((reduced, observed[1]), (closed, observed[2])))
    for original, copied in ((opened, observed[0]), (reduced, observed[1]), (closed, observed[2])):
        assert copied.receipt["producer_receipt"] == original.receipt
        row = scoped.get_evidence_outbox_event(original.execution_id)
        assert copied.receipt["booked_fees"] == row["receipt"]["booked_fees"]
        assert copied.price == row["receipt"]["price"] and copied.size == row["receipt"]["size"]
    report = capture.reconcile_report(scoped)
    assert report["status"] == "PARTIAL", report
    assert report["counts"]["conflicting_events"] == 0, report
    assert report["counts"]["missing_events"] == 0, report
    assert Decimal(report["financial_totals"]["net_pnl_delta"]) == 0
    assert Decimal(report["financial_totals"]["fees_delta"]) == 0
    assert report["counts"]["missing_cost_components"] > 0  # funding is not modeled
    episodes = store.completed_evidence_episodes(instance_id="precision")
    assert len(episodes) == 1 and episodes[0]["strategy_config_hash"] == identity["strategy_config_hash"]
    events = store.evidence_events()
    capture.reconcile_report(scoped)
    assert store.evidence_events() == events
    assert scoped.get_authoritative_evidence_snapshot() == before
