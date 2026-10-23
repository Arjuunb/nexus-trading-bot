"""Read-only RC3 inventory/config comparison. Never prints credentials or starts services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

STAGE = "nexus-remote-stage-rc3-28acdbc"
PROD = "nexus-trading-bot"
ROOT = Path("/home/ubuntu/nexus-rc3-staging-28acdbc")
RELEASE = ROOT / "release"
FILES = [RELEASE / "ops/staging.alpine.compose.yaml",
         RELEASE / "customer-workspace/compose.customer-workspace.yaml",
         RELEASE / "intelligence-recovery-20261018/compose.memory.yaml"]
ENVFILE = ROOT / "private/compose.env"
FLAGS = ("HUB_AUTH_MODE", "HUB_EMERGENCY_ADMIN_ENABLED", "HUB_AUTO_ENGINE",
         "HUB_ENABLE_EXTERNAL_LIVE", "HUB_USE_LIVE_DATA", "HUB_RESEARCH_AUTOSTART",
         "HUB_MONITOR_AGENT", "HUB_DAILY_REPORT_HOUR", "NEXUS_SHADOW_SUPERVISED")

DATABASE_READONLY_CODE = r'''
import hashlib, json, os, sqlite3, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

report = {"scope": "STAGING_DATABASE_READ_ONLY", "backups_created": False, "blockers": []}
try:
    import psycopg
    from psycopg.rows import dict_row
    dsn = os.environ["NEXUS_RESEARCH_WORKER_DSN"]
    target = urlsplit(dsn)
    assert target.hostname == "postgres" and target.path == "/nexus_staging_metadata"
    assert target.username == "nexus_staging_worker"
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5,
                        options="-c default_transaction_read_only=on -c statement_timeout=10000") as db:
        pg = dict(db.execute("SELECT current_database() AS database, current_user AS role, current_setting('transaction_read_only') AS read_only, current_setting('server_version') AS server_version").fetchone())
        pg["role_attributes"] = dict(db.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone())
        pg["dangerous_role_membership"] = db.execute("SELECT COALESCE(bool_or(pg_has_role(current_user,rolname,'MEMBER')),false) AS unsafe FROM pg_roles WHERE rolname IN ('pg_read_server_files','pg_write_server_files','pg_execute_server_program','pg_read_all_data','pg_write_all_data','pg_signal_backend')").fetchone()["unsafe"]
        pg["can_create_schema_objects"] = db.execute("SELECT has_schema_privilege(current_user,'nexus_workers','CREATE') AS unsafe").fetchone()["unsafe"]
        pg["identity"] = [dict(r) for r in db.execute("SELECT purpose,authority_id,version,writer_role FROM nexus_workers.worker_store_identity")]
        pg["tables"] = [r["tablename"] for r in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='nexus_workers' ORDER BY tablename")]
        pg["immutable_triggers"] = [dict(r) for r in db.execute("SELECT c.relname AS table_name,t.tgname AS trigger_name,t.tgenabled AS enabled FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='nexus_workers' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname")]
        pg["job_status_counts"] = [dict(r) for r in db.execute("SELECT status,count(*) AS count FROM nexus_workers.research_jobs GROUP BY status ORDER BY status")]
        pg["validation_run_count"] = db.execute("SELECT count(*) AS count FROM nexus_workers.strategy_validation_runs").fetchone()["count"]
        pg["non_metadata_financial_tables"] = [dict(r) for r in db.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') AND (tablename IN ('paper_trades','paper_positions','trade_decision_journal','orders','positions','balances','pa_sessions','smc_sessions','financial_journal') OR left(tablename,3)='v2_') ORDER BY schemaname,tablename")]
        required = {"worker_store_identity","research_jobs","research_job_attempts","research_job_events","strategy_validation_runs","retained_research_datasets","retained_research_dataset_refs","local_readiness_assessments","operational_retention_segments","paper_evidence_reviews"}
        pg["required_tables_missing"] = sorted(required-set(pg["tables"]))
        if pg["read_only"] != "on" or any(pg["role_attributes"].values()) or pg["dangerous_role_membership"] or pg["can_create_schema_objects"] or pg["non_metadata_financial_tables"]:
            report["blockers"].append("POSTGRES_ROLE_OR_TARGET_UNSAFE")
        if pg["required_tables_missing"] or len(pg["identity"]) != 1 or any(r["purpose"] != "research_shadow_metadata" or r["version"] != 1 or r["writer_role"] != "nexus_staging_worker" for r in pg["identity"]):
            report["blockers"].append("POSTGRES_METADATA_SCHEMA_OR_IDENTITY_UNEXPECTED")
        report["postgres"] = pg
except Exception as exc:
    report["postgres"] = {"status": "BLOCKED", "error_type": type(exc).__name__, "sqlstate": getattr(exc,"sqlstate",None)}
    report["blockers"].append("STAGING_POSTGRES_READ_ONLY_CHECK_FAILED")

health_path = Path('/workers/research/research-health.json')
try:
    assert health_path.stat().st_size <= 65536 and not health_path.is_symlink()
    health = json.loads(health_path.read_text())
    report["research_worker"] = {k: health.get(k) for k in ("worker_kind","readiness","database_status","backend","metadata_authority_id","execution_runtime_loaded","recorded_at","pid","process_start_ticks","queue_backlog")}
    age = (datetime.now(timezone.utc)-datetime.fromisoformat(health["recorded_at"])).total_seconds()
    report["research_worker"]["health_age_seconds"] = round(age,3)
    if not 0 <= age <= 20 or health.get("readiness") != "READY" or health.get("database_status") != "AVAILABLE" or health.get("backend") != "postgresql" or health.get("execution_runtime_loaded") is not False:
        report["blockers"].append("RESEARCH_WORKER_NOT_FRESH_RESEARCH_ONLY_READY")
    identities = report.get("postgres",{}).get("identity",[])
    if len(identities) == 1 and health.get("metadata_authority_id") != identities[0]["authority_id"]:
        report["blockers"].append("API_WORKER_METADATA_AUTHORITY_MISMATCH")
except Exception as exc:
    report["research_worker"] = {"status":"BLOCKED","error_type":type(exc).__name__}
    report["blockers"].append("RESEARCH_WORKER_HEALTH_UNAVAILABLE")

report["sqlite_files"] = []
report["excluded_backup_directories"] = []
for root in (Path('/var/lib/tradexa'),Path('/workers/research'),Path('/workers/shadow')):
    for base, directories, files in os.walk(root,followlinks=False):
        skip = [d for d in directories if 'backup' in d.lower() or 'restore' in d.lower()]
        report["excluded_backup_directories"] += [str(Path(base)/d) for d in skip]
        directories[:] = sorted(d for d in directories if d not in skip)
        for name in sorted(files):
            path = Path(base)/name
            if path.suffix not in ('.db','.sqlite','.sqlite3'):
                continue
            item = {"path":str(path),"bytes":path.stat().st_size,"wal_bytes":Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0}
            if path.is_symlink() or len(report["sqlite_files"]) >= 96:
                report["blockers"].append("SQLITE_SYMLINK_OR_INVENTORY_LIMIT")
                continue
            try:
                with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2) as db:
                    db.execute('PRAGMA query_only=ON')
                    deadline = time.monotonic()+5
                    db.set_progress_handler(lambda: int(time.monotonic()>deadline),10000)
                    result = db.execute('PRAGMA quick_check').fetchall()
                    item['quick_check'] = 'ok' if result == [('ok',)] else 'FAILED'
                    item['user_version'] = db.execute('PRAGMA user_version').fetchone()[0]
                    item['tables'] = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                    if item['quick_check'] != 'ok':
                        report['blockers'].append('SQLITE_QUICK_CHECK_FAILED:'+str(path))
            except Exception as exc:
                item['status'] = 'BLOCKED'
                item['error_type'] = type(exc).__name__
                report['blockers'].append('SQLITE_READ_ONLY_CHECK_FAILED:'+str(path))
            report['sqlite_files'].append(item)
report['sqlite_total_main_bytes'] = sum(item['bytes'] for item in report['sqlite_files'])
report['image_migration_file_sha256'] = {}
for directory in (Path('/app/automation-hub/migrations/workers'),Path('/app/automation-hub/database/migrations')):
    for path in sorted(directory.glob('*.sql')):
        report['image_migration_file_sha256'][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
report['status'] = 'READ_ONLY_CHECKS_PASSED' if not report['blockers'] else 'BLOCKED'
print(json.dumps(report))
'''


def command(args, *, env=None, input_text=None):
    result = subprocess.run(["docker", *args], capture_output=True, text=True,
                            timeout=60, env=env, input=input_text)
    if result.returncode:
        # Captured Docker/Compose errors may contain interpolated secrets.
        raise RuntimeError("DOCKER_READ_FAILED:" + args[0] + ":exit=" + str(result.returncode))
    return result.stdout


def environment(items):
    return dict(item.split("=", 1) for item in items or [] if "=" in item)


def byte_count(value):
    """Compose JSON versions emit canonical byte counts as integers or strings."""
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def mount_summary(mounts):
    return sorted([{"type": m["Type"], "source": m.get("Name") or m["Source"],
                    "target": m["Destination"], "read_only": not m["RW"]}
                   for m in mounts], key=lambda m: (m["target"], m["source"]))


def main():
    if sys.argv[1:] not in ([], ["--database-readonly"]):
        raise ValueError("UNKNOWN_READ_ONLY_OPTION")
    report = {"scope": "READ_ONLY_EXISTING_RC3", "deployment_performed": False,
              "credential_values_printed": False, "containers": [], "blockers": []}
    all_containers = []
    for project in (STAGE, PROD):
        ids = command(["ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]).split()
        if not ids:
            report["blockers"].append("PROJECT_NOT_FOUND:" + project)
            continue
        for c in json.loads(command(["inspect", *ids])):
            labels, state, host = c["Config"].get("Labels") or {}, c["State"], c["HostConfig"]
            all_containers.append(c)
            report["containers"].append({
                "project": project, "service": labels.get("com.docker.compose.service"),
                "id": c["Id"], "name": c["Name"], "image": c["Image"],
                "status": state["Status"], "exit_code": state["ExitCode"],
                "health": (state.get("Health") or {}).get("Status", "none"),
                "started": state["StartedAt"], "restarts": c["RestartCount"],
                "oom": state["OOMKilled"], "user": c["Config"].get("User", ""),
                "memory": host["Memory"], "memory_plus_swap": host["MemorySwap"],
                "mounts": mount_summary(c.get("Mounts", []))})
    prod_sources = {m["Source"] for c in all_containers
                    if (c["Config"].get("Labels") or {}).get("com.docker.compose.project") == PROD
                    for m in c.get("Mounts", [])}
    shared = sorted({m["Source"] for c in all_containers
                     if (c["Config"].get("Labels") or {}).get("com.docker.compose.project") == STAGE
                     for m in c.get("Mounts", []) if m["Source"] in prod_sources})
    report["stage_production_shared_mount_sources"] = shared
    if shared:
        report["blockers"].append("STAGE_PRODUCTION_SHARED_MOUNT_SOURCE")
    report["files"] = [{"path": str(p), "exists": p.is_file(),
                         "mode": oct(p.stat().st_mode & 0o777) if p.exists() else None}
                        for p in [*FILES, ENVFILE]]
    missing = [str(p) for p in [*FILES, ENVFILE] if not p.is_file()]
    if missing:
        report["blockers"].append("REQUIRED_BINDING_FILE_MISSING")
    else:
        # Use existing files, research profile and the required memory overlay LAST.
        args = ["compose", "--project-directory", str(RELEASE / "ops"),
                "--env-file", str(ENVFILE), "-p", STAGE, "--profile", "research"]
        for p in FILES:
            args += ["-f", str(p)]
        # Shell overrides are not allowed to replace the identified file bindings.
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith(("NEXUS_", "HUB_", "COMPOSE_"))}
        config = json.loads(command([*args, "config", "--format", "json"], env=clean_env))
        report["rendered_project"] = config.get("name")
        report["memory_overlay_last"] = str(FILES[-1])
        report["services"] = {}
        if config.get("name") != STAGE:
            report["blockers"].append("RENDERED_PROJECT_MISMATCH")
        for name in ("api", "research-worker", "postgres", "ingress"):
            service = config["services"][name]
            current = [c for c in all_containers
                       if (c["Config"].get("Labels") or {}).get("com.docker.compose.project") == STAGE
                       and c["Config"]["Labels"].get("com.docker.compose.service") == name
                       and not c["Config"]["Labels"].get("com.docker.compose.oneoff", "false").lower() == "true"]
            expected_mounts = []
            for v in service.get("volumes", []):
                source = v["source"]
                if v["type"] == "volume":
                    source = config["volumes"][source].get("name", STAGE + "_" + source)
                expected_mounts.append({"type": v["type"], "source": source,
                                        "target": v["target"], "read_only": v.get("read_only", False)})
            expected_mounts.sort(key=lambda m: (m["target"], m["source"]))
            check = {"image_reference": service["image"], "configured_user": service.get("user", "IMAGE_DEFAULT"),
                     "memory": byte_count(service.get("mem_limit")), "memory_plus_swap": byte_count(service.get("memswap_limit")),
                     "ports": service.get("ports", []), "mounts": expected_mounts,
                     "profiles": service.get("profiles", [])}
            env = service.get("environment") or {}
            check["safe_controls"] = {k: env[k] for k in FLAGS if k in env}
            if len(current) != 1:
                report["blockers"].append("EXPECTED_ONE_CURRENT_SERVICE:" + name)
            else:
                c = current[0]
                actual_env = environment(c["Config"].get("Env"))
                changed = sorted(k for k, v in env.items()
                                 if (actual_env.get(k) != str(v) if v is not None else k in actual_env))
                check["environment_diff_keys"] = changed
                check["configured_environment_preserved"] = not changed
                check["mounts_preserved"] = expected_mounts == mount_summary(c.get("Mounts", []))
                image = json.loads(command(["image", "inspect", c["Image"]]))[0]
                expected_user = service.get("user", image["Config"].get("User", ""))
                check["runtime_user_preserved"] = str(expected_user) == c["Config"].get("User", "")
                check["current_image_revision"] = (image["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
                check["current_image_source_sha256"] = (image["Config"].get("Labels") or {}).get("nexus.source.sha256")
                if changed or not check["mounts_preserved"] or not check["runtime_user_preserved"]:
                    report["blockers"].append("CURRENT_CONFIG_DIFFERENCE_REQUIRES_REVIEW:" + name)
            if name == "api" and (check["memory"], check["memory_plus_swap"]) != (1610612736, 1610612736):
                report["blockers"].append("API_1536_MIB_OVERRIDE_NOT_PRESERVED")
            report["services"][name] = check
        report["networks"] = {k: {"name": v.get("name"), "internal": v.get("internal", False)}
                              for k, v in config.get("networks", {}).items()}
    report["disk_free_bytes"] = shutil.disk_usage("/").free
    if sys.argv[1:] == ["--database-readonly"] and not report["blockers"]:
        candidates = [c for c in all_containers
                      if (c["Config"].get("Labels") or {}).get("com.docker.compose.project") == STAGE
                      and c["Config"]["Labels"].get("com.docker.compose.service") == "api"
                      and c["State"]["Status"] == "running"]
        if len(candidates) != 1:
            report["blockers"].append("EXPECTED_ONE_RUNNING_STAGE_API_FOR_DATABASE_CHECK")
        else:
            # No application imports, migrations, DDL/DML, job processing or production exec.
            report["database_read_only"] = json.loads(command(
                ["exec", "-i", candidates[0]["Id"], "python", "-B", "-"],
                input_text=DATABASE_READONLY_CODE))
            report["blockers"] += report["database_read_only"]["blockers"]
    report["acceptance_and_backups"] = "NOT_TESTED_BY_THIS_READ_ONLY_CHECK"
    print(json.dumps(report, indent=2))
    return 1 if report["blockers"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Do not print interpolated config, subprocess stderr or exception values.
        reason = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith("DOCKER_READ_FAILED:") else type(exc).__name__
        print(json.dumps({"scope": "READ_ONLY_EXISTING_RC3", "status": "BLOCKED", "reason": reason,
                          "deployment_performed": False, "credential_values_printed": False}))
        sys.exit(1)
