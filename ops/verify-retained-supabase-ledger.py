#!/usr/bin/env python3
"""Reverify a retained local restore without source access or a password.

Only the helper's private, stopped detached PostgreSQL 17 cluster is started.
The retained dump is restored again into a new randomly named local database;
original archive, metadata, fingerprints and restored database are preserved.
No external database, financial API, VPS or source credential is used.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

HELPER_SHA256 = "172d0deacc84432cda85436bc882c00718157e1b392eb087722cd4cb098f3fdb"
path = Path(__file__).with_name("backup-supabase-ledger-to-mac.py")
if hashlib.sha256(path.read_bytes()).hexdigest() != HELPER_SHA256:
    raise SystemExit("BLOCKED:PINNED_BACKUP_HELPER_REQUIRED")
spec = importlib.util.spec_from_file_location("retained_ledger_helper", path)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

INPUTS = ("application-ledger.dump", "source-schema.json", "source-fingerprints.json",
          "restored-schema.json", "blocked-receipt.json")


def private_owned(path, *, directory=False):
    info = path.lstat()
    helper.require(not stat.S_ISLNK(info.st_mode) and info.st_uid == os.getuid()
                   and info.st_mode & 0o077 == 0,
                   "PRIVATE_OWNER_CONTROLLED_RESTORE_BUNDLE_REQUIRED")
    helper.require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
                   "UNEXPECTED_RESTORE_BUNDLE_OBJECT")


def verify(bundle, *, bin_dir, destination):
    import psycopg
    from psycopg import sql

    helper.require(not bundle.is_symlink(), "RESTORE_BUNDLE_SYMLINK_NOT_ALLOWED")
    bundle = bundle.resolve()
    helper.require(bundle.name.startswith("external-ledger-"), "RETAINED_HELPER_BUNDLE_REQUIRED")
    private_owned(bundle, directory=True)
    for name in INPUTS:
        private_owned(bundle / name)
    cluster = bundle / "detached-postgres-17"
    private_owned(cluster, directory=True)
    for name in ("PG_VERSION", "postgresql.conf", "postgresql.auto.conf", "pg_hba.conf"):
        private_owned(cluster / name)
    helper.require((cluster / "PG_VERSION").read_text().strip() == "17", "LOCAL_POSTGRES_17_REQUIRED")
    helper.require(not (cluster / "postmaster.pid").exists(), "RETAINED_CLUSTER_MUST_BE_STOPPED")
    helper.require(not (bundle / ".pgpass").exists(), "SOURCE_CREDENTIAL_MUST_BE_REMOVED")
    blocked = json.loads((bundle / "blocked-receipt.json").read_text())
    helper.require(blocked.get("reason") == "RESTORED_SCHEMA_DEFINITION_MISMATCH"
                   and Path(blocked.get("directory", "")).resolve() == bundle,
                   "EXPECTED_SCHEMA_COMPARISON_FAILURE_REQUIRED")
    before = json.loads((bundle / "source-schema.json").read_text())
    prior_restore = json.loads((bundle / "restored-schema.json").read_text())
    helper.require(helper.logical_catalog(before) == helper.logical_catalog(prior_restore),
                   "RETAINED_SCHEMA_HAS_ADDITIONAL_DIFFERENCES")
    identities = json.loads((bundle / "source-fingerprints.json").read_text())
    tables = [r for r in before["relations"] if r["kind"] == "r"]
    helper.require(isinstance(identities, list) and len(identities) == len(tables),
                   "RECORDED_FINANCIAL_FINGERPRINTS_REQUIRED")
    helper.require(helper.CORE_TABLES <= {r["name"] for r in tables if r["schema"] == "public"},
                   "EXPECTED_FINANCIAL_TABLES_MISSING")
    helper.require(0 < (bundle / INPUTS[0]).stat().st_size <= helper.MAX_DATABASE, "DUMP_SIZE_LIMIT")
    helper.require(destination.is_dir() and helper.shutil.disk_usage(destination).free
                   >= helper.RESERVE + helper.WORK_BUDGET, "MAC_RECOVERY_SPACE_REQUIRED")
    helper.check_tools(bin_dir)
    seals = {name: helper.file_hash(bundle / name) for name in INPUTS}
    output = Path(tempfile.mkdtemp(prefix="external-ledger-verified-", dir=destination))
    helper.private_json(output / "input-seals.json", seals)
    socket_dir = Path(tempfile.mkdtemp(prefix="nxverify-", dir="/tmp"))
    lock = bundle / ".nexus-local-reverification.lock"
    started = locked = False
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        locked = True
        with os.fdopen(fd, "w") as marker:
            marker.write(str(os.getpid()))
        helper.require(not (cluster / "postmaster.pid").exists(), "RETAINED_CLUSTER_MUST_BE_STOPPED")
        helper.command([bin_dir / "pg_restore", "--list", bundle / INPUTS[0]],
                       directory=output, name="archive-index")
        options = "-c listen_addresses='' -c unix_socket_directories='" + str(socket_dir) + "'"
        options += " -c shared_buffers=16MB -c max_connections=6 -c max_worker_processes=0"
        options += " -c max_parallel_workers=0 -c autovacuum=off -c archive_mode=off"
        options += " -c shared_preload_libraries='' -c session_preload_libraries=''"
        options += " -c local_preload_libraries='' -c restore_command='' -c archive_command=''"
        started = True
        print(json.dumps({"phase": "START_RETAINED_DETACHED_LOCAL_CLUSTER_NO_NETWORK"}), flush=True)
        helper.command([bin_dir / "pg_ctl", "-D", cluster, "-l", output / "local-postgres.log",
                        "-w", "-t", "30", "-o", options, "start"],
                       directory=output, name="start-local-cluster", timeout=40)
        target = {"host": str(socket_dir), "port": 5432, "user": "nexus_recovery_admin", "dbname": "postgres"}
        name = "nexus_ledger_reverify_" + uuid.uuid4().hex
        with psycopg.connect(**target, autocommit=True) as admin:
            helper.require(admin.execute("SHOW listen_addresses").fetchone() == ("",),
                           "LOCAL_TCP_LISTENING_MUST_BE_DISABLED")
            owner = admin.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='nexus_ledger_restore'").fetchone()
            helper.require(bool(owner), "PRIOR_HELPER_RESTORE_DATABASE_REQUIRED")
            roles = [row[0] for row in admin.execute("SELECT rolname FROM pg_roles ORDER BY rolname")]
            helper.private_json(output / "restore-bootstrap.json", {
                "postgres_major": 17, "database_owner": owner[0], "roles": roles,
                "non_builtin_roles_recreated_as": "NOLOGIN_WITHOUT_ORIGINAL_PASSWORDS_OR_MEMBERSHIPS"})
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(
                sql.Identifier(name), sql.Identifier(owner[0])))
        target["dbname"] = name
        with psycopg.connect(**target, autocommit=True) as restored:
            restored.execute("DROP SCHEMA public")  # Empty schema in our new database only.
        print(json.dumps({"phase": "RESTORE_RETAINED_ARCHIVE_IN_NEW_LOCAL_DATABASE"}), flush=True)
        helper.command([bin_dir / "pg_restore", "--no-password", "--exit-on-error", "--single-transaction",
                        "--dbname=" + psycopg.conninfo.make_conninfo(**target), bundle / INPUTS[0]],
                       directory=output, name="restore")
        with psycopg.connect(**target, autocommit=True) as restored:
            helper.canonical_session(restored)
            grants_reapplied = helper.restore_recorded_schema_grants(restored, before)
            restored.execute("SET default_transaction_read_only=on")
            after = helper.catalog(restored)
            helper.private_json(output / "restored-schema.json", after)
            helper.require(helper.logical_catalog(before) == helper.logical_catalog(after),
                           "RESTORED_SCHEMA_DEFINITION_MISMATCH")
            recovered = helper.fingerprint(restored, tables, deadline=time.monotonic() + 900)
            helper.require(identities == recovered, "RESTORED_FINANCIAL_ROW_FINGERPRINT_MISMATCH")
            invalid = restored.execute("""SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY(%s)
                AND (NOT i.indisvalid OR NOT i.indisready)""", (list(helper.SCHEMAS),)).fetchone()[0]
            helper.require(invalid == 0, "RESTORED_INVALID_INDEX")
        helper.require(all(helper.file_hash(bundle / n) == v for n, v in seals.items()),
                       "RETAINED_BACKUP_INPUT_CHANGED")
        helper.require(helper.shutil.disk_usage(destination).free >= helper.RESERVE,
                       "MAC_FREE_SPACE_RESERVE_VIOLATED")
        report = {"status": "RETAINED_EXTERNAL_LEDGER_BACKUP_AND_NEW_LOCAL_RESTORE_VERIFIED",
                  "scope": "PUBLIC_APPLICATION_SCHEMA_AND_AUTH_EXTENSION_DEPENDENCIES",
                  "external_application_ledger_recovery_proven": True,
                  "full_supabase_platform_recovery_proven": False,
                  "storage_object_payloads_included": False, "original_role_passwords_included": False,
                  "original_role_login_attributes_and_memberships_recovered": False,
                  "snapshot_scope": "RECORDED_SOURCE_SNAPSHOT_NOT_CURRENT_LIVE_STATE",
                  "visible_column_order_and_all_recorded_definitions_match": True,
                  "object_ownership_and_grants_match": True,
                  "all_selected_table_rows_match_recorded_snapshot": True,
                  "recorded_schema_grants_reapplied": grants_reapplied,
                  "retained_input_files_unchanged": True, "input_sha256": seals,
                  "recovery_requires_retained_archive_source_metadata_and_bootstrap_record": True,
                  "restore_bootstrap_record": str(output / "restore-bootstrap.json"),
                  "tables_verified": len(tables), "rows_verified": sum(r["rows"] for r in identities),
                  "original_bundle": str(bundle), "verification_directory": str(output),
                  "new_local_restore_database": name, "source_connection_attempted": False,
                  "source_rows_mutated": False, "deployment_performed": False,
                  "original_data_deleted": False, "task6_complete": False}
    except Exception as error:
        error.verification_directory = str(output)
        raise
    finally:
        try:
            if started:
                helper.command([bin_dir / "pg_ctl", "-D", cluster, "-w", "-t", "30", "-m", "fast", "stop"],
                               directory=output, name="stop-local-cluster", timeout=40)
        finally:
            if locked:
                lock.unlink()
            if not any(socket_dir.iterdir()):
                socket_dir.rmdir()
    report["local_restore_server_stopped"] = True
    helper.private_json(output / "recovery-receipt.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path.home() / "Nexus-Backups")
    parser.add_argument("--pg-bin", type=Path, default=Path("/Applications/Postgres.app/Contents/Versions/17/bin"))
    args = parser.parse_args()
    os.umask(0o077)
    for key in list(os.environ):
        if key.startswith("PG"):
            del os.environ[key]
    try:
        report = verify(args.bundle, bin_dir=args.pg_bin.resolve(), destination=args.destination.expanduser().resolve())
    except Exception as error:
        report = helper.failure_report(error)
        report.update(source_connection_attempted=False, new_backup_performed=False)
        if getattr(error, "verification_directory", None):
            report["verification_directory"] = error.verification_directory
        print(json.dumps(report, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
