"""Resume only the first owned SQLite snapshot after a bounded integrity timeout.

The original, checksum-pinned helper supplies all backup, restore, financial
comparison and staging-isolation checks. This wrapper adds a narrowly checked
resume path, a 32 MiB SQLite cache, process memory/time bounds and VM counters.
Actual operational evidence stays private on the existing staging host.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import types

BASE = Path('/home/ubuntu/nexus-rc3-staging-28acdbc')
ORIGINAL_SHA256 = '9f2c04702418d57c75dae191895b4c199bad558b2b434738ddcd736a5cdbfb3d'


class Blocked(RuntimeError):
    pass


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise Blocked('PINNED_HELPER_ANCHOR_MISMATCH')
    return source.replace(old, new, 1)


def build_child(source):
    tree = ast.parse(source)
    assignments = [n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'CHILD' for t in n.targets)]
    if len(assignments) != 1:
        raise Blocked('PINNED_CHILD_REQUIRED')
    child = ast.literal_eval(assignments[0].value)
    child = replace_once(child, 'import fcntl,gzip,hashlib,json,os,re,shutil,sqlite3,stat,sys,time',
        'import fcntl,gzip,hashlib,json,os,re,resource,shutil,sqlite3,stat,sys,time')
    child = replace_once(child, "guard_error=None\n", "guard_error=None\nphase_started=0;vm_steps=0;job_started=time.monotonic();resume_validated=False\n")
    child = replace_once(child, 'if deadline and now>deadline:',
        'if (deadline and now>deadline) or now-job_started>8*3600:')
    child = replace_once(child, '        last_space=now',
        """        if memory_max.is_file() and memory_stat.is_file():
            limit=memory_max.read_text().strip()
            stats=dict(line.split() for line in memory_stat.read_text().splitlines())
            resident=int(stats.get('anon','0'))+int(stats.get('kernel','0'))
            if limit.isdigit() and int(limit)-resident<128*1024**2:
                guard_error='API_MEMORY_RESERVE_BELOW_128_MIB';raise RuntimeError(guard_error)
        last_space=now""")
    child = replace_once(child, 'log(current_phase,free_bytes=shutil.disk_usage(ROOT).free)',
        'log(current_phase,free_bytes=shutil.disk_usage(ROOT).free,elapsed_seconds=round(now-phase_started,1),'
        'sqlite_vm_steps=vm_steps,cpu_seconds=round(time.process_time(),1),'
        'peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)')
    child = replace_once(child, 'global current_phase,deadline,last_log',
        'global current_phase,deadline,last_log,phase_started,vm_steps')
    child = replace_once(child,
        'current_phase=name;deadline=time.monotonic()+3600;last_log=time.monotonic()',
        'current_phase=name;phase_started=time.monotonic();deadline=phase_started+4*3600;last_log=phase_started;vm_steps=0')
    child = replace_once(child, 'def progress():\n    guard();return 0',
        'def progress():\n    global vm_steps\n    vm_steps+=10000\n    guard();return 0')
    child = replace_once(child, "db.execute('PRAGMA cache_size=-2048')", "db.execute('PRAGMA cache_size=-32768')")
    child = replace_once(child, 'os.umask(0o077);os.nice(15)',
        'os.umask(0o077);os.nice(15)\n    resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))')
    child = replace_once(child,
        "    work.mkdir(mode=0o700)\n    (work/'ownership.json').write_text(json.dumps({'token':token,'purpose':'TASK6_OWNED_TEMPORARIES_ONLY'}))",
        """    if work.is_symlink() or not work.is_dir():raise RuntimeError('OWNED_RESUME_DIRECTORY_REQUIRED')
    for name in ('ownership.json','manifest.json','owned-snapshot.sqlite'):
        path=work/name
        if path.is_symlink() or not path.is_file():raise RuntimeError('OWNED_RESUME_FILES_REQUIRED')
    marker=json.loads((work/'ownership.json').read_text())
    previous=json.loads((work/'manifest.json').read_text())
    if marker!={'token':token,'purpose':'TASK6_OWNED_TEMPORARIES_ONLY'}:
        raise RuntimeError('OWNED_RESUME_MARKER_MISMATCH')
    if (previous.get('token')!=token or previous.get('status')!='BLOCKED'
        or previous.get('phase')!='FULL_INTEGRITY_CHECK'
        or previous.get('reason')!='PHASE_TIME_BOUND_EXCEEDED'
        or previous.get('entries') or previous.get('non_sqlite_entries')):
        raise RuntimeError('FIRST_SNAPSHOT_INTEGRITY_TIMEOUT_REQUIRED')
    if set(p.name for p in work.iterdir())!={'ownership.json','manifest.json','owned-snapshot.sqlite'}:
        raise RuntimeError('UNEXPECTED_RESUME_DIRECTORY_CONTENTS')
    if raw.stat().st_size==0:raise RuntimeError('EMPTY_RESUME_SNAPSHOT')
    resume_validated=True
    with (work/'manifest.before-resume.json').open('x') as proof:
        json.dump(previous,proof,indent=2)
    manifest.update(resumed_owned_first_snapshot=True,sqlite_cache_mib=32,
                    phase_time_limit_seconds=4*3600,job_time_limit_seconds=8*3600,
                    process_address_space_limit_bytes=256*1024**2)
""")
    child = replace_once(child, "int(limit)-resident<128*1024**2:\n            raise RuntimeError('API_MEMORY_HEADROOM_BELOW_128_MIB')",
        "int(limit)-resident<256*1024**2:\n            raise RuntimeError('API_MEMORY_HEADROOM_BELOW_256_MIB')")
    child = replace_once(child, "    if work.is_dir() and (work/'ownership.json').is_file():save()",
        '    if resume_validated:save()')
    child = replace_once(child, "    manifest.update(source_database_count=len(sources),other_file_count=len(other));save()",
        """    if previous.get('source_database_count')!=len(sources) or previous.get('source')!=str(sources[0][1]):
        raise RuntimeError('RESUME_SOURCE_INVENTORY_CHANGED')
    manifest.update(source_database_count=len(sources),other_file_count=len(other));save()""")
    child = replace_once(child, "        if raw.exists():raise RuntimeError('OWNED_SCRATCH_SLOT_NOT_EMPTY')",
        """        reuse=(number==1)
        if raw.exists()!=reuse:raise RuntimeError('OWNED_RESUME_SCRATCH_SLOT_INVALID')
        if reuse and raw.is_symlink():raise RuntimeError('OWNED_RESUME_SNAPSHOT_SYMLINK')""")
    child = replace_once(child, 'if shutil.disk_usage(ROOT).free<estimated+archive_limit-used+reserve:',
        'if shutil.disk_usage(ROOT).free<(0 if reuse else estimated)+archive_limit-used+reserve:')
    child = replace_once(child,
        "            with closing(sqlite3.connect(str(raw))) as destination:\n                def backup_progress(status,remaining,total):\n                    guard()\n                    if status not in (sqlite3.SQLITE_OK,sqlite3.SQLITE_DONE,sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):\n                        raise RuntimeError('SQLITE_BACKUP_STATUS_FAILED')\n                    time.sleep(0.002)\n                original.backup(destination,pages=256,progress=backup_progress,sleep=0.05)",
        """            if reuse:
                with closing(open_readonly(raw)) as existing:
                    if existing.execute('PRAGMA journal_mode').fetchone()[0].lower()!='delete':
                        raise RuntimeError('RESUME_SNAPSHOT_NOT_STANDALONE')
                    if schema_proof(existing)!=schema_proof(original) or financial_fingerprint(existing)!=reference:
                        raise RuntimeError('RESUME_SNAPSHOT_SCHEMA_OR_FINANCIAL_RECORDS_CHANGED')
                log('EXISTING_OWNED_SNAPSHOT_REUSED',snapshot_bytes=raw.stat().st_size)
            else:
                with closing(sqlite3.connect(str(raw))) as destination:
                    def backup_progress(status,remaining,total):
                        guard()
                        if status not in (sqlite3.SQLITE_OK,sqlite3.SQLITE_DONE,sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                            raise RuntimeError('SQLITE_BACKUP_STATUS_FAILED')
                        time.sleep(0.002)
                    original.backup(destination,pages=256,progress=backup_progress,sleep=0.05)""")
    compile(child, '<bounded-sqlite-resume>', 'exec')
    return child


def load_original(directory):
    runner = directory / 'runner.py'
    if runner.is_symlink() or not runner.is_file():
        raise Blocked('ORIGINAL_RUNNER_REQUIRED')
    content = runner.read_bytes()
    if hashlib.sha256(content).hexdigest() != ORIGINAL_SHA256:
        raise Blocked('ORIGINAL_RUNNER_CHECKSUM_MISMATCH')
    module = types.ModuleType('task6_original_recovery')
    module.__file__ = str(runner)
    exec(compile(content, str(runner), 'exec'), module.__dict__)
    module.Blocked = Blocked
    return module, content.decode()


def validate_directory(directory):
    if directory.parent != BASE or directory.is_symlink() or not directory.is_dir():
        raise Blocked('EXISTING_PRIVATE_RECOVERY_DIRECTORY_REQUIRED')
    if not re.fullmatch(r'task6-sqlite-recovery-\d{8}T\d{6}Z-[0-9a-f]{12}', directory.name):
        raise Blocked('EXISTING_PRIVATE_RECOVERY_DIRECTORY_REQUIRED')
    if directory.stat().st_mode & 0o077:
        raise Blocked('PRIVATE_RECOVERY_DIRECTORY_PERMISSIONS_REQUIRED')
    for name in ('job.json', 'status.json'):
        if (directory / name).is_symlink():
            raise Blocked('RECOVERY_CONTROL_FILE_SYMLINK')
    config = json.loads((directory / 'job.json').read_text())
    token = config.get('token', '')
    if not re.fullmatch('[0-9a-f]{32}', token) or not directory.name.endswith('-'+token[:12]):
        raise Blocked('ORIGINAL_RECOVERY_TOKEN_REQUIRED')
    previous = json.loads((directory / 'status.json').read_text())
    if previous.get('status') != 'BLOCKED' or previous.get('reason') != 'DOCKER_OPERATION_FAILED:exec:exit=1':
        raise Blocked('FAILED_ORIGINAL_RECOVERY_REQUIRED')
    if (directory / 'sqlite').exists():
        raise Blocked('EXISTING_HOST_BACKUP_REQUIRES_SEPARATE_REVIEW')
    return config, previous


def read_child(module, config):
    root = '/var/lib/tradexa/.task6-sqlite-recovery-'+config['token']
    code = 'from pathlib import Path; print(Path('+repr(root+'/manifest.json')+').read_text())'
    return json.loads(module.docker(['exec', config['api_id'], 'python', '-B', '-c', code]))


def prepare(directory):
    config, previous = validate_directory(directory)
    module, source = load_original(directory)
    api, production, _ = module.preflight()
    if api['Id'] != config['api_id']:
        raise Blocked('ORIGINAL_STAGE_API_REQUIRED')
    if production != previous.get('production_before'):
        raise Blocked('PRODUCTION_CONTAINER_IDENTITIES_CHANGED_SINCE_ORIGINAL_JOB')
    child = read_child(module, config)
    if (child.get('status'), child.get('phase'), child.get('reason')) != (
            'BLOCKED', 'FULL_INTEGRITY_CHECK', 'PHASE_TIME_BOUND_EXCEEDED'):
        raise Blocked('FIRST_SNAPSHOT_INTEGRITY_TIMEOUT_REQUIRED')
    if child.get('entries') or child.get('non_sqlite_entries') or child.get('token') != config['token']:
        raise Blocked('FIRST_OWNED_SNAPSHOT_REQUIRED')
    module.CHILD = build_child(source)
    return module, config


def run(directory):
    try:
        module, config = prepare(directory)
    except Exception as error:
        validate_directory(directory)
        report={'status':'BLOCKED','reason':str(error) if isinstance(error,Blocked) else None,
                'error_type':type(error).__name__,'services_updated':False,
                'new_paper_execution':False,'task6_complete':False}
        temporary=directory/'resume-status.tmp'
        temporary.write_text(json.dumps(report,indent=2))
        os.replace(temporary,directory/'resume-status.json')
        print(json.dumps(report),flush=True)
        return 1
    def resume_status(target, report):
        if report.get('status') == 'BLOCKED':
            try:
                failures=[]
                for line in (target/'resume.log').read_text().splitlines():
                    try:
                        event=json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event,dict) and event.get('phase')=='BLOCKED':
                        failures.append(event)
                if failures:
                    child=failures[-1]
                    report.update(parent_reason=report.get('reason'), reason=child.get('reason'),
                                  failure_source=child.get('source'), sqlite_name=child.get('sqlite_name'))
            except Exception:
                pass
        report.update(original_timeout_evidence_preserved=True, resume_attempt=True,
                      owned_snapshot_reused=report.get('status')=='SQLITE_BACKUP_AND_RESTORE_PASSED',
                      services_updated=False, new_paper_execution=False, task6_complete=False)
        temporary = target/'resume-status.tmp'
        temporary.write_text(json.dumps(report, indent=2))
        os.replace(temporary, target/'resume-status.json')
    module.write_status = resume_status
    return module.run_job(directory)


def main():
    os.umask(0o077)
    internal = len(sys.argv) == 3 and sys.argv[1] == '--run'
    if not internal and len(sys.argv) != 2:
        raise Blocked('EXISTING_RECOVERY_DIRECTORY_ARGUMENT_REQUIRED')
    directory = Path(sys.argv[2] if internal else sys.argv[1])
    if internal:
        return run(directory)
    prepare(directory)
    runner = directory/'resume-runner.py'
    # Exclusive create prevents accidentally launching a second resume job.
    with runner.open('x') as target:
        target.write(Path(__file__).read_text())
    (directory/'resume-status.json').write_text(json.dumps({'status':'STARTING',
        'services_updated':False,'new_paper_execution':False,'task6_complete':False}))
    with (directory/'resume.log').open('xb') as log:
        process = subprocess.Popen([sys.executable, '-u', str(runner), '--run', str(directory)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    print(json.dumps({'status':'SQLITE_RESUME_JOB_STARTED', 'pid':process.pid,
        'status_file':str(directory/'resume-status.json'), 'progress_log':str(directory/'resume.log'),
        'snapshot_reuse':'FIRST_OWNED_SNAPSHOT_AFTER_TIMEOUT', 'sqlite_cache_mib':32,
        'phase_time_limit_seconds':14400, 'job_time_limit_seconds':28800,
        'process_address_space_limit_bytes':268435456, 'services_updated':False,
        'new_paper_execution':False, 'task6_complete':False},indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        report={'status':'BLOCKED','error_type':type(error).__name__,
                'reason':str(error) if isinstance(error,Blocked) else None,
                'services_updated':False,'new_paper_execution':False,'task6_complete':False}
        print(json.dumps(report),flush=True)
        sys.exit(1)
