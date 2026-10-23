"""Real PostgreSQL 17 backup/restore checks; never connects to an external provider."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql

SCRIPT = Path(__file__).resolve().parents[1] / "ops/backup-supabase-ledger-to-mac.py"
spec = importlib.util.spec_from_file_location("ledger_backup", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
PG_BIN = Path(os.environ.get("NEXUS_TEST_PG17_BIN", "/Applications/Postgres.app/Contents/Versions/17/bin"))


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not (PG_BIN / "initdb").is_file():
        pytest.skip("Native PostgreSQL 17 binaries not available")
    directory = tmp_path_factory.mktemp("ledger-source")
    socket = Path(tempfile.mkdtemp(prefix="nxsrc-", dir="/tmp"))
    cluster = directory / "cluster"
    helper.check_tools(PG_BIN)
    helper.command([PG_BIN / "initdb", "-D", cluster, "-U", "postgres", "--locale=C", "-E", "UTF8",
                    "--auth-local=trust", "--auth-host=reject"], directory=directory, name="init")
    options = "-c listen_addresses='' -c unix_socket_directories='" + str(socket) + "'"
    helper.command([PG_BIN / "pg_ctl", "-D", cluster, "-l", directory / "postgres.log", "-w", "-t", "30",
                    "-o", options, "start"], directory=directory, name="start")
    config = {"host": str(socket), "port": 5432, "dbname": "postgres", "user": "postgres", "password": "fixture-only"}
    try:
        with psycopg.connect(**config, autocommit=True) as c:
            c.execute("CREATE ROLE authenticated NOLOGIN")
        yield config
    finally:
        helper.command([PG_BIN / "pg_ctl", "-D", cluster, "-m", "fast", "-w", "stop"], directory=directory, name="stop")
        socket.rmdir()


@pytest.fixture
def source(server):
    name = "source_" + uuid.uuid4().hex
    with psycopg.connect(**server, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    config = dict(server, dbname=name)
    with psycopg.connect(**config, autocommit=True) as c:
        c.execute("CREATE SCHEMA auth; CREATE SCHEMA extensions")
        c.execute("CREATE EXTENSION pgcrypto SCHEMA extensions")
        c.execute("CREATE TYPE public.trade_side AS ENUM ('BUY','SELL')")
        c.execute("CREATE TABLE auth.users(id uuid PRIMARY KEY, email text UNIQUE)")
        c.execute("INSERT INTO auth.users VALUES('00000000-0000-0000-0000-000000000001','fixture@example.invalid')")
        c.execute("CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT '00000000-0000-0000-0000-000000000001'::uuid $$")
        for table in helper.CORE_TABLES:
            c.execute(sql.SQL("CREATE TABLE public.{} (id text PRIMARY KEY, amount numeric(18,8), payload jsonb, ts timestamptz)").format(sql.Identifier(table)))
            c.execute(sql.SQL("INSERT INTO public.{} VALUES ('row-1',123.45678901,%s,'2026-10-23 13:00:00+00')").format(sql.Identifier(table)),
                      ('{"text":"Unicode नेपाल\\nline\\twith tabs","nested":[1,true,null]}',))
        c.execute("ALTER TABLE public.positions ADD COLUMN owner uuid REFERENCES auth.users(id)")
        c.execute("UPDATE public.positions SET owner='00000000-0000-0000-0000-000000000001'")
        c.execute("ALTER TABLE public.positions ENABLE ROW LEVEL SECURITY")
        c.execute("CREATE POLICY owned_positions ON public.positions FOR SELECT TO authenticated USING (owner=auth.uid())")
        c.execute("GRANT USAGE ON SCHEMA auth TO authenticated")
        c.execute("GRANT SELECT ON public.positions TO authenticated")
        c.execute("CREATE INDEX position_amount ON public.positions(amount)")
        c.execute("CREATE FUNCTION public.keep_record() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
        c.execute("CREATE TRIGGER keep_record BEFORE UPDATE ON public.positions FOR EACH ROW EXECUTE FUNCTION public.keep_record()")
        c.execute("CREATE TABLE public.serial_history(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, side public.trade_side)")
        c.execute("INSERT INTO public.serial_history(side) VALUES ('BUY')")
    yield config
    # Private test fixture data only; originals operated by the helper are never dropped.
    with psycopg.connect(**server, autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def run_recovery(source, tmp_path, **kwargs):
    path = tmp_path / "recovery"
    path.mkdir(mode=0o700)
    try:
        return helper.recover(source, bin_dir=PG_BIN, directory=path, require_tls=False, **kwargs), path
    except helper.Blocked as error:
        if str(error) == 'RESTORED_SCHEMA_DEFINITION_MISMATCH':
            before=json.loads((path/'source-schema.json').read_text())
            after=json.loads((path/'restored-schema.json').read_text())
            # Only synthetic test definitions. The operator helper never prints these.
            print(json.dumps({k:{'before':before[k],'after':after[k]} for k in before if before[k]!=after[k]},indent=2))
        raise


def test_real_snapshot_restores_every_row_and_definition_during_concurrent_write(source, tmp_path):
    def concurrent_external_write(exporter):
        exporter.execute("SAVEPOINT write_probe")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            exporter.execute("INSERT INTO public.positions(id) VALUES('forbidden')")
        exporter.execute("ROLLBACK TO SAVEPOINT write_probe")
        with psycopg.connect(**source, autocommit=True) as c:
            c.execute("INSERT INTO public.positions(id,amount) VALUES('late-arrival',9.5)")
    report, directory = run_recovery(source, tmp_path, snapshot_hook=concurrent_external_write)
    assert report["tables_verified"] == 7
    assert report["rows_verified"] == 7
    assert report["all_selected_table_rows_match_same_exported_snapshot"] is True
    assert report["schema_columns_constraints_indexes_triggers_functions_policies_match"] is True
    assert report["object_ownership_and_grants_match"] is True
    assert report["original_role_login_attributes_and_memberships_recovered"] is False
    assert not (directory / ".pgpass").exists()
    assert not (directory / "detached-postgres-17/postmaster.pid").exists()
    with psycopg.connect(**source) as c:
        assert c.execute("SELECT count(*) FROM public.positions").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM public.positions WHERE id='forbidden'").fetchone()[0] == 0


def test_tls_cannot_be_silently_downgraded(source, tmp_path):
    directory = tmp_path / "recovery"
    directory.mkdir()
    with pytest.raises(helper.Blocked, match="VERIFIED_TLS_REQUIRED"):
        helper.recover(source, bin_dir=PG_BIN, directory=directory)
    assert not (directory / ".pgpass").exists()
    assert not (directory / "application-ledger.dump").exists()


def test_uncovered_foreign_key_schema_stops_before_dump(source, tmp_path):
    with psycopg.connect(**source, autocommit=True) as c:
        c.execute("CREATE SCHEMA extra; CREATE TABLE extra.refs(id int PRIMARY KEY)")
        c.execute("ALTER TABLE public.positions ADD COLUMN extra_id int REFERENCES extra.refs(id)")
    with pytest.raises(helper.Blocked, match="ADDITIONAL_FOREIGN_KEY_SCHEMA_BACKUP_REQUIRED"):
        run_recovery(source, tmp_path)
    assert not (tmp_path / "recovery/application-ledger.dump").exists()


def test_wrong_ledger_cannot_be_accepted(source, tmp_path):
    with psycopg.connect(**source, autocommit=True) as c:
        c.execute("DROP TABLE public.alerts")
    with pytest.raises(helper.Blocked, match="EXPECTED_FINANCIAL_TABLES_MISSING"):
        run_recovery(source, tmp_path)


def test_fingerprint_mismatch_retains_archive_and_stops_detached_server(source, tmp_path, monkeypatch):
    original = helper.fingerprint
    calls = 0
    def corrupt_target(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 2:
            result[0]["sha256"] = "0" * 64
        return result
    monkeypatch.setattr(helper, "fingerprint", corrupt_target)
    with pytest.raises(helper.Blocked, match="RESTORED_FINANCIAL_ROW_FINGERPRINT_MISMATCH"):
        run_recovery(source, tmp_path)
    directory = tmp_path / "recovery"
    assert (directory / "application-ledger.dump").stat().st_size > 0
    assert not (directory / ".pgpass").exists()
    assert not (directory / "detached-postgres-17/postmaster.pid").exists()
    with psycopg.connect(**source) as c:
        assert c.execute("SELECT count(*) FROM public.positions").fetchone()[0] == 1


def test_insufficient_space_never_connects_or_copies(source, tmp_path, monkeypatch):
    monkeypatch.setattr(helper.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(100,90,10))
    with pytest.raises(helper.Blocked, match="MAC_RECOVERY_SPACE_REQUIRED"):
        run_recovery(source, tmp_path)
    assert not (tmp_path / "recovery/.pgpass").exists()


def test_password_escaping_does_not_change_special_characters():
    config = dict(host="example.invalid", port=5432, dbname="postgres", user="postgres.ref", password="a:b\\c'$")
    assert helper.passfile_line(config).endswith(":a\\:b\\\\c'$\n")
    config["password"] = "first\nsecond"
    with pytest.raises(helper.Blocked, match="INVALID_CREDENTIAL_CHARACTER"):
        helper.passfile_line(config)


def test_private_metadata_is_not_world_readable_or_overwritten(tmp_path):
    path = tmp_path / "metadata.json"
    helper.private_json(path, {"schema": "fixture"})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        helper.private_json(path, {"schema": "replaced"})
