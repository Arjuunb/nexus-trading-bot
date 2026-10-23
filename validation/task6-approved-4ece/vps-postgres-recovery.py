"""Bounded backup/recovery of existing RC3 PostgreSQL metadata, never production.

Writes a new private backup and a uniquely named detached restore database.
Does not update services, replace databases, execute jobs or touch paper stores.
No credentials, database contents or actual infrastructure reports belong in git.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

STAGE = "nexus-remote-stage-rc3-28acdbc"
PROD = "nexus-trading-bot"
PG = STAGE + "-postgres-1"
SOURCE_DB = "nexus_staging_metadata"
BASE = Path("/home/ubuntu/nexus-rc3-staging-28acdbc")
MAX_DATABASE_BYTES = 1024 ** 3
RESERVE_BYTES = 8 * 1024 ** 3
PROOF_SQL = """
SELECT json_build_object(
 'identity',(SELECT json_agg(row_to_json(i) ORDER BY purpose) FROM
   (SELECT purpose,authority_id,version,writer_role FROM nexus_workers.worker_store_identity) i),
 'tables',(SELECT json_agg(tablename ORDER BY tablename) FROM pg_tables WHERE schemaname='nexus_workers'),
 'columns',(SELECT json_agg(row_to_json(c) ORDER BY table_name,ordinal_position) FROM
   (SELECT table_name,ordinal_position,column_name,data_type,is_nullable,column_default
    FROM information_schema.columns WHERE table_schema='nexus_workers') c),
 'indexes',(SELECT json_agg(indexdef ORDER BY tablename,indexname) FROM pg_indexes WHERE schemaname='nexus_workers'),
 'triggers',(SELECT json_agg(pg_get_triggerdef(t.oid) ORDER BY c.relname,t.tgname)
    FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='nexus_workers' AND NOT t.tgisinternal),
 'financial_tables',(SELECT json_agg(tablename ORDER BY tablename) FROM pg_tables
    WHERE schemaname NOT IN ('pg_catalog','information_schema') AND
    (tablename IN ('paper_trades','paper_positions','trade_decision_journal','orders','positions','balances','pa_sessions','smc_sessions','financial_journal')
     OR left(tablename,3)='v2_'))
);
"""


class Blocked(RuntimeError):
    pass


def docker(args, operation, *, stdin=None, stdout=None, timeout=300):
    result = subprocess.run(["docker", *args], stdin=stdin, stdout=stdout or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Do not print raw stderr: a database error can contain private values.
        error = result.stderr.decode(errors="replace").lower()
        category = next((label for text, label in (
            ("no password supplied", "LOCAL_POSTGRES_CREDENTIAL_REQUIRED"),
            ("password authentication failed", "LOCAL_POSTGRES_AUTHENTICATION_FAILED"),
            ("peer authentication failed", "LOCAL_POSTGRES_PEER_AUTHENTICATION_FAILED"),
            ("permission denied", "PERMISSION_DENIED"),
            ("does not exist", "REQUIRED_RESOURCE_NOT_FOUND"),
            ("already exists", "RESTORE_NAME_OR_FILE_COLLISION"),
            ("connection refused", "POSTGRES_CONNECTION_REFUSED"),
            ("timeout", "RESOURCE_TIMEOUT")) if text in error), "COMMAND_FAILED")
        raise Blocked(operation + ":" + category + ":exit=" + str(result.returncode))
    return result.stdout.decode() if stdout is None else None


def inspect(name):
    return json.loads(docker(["inspect", name], "CONTAINER_INSPECTION"))[0]


def production_snapshot():
    ids = docker(["ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROD],
                 "PRODUCTION_IDENTITY_READ").split()
    if not ids:
        raise Blocked("PRODUCTION_BASELINE_NOT_FOUND")
    rows = json.loads(docker(["inspect", *ids], "PRODUCTION_IDENTITY_READ"))
    # Store identities/mounts only, never production Config.Env or credentials.
    return sorted([{"id": c["Id"], "image": c["Image"], "started": c["State"]["StartedAt"],
                    "restarts": c["RestartCount"], "mounts": c["Mounts"]}
                   for c in rows], key=lambda row: row["id"])


def query(database, sql):
    return docker(["exec", "--env", "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=15000",
                   "--user", "postgres", PG, "psql", "--no-password", "-X", "-qAt",
                   "-v", "ON_ERROR_STOP=1", "--username", "postgres", "--dbname", database,
                   "--command", sql],
                  "STAGING_READ_ONLY_QUERY").strip()


def proof(database):
    return json.loads(query(database, PROOF_SQL))


def main():
    if sys.argv[1:]:
        raise Blocked("NO_TARGET_OVERRIDES_ACCEPTED")
    os.umask(0o077)
    report = {"scope": "EXISTING_RC3_POSTGRES_BACKUP_AND_DETACHED_RESTORE", "status": "STARTED",
              "production_database_access": False, "service_updates": False, "new_paper_execution": False,
              "sqlite_backups": "PENDING_SEPARATE_STEP", "task6_complete": False}
    backup_dir = None
    try:
        before_pg = inspect(PG)
        if (before_pg["Config"].get("Labels") or {}).get("com.docker.compose.project") != STAGE or before_pg["State"]["Status"] != "running":
            raise Blocked("EXISTING_STAGE_POSTGRES_REQUIRED")
        prod_before = production_snapshot()
        source_mounts = {m["Source"] for m in before_pg["Mounts"]}
        if any(m["Source"] in source_mounts for row in prod_before for m in row["mounts"]):
            raise Blocked("STAGE_POSTGRES_SHARES_PRODUCTION_STORAGE")
        expected_volume = STAGE + "_postgres-state"
        if not any(m.get("Name") == expected_volume and m["Destination"] == "/var/lib/postgresql/data" for m in before_pg["Mounts"]):
            raise Blocked("STAGE_POSTGRES_VOLUME_MISMATCH")
        original = proof(SOURCE_DB)
        identities = original["identity"] or []
        if len(identities) != 1 or identities[0]["purpose"] != "research_shadow_metadata" or identities[0]["version"] != 1 or identities[0]["writer_role"] != "nexus_staging_worker" or original["financial_tables"]:
            raise Blocked("ISOLATED_METADATA_IDENTITY_REQUIRED")
        source_bytes = int(query(SOURCE_DB, "SELECT pg_database_size(current_database());"))
        free_bytes = shutil.disk_usage(BASE).free
        if source_bytes > MAX_DATABASE_BYTES or free_bytes < 4 * source_bytes + RESERVE_BYTES:
            raise Blocked("POSTGRES_RECOVERY_SPACE_BOUND_FAILED")
        token = uuid.uuid4().hex[:16]
        backup_dir = BASE / ("task6-pg-recovery-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + token)
        backup_dir.mkdir(mode=0o700)
        restore_db = "nexus_task6_restore_" + token
        report.update(backup_directory=str(backup_dir), source_database=SOURCE_DB,
                      detached_restore_database=restore_db, source_database_bytes=source_bytes,
                      production_before=prod_before)
        dump_path = backup_dir / "metadata.dump"
        with dump_path.open("xb") as target:
            docker(["exec", "--user", "postgres", PG, "pg_dump", "--no-password", "--username", "postgres",
                    "--dbname", SOURCE_DB, "--format", "custom", "--lock-wait-timeout=10s"],
                   "STAGING_METADATA_BACKUP", stdout=target)
        if not dump_path.stat().st_size:
            raise Blocked("EMPTY_POSTGRES_BACKUP")
        with dump_path.open("rb") as source:
            catalog = docker(["exec", "-i", "--user", "postgres", PG, "pg_restore", "--list"],
                             "POSTGRES_BACKUP_ARCHIVE_CHECK", stdin=source)
        (backup_dir / "metadata-contents.txt").write_text(catalog)
        digest = hashlib.sha256()
        with dump_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        dump_sha = digest.hexdigest()
        report.update(dump_bytes=dump_path.stat().st_size, dump_sha256=dump_sha)
        # Never --clean, overwrite, reconfigure workers or restore into SOURCE_DB.
        docker(["exec", "--user", "postgres", PG, "createdb", "--no-password", "--username", "postgres",
                "--template", "template0", "--owner", "postgres", restore_db], "CREATE_NEW_DETACHED_RESTORE_DATABASE")
        with dump_path.open("rb") as source:
            docker(["exec", "-i", "--user", "postgres", PG, "pg_restore", "--no-password", "--username", "postgres",
                    "--exit-on-error", "--single-transaction", "--dbname", restore_db],
                   "RESTORE_ONLY_TO_NEW_DETACHED_DATABASE", stdin=source)
        restored = proof(restore_db)
        if restored != original:
            raise Blocked("RESTORED_IDENTITY_OR_SCHEMA_DIFFERENCE")
        if proof(SOURCE_DB) != original:
            raise Blocked("SOURCE_METADATA_IDENTITY_OR_SCHEMA_CHANGED")
        after_pg = inspect(PG)
        if (after_pg["Id"], after_pg["State"]["StartedAt"], after_pg["Image"], after_pg["Mounts"]) != (before_pg["Id"], before_pg["State"]["StartedAt"], before_pg["Image"], before_pg["Mounts"]):
            raise Blocked("STAGE_POSTGRES_CONTAINER_CHANGED")
        if production_snapshot() != prod_before:
            raise Blocked("PRODUCTION_IDENTITY_CHANGED_DURING_CHECK")
        (backup_dir / "source-schema-and-identity.json").write_text(json.dumps(original, indent=2))
        report.update(status="POSTGRES_BACKUP_AND_DETACHED_RESTORE_PASSED", restored_table_count=len(restored["tables"]),
                      schema_indexes_triggers_and_identity_match=True, original_database_schema_unchanged=True,
                      production_container_identities_unchanged=True, free_disk_bytes_after=shutil.disk_usage(BASE).free,
                      backup_scope="POSTGRES_METADATA_ONLY; ROLE_PASSWORDS_AND_SQLITE_NOT_INCLUDED",
                      source_rows_mutated_by_this_script=False)
    except Exception as exc:
        report.update(status="BLOCKED", reason=str(exc) if isinstance(exc, Blocked) else type(exc).__name__)
    if backup_dir is not None:
        (backup_dir / "report.json").write_text(json.dumps(report, indent=2))
    # Actual environment details remain in the private on-VPS report, not git.
    print(json.dumps({k: v for k, v in report.items() if k != "production_before"}, indent=2))
    return 0 if report["status"] == "POSTGRES_BACKUP_AND_DETACHED_RESTORE_PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
