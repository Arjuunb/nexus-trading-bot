"""Preserve and restore-test only the earlier job's owned point-in-time copy.

This never replaces a live database or certifies a current cutover backup.
Runtime evidence and archives remain private on the existing staging host.
"""
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import types

BASE = Path('/home/ubuntu/nexus-rc3-staging-28acdbc')
ROOT = Path('/var/lib/tradexa')
ORIGINAL_SHA256 = '9f2c04702418d57c75dae191895b4c199bad558b2b434738ddcd736a5cdbfb3d'
RESERVE = 8 * 1024**3
ARCHIVE_LIMIT = 4 * 1024**3


class Blocked(RuntimeError):
    pass


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as output:
        json.dump(value, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Guard:
    def __init__(self, root, reserve=RESERVE, phase_seconds=4*3600, job_seconds=8*3600, check_memory=True):
        self.root = root
        self.reserve = reserve
        self.phase_seconds = phase_seconds
        self.job_seconds = job_seconds
        self.check_memory = check_memory
        self.started = time.monotonic()
        self.phase_started = self.started
        self.last_log = self.started
        self.last_check = 0
        self.phase_name = 'STARTING'
        self.vm_steps = 0

    def phase(self, name):
        self.phase_name = name
        self.phase_started = time.monotonic()
        self.vm_steps = 0
        self(True)
        print(json.dumps({'phase': name}), flush=True)

    def __call__(self, force=False):
        now = time.monotonic()
        if now-self.started > self.job_seconds or now-self.phase_started > self.phase_seconds:
            raise Blocked('TIME_BOUND_EXCEEDED')
        if force or now-self.last_check >= 1:
            if shutil.disk_usage(self.root).free < self.reserve:
                raise Blocked('FREE_DISK_RESERVE_REACHED')
            if self.check_memory:
                limit_path = Path('/sys/fs/cgroup/memory.max')
                stats_path = Path('/sys/fs/cgroup/memory.stat')
                if not limit_path.is_file() or not stats_path.is_file():
                    raise Blocked('CGROUP_V2_MEMORY_EVIDENCE_REQUIRED')
                limit = limit_path.read_text().strip()
                values = dict(line.split() for line in stats_path.read_text().splitlines())
                resident = int(values.get('anon', 0)) + int(values.get('kernel', 0))
                if not limit.isdigit() or int(limit)-resident < 128*1024**2:
                    raise Blocked('API_MEMORY_RESERVE_BELOW_128_MIB')
            self.last_check = now
        if now-self.last_log >= 15:
            print(json.dumps({'phase': self.phase_name, 'elapsed_seconds': round(now-self.phase_started, 1),
                'sqlite_vm_steps': self.vm_steps, 'cpu_seconds': round(time.process_time(), 1),
                'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'free_bytes': shutil.disk_usage(self.root).free}), flush=True)
            self.last_log = now

    def progress(self):
        self.vm_steps += 10000
        self()
        return 0


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def readonly(path, guard):
    # Only a validated, standalone, owned copy is opened immutable.
    db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, isolation_level=None)
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA cache_size=-32768')
    db.set_progress_handler(guard.progress, 10000)
    return db


def proof(path, guard):
    with closing(readonly(path, guard)) as db:
        schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        financial = {}
        names = {'orders', 'positions', 'balances', 'paper_trades', 'paper_positions', 'financial_journal',
            'trade_decision_journal', 'trade_decision_events', 'pa_sessions', 'pa_order_meta', 'pa_funding_events',
            'smc_sessions', 'smc_order_meta', 'smc_funding_events'}
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            if not (table.startswith('v2_') or table in names):
                continue
            columns = db.execute('PRAGMA table_info(' + quote(table) + ')').fetchall()
            keys = [c[1] for c in sorted(columns, key=lambda c: c[5]) if c[5]]
            order = ','.join(quote(k) for k in keys) if keys else 'rowid'
            digest = hashlib.sha256()
            count = 0
            for row in db.execute('SELECT * FROM ' + quote(table) + ' ORDER BY ' + order):
                digest.update(json.dumps(list(row), ensure_ascii=True, separators=(',', ':'),
                    default=lambda value: {'bytes_hex': value.hex()}).encode() + b'\n')
                count += 1
                guard()
            financial[table] = {'rows': count, 'sha256': digest.hexdigest()}
        return {'schema_sha256': hashlib.sha256(json.dumps(schema, separators=(',', ':')).encode()).hexdigest(),
            'user_version': db.execute('PRAGMA user_version').fetchone()[0], 'financial_records': financial}


def verify_archive(path, expected_size, expected_sha256, guard, destination=None):
    total = 0
    digest = hashlib.sha256()
    with gzip.open(path, 'rb') as source:
        target = destination.open('xb') if destination else None
        try:
            for chunk in iter(lambda: source.read(1024**2), b''):
                guard()
                total += len(chunk)
                if total > expected_size:
                    raise Blocked('ARCHIVE_EXPANSION_BOUND_EXCEEDED')
                digest.update(chunk)
                if target:
                    target.write(chunk)
            if target:
                target.flush()
                os.fsync(target.fileno())
        finally:
            if target:
                target.close()
    if total != expected_size or digest.hexdigest() != expected_sha256:
        raise Blocked('ARCHIVE_BYTE_ROUNDTRIP_MISMATCH')


def signature(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise Blocked('REGULAR_SINGLE_LINK_OWNED_SNAPSHOT_REQUIRED')
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def preserve(root, token, guard, archive_limit=ARCHIVE_LIMIT):
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise Blocked('ORIGINAL_RECOVERY_TOKEN_REQUIRED')
    work = root / ('.task6-sqlite-recovery-' + token)
    if root.is_symlink() or work.is_symlink() or not work.is_dir() or work.stat().st_mode & 0o077:
        raise Blocked('PRIVATE_OWNED_SNAPSHOT_DIRECTORY_REQUIRED')
    marker = work / 'ownership.json'
    if marker.is_symlink() or json.loads(marker.read_text()) != {'token': token, 'purpose': 'TASK6_OWNED_TEMPORARIES_ONLY'}:
        raise Blocked('OWNED_SNAPSHOT_MARKER_MISMATCH')
    raw = work / 'owned-snapshot.sqlite'
    identity = signature(raw)
    if any(raw.with_name(raw.name + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise Blocked('STANDALONE_DELETE_MODE_REQUIRED')
    with raw.open('rb') as source:
        header = source.read(100)
    if header[:16] != b'SQLite format 3\0' or len(header) != 100:
        raise Blocked('SQLITE_HEADER_INVALID')
    if header[18:20] != b'\x01\x01':
        raise Blocked('STANDALONE_DELETE_MODE_REQUIRED')
    target = work / 'preservation'
    if target.exists() or target.is_symlink():
        raise Blocked('PRESERVATION_ALREADY_EXISTS')
    guard(True)
    target.mkdir(mode=0o700)
    archive_part = target / 'snapshot.sqlite.gz.part'
    archive = target / 'snapshot.sqlite.gz'
    restored = target / 'restored.sqlite'
    report = {'status': 'RUNNING', 'scope': 'EARLIER_OWNED_POINT_IN_TIME_COPY_ONLY',
        'full_sqlite_recovery_passed': False, 'current_live_financial_records_backed_up': False,
        'source_database_writes': False, 'services_updated': False, 'new_paper_execution': False, 'task6_complete': False,
        'snapshot_financial_as_of_utc': None, 'owned_snapshot_file_mtime_utc': datetime.fromtimestamp(
            identity[3]/1e9, timezone.utc).isoformat(), 'original_failed_reports_preserved': True}
    try:
        guard.phase('OWNED_SNAPSHOT_SCHEMA_AND_FULL_FINANCIAL_FINGERPRINT')
        reference = proof(raw, guard)
        report.update(reference)
        atomic_json(target / 'manifest.json', report)
        guard.phase('COMPRESS_EARLIER_OWNED_SNAPSHOT')
        digest = hashlib.sha256()
        with raw.open('rb') as source, archive_part.open('xb') as output:
            with gzip.GzipFile(fileobj=output, mode='wb', compresslevel=6, mtime=0) as compressed:
                for chunk in iter(lambda: source.read(1024**2), b''):
                    guard()
                    digest.update(chunk)
                    compressed.write(chunk)
                    if output.tell() > archive_limit:
                        raise Blocked('COMPRESSED_ARCHIVE_LIMIT_REACHED')
            if output.tell() > archive_limit:
                raise Blocked('COMPRESSED_ARCHIVE_LIMIT_REACHED')
            output.flush()
            os.fsync(output.fileno())
        if signature(raw) != identity:
            raise Blocked('OWNED_SNAPSHOT_CHANGED_DURING_COMPRESSION')
        guard.phase('VERIFY_GZIP_CRC_AND_UNCOMPRESSED_SHA256')
        verify_archive(archive_part, identity[2], digest.hexdigest(), guard)
        os.replace(archive_part, archive)
        archive_hash = hashlib.sha256()
        with archive.open('rb') as source:
            for chunk in iter(lambda: source.read(1024**2), b''):
                guard()
                archive_hash.update(chunk)
        report.update(status='EXACT_BYTES_PRESERVED_SQLITE_RESTORE_PENDING', snapshot_bytes=identity[2],
            snapshot_sha256=digest.hexdigest(), archive_bytes=archive.stat().st_size,
            archive_sha256=archive_hash.hexdigest(), archive_path=str(archive))
        atomic_json(target / 'manifest.json', report)
        # Delete only the new job-owned scratch copy after fsynced byte/CRC verification.
        # The live DB, its WAL, and every existing customer volume are untouched.
        if signature(raw) != identity or json.loads(marker.read_text()).get('token') != token:
            raise Blocked('OWNED_SNAPSHOT_REMOVAL_GUARD_FAILED')
        raw.unlink()
        guard.phase('RESTORE_OWNED_ARCHIVE_TO_SEPARATE_SCRATCH')
        if shutil.disk_usage(root).free < identity[2] + guard.reserve:
            raise Blocked('RESTORE_AND_DISK_RESERVE_SPACE_UNAVAILABLE_ARCHIVE_RETAINED')
        verify_archive(archive, identity[2], digest.hexdigest(), guard, restored)
        guard.phase('FULL_SQLITE_INTEGRITY_CHECK')
        with closing(readonly(restored, guard)) as db:
            rows = db.execute('PRAGMA integrity_check')
            if rows.fetchone() != ('ok',) or rows.fetchone() is not None:
                raise Blocked('FULL_INTEGRITY_CHECK_FAILED')
            guard.phase('FULL_SQLITE_FOREIGN_KEY_CHECK')
            if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise Blocked('FOREIGN_KEY_CHECK_FAILED')
        guard.phase('COMPARE_RESTORED_SCHEMA_AND_ALL_FINANCIAL_COLUMNS')
        if proof(restored, guard) != reference:
            raise Blocked('RESTORED_SCHEMA_OR_FINANCIAL_RECORDS_MISMATCH')
        report.update(status='OWNED_POINT_IN_TIME_SNAPSHOT_RESTORED', integrity_check_ok=True,
            foreign_key_check_ok=True, schema_and_financial_records_match=True,
            completed_at_utc=datetime.now(timezone.utc).isoformat())
        atomic_json(target / 'manifest.json', report)
        if signature(restored)[2] != identity[2]:
            raise Blocked('OWNED_RESTORE_REMOVAL_GUARD_FAILED')
        restored.unlink()
        report['free_bytes_after'] = shutil.disk_usage(root).free
        atomic_json(target / 'manifest.json', report)
        return report
    except Exception as error:
        report.update(status='BLOCKED', phase=guard.phase_name, error_type=type(error).__name__,
            reason=str(error) if isinstance(error, Blocked) else None,
            sqlite_name=getattr(error, 'sqlite_errorname', None), verified_archive_retained=archive.is_file())
        atomic_json(target / 'manifest.json', report)
        raise


def child(token):
    os.umask(0o077)
    os.nice(15)
    resource.setrlimit(resource.RLIMIT_AS, (256*1024**2, 256*1024**2))
    guard = Guard(ROOT)
    guard(True)
    stats = dict(line.split() for line in Path('/sys/fs/cgroup/memory.stat').read_text().splitlines())
    limit = int(Path('/sys/fs/cgroup/memory.max').read_text())
    if limit-int(stats.get('anon', 0))-int(stats.get('kernel', 0)) < 256*1024**2:
        raise Blocked('API_MEMORY_HEADROOM_BELOW_256_MIB')
    fd = os.open(ROOT / '.task6-sqlite-recovery.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = preserve(ROOT, token, guard)
        print(json.dumps(report), flush=True)
    finally:
        os.close(fd)


def prepare(directory):
    if directory.parent != BASE or directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise Blocked('EXISTING_PRIVATE_RECOVERY_DIRECTORY_REQUIRED')
    if not re.fullmatch(r'task6-sqlite-recovery-\d{8}T\d{6}Z-[0-9a-f]{12}', directory.name):
        raise Blocked('EXISTING_PRIVATE_RECOVERY_DIRECTORY_REQUIRED')
    for name in ('runner.py', 'job.json', 'status.json', 'resume-status.json'):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise Blocked('ORIGINAL_RECOVERY_CONTROL_FILES_REQUIRED')
    content = (directory / 'runner.py').read_bytes()
    if hashlib.sha256(content).hexdigest() != ORIGINAL_SHA256:
        raise Blocked('ORIGINAL_RUNNER_CHECKSUM_MISMATCH')
    config = json.loads((directory / 'job.json').read_text())
    token = config.get('token', '')
    if not re.fullmatch('[0-9a-f]{32}', token) or not directory.name.endswith('-' + token[:12]):
        raise Blocked('ORIGINAL_RECOVERY_TOKEN_REQUIRED')
    previous = json.loads((directory / 'status.json').read_text())
    resume = json.loads((directory / 'resume-status.json').read_text())
    if previous.get('status') != 'BLOCKED' or (resume.get('status'), resume.get('reason')) != (
            'BLOCKED', 'RESUME_SNAPSHOT_SCHEMA_OR_FINANCIAL_RECORDS_CHANGED'):
        raise Blocked('EXPECTED_PRIOR_TIMEOUT_AND_DRIFT_FAILURES_REQUIRED')
    module = types.ModuleType('task6_pinned_original_recovery')
    module.__file__ = str(directory / 'runner.py')
    exec(compile(content, module.__file__, 'exec'), module.__dict__)
    api, production, _ = module.preflight()
    if api['Id'] != config['api_id']:
        raise Blocked('ORIGINAL_STAGE_API_REQUIRED')
    if production != previous.get('production_before'):
        raise Blocked('PRODUCTION_CONTAINER_IDENTITIES_CHANGED')
    return module, config, production


def run(directory):
    module, config, before = prepare(directory)
    manifest_path = str(ROOT / ('.task6-sqlite-recovery-' + config['token']) / 'preservation' / 'manifest.json')
    result = subprocess.run(['docker', 'exec', '-i', config['api_id'], 'python', '-B', '-', '--child', config['token']],
        input=Path(__file__).read_text(), text=True)
    report = {'status': 'BLOCKED', 'reason': 'SNAPSHOT_PRESERVATION_PROCESS_FAILED', 'task6_complete': False,
        'services_updated': False, 'new_paper_execution': False, 'full_sqlite_recovery_passed': False}
    try:
        output = module.docker(['exec', config['api_id'], 'python', '-B', '-c',
            'from pathlib import Path; print(Path(' + repr(manifest_path) + ').read_text())'])
        report = json.loads(output)
    except Exception:
        pass
    production = module.selected_snapshot(module.project_containers(module.PROD))
    report['production_container_identities_unchanged'] = production == before
    if production != before:
        report.update(status='BLOCKED', reason='PRODUCTION_CONTAINER_IDENTITIES_CHANGED')
    if result.returncode and report.get('status') != 'BLOCKED':
        report.update(status='BLOCKED', reason='SNAPSHOT_PRESERVATION_PROCESS_FAILED')
    try:
        after, _, _ = module.preflight()
        if after['Id'] != config['api_id']:
            raise Blocked('ORIGINAL_STAGE_API_REQUIRED')
        report['api_health_after'] = 'healthy'
        report['api_memory_limit_bytes_after'] = after['HostConfig']['Memory']
        report['api_memory_swap_limit_bytes_after'] = after['HostConfig']['MemorySwap']
    except Exception as error:
        report.update(status='BLOCKED', reason='STAGING_POSTCHECK_FAILED',
            postcheck_error_type=type(error).__name__)
    atomic_json(directory / 'preservation-status.json', report)
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report.get('status') == 'OWNED_POINT_IN_TIME_SNAPSHOT_RESTORED' else 1


def launch(directory):
    prepare(directory)
    fd = os.open(directory / 'preservation-launch.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = directory / 'preservation-runner.py'
        with runner.open('x') as output:
            output.write(Path(__file__).read_text())
        atomic_json(directory / 'preservation-status.json', {'status': 'RUNNING',
            'scope': 'EARLIER_OWNED_POINT_IN_TIME_COPY_ONLY', 'services_updated': False,
            'new_paper_execution': False, 'task6_complete': False, 'full_sqlite_recovery_passed': False})
        with (directory / 'preservation.log').open('x') as log:
            process = subprocess.Popen([sys.executable, '-B', str(runner), '--run', str(directory)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        print(json.dumps({'status': 'OWNED_SNAPSHOT_PRESERVATION_JOB_STARTED', 'pid': process.pid,
            'status_file': str(directory / 'preservation-status.json'),
            'progress_log': str(directory / 'preservation.log'), 'services_updated': False,
            'new_paper_execution': False, 'task6_complete': False}), flush=True)
    finally:
        os.close(fd)


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == '--child':
        child(sys.argv[2])
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == '--run':
        return run(Path(sys.argv[2]))
    if len(sys.argv) == 2:
        launch(Path(sys.argv[1]))
        return 0
    raise Blocked('EXISTING_RECOVERY_DIRECTORY_ARGUMENT_REQUIRED')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        failure = {'status': 'BLOCKED', 'error_type': type(error).__name__,
            'reason': str(error) if isinstance(error, Blocked) else None,
            'services_updated': False, 'new_paper_execution': False, 'task6_complete': False}
        if len(sys.argv) == 3 and sys.argv[1] == '--run':
            directory = Path(sys.argv[2])
            if directory.parent == BASE and directory.is_dir() and not directory.is_symlink():
                atomic_json(directory / 'preservation-status.json', failure)
        print(json.dumps(failure), flush=True)
        sys.exit(1)
