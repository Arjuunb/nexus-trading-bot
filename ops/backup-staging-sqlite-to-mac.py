"""Owner-run staging SQLite snapshots to an existing Mac; never deploy or delete.

Requires explicit staging container/project/image and an existing SSH identity.
Keeps failed copies and every original. Each DB is a separate consistent SQLite
snapshot, NOT a transaction across DBs, PostgreSQL or an external provider.
Runtime receipts and financial backups must stay private, outside source control.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import resource
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

GIB = 1024 ** 3
RESERVE = 8 * GIB
WAL_GROWTH_CAP = GIB
SCRATCH_CAP = 256 * 1024 ** 2
ORIGIN_SECONDS = 1800
VERSION = '3540100'
MAC_ZIP = 'sqlite-tools-osx-arm64-' + VERSION + '.zip'
LINUX_ZIP = 'sqlite-tools-linux-x64-' + VERSION + '.zip'
ZIP_SHA3 = {
    MAC_ZIP: 'a3106c6db3b50ec9ef4fde4edfafe7504179e9bfd7e0816a280b29c4d56a2e7b',
    LINUX_ZIP: '555635de98534d87a67ee2e6e1b0d79f494b1b33b223477d3e7a8ea0c66e416f',
}
TOOL_SHA256 = {
    'mac_rsync': '5ee4433058ac205466c73c97032dda2713d70f88130ce000b12fd3af51dbb748',
    'mac_sqlite': '9a538234fe523a7bda07d526950c53f7054e5c41401510d1af3d7adf9ba2026d',
    'linux_rsync': '5203304e7ce33b872eea14ad3888d557d86f6860cf3643a6d03fa987e8d980fa',
    'linux_sqlite': 'c9daa875c28379f0d61a833d47928c6e43b352d98638cd824ec405f23c99d870',
}
BINDINGS = (
    ('HUB_ACCOUNT_DB', 'account.db'), ('HUB_JOURNAL_DB', 'journal.db'),
    ('HUB_LEDGER_PATH', 'ledger.db'), ('HUB_PAPER_BROKER_V2_DB', 'paper_broker_v2.db'),
    ('HUB_PRICE_ACTION_PAPER_DB', 'price_action_paper.db'),
    ('HUB_PRICE_ACTION_RESEARCH_DB', 'price_action_research.db'),
    ('HUB_SMC_PAPER_DB', 'smc_strategy_paper.db'),
)


class Blocked(RuntimeError):
    pass


def require(ok, code):
    if not ok:
        raise Blocked(code)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def run(args, input=None, timeout=30):
    result = subprocess.run(args, input=input, capture_output=True, timeout=timeout)
    require(result.returncode == 0, 'COMMAND_FAILED:' + Path(args[0]).name)
    return result.stdout


def docker(args):
    return json.loads(run(['docker', '--host', 'unix:///var/run/docker.sock', *args]))


def identities(rows):
    return {r['Id']: hashlib.sha256(json.dumps(
                {'image': r['Image'], 'state': r['State']['Status'],
                 'mounts': r.get('Mounts'), 'limits': r.get('HostConfig')},
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            for r in rows}


def select_state(rows, container, project, expected_image):
    require(re.fullmatch(r'nexus-remote-stage-[a-zA-Z0-9_-]+', project), 'EXPLICIT_STAGING_PROJECT_REQUIRED')
    candidates = [r for r in rows if (r.get('Config', {}).get('Labels') or {}).get('com.docker.compose.project') == project
                  and (r['Config'].get('Labels') or {}).get('com.docker.compose.service') == 'api'
                  and r['State']['Status'] == 'running']
    require(len(candidates) == 1, 'ONE_RUNNING_STAGING_API_REQUIRED')
    api = candidates[0]
    require(api['Name'].lstrip('/') == container, 'CONTAINER_NAME_MISMATCH')
    require(api['Image'] == expected_image, 'EXPECTED_IMAGE_MISMATCH')
    mounts = [m for m in api['Mounts'] if m['Destination'] == '/var/lib/tradexa']
    require(len(mounts) == 1 and mounts[0]['Type'] == 'volume', 'ONE_EXISTING_NAMED_STATE_VOLUME_REQUIRED')
    mount = mounts[0]
    for row in rows:
        if any(m['Source'] == mount['Source'] for m in row.get('Mounts', [])):
            require((row['Config'].get('Labels') or {}).get('com.docker.compose.project') == project,
                    'STATE_VOLUME_SHARED_WITH_ANOTHER_PROJECT')
    return api, mount


def file_identity(path):
    p = Path(path)
    require(not p.is_symlink() and p.resolve() == p, 'SOURCE_SYMLINK_REJECTED')
    s = p.stat()
    require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, 'REGULAR_UNSHARED_SOURCE_FILE_REQUIRED')
    return {'device': s.st_dev, 'inode': s.st_ino, 'uid': s.st_uid, 'gid': s.st_gid}


def wal_size(path):
    p = Path(str(path) + '-wal')
    require(not p.is_symlink(), 'WAL_SYMLINK_REJECTED')
    if p.exists():
        require(stat.S_ISREG(p.stat().st_mode) and p.stat().st_nlink == 1, 'REGULAR_UNSHARED_WAL_REQUIRED')
        return p.stat().st_size
    return 0


def check_sidecars(path):
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path) + suffix)
        require(not sidecar.is_symlink(), 'SQLITE_SIDECAR_SYMLINK_REJECTED')
        if sidecar.exists():
            file_identity(sidecar)
            require(suffix != '-journal' or sidecar.stat().st_size == 0, 'ROLLBACK_JOURNAL_REQUIRES_REVIEW')


def free_bytes(path):
    v = os.statvfs(str(path))
    return v.f_bavail * v.f_frsize


def inspect_stage(container, project, expected_image):
    ids = run(['docker', '--host', 'unix:///var/run/docker.sock', 'ps', '-aq']).decode().split()
    require(0 < len(ids) <= 256, 'BOUNDED_CONTAINER_INVENTORY_REQUIRED')
    rows = docker(['inspect', *ids])
    api, mount = select_state(rows, container, project, expected_image)
    volume = docker(['volume', 'inspect', mount['Name']])
    require(len(volume) == 1 and volume[0]['Mountpoint'] == mount['Source'], 'NAMED_VOLUME_PATH_MISMATCH')
    root = Path(mount['Source'])
    require(root.is_absolute() and not root.is_symlink() and root.resolve() == root, 'CANONICAL_STATE_ROOT_REQUIRED')
    env = dict(v.split('=', 1) for v in api['Config'].get('Env', []) if '=' in v)
    files = []
    for key, name in BINDINGS:
        inside = PurePosixPath(env.get(key, '/var/lib/tradexa/' + name))
        require(inside == PurePosixPath('/var/lib/tradexa') / name, 'NONSTANDARD_BINDING_REQUIRES_REVIEW:' + key)
        path = root / name
        if not path.exists():
            require(not path.is_symlink(), 'SOURCE_SYMLINK_REJECTED')
            files.append({'binding': key, 'name': name, 'exists': False})
            continue
        identity = file_identity(path)
        check_sidecars(path)
        deadline = time.monotonic() + 3
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            db.execute('PRAGMA query_only=ON')
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            logical = db.execute('PRAGMA page_size').fetchone()[0] * db.execute('PRAGMA page_count').fetchone()[0]
        finally:
            db.close()
        files.append({'binding': key, 'name': name, 'exists': True, 'source': str(path),
                      'identity': identity, 'logical_bytes': logical, 'wal_bytes': wal_size(path)})
    require(all(x['exists'] for x in files if x['binding'] != 'HUB_LEDGER_PATH'), 'REQUIRED_EXISTING_FINANCIAL_STORE_MISSING')
    require(len({(x['identity']['device'], x['identity']['inode']) for x in files if x['exists']}) == sum(x['exists'] for x in files),
            'DATABASE_IDENTITY_ALIAS')
    require(free_bytes(root) >= RESERVE + SCRATCH_CAP, 'VPS_RESERVE_NOT_AVAILABLE')
    require(shutil.which('ionice') and shutil.which('nice'), 'LOW_PRIORITY_HOST_TOOLS_REQUIRED')
    require(platform.machine() == 'x86_64', 'LINUX_X64_HOST_REQUIRED')
    return {'container': container, 'container_id': api['Id'], 'project': project, 'image': expected_image,
            'state_root': str(root), 'identities': identities(rows), 'files': files,
            'vps_free_bytes': free_bytes(root),
            'external_primary_configured': bool(env.get('SUPABASE_URL') and env.get('SUPABASE_KEY')),
            'cross_database_atomicity': False, 'external_recovery_proven': False}


def unchanged(plan):
    ids = run(['docker', '--host', 'unix:///var/run/docker.sock', 'ps', '-aq']).decode().split()
    require(0 < len(ids) <= 256, 'BOUNDED_CONTAINER_INVENTORY_REQUIRED')
    require(identities(docker(['inspect', *ids])) == plan['identities'], 'CONTAINER_STATE_OR_CONFIGURATION_CHANGED')


def scratch_bytes(directory):
    total = 0
    for p in Path(directory).iterdir():
        require(not p.is_symlink() and p.is_file(), 'UNEXPECTED_SCRATCH_ENTRY')
        total += p.stat().st_size
    return total


def budget_guard(path, identity, initial_wal, scratch, started):
    require(file_identity(path) == identity, 'SOURCE_IDENTITY_CHANGED')
    check_sidecars(path)
    require(free_bytes(path.parent) >= RESERVE, 'VPS_RESERVE_REACHED')
    require(wal_size(path) <= initial_wal + WAL_GROWTH_CAP, 'WAL_GROWTH_CAP_REACHED')
    require(scratch_bytes(scratch) <= SCRATCH_CAP, 'SCRATCH_CAP_REACHED')
    require(time.monotonic() - started < ORIGIN_SECONDS, 'ORIGIN_DEADLINE_REACHED')


def stop_process(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def origin(config, rest):
    require(len(rest) == 3 and rest[0] == '--origin', 'ONLY_ORIGIN_ROLE_ALLOWED')
    plan = json.loads(Path(config).read_text())
    rows = [r for r in plan['files'] if r.get('source') == rest[1] and r['exists']]
    require(len(rows) == 1 and rest[2] == rows[0]['name'], 'EXACT_PLANNED_SOURCE_REQUIRED')
    row = rows[0]
    unchanged(plan)
    path = Path(row['source'])
    require(file_identity(path) == row['identity'], 'SOURCE_IDENTITY_CHANGED')
    base = Path(config).parent
    tool = base / 'sqlite3_rsync'
    require(digest(tool) == TOOL_SHA256['linux_rsync'], 'LINUX_TOOL_HASH_MISMATCH')
    scratch = base / ('scratch-' + row['name'])
    scratch.mkdir(mode=0o700)
    started = time.monotonic()
    initial_wal = wal_size(path)
    budget_guard(path, row['identity'], initial_wal, scratch, started)
    environment = {'PATH': '/usr/bin:/bin', 'SQLITE_TMPDIR': str(scratch), 'TMPDIR': str(scratch)}

    def limits():
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 ** 2, 512 * 1024 ** 2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (SCRATCH_CAP, SCRATCH_CAP))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 125))

    # Official origin uses a read transaction and TEMP bookkeeping. No application
    # imports, financial SQL writes, database checkpoints or source file copies.
    with (base / ('origin-' + row['name'] + '.log')).open('xb') as error_log:
        p = subprocess.Popen(['ionice', '-c', '3', 'nice', '-n', '19', str(tool), *rest],
                             stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=error_log,
                             env=environment, start_new_session=True, preexec_fn=limits)
        def interrupted(signum, frame):
            raise Blocked('ORIGIN_INTERRUPTED')
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, interrupted)
        try:
            while p.poll() is None:
                budget_guard(path, row['identity'], initial_wal, scratch, started)
                time.sleep(1)
            require(p.returncode == 0, 'NATIVE_ORIGIN_FAILED')
            budget_guard(path, row['identity'], initial_wal, scratch, started)
            unchanged(plan)
            # A completion receipt is separate from the native binary protocol.
            write_json(base / ('completed-' + row['name'] + '.json'),
                       {'name': row['name'], 'source_identity': file_identity(path),
                        'vps_free_bytes': free_bytes(path.parent), 'wal_growth_bytes': wal_size(path) - initial_wal,
                        'elapsed_seconds': round(time.monotonic() - started, 3), 'at_utc': utc()})
        finally:
            stop_process(p)


def utc():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    Path(path).chmod(0o600)


def bootstrap(payload):
    require(set(payload) == {'script', 'linux_tool', 'plan'}, 'EXACT_BOOTSTRAP_PACKAGE_REQUIRED')
    script = base64.b64decode(payload['script'], validate=True)
    tool = base64.b64decode(payload['linux_tool'], validate=True)
    require(len(script) < 100000 and len(tool) < 4 * 1024 ** 2, 'BOUNDED_BOOTSTRAP_REQUIRED')
    require(hashlib.sha256(tool).hexdigest() == TOOL_SHA256['linux_rsync'], 'LINUX_TOOL_HASH_MISMATCH')
    base = Path(tempfile.mkdtemp(prefix='.nexus-staging-sqlite-', dir=str(Path.home())))
    base.chmod(0o700)
    for name, data in [('backup.py', script), ('sqlite3_rsync', tool)]:
        p = base / name
        p.write_bytes(data)
        p.chmod(0o700)
    write_json(base / 'request.json', payload['plan'])
    command = ['sudo', '-n', '--', '/usr/bin/python3', '-B', str(base / 'backup.py'), 'origin', str(base / 'request.json')]
    wrapper = base / 'rsync-origin'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n')
    wrapper.chmod(0o700)
    return {'remote_directory': str(base), 'script_sha256': hashlib.sha256(script).hexdigest()}


def tools_from_zip(data, name):
    require(hashlib.sha3_256(data).hexdigest() == ZIP_SHA3[name], 'OFFICIAL_SQLITE_ZIP_HASH_MISMATCH')
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for tool in ('sqlite3_rsync', 'sqlite3'):
            members = [m for m in archive.infolist() if PurePosixPath(m.filename).name == tool]
            require(len(members) == 1 and members[0].file_size < 4 * 1024 ** 2, 'EXACT_TOOL_ARCHIVE_MEMBER_REQUIRED')
            result[tool] = archive.read(members[0])
    return result


def get_tools(base):
    found = {}
    for name in (MAC_ZIP, LINUX_ZIP):
        with urllib.request.urlopen('https://sqlite.org/2026/' + name, timeout=30) as response:
            data = response.read(12 * 1024 ** 2)
        found[name] = tools_from_zip(data, name)
    for name, content in found[MAC_ZIP].items():
        key = 'mac_rsync' if name == 'sqlite3_rsync' else 'mac_sqlite'
        require(hashlib.sha256(content).hexdigest() == TOOL_SHA256[key], 'MAC_TOOL_HASH_MISMATCH')
        path = base / name
        path.write_bytes(content)
        path.chmod(0o700)
    return found[LINUX_ZIP]['sqlite3_rsync']


def sqlite_cli(tool, db, text, timeout=3600):
    return run([str(tool), '-noinit', '-nofollow', '-readonly', '-batch', '-bail', '-list', '-noheader', str(db)],
               input=text.encode(), timeout=timeout).decode().strip()


def check_snapshot(tool, db):
    require(sqlite_cli(tool, db, 'PRAGMA integrity_check;\n') == 'ok', 'FULL_INTEGRITY_CHECK_FAILED')
    require(sqlite_cli(tool, db, 'PRAGMA foreign_key_check;\n') == '', 'FOREIGN_KEY_CHECK_FAILED')
    fingerprint = sqlite_cli(tool, db, '.sha3sum --sha3-256 --schema\n')
    require(bool(re.fullmatch(r'[a-f0-9]{64}', fingerprint)), 'SCHEMA_AND_ROW_FINGERPRINT_REQUIRED')
    return fingerprint


def verify_and_restore(tool, snapshot, restored):
    require(snapshot.is_file() and not snapshot.is_symlink() and not restored.exists(), 'NEW_DISTINCT_RESTORE_REQUIRED')
    before = digest(snapshot)
    fingerprint = check_snapshot(tool, snapshot)
    quoted = '"' + str(restored).replace('\\', '\\\\').replace('"', '\\"') + '"'
    sqlite_cli(tool, snapshot, '.backup ' + quoted + '\n')
    restored.chmod(0o600)
    require(check_snapshot(tool, restored) == fingerprint, 'RESTORED_SCHEMA_OR_ROWS_DIFFER')
    require(digest(snapshot) == before, 'BACKUP_CHANGED_DURING_RESTORE_CHECK')
    return {'backup_bytes': snapshot.stat().st_size, 'restore_bytes': restored.stat().st_size,
            'backup_sha256': before, 'restored_sha256': digest(restored),
            'schema_and_rows_sha3': fingerprint, 'full_integrity_and_foreign_keys_passed': True,
            'separate_restore_passed': True}


def ssh_base(host, key):
    require(bool(re.fullmatch(r'[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+', host)), 'EXPLICIT_SSH_USER_AND_HOST_REQUIRED')
    require(Path(key).is_file(), 'EXISTING_PRIVATE_SSH_KEY_REQUIRED')
    return ['ssh', '-i', str(key), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']


def remote_code(ssh, host, source, args):
    cmd = shlex.join(['sudo', '-n', '--', '/usr/bin/python3', '-B', '-', *args])
    return json.loads(run([*ssh, host, cmd], input=source.encode(), timeout=90))


def main_mac(args):
    require(platform.system() == 'Darwin' and platform.machine() == 'arm64', 'OWNER_ARM64_MAC_REQUIRED')
    os.umask(0o077)
    destination = Path(args.destination).expanduser()
    require(not destination.is_symlink(), 'DESTINATION_SYMLINK_REJECTED')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = destination.resolve()
    base = Path(tempfile.mkdtemp(prefix='staging-sqlite-', dir=str(destination)))
    require(not any(c in str(base) for c in '\r\n\x00'), 'SINGLE_LINE_DESTINATION_REQUIRED')
    source = Path(__file__).read_text()
    ssh = ssh_base(args.host, Path(args.key).expanduser())
    plan = remote_code(ssh, args.host, source, ['inspect', args.container, args.project, args.expected_image])
    needed = 2 * sum(r['logical_bytes'] for r in plan['files'] if r['exists']) + RESERVE
    require(free_bytes(base) >= needed, 'MAC_BACKUP_AND_RESTORE_SPACE_REQUIRED')
    write_json(base / 'plan.json', plan)
    print(json.dumps({'phase': 'STAGING_SQLITE_PLAN', 'directory': str(base),
                      'mac_minimum_bytes': needed, 'vps_free_bytes': plan['vps_free_bytes'],
                      'missing_bindings': [r['binding'] for r in plan['files'] if not r['exists']],
                      'cross_database_atomicity': False}), flush=True)
    if not args.backup:
        return
    linux_tool = get_tools(base)
    package = {'script': base64.b64encode(source.encode()).decode(),
               'linux_tool': base64.b64encode(linux_tool).decode(), 'plan': plan}
    # Bootstrap runs as the SSH owner; only origin inspection/copy needs sudo.
    command = shlex.join(['/usr/bin/python3', '-B', '-c',
                          'import sys,json; p=json.load(sys.stdin); ns={"__name__":"backup_bootstrap"}; '
                          'exec(p.pop("source"),ns); print(json.dumps(ns["bootstrap"](p)))'])
    upload = dict(package, source=source)
    received = json.loads(run([*ssh, args.host, command], input=json.dumps(upload).encode(), timeout=60))
    require(received['script_sha256'] == hashlib.sha256(source.encode()).hexdigest(), 'REMOTE_HELPER_HASH_MISMATCH')
    remote = received['remote_directory']
    require(re.fullmatch(r'/home/[a-zA-Z0-9_.-]+/\.nexus-staging-sqlite-[a-zA-Z0-9_-]+', remote), 'PRIVATE_REMOTE_DIRECTORY_REQUIRED')
    wrapper = base / 'ssh-for-sqlite'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(ssh) + ' "$@"\n')
    wrapper.chmod(0o700)
    rows = []
    snapshots = base / 'backups'; restores = base / 'restore-checks'
    snapshots.mkdir(mode=0o700); restores.mkdir(mode=0o700)
    for row in plan['files']:
        if not row['exists']:
            continue
        require(free_bytes(base) >= 2 * row['logical_bytes'] + RESERVE, 'MAC_SPACE_RECHECK_FAILED')
        snapshot = snapshots / row['name']
        require(not snapshot.exists(), 'NEW_SNAPSHOT_REQUIRED')
        print(json.dumps({'phase': 'COPY_CONSISTENT_STAGING_SNAPSHOT', 'database': row['name']}), flush=True)
        with (base / ('transfer-' + row['name'] + '.log')).open('xb') as log:
            process = subprocess.Popen([str(base / 'sqlite3_rsync'), args.host + ':' + row['source'], str(snapshot),
                                        '--ssh', str(wrapper), '--exe', remote + '/rsync-origin'],
                                       stdout=log, stderr=log, start_new_session=True)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    require(free_bytes(base) >= RESERVE, 'MAC_RESERVE_REACHED')
                    require(time.monotonic() - started < ORIGIN_SECONDS + 60, 'TRANSFER_DEADLINE_REACHED')
                    time.sleep(1)
                require(process.returncode == 0, 'SNAPSHOT_TRANSFER_FAILED:' + row['name'])
            finally:
                stop_process(process)
        receipt_cmd = ['finish', remote + '/request.json', row['name']]
        origin_receipt = remote_code(ssh, args.host, source, receipt_cmd)
        require(free_bytes(base) >= snapshot.stat().st_size + RESERVE, 'MAC_RESTORE_SPACE_REQUIRED')
        print(json.dumps({'phase': 'VERIFY_AND_RESTORE_ON_MAC', 'database': row['name']}), flush=True)
        checked = verify_and_restore(base / 'sqlite3', snapshot, restores / row['name'])
        rows.append(dict(checked, name=row['name'], origin=origin_receipt))
        write_json(base / ('verified-' + row['name'] + '.json'), rows[-1])
    remote_code(ssh, args.host, source, ['unchanged', remote + '/request.json'])
    receipt = {'status': 'STAGING_LOCAL_SQLITE_BACKUPS_AND_SEPARATE_RESTORES_VERIFIED',
               'directory': str(base), 'databases_verified': len(rows),
               'backup_bytes': sum(r['backup_bytes'] for r in rows), 'databases': rows,
               'missing_bindings': [r['binding'] for r in plan['files'] if not r['exists']],
               'cross_database_atomicity': False, 'external_recovery_proven': False,
               'postgres_recovery_proven': False, 'deployment_performed': False,
               'files_deleted': False, 'task6_complete': False, 'at_utc': utc()}
    write_json(base / 'receipt.json', receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k != 'databases'}, indent=2))


def entry():
    if len(sys.argv) > 1 and sys.argv[1] == 'inspect':
        print(json.dumps(inspect_stage(*sys.argv[2:])))
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'origin':
        origin(sys.argv[2], sys.argv[3:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in ('finish', 'unchanged'):
        plan = json.loads(Path(sys.argv[2]).read_text())
        unchanged(plan)
        if sys.argv[1] == 'finish':
            require(sys.argv[3] in [r['name'] for r in plan['files']], 'PLANNED_DATABASE_REQUIRED')
            completed = Path(sys.argv[2]).parent / ('completed-' + sys.argv[3] + '.json')
            print(completed.read_text())
        else:
            print(json.dumps({'container_identities_and_configuration_unchanged': True}))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('host', 'key', 'project', 'container', 'expected-image', 'destination'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--backup', action='store_true', help='Create snapshots and restore-check copies on the Mac; default only plans.')
    main_mac(parser.parse_args())


if __name__ == '__main__':
    try:
        entry()
    except Exception as error:
        message = str(error) if isinstance(error, Blocked) else type(error).__name__
        output = {'status': 'BLOCKED', 'reason': message, 'deployment_performed': False,
                  'files_deleted': False, 'task6_complete': False,
                  'partial_copies_retained': True, 'financial_recovery_proven': False}
        # Never insert text into the binary protocol consumed by sqlite3_rsync.
        target = sys.stderr if len(sys.argv) > 1 and sys.argv[1] == 'origin' else sys.stdout
        print(json.dumps(output), file=target, flush=True)
        sys.exit(1)
