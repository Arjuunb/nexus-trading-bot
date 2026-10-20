"""Private bounded RC3 SQLite backup/recovery. Default launches a logged job.

Only generic operator code belongs in git. All runtime reports and backups stay
private on the existing staging host. No application imports or service update.
"""
from datetime import datetime, timezone
import fcntl
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
API = STAGE + "-api-1"
BASE = Path("/home/ubuntu/nexus-rc3-staging-28acdbc")
RESERVE = 8 * 1024 ** 3
ARCHIVE_LIMIT = 4 * 1024 ** 3

CHILD = r'''
from contextlib import closing
import fcntl,gzip,hashlib,json,os,re,shutil,sqlite3,stat,sys,time
from pathlib import Path
ROOT=Path('/var/lib/tradexa')
token=os.environ['NEXUS_TASK6_RECOVERY_TOKEN']
assert re.fullmatch('[0-9a-f]{32}',token)
reserve=int(os.environ['NEXUS_TASK6_RECOVERY_RESERVE'])
archive_limit=int(os.environ['NEXUS_TASK6_RECOVERY_ARCHIVE_LIMIT'])
assert reserve>=8*1024**3 and 0<archive_limit<=4*1024**3
work=ROOT/('.task6-sqlite-recovery-'+token)
raw=work/'owned-snapshot.sqlite'
manifest={'scope':'EXISTING_RC3_CUSTOMER_STATE_BACKUP','status':'RUNNING','token':token,
          'source_database_writes':False,'new_paper_execution':False,'entries':[],
          'non_sqlite_entries':[],'archive_limit_bytes':archive_limit,'reserve_bytes':reserve}
current_phase='STARTING';current_source=None;last_log=0;last_space=0;deadline=0;guard_error=None

def log(phase,**extra):
    print(json.dumps({'phase':phase,'source':current_source,**extra}),flush=True)

def save():
    temporary=work/'manifest.tmp'
    temporary.write_text(json.dumps(manifest,indent=2))
    os.replace(temporary,work/'manifest.json')

def guard(force=False):
    global last_space,last_log,guard_error
    now=time.monotonic()
    if deadline and now>deadline:
        guard_error='PHASE_TIME_BOUND_EXCEEDED';raise RuntimeError(guard_error)
    if force or now-last_space>2:
        if shutil.disk_usage(ROOT).free<reserve:
            guard_error='FREE_DISK_RESERVE_REACHED';raise RuntimeError(guard_error)
        last_space=now
    if now-last_log>15:
        log(current_phase,free_bytes=shutil.disk_usage(ROOT).free)
        last_log=now

def phase(name):
    global current_phase,deadline,last_log
    current_phase=name;deadline=time.monotonic()+3600;last_log=time.monotonic()
    guard(True);log(name)

def digest_file(path):
    digest=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):
            guard();digest.update(chunk)
    return digest.hexdigest()

def progress():
    guard();return 0

def quote(name):return '"'+name.replace('"','""')+'"'

def financial_fingerprint(db):
    tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    names={'orders','positions','balances','paper_trades','paper_positions','financial_journal',
           'trade_decision_journal','trade_decision_events','pa_sessions','pa_order_meta','pa_funding_events',
           'smc_sessions','smc_order_meta','smc_funding_events'}
    result={}
    for table in tables:
        if not (table.startswith('v2_') or table in names):continue
        info=db.execute('PRAGMA table_info('+quote(table)+')').fetchall()
        primary=[r[1] for r in sorted(info,key=lambda r:r[5]) if r[5]]
        order=','.join(quote(k) for k in primary) if primary else 'rowid'
        digest=hashlib.sha256();count=0
        for row in db.execute('SELECT * FROM '+quote(table)+' ORDER BY '+order):
            encoded=json.dumps(list(row),ensure_ascii=True,separators=(',',':'),
                               default=lambda value:{'bytes_hex':value.hex()}).encode()
            digest.update(encoded+b'\n');count+=1
            if count%1000==0:guard()
        result[table]={'rows':count,'sha256':digest.hexdigest()}
    return result

def open_readonly(path):
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5,isolation_level=None)
    db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-2048')
    db.set_progress_handler(progress,10000)
    return db

def schema_proof(db):
    rows=db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    return {'sha256':hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest(),
            'user_version':db.execute('PRAGMA user_version').fetchone()[0]}

def owned_raw_remove():
    marker=json.loads((work/'ownership.json').read_text())
    if marker.get('token')!=token or raw.parent!=work or raw.is_symlink():
        raise RuntimeError('OWNED_TEMPORARY_FILE_GUARD_FAILED')
    # Only this job's newly created snapshot/restore file, never an original.
    raw.unlink()

def compress(source,target,used):
    with source.open('rb') as src,target.open('xb') as dst:
        with gzip.GzipFile(fileobj=dst,mode='wb',compresslevel=6,mtime=0) as packed:
            for chunk in iter(lambda:src.read(1024*1024),b''):
                guard();packed.write(chunk)
                if used+dst.tell()>archive_limit:raise RuntimeError('COMPRESSED_ARCHIVE_LIMIT_REACHED')
        if used+dst.tell()>archive_limit:raise RuntimeError('COMPRESSED_ARCHIVE_LIMIT_REACHED')

def unpack(archive,destination,expected_bytes,write):
    digest=hashlib.sha256();total=0
    with gzip.open(archive,'rb') as source:
        target=destination.open('xb') if write else None
        try:
            for chunk in iter(lambda:source.read(1024*1024),b''):
                guard();total+=len(chunk)
                if total>expected_bytes:raise RuntimeError('RESTORE_EXPANSION_BOUND_EXCEEDED')
                if target:target.write(chunk)
                digest.update(chunk)
        finally:
            if target:target.close()
    if total!=expected_bytes:raise RuntimeError('RESTORE_BYTE_COUNT_MISMATCH')
    return digest.hexdigest()

lock=None
try:
    os.umask(0o077);os.nice(15)
    lock=os.open(str(ROOT/'.task6-sqlite-recovery.lock'),os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    work.mkdir(mode=0o700)
    (work/'ownership.json').write_text(json.dumps({'token':token,'purpose':'TASK6_OWNED_TEMPORARIES_ONLY'}))
    memory_max=Path('/sys/fs/cgroup/memory.max')
    memory_stat=Path('/sys/fs/cgroup/memory.stat')
    if memory_max.is_file() and memory_stat.is_file():
        limit=memory_max.read_text().strip()
        stats=dict(line.split() for line in memory_stat.read_text().splitlines())
        resident=int(stats.get('anon','0'))+int(stats.get('kernel','0'))
        manifest['cgroup_memory_before']={'limit':limit,'anon_plus_kernel_bytes':resident}
        if limit.isdigit() and int(limit)-resident<128*1024**2:
            raise RuntimeError('API_MEMORY_HEADROOM_BELOW_128_MIB')
    roots=[ROOT,Path('/workers/research'),Path('/workers/shadow')]
    sources=[];other=[]
    for index,root in enumerate(roots):
        if not root.is_dir():raise RuntimeError('EXPECTED_STATE_ROOT_MISSING')
        for base,dirs,files in os.walk(root,followlinks=False):
            if any((Path(base)/d).is_symlink() for d in dirs):
                raise RuntimeError('SOURCE_DIRECTORY_SYMLINK_REQUIRES_REVIEW')
            dirs[:]=sorted(d for d in dirs if not d.startswith('.task6-sqlite-recovery-') and d not in ('backups','verified-backup','verified-restore'))
            for name in sorted(files):
                path=Path(base)/name
                if path.name=='.task6-sqlite-recovery.lock':continue
                if path.is_symlink():raise RuntimeError('SOURCE_SYMLINK_REQUIRES_REVIEW')
                if not stat.S_ISREG(path.stat().st_mode):raise RuntimeError('NON_REGULAR_SOURCE_REQUIRES_REVIEW')
                companion=next((suffix for suffix in ('-wal','-shm','-journal') if name.endswith(suffix)),None)
                if companion:
                    primary=path.with_name(name[:-len(companion)])
                    if primary.is_file() and primary.suffix in ('.db','.sqlite','.sqlite3'):continue
                if len(sources)+len(other)>=512:raise RuntimeError('SOURCE_FILE_COUNT_BOUND_EXCEEDED')
                entry=(index,path,str(path.relative_to(root)))
                if path.suffix in ('.db','.sqlite','.sqlite3'):sources.append(entry)
                else:other.append(entry)
    if not sources:raise RuntimeError('NO_SQLITE_SOURCES_FOUND')
    # Largest first: prove the disk bound before accumulating compressed files.
    sources.sort(key=lambda row:row[1].stat().st_size,reverse=True)
    manifest.update(source_database_count=len(sources),other_file_count=len(other));save()
    used=0
    for number,(root_index,source,relative) in enumerate(sources,1):
        current_source=str(source);phase('SNAPSHOT')
        archive=work/(str(number).zfill(3)+'.sqlite.gz')
        if raw.exists():raise RuntimeError('OWNED_SCRATCH_SLOT_NOT_EMPTY')
        with closing(open_readonly(source)) as original:
            original.execute('BEGIN');original.execute('SELECT count(*) FROM sqlite_master').fetchone()
            estimated=original.execute('PRAGMA page_count').fetchone()[0]*original.execute('PRAGMA page_size').fetchone()[0]
            # Reserve the full archive cap as well as one uncompressed snapshot.
            if shutil.disk_usage(ROOT).free<estimated+archive_limit-used+reserve:
                raise RuntimeError('SQLITE_SNAPSHOT_AND_ARCHIVE_SPACE_BOUND_FAILED')
            reference=financial_fingerprint(original)
            with closing(sqlite3.connect(str(raw))) as destination:
                def backup_progress(status,remaining,total):
                    guard()
                    if status not in (sqlite3.SQLITE_OK,sqlite3.SQLITE_DONE,sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                        raise RuntimeError('SQLITE_BACKUP_STATUS_FAILED')
                    time.sleep(0.002)
                original.backup(destination,pages=256,progress=backup_progress,sleep=0.05)
            original.rollback()
        # Only the newly owned copy is made standalone; source WAL is untouched.
        with closing(sqlite3.connect(raw.as_uri()+'?mode=rw',uri=True)) as standalone:
            if standalone.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower()!='delete':
                raise RuntimeError('SNAPSHOT_NOT_STANDALONE')
        phase('FULL_INTEGRITY_CHECK')
        with closing(open_readonly(raw)) as snapshot:
            if snapshot.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
                raise RuntimeError('SQLITE_SNAPSHOT_INTEGRITY_FAILED')
            if snapshot.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise RuntimeError('SQLITE_SNAPSHOT_FOREIGN_KEY_CHECK_FAILED')
            fingerprint=financial_fingerprint(snapshot)
            if fingerprint!=reference:raise RuntimeError('SNAPSHOT_FINANCIAL_RECORDS_DIFFER')
            schema=schema_proof(snapshot)
        phase('COMPRESS_AND_VERIFY')
        size=raw.stat().st_size;sha=digest_file(raw)
        compress(raw,archive,used)
        if unpack(archive,raw,size,False)!=sha:raise RuntimeError('COMPRESSED_SNAPSHOT_HASH_MISMATCH')
        used+=archive.stat().st_size
        owned_raw_remove()
        phase('RESTORE_AND_VERIFY')
        if shutil.disk_usage(ROOT).free<size+reserve:raise RuntimeError('SQLITE_RESTORE_SPACE_BOUND_FAILED')
        if unpack(archive,raw,size,True)!=sha:raise RuntimeError('RESTORED_SNAPSHOT_HASH_MISMATCH')
        with closing(open_readonly(raw)) as restored:
            if schema_proof(restored)!=schema or financial_fingerprint(restored)!=reference:
                raise RuntimeError('RESTORED_SQLITE_SCHEMA_OR_FINANCIAL_RECORDS_DIFFER')
        owned_raw_remove()
        item={'source_root':root_index,'source_relative_path':relative,'archive':archive.name,
              'snapshot_bytes':size,'snapshot_sha256':sha,'archive_bytes':archive.stat().st_size,
              'archive_sha256':digest_file(archive),'full_integrity':'ok','foreign_keys':'ok',
              'restored_bytes_schema_and_financial_records_match':True,'financial_records':reference,'schema':schema}
        manifest['entries'].append(item);manifest['compressed_bytes']=used;save()
        log('DATABASE_RECOVERY_VERIFIED',completed=number,total=len(sources),archive_bytes=item['archive_bytes'])
    for number,(root_index,source,relative) in enumerate(other,1):
        current_source=str(source);phase('PRESERVE_OTHER_STATE_FILE')
        before=source.stat();archive=work/('other-'+str(number).zfill(3)+'.gz')
        sha=digest_file(source);compress(source,archive,used)
        if unpack(archive,raw,before.st_size,False)!=sha:raise RuntimeError('OTHER_STATE_FILE_HASH_MISMATCH')
        used+=archive.stat().st_size
        manifest['non_sqlite_entries'].append({'source_root':root_index,'source_relative_path':relative,
            'archive':archive.name,'source_bytes':before.st_size,'source_sha256':sha,
            'archive_bytes':archive.stat().st_size,'archive_sha256':digest_file(archive)})
        save()
    current_source=None;phase('CHECK_SOURCE_FINANCIAL_RECORDS_UNCHANGED')
    for item in manifest['entries']:
        path=roots[item['source_root']]/item['source_relative_path']
        with closing(open_readonly(path)) as original:
            if financial_fingerprint(original)!=item['financial_records']:
                raise RuntimeError('SOURCE_FINANCIAL_RECORDS_CHANGED_DURING_RECOVERY')
    manifest.update(status='SQLITE_BACKUP_AND_RESTORE_PASSED',compressed_bytes=used,
                    recovery_scope='LOCAL_SAME_HOST; EACH_DATABASE_CONSISTENT_SNAPSHOT',
                    sources_financial_records_unchanged=True,free_bytes=shutil.disk_usage(ROOT).free)
    save();log(manifest['status'],databases=len(sources),compressed_bytes=used)
except Exception as error:
    manifest.update(status='BLOCKED',phase=current_phase,source=current_source,error_type=type(error).__name__,
                    reason=str(error) if isinstance(error,RuntimeError) else ('ANOTHER_SQLITE_BACKUP_ALREADY_RUNNING' if isinstance(error,BlockingIOError) else guard_error),
                    sqlite_code=getattr(error,'sqlite_errorcode',None),sqlite_name=getattr(error,'sqlite_errorname',None))
    if work.is_dir() and (work/'ownership.json').is_file():save()
    log('BLOCKED',error_type=type(error).__name__,reason=manifest.get('reason'),sqlite_name=manifest.get('sqlite_name'))
    sys.exit(1)
finally:
    if lock is not None:os.close(lock)
'''


class Blocked(RuntimeError):
    pass


def docker(args, *, input_text=None, stream=False):
    result = subprocess.run(["docker", *args], input=input_text, text=True,
                            stdout=None if stream else subprocess.PIPE,
                            stderr=None if stream else subprocess.PIPE)
    if result.returncode:
        raise Blocked("DOCKER_OPERATION_FAILED:" + args[0] + ":exit=" + str(result.returncode))
    return result.stdout or ""


def project_containers(project):
    ids = docker(["ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]).split()
    if not ids:
        raise Blocked("EXPECTED_PROJECT_NOT_FOUND:" + project)
    return json.loads(docker(["inspect", *ids]))


def selected_snapshot(rows):
    return sorted([{"id": c["Id"], "image": c["Image"], "started": c["State"]["StartedAt"],
                    "restarts": c["RestartCount"], "mounts": c["Mounts"]} for c in rows], key=lambda r: r["id"])


def environment(container):
    return dict(value.split("=", 1) for value in container["Config"].get("Env", []) if "=" in value)


def preflight():
    stage = project_containers(STAGE)
    prod = project_containers(PROD)
    api = [c for c in stage if c["Name"] == "/" + API]
    if len(api) != 1 or api[0]["State"]["Status"] != "running":
        raise Blocked("ONE_RUNNING_EXISTING_STAGE_API_REQUIRED")
    api = api[0]
    if (api["State"].get("Health") or {}).get("Status") != "healthy":
        raise Blocked("EXISTING_STAGE_API_MUST_BE_HEALTHY")
    if api["HostConfig"]["Memory"] != 1610612736 or api["HostConfig"]["MemorySwap"] != 1610612736:
        raise Blocked("EXISTING_API_MEMORY_LIMIT_CHANGED")
    prod_sources = {m["Source"] for c in prod for m in c["Mounts"]}
    if any(m["Source"] in prod_sources for c in stage for m in c["Mounts"]):
        raise Blocked("STAGING_SHARES_PRODUCTION_STORAGE")
    mounts = api["Mounts"]
    expected = {"/var/lib/tradexa": STAGE + "_api-state", "/workers/research": STAGE + "_research-state",
                "/workers/shadow": STAGE + "_shadow-state"}
    if any(not any(m["Destination"] == target and m.get("Name") == volume for m in mounts)
           for target, volume in expected.items()):
        raise Blocked("EXISTING_STAGE_STATE_VOLUMES_REQUIRED")
    env = environment(api)
    for key in ("HUB_AUTO_ENGINE", "HUB_ENABLE_EXTERNAL_LIVE", "HUB_USE_LIVE_DATA", "HUB_RESEARCH_AUTOSTART", "NEXUS_SHADOW_SUPERVISED"):
        if env.get(key) != "0":
            raise Blocked("EXPECTED_DISABLED_CONTROL:" + key)
    if shutil.disk_usage(BASE).free < RESERVE + ARCHIVE_LIMIT:
        raise Blocked("SQLITE_ARCHIVE_AND_RESERVE_SPACE_UNAVAILABLE")
    # Presence/equality only. Never reveal endpoints or credentials.
    prod_envs = [environment(c) for c in prod]
    remote = bool(env.get("SUPABASE_URL") and env.get("SUPABASE_KEY"))
    provider = {"external_financial_backend_configured": remote,
                "financial_endpoint_matches_production": remote and any(
                    p.get("SUPABASE_URL") == env["SUPABASE_URL"] and p.get("SUPABASE_KEY") for p in prod_envs),
                "financial_credential_matches_production": remote and any(
                    p.get("SUPABASE_URL") == env["SUPABASE_URL"] and p.get("SUPABASE_KEY") == env["SUPABASE_KEY"] for p in prod_envs)}
    return api, selected_snapshot(prod), provider


def write_status(directory, report):
    temporary = directory / "status.tmp"
    temporary.write_text(json.dumps(report, indent=2))
    os.replace(temporary, directory / "status.json")


def run_job(directory):
    if directory.parent != BASE or not directory.name.startswith("task6-sqlite-recovery-") or directory.is_symlink():
        raise Blocked("PRIVATE_RECOVERY_DIRECTORY_REQUIRED")
    config = json.loads((directory / "job.json").read_text())
    token = config["token"]
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise Blocked("RECOVERY_TOKEN_INVALID")
    report = {"status": "RUNNING", "scope": "EXISTING_RC3_SQLITE_LOCAL_RECOVERY", "backup_directory": str(directory),
              "services_updated": False, "new_paper_execution": False, "task6_complete": False}
    try:
        api, prod, provider = preflight()
        report.update(production_before=prod, external_financial_provider=provider)
        if api["Id"] != config["api_id"]:
            raise Blocked("STAGE_API_CHANGED_BEFORE_BACKUP")
        report["phase"] = "SQLITE_SNAPSHOTS_INTEGRITY_AND_RESTORE"
        write_status(directory, report)
        docker(["exec", "-i", "--env", "NEXUS_TASK6_RECOVERY_TOKEN=" + token,
                "--env", "NEXUS_TASK6_RECOVERY_RESERVE=" + str(RESERVE),
                "--env", "NEXUS_TASK6_RECOVERY_ARCHIVE_LIMIT=" + str(ARCHIVE_LIMIT),
                api["Id"], "python", "-B", "-"], input_text=CHILD, stream=True)
        container_dir = "/var/lib/tradexa/.task6-sqlite-recovery-" + token
        manifest_code = "from pathlib import Path; print(Path(" + repr(container_dir + "/manifest.json") + ").read_text())"
        manifest = json.loads(docker(["exec", api["Id"], "python", "-B", "-c", manifest_code]))
        if manifest["status"] != "SQLITE_BACKUP_AND_RESTORE_PASSED":
            raise Blocked("SQLITE_CHILD_RECOVERY_NOT_VERIFIED")
        # At this point the large scratch file is gone. Copy only capped archives.
        if shutil.disk_usage(BASE).free < manifest["compressed_bytes"] + RESERVE:
            raise Blocked("PRIVATE_HOST_ARCHIVE_COPY_SPACE_UNAVAILABLE")
        report["phase"] = "COPY_VERIFIED_ARCHIVES_TO_PRIVATE_HOST_DIRECTORY"
        write_status(directory, report)
        destination = directory / "sqlite"
        docker(["cp", api["Id"] + ":" + container_dir, str(destination)])
        for item in [*manifest["entries"], *manifest["non_sqlite_entries"]]:
            path = destination / item["archive"]
            if path.is_symlink() or path.parent != destination:
                raise Blocked("ARCHIVE_PATH_INVALID")
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != item["archive_sha256"]:
                raise Blocked("HOST_ARCHIVE_COPY_HASH_MISMATCH")
            path.chmod(0o600)
        if selected_snapshot(project_containers(PROD)) != prod:
            raise Blocked("PRODUCTION_CONTAINER_IDENTITY_CHANGED")
        api_after, _, provider_after = preflight()
        if (api_after["Id"], api_after["Image"], api_after["State"]["StartedAt"], api_after["RestartCount"]) != (api["Id"], api["Image"], api["State"]["StartedAt"], api["RestartCount"]) or provider_after != provider:
            raise Blocked("STAGE_API_OR_PROVIDER_CONFIGURATION_CHANGED")
        report.update(status="SQLITE_BACKUP_AND_RESTORE_PASSED", phase="COMPLETE",
                      databases_verified=len(manifest["entries"]), other_files_preserved=len(manifest["non_sqlite_entries"]),
                      compressed_bytes=manifest["compressed_bytes"], full_integrity_and_foreign_keys="PASSED",
                      restored_bytes_schema_financial_records="MATCH", source_financial_records="UNCHANGED",
                      production_container_identities="UNCHANGED", local_same_host_recovery_only=True,
                      api_health=api_after["State"]["Health"]["Status"], api_restarts_unchanged=True,
                      free_disk_bytes_after=shutil.disk_usage(BASE).free,
                      deployment_requires_provider_and_execution_intent_verification=True)
    except Exception as error:
        report.update(status="BLOCKED", error_type=type(error).__name__,
                      reason=str(error) if isinstance(error, Blocked) else None)
    write_status(directory, report)
    print(json.dumps({k: v for k, v in report.items() if k != "production_before"}, indent=2), flush=True)
    return 0 if report["status"] == "SQLITE_BACKUP_AND_RESTORE_PASSED" else 1


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        return run_job(Path(sys.argv[2]))
    if sys.argv[1:]:
        raise Blocked("NO_TARGET_OVERRIDES_ACCEPTED")
    api, _, provider = preflight()
    token = uuid.uuid4().hex
    directory = BASE / ("task6-sqlite-recovery-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + token[:12])
    directory.mkdir(mode=0o700)
    (directory / "job.json").write_text(json.dumps({"token": token, "api_id": api["Id"]}))
    runner = directory / "runner.py"
    shutil.copyfile(Path(__file__), runner)
    write_status(directory, {"status": "STARTING", "backup_directory": str(directory), "task6_complete": False})
    with (directory / "recovery.log").open("xb") as log:
        process = subprocess.Popen([sys.executable, "-u", str(runner), "--run", str(directory)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True)
    print(json.dumps({"status": "SQLITE_RECOVERY_JOB_STARTED", "backup_directory": str(directory),
                      "pid": process.pid, "status_file": str(directory / "status.json"),
                      "progress_log": str(directory / "recovery.log"), "services_updated": False,
                      "external_financial_provider": provider,
                      "new_paper_execution": False, "task6_complete": False}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"status": "BLOCKED", "error_type": type(error).__name__,
                          "reason": str(error) if isinstance(error, Blocked) else None,
                          "services_updated": False, "task6_complete": False}))
        sys.exit(1)
