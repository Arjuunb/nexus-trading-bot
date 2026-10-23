"""Only synthetic catalogs and disposable native PostgreSQL 17 fixtures."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("retained_ledger", ROOT / "ops/verify-retained-supabase-ledger.py")
retained = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retained)
spec = importlib.util.spec_from_file_location("retained_ledger_fixtures", ROOT / "tests/test_supabase_ledger_to_mac.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
server, source, tls_certificates = base.server, base.source, base.tls_certificates


def fixture_catalog():
    return {"columns": [
        dict(schema="auth", relation="accounts", name="id", ordinal=1, type="integer",
             not_null=True, identity="", generated="", default_expression=None),
        dict(schema="auth", relation="accounts", name="label", ordinal=3, type="text",
             not_null=False, identity="", generated="", default_expression="'fixture'::text"),
    ], "policies": [{"name": "owner", "cmd": "SELECT"}],
        "relation_permissions": [{"privilege_type": "SELECT", "is_grantable": False}]}


def test_physical_column_gaps_are_canonicalized_without_mutating_recorded_evidence():
    before = fixture_catalog()
    untouched = copy.deepcopy(before)
    after = copy.deepcopy(before)
    after["columns"][1]["ordinal"] = 2
    assert before != after
    assert retained.helper.logical_catalog(before) == retained.helper.logical_catalog(after)
    assert before == untouched


@pytest.mark.parametrize("field,value", [
    ("name", "changed"), ("type", "bigint"), ("not_null", False),
    ("identity", "a"), ("generated", "s"), ("default_expression", "42"),
])
def test_every_other_column_definition_still_has_to_match(field, value):
    before = fixture_catalog()
    after = copy.deepcopy(before)
    after["columns"][0][field] = value
    assert retained.helper.logical_catalog(before) != retained.helper.logical_catalog(after)


def test_visible_column_reordering_is_rejected():
    before = fixture_catalog()
    after = copy.deepcopy(before)
    after["columns"][0]["ordinal"] = 4
    assert retained.helper.logical_catalog(before) != retained.helper.logical_catalog(after)


@pytest.mark.parametrize("section", ["policies", "relation_permissions"])
def test_other_schema_sections_are_never_ignored(section):
    before = fixture_catalog()
    after = copy.deepcopy(before)
    after[section] = []
    assert retained.helper.logical_catalog(before) != retained.helper.logical_catalog(after)


@pytest.mark.parametrize("ordinal", [0, -1, True, "3", None])
def test_malformed_physical_ordinals_are_blocked(ordinal):
    catalog = fixture_catalog()
    catalog["columns"][1]["ordinal"] = ordinal
    with pytest.raises(retained.helper.Blocked, match="INVALID_RECORDED_COLUMN_METADATA"):
        retained.helper.logical_catalog(catalog)


def test_duplicate_column_positions_are_blocked():
    catalog = fixture_catalog()
    catalog["columns"][1]["ordinal"] = 1
    with pytest.raises(retained.helper.Blocked, match="DUPLICATE_RECORDED_COLUMN_METADATA"):
        retained.helper.logical_catalog(catalog)


def add_dropped_column_fixture(source):
    with base.psycopg.connect(**source, autocommit=True) as connection:
        connection.execute("ALTER TABLE auth.users ADD COLUMN obsolete text")
        connection.execute("ALTER TABLE auth.users ADD COLUMN provider text")
        connection.execute("ALTER TABLE auth.users DROP COLUMN obsolete")
        connection.execute("UPDATE auth.users SET provider='synthetic-provider'")


def test_real_dump_restore_preserves_visible_columns_and_rows_despite_dropped_slot(source, tmp_path):
    add_dropped_column_fixture(source)
    report, directory = base.run_recovery(source, tmp_path)
    before = json.loads((directory / "source-schema.json").read_text())
    after = json.loads((directory / "restored-schema.json").read_text())
    assert before != after
    assert retained.helper.logical_catalog(before) == retained.helper.logical_catalog(after)
    assert report["all_selected_table_rows_match_same_exported_snapshot"] is True
    assert report["object_ownership_and_grants_match"] is True
    with base.psycopg.connect(**source) as connection:
        assert connection.execute("SELECT attnum FROM pg_attribute WHERE attrelid='auth.users'::regclass AND attname='provider'").fetchone() == (4,)


@pytest.fixture
def failed_old_bundle(source, tmp_path, monkeypatch):
    add_dropped_column_fixture(source)
    directory = tmp_path / "external-ledger-synthetic"
    directory.mkdir(mode=0o700)
    with monkeypatch.context() as patch:
        patch.setattr(base.helper, "logical_catalog", lambda value: value)
        with pytest.raises(base.helper.Blocked, match="RESTORED_SCHEMA_DEFINITION_MISMATCH"):
            base.helper.recover(source, bin_dir=base.PG_BIN, directory=directory, require_tls=False)
    base.helper.private_json(directory / "blocked-receipt.json", {
        "status": "BLOCKED", "reason": "RESTORED_SCHEMA_DEFINITION_MISMATCH", "directory": str(directory)})
    return directory


def local_only_connections(monkeypatch):
    connect = base.psycopg.connect
    def local_connect(*args, **kwargs):
        assert not args and str(kwargs.get("host", "")).startswith("/tmp/nxverify-")
        return connect(**kwargs)
    monkeypatch.setattr(base.psycopg, "connect", local_connect)
    for name in ("connect_source", "verify_native_source", "read_database_password", "recover"):
        monkeypatch.setattr(retained.helper, name, lambda *a, **kw: pytest.fail("External access or password requested"))


def test_retained_archive_is_restored_again_without_a_source_connection_or_password(failed_old_bundle, tmp_path, monkeypatch):
    seals = {name: retained.helper.file_hash(failed_old_bundle / name) for name in retained.INPUTS}
    for key in list(retained.os.environ):
        if key.startswith("PG"):
            monkeypatch.delenv(key)
    with monkeypatch.context() as patch:
        local_only_connections(patch)
        report = retained.verify(failed_old_bundle, bin_dir=base.PG_BIN, destination=tmp_path)
    assert report["status"] == "RETAINED_EXTERNAL_LEDGER_BACKUP_AND_NEW_LOCAL_RESTORE_VERIFIED"
    assert report["source_connection_attempted"] is False
    assert report["all_selected_table_rows_match_recorded_snapshot"] is True
    assert report["local_restore_server_stopped"] is True
    assert report["new_local_restore_database"].startswith("nexus_ledger_reverify_")
    assert report["input_sha256"] == seals
    assert all(retained.helper.file_hash(failed_old_bundle / name) == value for name, value in seals.items())
    assert not (failed_old_bundle / "detached-postgres-17/postmaster.pid").exists()
    assert not (failed_old_bundle / ".nexus-local-reverification.lock").exists()


def test_offline_financial_mismatch_remains_blocked_and_stops_the_local_cluster(failed_old_bundle, tmp_path, monkeypatch):
    path = failed_old_bundle / "source-fingerprints.json"
    fingerprints = json.loads(path.read_text())
    fingerprints[0]["sha256"] = "0" * 64
    path.write_text(json.dumps(fingerprints))
    with monkeypatch.context() as patch:
        local_only_connections(patch)
        with pytest.raises(retained.helper.Blocked, match="RESTORED_FINANCIAL_ROW_FINGERPRINT_MISMATCH"):
            retained.verify(failed_old_bundle, bin_dir=base.PG_BIN, destination=tmp_path)
    assert not (failed_old_bundle / "detached-postgres-17/postmaster.pid").exists()
    assert not (failed_old_bundle / ".nexus-local-reverification.lock").exists()
    assert (failed_old_bundle / "application-ledger.dump").exists()


def test_additional_retained_schema_difference_blocks_before_starting_any_server(failed_old_bundle, tmp_path, monkeypatch):
    path = failed_old_bundle / "source-schema.json"
    recorded = json.loads(path.read_text())
    recorded["columns"][0]["type"] = "bigint"
    path.write_text(json.dumps(recorded))
    monkeypatch.setattr(retained.helper, "command", lambda *a, **kw: pytest.fail("Local server started"))
    with pytest.raises(retained.helper.Blocked, match="RETAINED_SCHEMA_HAS_ADDITIONAL_DIFFERENCES"):
        retained.verify(failed_old_bundle, bin_dir=base.PG_BIN, destination=tmp_path)


def test_running_retained_cluster_is_never_stopped_or_modified(failed_old_bundle, tmp_path, monkeypatch):
    marker = failed_old_bundle / "detached-postgres-17/postmaster.pid"
    marker.write_text("synthetic sentinel")
    monkeypatch.setattr(retained.helper, "command", lambda *a, **kw: pytest.fail("Server operation attempted"))
    with pytest.raises(retained.helper.Blocked, match="RETAINED_CLUSTER_MUST_BE_STOPPED"):
        retained.verify(failed_old_bundle, bin_dir=base.PG_BIN, destination=tmp_path)
    assert marker.read_text() == "synthetic sentinel"
    marker.unlink()  # Synthetic sentinel only; this never represented a running server.
