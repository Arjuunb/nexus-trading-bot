#!/usr/bin/env python3
"""Snapshot the pinned external application ledger; restore only to a new local cluster.

This is not a full Supabase platform/Storage backup. Originals are never changed,
and failed or successful financial copies are retained. No application is imported.
Requires psycopg 3 and PostgreSQL 17 client/server binaries on the operator's Mac.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

SCHEMAS = ("public", "auth", "extensions")
CORE_TABLES = {"webhook_events", "positions", "paper_trades", "bot_logs", "alerts"}
SAFE_EXTENSIONS = {"plpgsql", "pgcrypto", "uuid-ossp", "pg_stat_statements", "pg_trgm", "citext", "btree_gist", "btree_gin"}
MIB = 1024 ** 2
RESERVE = 8 * 1024 ** 3
WORK_BUDGET = 2 * 1024 ** 3
MAX_DATABASE = 256 * MIB
MAX_LOGICAL = 512 * MIB


class Blocked(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def clean_environment():
    return {k: v for k, v in os.environ.items() if not k.startswith("PG")}


def private_json(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(value, out, indent=2, sort_keys=True)
        out.write("\n")


def file_hash(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(MIB), b""):
            value.update(chunk)
    return value.hexdigest()


def check_tools(bin_dir):
    names = ("pg_dump", "pg_restore", "psql", "initdb", "pg_ctl")
    for name in names:
        binary = bin_dir / name
        require(binary.is_file() and os.access(binary, os.X_OK), "POSTGRES_17_TOOLS_REQUIRED")
        value = subprocess.run([str(binary), "--version"], capture_output=True,
                               text=True, timeout=10, env=clean_environment())
        require(value.returncode == 0 and re.search(r"\b17\.\d+\b", value.stdout),
                "ALL_POSTGRES_TOOLS_MUST_BE_VERSION_17")


def command(args, *, directory, name, env=None, timeout=180):
    """Keep SQL/tool diagnostics private; never echo stderr or credentials."""
    log = directory / (name + ".log")
    fd = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as output:
        value = subprocess.run([str(a) for a in args], stdout=output,
                               stderr=subprocess.STDOUT, timeout=timeout,
                               env=env or clean_environment(), umask=0o077)
    require(value.returncode == 0, "POSTGRES_OPERATION_FAILED:" + name)


def passfile_line(config):
    def escape(value):
        value = str(value)
        require(not any(c in value for c in "\r\n\x00"), "INVALID_CREDENTIAL_CHARACTER")
        return value.replace("\\", "\\\\").replace(":", "\\:")
    return ":".join(escape(config[k]) for k in ("host", "port", "dbname", "user", "password")) + "\n"


def rows(conn, query, params=()):
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def catalog(conn):
    """Compare complete definitions, preserving function bodies and column order."""
    namespace = "n.nspname = ANY(%s)"
    params = (list(SCHEMAS),)
    result = {}
    result["relations"] = rows(conn, f"""
        SELECT n.nspname AS schema, c.relname AS name, c.relkind::text AS kind,
          c.relrowsecurity AS rls, c.relforcerowsecurity AS force_rls,
          CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid, true) ELSE NULL END AS view
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {namespace} AND c.relkind IN ('r','p','v','m','S','f')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass
            AND d.objid=c.oid AND d.deptype='e')
        ORDER BY 1,2""", params)
    result["columns"] = rows(conn, f"""
        SELECT n.nspname AS schema,c.relname AS relation,a.attnum AS ordinal,
          a.attname AS name,format_type(a.atttypid,a.atttypmod) AS type,
          a.attnotnull AS not_null,a.attidentity::text AS identity,a.attgenerated::text AS generated,
          pg_get_expr(d.adbin,d.adrelid) AS default_expression
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE {namespace} AND a.attnum>0 AND NOT a.attisdropped
          AND c.relkind IN ('r','p','v','m','S')
          AND NOT EXISTS (SELECT 1 FROM pg_depend x WHERE x.classid='pg_class'::regclass
            AND x.objid=c.oid AND x.deptype='e')
        ORDER BY 1,2,3""", params)
    result["constraints"] = rows(conn, f"""
        SELECT n.nspname AS schema,c.relname AS relation,x.conname AS name,
          x.contype::text AS type,x.convalidated AS validated,pg_get_constraintdef(x.oid) AS definition
        FROM pg_constraint x JOIN pg_namespace n ON n.oid=x.connamespace
        LEFT JOIN pg_class c ON c.oid=x.conrelid
        WHERE {namespace} ORDER BY 1,2,3""", params)
    result["indexes"] = rows(conn, f"""
        SELECT n.nspname AS schema,c.relname AS name,pg_get_indexdef(c.oid) AS definition,
          i.indisvalid AS valid,i.indisready AS ready
        FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {namespace} ORDER BY 1,2""", params)
    result["triggers"] = rows(conn, f"""
        SELECT n.nspname AS schema,c.relname AS relation,t.tgname AS name,
          t.tgenabled::text AS enabled,pg_get_triggerdef(t.oid) AS definition
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE {namespace} AND NOT t.tgisinternal ORDER BY 1,2,3""", params)
    result["functions"] = rows(conn, f"""
        SELECT n.nspname AS schema,p.proname AS name,pg_get_function_identity_arguments(p.oid) AS arguments,
          pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE {namespace} AND p.prokind <> 'a'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass
            AND d.objid=p.oid AND d.deptype='e')
        ORDER BY 1,2,3""", params)
    result["policies"] = rows(conn, """
        SELECT schemaname AS schema,tablename AS relation,policyname AS name,
          permissive,roles::text,cmd,qual,with_check FROM pg_policies
        WHERE schemaname = ANY(%s) ORDER BY 1,2,3""", params)
    result["enums"] = rows(conn, f"""
        SELECT n.nspname AS schema,t.typname AS name,e.enumsortorder::text AS ordinal,
          e.enumlabel AS label FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid
        JOIN pg_namespace n ON n.oid=t.typnamespace WHERE {namespace} ORDER BY 1,2,e.enumsortorder""", params)
    result["extensions"] = rows(conn, """
        SELECT e.extname AS name,e.extversion AS version,n.nspname AS schema
        FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
        WHERE n.nspname=ANY(%s) ORDER BY 1""", params)
    return result


def fingerprint(conn, tables, *, deadline):
    from psycopg import sql
    result, total = [], 0
    for table in tables:
        value, count = hashlib.sha256(), 0
        query = sql.SQL("COPY (SELECT row_to_json(t)::text FROM ONLY {} t ORDER BY row_to_json(t)::text COLLATE \"C\") TO STDOUT").format(
            sql.Identifier(table["schema"], table["name"]))
        with conn.cursor() as cursor, cursor.copy(query) as stream:
            for block in stream:
                block = bytes(block)
                total += len(block)
                require(total <= MAX_LOGICAL, "LOGICAL_DATA_SIZE_LIMIT")
                require(time.monotonic() < deadline, "RECOVERY_DEADLINE_EXCEEDED")
                value.update(block)
                count += block.count(b"\n")
        result.append({"schema": table["schema"], "table": table["name"],
                       "rows": count, "sha256": value.hexdigest()})
    return result


def canonical_session(conn):
    conn.execute("SET search_path=pg_catalog")
    conn.execute("SET TIME ZONE 'UTC'")
    conn.execute("SET DateStyle='ISO,YMD'")
    conn.execute("SET IntervalStyle='postgres'")
    conn.execute("SET extra_float_digits=3")
    conn.execute("SET row_security=off")


def recover(config, *, bin_dir, directory, require_tls=True, snapshot_hook=None):
    import psycopg
    from psycopg import sql
    deadline = time.monotonic() + 900
    source = None
    password_file = directory / ".pgpass"
    socket_dir = Path(tempfile.mkdtemp(prefix="nxpg-", dir="/tmp"))
    cluster = directory / "detached-postgres-17"
    archive = directory / "application-ledger.dump"
    started = False
    try:
        require(shutil.disk_usage(directory).free >= RESERVE + WORK_BUDGET,
                "MAC_RECOVERY_SPACE_REQUIRED")
        check_tools(bin_dir)
        # This file is used only by pg_dump, is never printed, and is removed below.
        fd = os.open(password_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(passfile_line(config))
        source = psycopg.connect(**config, autocommit=True,
            options="-c default_transaction_read_only=on -c statement_timeout=120000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=240000")
        require(source.info.server_version // 10000 == 17, "SOURCE_POSTGRES_VERSION_CHANGED")
        require(source.execute("SELECT pg_database_size(current_database())").fetchone()[0] <= MAX_DATABASE,
                "SOURCE_DATABASE_SIZE_REVIEW_REQUIRED")
        if require_tls:
            require(source.execute("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone() == (True,),
                    "VERIFIED_TLS_REQUIRED")
        source.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        canonical_session(source)
        require(source.execute("SHOW transaction_read_only").fetchone()[0] == "on", "READ_ONLY_SOURCE_REQUIRED")
        snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
        require(re.fullmatch(r"[0-9A-Fa-f-]+", snapshot), "INVALID_EXPORTED_SNAPSHOT")
        before = catalog(source)
        tables = [r for r in before["relations"] if r["kind"] == "r"]
        require(0 < len(tables) <= 200, "TABLE_COUNT_REVIEW_REQUIRED")
        require(CORE_TABLES <= {r["name"] for r in tables if r["schema"] == "public"},
                "EXPECTED_FINANCIAL_TABLES_MISSING")
        require(not any(r["kind"] == "f" for r in before["relations"]), "FOREIGN_TABLE_BACKUP_REVIEW_REQUIRED")
        require(all(r["name"] in SAFE_EXTENSIONS for r in before["extensions"]),
                "LOCAL_RESTORE_EXTENSION_REVIEW_REQUIRED")
        dependencies = source.execute("""
            SELECT DISTINCT nr.nspname FROM pg_constraint c
            JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
            JOIN pg_class r ON r.oid=c.confrelid JOIN pg_namespace nr ON nr.oid=r.relnamespace
            WHERE c.contype='f' AND n.nspname=ANY(%s) AND NOT nr.nspname=ANY(%s)
            """, (list(SCHEMAS), list(SCHEMAS))).fetchall()
        require(not dependencies, "ADDITIONAL_FOREIGN_KEY_SCHEMA_BACKUP_REQUIRED")
        require(source.execute("SELECT count(*) FROM pg_largeobject_metadata").fetchone()[0] == 0,
                "LARGE_OBJECT_BACKUP_REVIEW_REQUIRED")
        require(not any(r["kind"] == "m" for r in before["relations"]),
                "MATERIALIZED_VIEW_RESTORE_REVIEW_REQUIRED")
        role_names = [r[0] for r in source.execute("SELECT rolname FROM pg_roles ORDER BY rolname")]
        identities = fingerprint(source, tables, deadline=deadline)
        private_json(directory / "source-schema.json", before)
        private_json(directory / "source-fingerprints.json", identities)
        if snapshot_hook:
            snapshot_hook(source)
        source_env = clean_environment()
        source_env.update(PGPASSFILE=str(password_file), PGCONNECT_TIMEOUT="10",
                          PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=120000 -c lock_timeout=3000")
        # A password-free connection string; secrets are only in the private passfile.
        public_config = {k: v for k, v in config.items() if k != "password"}
        dsn = psycopg.conninfo.make_conninfo(**public_config)
        dump_args = [bin_dir / "pg_dump", "--no-password", "--format=custom", "--strict-names",
                     "--lock-wait-timeout=3000", "--snapshot=" + snapshot, "--dbname=" + dsn,
                     "--file=" + str(archive)]
        for name in SCHEMAS:
            dump_args.append("--schema=" + name)
        print(json.dumps({"phase": "COPY_CONSISTENT_EXTERNAL_LEDGER_SNAPSHOT"}), flush=True)
        command(dump_args, directory=directory, name="dump", env=source_env)
        require(archive.is_file() and 0 < archive.stat().st_size <= MAX_DATABASE, "DUMP_SIZE_LIMIT")
        source.execute("COMMIT")
        source.close()
        source = None
        password_file.unlink()
        original_hash = file_hash(archive)
        command([bin_dir / "pg_restore", "--list", archive], directory=directory, name="archive-index")
        command([bin_dir / "initdb", "--pgdata=" + str(cluster), "--username=nexus_recovery_admin",
                 "--encoding=UTF8", "--locale=C", "--auth-local=trust", "--auth-host=reject"],
                directory=directory, name="init-local-cluster", timeout=60)
        options = "-c listen_addresses='' -c unix_socket_directories='" + str(socket_dir) + "'"
        options += " -c shared_buffers=16MB -c max_connections=6 -c max_worker_processes=0 -c max_parallel_workers=0"
        options += " -c max_wal_size=64MB -c min_wal_size=32MB -c autovacuum=off -c archive_mode=off"
        started = True  # Even a startup timeout must attempt to stop this exact new cluster.
        command([bin_dir / "pg_ctl", "-D", cluster, "-l", directory / "local-postgres.log",
                 "-w", "-t", "30", "-o", options, "start"], directory=directory, name="start-local-cluster", timeout=40)
        target = {"host": str(socket_dir), "port": 5432, "user": "nexus_recovery_admin", "dbname": "postgres"}
        with psycopg.connect(**target, autocommit=True) as admin:
            existing = {r[0] for r in admin.execute("SELECT rolname FROM pg_roles")}
            for name in role_names:
                if name not in existing:
                    require(not name.startswith("pg_"), "POSTGRES_BUILTIN_ROLE_VERSION_MISMATCH")
                    admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(name)))
            admin.execute("CREATE DATABASE nexus_ledger_restore TEMPLATE template0 ENCODING 'UTF8'")
        target["dbname"] = "nexus_ledger_restore"
        with psycopg.connect(**target, autocommit=True) as restored:
            # Only the empty default schema of our newly created local database.
            restored.execute("DROP SCHEMA public")
        print(json.dumps({"phase": "RESTORE_AND_COMPARE_IN_NEW_LOCAL_POSTGRES_17"}), flush=True)
        target_dsn = psycopg.conninfo.make_conninfo(**target)
        command([bin_dir / "pg_restore", "--no-password", "--exit-on-error", "--single-transaction",
                 "--no-owner", "--no-privileges", "--dbname=" + target_dsn, archive],
                directory=directory, name="restore")
        with psycopg.connect(**target, autocommit=True) as restored:
            canonical_session(restored)
            after = catalog(restored)
            private_json(directory / "restored-schema.json", after)
            require(before == after, "RESTORED_SCHEMA_DEFINITION_MISMATCH")
            recovered = fingerprint(restored, tables, deadline=deadline)
            require(identities == recovered, "RESTORED_FINANCIAL_ROW_FINGERPRINT_MISMATCH")
            invalid = restored.execute("""
                SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=ANY(%s) AND (NOT i.indisvalid OR NOT i.indisready)
                """, (list(SCHEMAS),)).fetchone()[0]
            require(invalid == 0, "RESTORED_INVALID_INDEX")
        require(file_hash(archive) == original_hash, "BACKUP_CHANGED_DURING_RESTORE")
        require(shutil.disk_usage(directory).free >= RESERVE, "MAC_FREE_SPACE_RESERVE_VIOLATED")
        return {"status": "EXTERNAL_APPLICATION_LEDGER_BACKUP_AND_DETACHED_LOCAL_RESTORE_VERIFIED",
                "scope": "PUBLIC_APPLICATION_SCHEMA_AND_AUTH_EXTENSION_DEPENDENCIES",
                "full_supabase_platform_recovery_proven": False,
                "external_application_ledger_recovery_proven": True,
                "storage_object_payloads_included": False,
                "original_role_passwords_included": False,
                "original_role_ownership_and_grants_restore_tested": False,
                "schema_columns_constraints_indexes_triggers_functions_policies_match": True,
                "all_selected_table_rows_match_same_exported_snapshot": True,
                "tables_verified": len(tables), "rows_verified": sum(r["rows"] for r in identities),
                "dump_bytes": archive.stat().st_size, "dump_sha256": original_hash,
                "directory": str(directory), "source_rows_mutated_by_helper": False,
                "vps_service_updates": False, "deployment_performed": False,
                "original_data_deleted": False, "task6_complete": False}
    finally:
        if source is not None:
            source.close()
        if password_file.exists():
            password_file.unlink()
        if started:
            command([bin_dir / "pg_ctl", "-D", cluster, "-w", "-t", "30", "-m", "fast", "stop"],
                    directory=directory, name="stop-local-cluster", timeout=40)
        socket_dir.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", action="store_true", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--pooler-host", required=True)
    parser.add_argument("--pg-bin", type=Path, default=Path("/Applications/Postgres.app/Contents/Versions/17/bin"))
    parser.add_argument("--destination", type=Path, default=Path.home() / "Nexus-Backups")
    args = parser.parse_args()
    os.umask(0o077)
    directory = None
    try:
        for key in list(os.environ):
            if key.startswith("PG"):
                del os.environ[key]
        import psycopg  # noqa: F401 — fail before requesting any password if unavailable
        require(re.fullmatch(r"[a-z0-9]{20}", args.project), "SUPABASE_PROJECT_REF_REQUIRED")
        require(re.fullmatch(r"aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com", args.pooler_host),
                "OFFICIAL_SESSION_POOLER_HOST_REQUIRED")
        check_tools(args.pg_bin)
        destination = args.destination.expanduser().resolve()
        require(destination.is_dir(), "EXISTING_BACKUP_DESTINATION_REQUIRED")
        require(shutil.disk_usage(destination).free >= RESERVE + WORK_BUDGET, "MAC_RECOVERY_SPACE_REQUIRED")
        require(Path("/etc/ssl/cert.pem").is_file(), "MAC_SYSTEM_CA_BUNDLE_REQUIRED")
        password = getpass.getpass("Staging Supabase DATABASE password (hidden): ")
        require(bool(password), "DATABASE_PASSWORD_REQUIRED")
        config = {"host": args.pooler_host, "port": 5432, "dbname": "postgres", "user": "postgres." + args.project,
                  "password": password, "sslmode": "verify-full", "sslrootcert": "/etc/ssl/cert.pem",
                  "connect_timeout": 10, "application_name": "nexus_read_only_ledger_backup"}
        directory = Path(tempfile.mkdtemp(prefix="external-ledger-", dir=destination))
        report = recover(config, bin_dir=args.pg_bin.resolve(), directory=directory)
        report.update(project=args.project, local_restore_server_stopped=True,
                      at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
        private_json(directory / "recovery-receipt.json", report)
        print(json.dumps(report, indent=2))
        return 0
    except Exception as error:
        report = {"status": "BLOCKED", "reason": str(error) if isinstance(error, Blocked) else type(error).__name__,
                  "deployment_performed": False, "original_data_deleted": False,
                  "external_ledger_recovery_proven": False, "partial_copies_retained": True}
        if directory:
            report["directory"] = str(directory)
            private_json(directory / "blocked-receipt.json", report)
        print(json.dumps(report, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
