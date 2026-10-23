#!/usr/bin/env python3
"""Owner-run, staging-only start of the scanned Task 6 image in research mode.

No production operation, image build/pull, data deletion, password prompt,
financial mutation, or automatic strategy promotion is implemented here.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from decimal import Decimal
from urllib.parse import urlsplit

PROJECT = 'nexus-remote-stage-rc3-28acdbc'
ROOT = Path('/home/ubuntu/nexus-rc3-staging-28acdbc')
API = PROJECT + '-api-1'
INGRESS = PROJECT + '-ingress-1'
WORKER = PROJECT + '-research-worker-1'
VOLUME = PROJECT + '_api-state'
COMMIT = 'f9ebd32a3eedf6740c14a3daafb32e981d1d7ce8'
IMAGE = 'sha256:9b7087da9cd66ff51f2120e84444705e1ae02464855e63b25267d3f0a2fbf492'
MEMORY = 1610612736
DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']
MODE_KEY = 'NEXUS_ACCEPTANCE_READ_ONLY'
RUN_PARENT = Path('/home/ubuntu')
LOCK_FILE = Path('/var/lib/nexus-ops/state/lock')
BACKUP_SHA = '81f07017470558ba6b83787903aebca0a33198fc203d2244ed40c7249f7a5436'
FINANCIAL = ('account.db', 'journal.db', 'paper_broker_v2.db',
             'smc_strategy_paper.db', 'price_action_paper.db', 'ledger.db')


class Blocked(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def phase(name):
    print(json.dumps({'phase': name}), flush=True)


def write_json(path, value):
    with open(path, 'x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def run(command, *, timeout=120, input=None):
    # Never expose a rendered Compose config, environment value, or raw stderr.
    environment = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                   'HOME': '/root', 'DOCKER_HOST': 'unix:///var/run/docker.sock'}
    try:
        result = subprocess.run(command, input=input, capture_output=True,
                                text=True, timeout=timeout, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Blocked('COMMAND_UNAVAILABLE_OR_TIMEOUT') from error
    require(result.returncode == 0, 'COMMAND_FAILED:' + Path(command[0]).name)
    return result.stdout


def docker(*args, **kwargs):
    return run([*DOCKER, *args], **kwargs)


def inspect(name):
    result = json.loads(docker('inspect', name))
    require(len(result) == 1, 'EXACTLY_ONE_DOCKER_OBJECT_REQUIRED')
    return result[0]


def environment(container):
    result = {}
    for entry in container['Config'].get('Env') or []:
        key, separator, value = entry.partition('=')
        require(separator and key not in result, 'INVALID_OR_DUPLICATE_ENVIRONMENT_KEY')
        result[key] = value
    return result


def identities():
    result = {}
    for cid in docker('ps', '-aq', '--no-trunc').split():
        c = inspect(cid)
        result[c['Name'].lstrip('/')] = [c['Id'], c['Image'], c['State']['Status'],
            c['State'].get('StartedAt'), c.get('RestartCount', 0)]
    return result


def preserved(container):
    # Config/HostConfig contain secrets and are used only in memory.
    config = {key: value for key, value in container['Config'].items()
              if key not in ('Hostname', 'Labels', 'Env')}
    env = environment(container)
    env.pop(MODE_KEY, None)
    mounts = sorted((m.get('Name') or m.get('Source'), m['Destination'], bool(m['RW']))
                    for m in container.get('Mounts', []))
    # Compose sets config-hash/working-dir labels and the container hostname.
    host = dict(container['HostConfig'])
    return {'config': config, 'env': env, 'mounts': mounts, 'host': host,
            'networks': sorted(container['NetworkSettings']['Networks']),
            'image': container['Image']}


def regular(path, *, under=None, source_file=False):
    path = Path(path)
    require(path.is_absolute(), 'ABSOLUTE_PATH_REQUIRED')
    current = path
    while current != current.parent:
        require(not current.is_symlink(), 'SYMLINK_PATH_REFUSED')
        current = current.parent
    st = path.stat()
    require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1,
            'SINGLE_LINK_REGULAR_FILE_REQUIRED')
    require((source_file or st.st_uid in (0, 1000)) and not st.st_mode & 0o022,
            'UNSAFE_FILE_OWNER_OR_PERMISSIONS')
    if under is not None:
        require(path.is_relative_to(under), 'FILE_OUTSIDE_APPROVED_ROOT')
    return path


def stopped_baseline():
    api = inspect(API)
    labels = api['Config'].get('Labels') or {}
    require(labels.get('com.docker.compose.project') == PROJECT
            and labels.get('com.docker.compose.service') == 'api', 'STAGING_IDENTITY_MISMATCH')
    require(not api['State']['Running'] and api['State']['Status'] in ('exited', 'created')
            and api['State'].get('ExitCode', 0) == 0, 'CLEAN_STOPPED_STAGING_API_REQUIRED')
    require(api['Image'] == IMAGE, 'SCANNED_F9_IMAGE_REQUIRED')
    image = inspect(IMAGE)
    require((image['Config'].get('Labels') or {}).get('org.opencontainers.image.revision')
            == COMMIT, 'IMAGE_SOURCE_REVISION_MISMATCH')
    require(api['HostConfig'].get('Memory') == MEMORY
            and api['HostConfig'].get('MemorySwap') == MEMORY, 'EXISTING_1536_MIB_REQUIRED')
    env = environment(api)
    require(env.get('PRICE_ACTION_LAB_ENABLED', 'false').lower()
            in ('false', '0', '', 'no', 'off'), 'PRICE_ACTION_MUST_STAY_DISABLED')
    require(urlsplit(env.get('SUPABASE_URL', '')).hostname
            == 'simgvasrjiyltnwtmyzt.supabase.co', 'EXACT_STAGING_PRIMARY_REQUIRED')
    volume = inspect(VOLUME)
    require((volume.get('Labels') or {}).get('com.docker.compose.project') == PROJECT,
            'STAGING_VOLUME_IDENTITY_MISMATCH')
    require(any(m.get('Name') == VOLUME for m in api.get('Mounts', [])),
            'EXISTING_STAGING_STATE_MOUNT_REQUIRED')
    source = Path(volume['Mountpoint'])
    api_sources = {m['Source'] for m in api.get('Mounts', []) if m.get('RW')}
    production_seen = False
    for cid in docker('ps', '-aq', '--no-trunc').split():
        other = inspect(cid)
        if other['Id'] == api['Id']:
            continue
        other_labels = other['Config'].get('Labels') or {}
        overlap = any(m.get('Source') in api_sources for m in other.get('Mounts', []))
        expected_stopped_initializer = (other_labels.get('com.docker.compose.project') == PROJECT
            and other_labels.get('com.docker.compose.service') == 'state-init'
            and not other['State']['Running'])
        require(not overlap or expected_stopped_initializer, 'API_WRITABLE_STORAGE_HAS_ANOTHER_CONSUMER')
        if other_labels.get('com.docker.compose.project') == 'nexus-trading-bot':
            require(urlsplit(environment(other).get('SUPABASE_URL', '')).hostname
                    != 'simgvasrjiyltnwtmyzt.supabase.co', 'PRIMARY_OVERLAPS_PRODUCTION')
            if other.get('Name') == '/nexus-trading-bot-app-1':
                production_seen = True
                require(other['State']['Running'] and (other['State'].get('Health') or {}).get('Status') == 'healthy',
                        'PRODUCTION_API_MUST_ALREADY_BE_HEALTHY')
    require(production_seen, 'EXISTING_PRODUCTION_BASELINE_REQUIRED')
    for name in (WORKER, PROJECT + '-postgres-1', INGRESS):
        c = inspect(name)
        require(c['State']['Running'] and (c['State'].get('Health') or {}).get('Status')
                in (None, 'healthy'), 'STAGING_DEPENDENCY_NOT_HEALTHY')
    files = [regular(p) for p in labels.get('com.docker.compose.project.config_files', '').split(',') if p]
    require(files and all(p.is_relative_to(ROOT) or p.is_relative_to('/var/lib/nexus-ops/pa-retirement')
                         for p in files), 'UNEXPECTED_COMPOSE_FILES')
    require(labels.get('com.docker.compose.project.working_dir') == str(ROOT / 'release/ops'),
            'COMPOSE_WORKING_DIRECTORY_MISMATCH')
    env_file = regular(ROOT / 'private/compose.env')
    return api, source, files, env_file


def compose(files, env_file, *args):
    command = [*DOCKER, 'compose', '--project-name', PROJECT,
               '--project-directory', str(ROOT / 'release/ops')]
    for path in files:
        command += ['-f', str(path)]
    command += ['--env-file', str(env_file), '--profile', 'research', *args]
    return run(command, timeout=600)


def validate_render(api, before, after):
    require(before.get('services') and after.get('services'), 'COMPOSE_SERVICES_REQUIRED')
    desired = json.loads(json.dumps(before))
    desired['services']['api'].setdefault('environment', {})[MODE_KEY] = '1'
    require(after == desired, 'COMPOSE_CHANGE_EXCEEDS_SINGLE_ACCEPTANCE_FLAG')
    service = after['services']['api']
    require(int(service.get('mem_limit', 0)) == MEMORY
            and int(service.get('memswap_limit', 0)) == MEMORY, 'COMPOSE_MEMORY_WOULD_CHANGE')
    host = api['HostConfig']
    cpu_nanos = int(Decimal(str(service.get('cpus') or 0)) * 1_000_000_000)
    require(cpu_nanos == int(host.get('NanoCpus') or 0), 'COMPOSE_CPU_LIMIT_WOULD_CHANGE')
    for key, host_key in (('cpu_quota', 'CpuQuota'), ('cpu_period', 'CpuPeriod'),
                          ('cpu_shares', 'CpuShares'), ('pids_limit', 'PidsLimit')):
        require(int(service.get(key) or 0) == int(host.get(host_key) or 0),
                'COMPOSE_RESOURCE_LIMIT_WOULD_CHANGE:' + key)
    require(str(service.get('cpuset') or '') == str(host.get('CpusetCpus') or ''),
            'COMPOSE_CPUSET_WOULD_CHANGE')
    actual = environment(api)
    original = before['services']['api'].get('environment') or {}
    require(all(actual.get(k) == ('' if v is None else str(v)) for k, v in original.items()),
            'COMPOSE_ENVIRONMENT_DIFFERS_FROM_STOPPED_CONTAINER')


def file_state(source):
    result = {}
    for name in FINANCIAL:
        for suffix in ('', '-wal', '-journal'):
            p = source / (name + suffix)
            if p.exists():
                st = p.lstat()
                require(stat.S_ISREG(st.st_mode) and not p.is_symlink(), 'UNSAFE_FINANCIAL_FILE')
                result[p.name] = [st.st_ino, st.st_size, st.st_mtime_ns]
    require('smc_strategy_paper.db' in result and 'account.db' in result,
            'EXISTING_FINANCIAL_STORES_REQUIRED')
    require(not any(name.startswith('price_action_paper.db') for name in result),
            'DELETED_PRICE_ACTION_STORE_REAPPEARED')
    return result


def backup_gate(base, source):
    result = json.loads(regular(base / 'result.json').read_text())
    manifest = json.loads(regular(base / 'manifest.json').read_text())
    require(result.get('status') == 'OK' and manifest.get('source_volume') == VOLUME,
            'CURRENT_STAGING_RESTORE_TEST_REQUIRED')
    require(manifest.get('restore_test', {}).get('sha256_match') is True
            and manifest['restore_test'].get('postgres_restored_counts'),
            'SQLITE_AND_POSTGRES_RESTORE_PROOF_REQUIRED')
    sqlite_results = manifest['restore_test'].get('sqlite') or {}
    require(sqlite_results and all(r.get('integrity_check') == 'ok'
            and r.get('foreign_key_violations') == 0 for r in sqlite_results.values()),
            'SQLITE_INTEGRITY_OR_FOREIGN_KEY_RESTORE_CHECK_FAILED')
    archive = regular(manifest['archive'], under=base)
    require(sha(archive) == manifest.get('archive_sha256'), 'BACKUP_ARCHIVE_CHANGED')
    for rel, expected in manifest.get('source', {}).items():
        p = Path(rel)
        require(not p.is_absolute() and '..' not in p.parts, 'INVALID_BACKUP_MEMBER_PATH')
        st = regular(source / rel, under=source, source_file=True).stat()
        require([st.st_ino, st.st_size, st.st_mtime_ns] ==
                [expected.get('inode'), expected.get('size'), expected.get('mtime_ns')],
                'SOURCE_CHANGED_SINCE_VERIFIED_BACKUP')
    require(manifest.get('source'), 'EMPTY_BACKUP_MANIFEST')
    return {'archive_sha256': manifest['archive_sha256'], 'files': len(manifest['source']),
            'restore_test_at': manifest['restore_test']['at']}


def fresh_backup(tool, base, source):
    require(sha(regular(tool)) == BACKUP_SHA, 'BACKUP_TOOL_HASH_MISMATCH')
    phase('BACK_UP_STOPPED_STAGING_AND_VERIFY_SQLITE_POSTGRES_RESTORES')
    # The upstream tool's location setting changes only the output directory.
    # Its space reserve, source identity and restore checks remain unchanged.
    run(['env', 'STAGING_BACKUP_TEST_BASE=' + str(base), 'sh', str(tool), 'start'])
    deadline = time.monotonic() + 7200
    last = -1
    while not (base / 'result.json').exists():
        require(time.monotonic() < deadline, 'BACKUP_NOT_FINISHED_NO_START_PERFORMED')
        elapsed = int(time.monotonic() // 60)
        if elapsed != last:
            phase('WAITING_FOR_CONSISTENT_BACKUP_AND_DETACHED_RESTORE')
            last = elapsed
        time.sleep(3)
    return backup_gate(base, source)


def api_request(path, *, method='GET'):
    # This child is an HTTP client; it does not import or reinitialize the app.
    code = """import json,urllib.request,urllib.error
try:
 r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/'+PATH,method=METHOD),timeout=10)
 b=r.read(2097152)
 print(json.dumps({'http':r.status,'json':json.loads(b) if r.headers.get_content_type()=='application/json' else None}))
except urllib.error.HTTPError as e: print(json.dumps({'http':e.code}))
""".replace('PATH', repr(path.lstrip('/'))).replace('METHOD', repr(method))
    return json.loads(docker('exec', API, 'python', '-B', '-c', code, timeout=20))


def wait_healthy():
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        c = inspect(API)
        require(c['State']['Running'], 'ACCEPTANCE_API_EXITED')
        require(not c['State'].get('OOMKilled'), 'ACCEPTANCE_API_OOM')
        if (c['State'].get('Health') or {}).get('Status') == 'healthy':
            h = api_request('/health')
            data = h.get('json') or {}
            require(h['http'] == 200 and data.get('status') == 'ok'
                    and data.get('acceptance_read_only') is True
                    and data.get('features', {}).get('price_action_lab_enabled') is False,
                    'RESEARCH_ONLY_HEALTH_CHECK_FAILED')
            ledger = data.get('persistence', {}).get('ledger_supabase', {})
            require(ledger.get('configured') is True and ledger.get('connected') is True,
                    'CONFIGURED_PRIMARY_LEDGER_NOT_HEALTHY')
            return c
        time.sleep(3)
    raise Blocked('ACCEPTANCE_API_HEALTH_TIMEOUT')


def other_identities(before):
    return {k: v for k, v in before.items() if k != API}


def price_action_connections():
    container = inspect(API)
    cid = container['Id']
    processes = threads = opened = 0
    main_pid_seen = False
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            belongs = cid in (directory / 'cgroup').read_text()
        except OSError:
            continue
        if not belongs:
            continue
        processes += 1
        main_pid_seen |= int(directory.name) == int(container['State']['Pid'])
        threads += len(list((directory / 'task').iterdir()))
        for fd in (directory / 'fd').iterdir():
            try:
                target = os.readlink(fd)
            except FileNotFoundError:
                continue  # An FD can close while it is being inspected.
            if 'price_action' in Path(target).name:
                opened += 1
    require(main_pid_seen and processes > 0 and opened == 0, 'PRICE_ACTION_FILE_DESCRIPTOR_CHECK_FAILED')
    return {'api_processes_observed': processes, 'api_threads_observed': threads,
            'price_action_file_descriptors': opened}


def measure_idle(source, expected_files):
    c = inspect(API)
    pid = int(c['State']['Pid'])
    group = None
    for line in Path('/proc/%s/cgroup' % pid).read_text().splitlines():
        hierarchy, controllers, path = line.split(':', 2)
        if hierarchy == '0':
            group = Path('/sys/fs/cgroup' + path)
    require(group is not None and (group / 'cpu.stat').is_file(), 'CGROUP_V2_RESOURCE_COUNTERS_REQUIRED')
    def counters():
        cpu = dict(line.split() for line in (group / 'cpu.stat').read_text().splitlines())
        memory = dict(line.split() for line in (group / 'memory.stat').read_text().splitlines())
        reads = writes = 0
        for line in (group / 'io.stat').read_text().splitlines():
            for item in line.split()[1:]:
                key, _, value = item.partition('=')
                if key == 'rbytes':
                    reads += int(value)
                elif key == 'wbytes':
                    writes += int(value)
        return (int(cpu['usage_usec']), int((group / 'memory.current').read_text()),
                int(memory['anon']), reads, writes)
    cpu0, memory0, anon0, read0, write0 = counters()
    start_time = time.monotonic()
    peak = memory0
    anon_peak = anon0
    for _ in range(4):
        time.sleep(15)
        require(inspect(API)['State']['Running'], 'API_STOPPED_DURING_MEASUREMENT')
        require(file_state(source) == expected_files, 'FINANCIAL_WRITES_DURING_IDLE_MEASUREMENT')
        _, memory, anon, _, _ = counters()
        peak = max(peak, memory)
        anon_peak = max(anon_peak, anon)
    cpu1, _, _, read1, write1 = counters()
    seconds = time.monotonic() - start_time
    cpu_pct = (cpu1 - cpu0) / 1e6 / seconds * 100
    require(cpu_pct < 40, 'IDLE_RESEARCH_MODE_CPU_REMAINS_TOO_HIGH')
    require(anon_peak < MEMORY * 0.9, 'IDLE_RESEARCH_MODE_APP_MEMORY_REMAINS_TOO_HIGH')
    samples = []
    for _ in range(3):
        started = time.monotonic()
        require(api_request('/health')['http'] == 200, 'HEALTH_FAILED_DURING_MEASUREMENT')
        samples.append(round((time.monotonic() - started) * 1000))
    return {'idle_window_seconds': round(time.monotonic() - start_time),
            'api_cpu_percent_one_core': round(cpu_pct, 2),
            'api_container_memory_peak_bytes': peak,
            'api_app_memory_peak_bytes': anon_peak,
            'api_disk_read_bytes_per_second': round((read1 - read0) / seconds),
            'api_disk_write_bytes_per_second': round((write1 - write0) / seconds),
            'host_free_disk_bytes': os.statvfs('/').f_bavail * os.statvfs('/').f_frsize,
            'health_client_elapsed_ms': samples,
            'measurement_scope': '60_SECOND_IDLE_SMOKE_NOT_FULL_LOAD_ACCEPTANCE'}


def private_directory(path):
    directory = Path(path)
    regular(directory / 'ledger-gate.json')
    require(directory.is_relative_to(RUN_PARENT) and directory.name.startswith('nexus-task6-acceptance-'),
            'PRIVATE_OPERATION_DIRECTORY_REQUIRED')
    directory_stat = directory.lstat()
    require(stat.S_ISDIR(directory_stat.st_mode) and directory_stat.st_uid in (0, 1000)
            and not directory_stat.st_mode & 0o077, 'PRIVATE_OPERATION_DIRECTORY_REQUIRED')
    return directory


def start(args):
    require(os.geteuid() == 0, 'RUN_WITH_SUDO_REQUIRED')
    os.umask(0o077)
    directory = private_directory(args.run_directory)
    receipt = {'task6_complete': False, 'production_deployment_performed': False,
               'financial_execution_enabled': False, 'data_deleted': False}
    started = False
    lock = None
    try:
        ledger = json.loads((directory / 'ledger-gate.json').read_text())
        require(ledger.get('external_application_ledger_recovery_proven') is True
                and ledger.get('status') == 'RETAINED_EXTERNAL_LEDGER_BACKUP_AND_NEW_LOCAL_RESTORE_VERIFIED',
                'VERIFIED_EXTERNAL_APPLICATION_SNAPSHOT_REQUIRED')
        lock = open(LOCK_FILE, 'a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Blocked('ANOTHER_VPS_OPERATION_IS_RUNNING') from error
        phase('VERIFY_STOPPED_F9_STAGING_PRODUCTION_SEPARATION_AND_UNCHANGED_LIMITS')
        api, source, files, env_file = stopped_baseline()
        original = identities()
        require('nexus-ops-staging-backup-pgtest' not in original,
                'EXISTING_BACKUP_TEST_CONTAINER_REQUIRES_REVIEW')
        config_files = {str(p): sha(p) for p in [*files, env_file]}
        before = json.loads(compose(files, env_file, 'config', '--format', 'json'))
        hashes = dict(line.split(None, 1) for line in compose(files, env_file, 'config', '--hash', '*').splitlines())
        require(hashes.get('api') == api['Config']['Labels'].get('com.docker.compose.config-hash'),
                'COMPOSE_NO_LONGER_REPRODUCES_STOPPED_API')
        overlay = directory / 'compose.acceptance.yaml'
        overlay.write_text("services:\n  api:\n    environment:\n      NEXUS_ACCEPTANCE_READ_ONLY: '1'\n")
        after = json.loads(compose([*files, overlay], env_file, 'config', '--format', 'json'))
        validate_render(api, before, after)
        backup = fresh_backup(directory / 'staging-backup.sh', directory / 'backup', source)
        # The full restore is complete before any API recreation. Recheck races.
        current, _, _, _ = stopped_baseline()
        require(current['Id'] == api['Id'] and identities() == original, 'CONTAINERS_CHANGED_DURING_BACKUP')
        require(all(sha(p) == digest for p, digest in config_files.items()), 'CONFIG_CHANGED_DURING_BACKUP')
        financial_before = file_state(source)
        write_json(directory / 'rollback.json', {'original_files': [str(p) for p in files],
            'env_file': str(env_file), 'image': IMAGE, 'backup': backup,
            'safe_rollback': 'Recreate the original API with --no-start; never start normal execution automatically.'})
        phase('START_ONLY_EXISTING_STAGING_API_IN_RESEARCH_ACCEPTANCE_MODE')
        started = True  # A failed up can still have created/started the API.
        compose([*files, overlay], env_file, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'api')
        new = wait_healthy()
        require(preserved(new) == preserved(api), 'CONTAINER_CHANGED_BEYOND_ACCEPTANCE_FLAG')
        require(environment(new).get(MODE_KEY) == '1', 'ACCEPTANCE_MODE_NOT_CONFIGURED')
        require(other_identities(identities()) == other_identities(original), 'UNRELATED_CONTAINER_CHANGED')
        require(api_request('/api/v2/strategy-intelligence/cohorts')['http'] == 401,
                'UNAUTHENTICATED_RESEARCH_ACCESS_NOT_DENIED')
        for path in ('/webhook', '/paper-v2/orders', '/instances/task6-safety-probe/start'):
            require(api_request(path, method='POST')['http'] == 423, 'EXECUTION_POLICY_NOT_CLOSED')
        require(api_request('/app/strategy-intelligence')['http'] == 200, 'DASHBOARD_ASSET_NOT_AVAILABLE')
        require(file_state(source) == financial_before, 'FINANCIAL_FILES_CHANGED')
        phase('RESTART_ONLY_RESEARCH_ACCEPTANCE_API_AND_RECHECK_PERSISTENCE')
        docker('restart', '--time', '20', API, timeout=90)
        new = wait_healthy()
        require(preserved(new) == preserved(api), 'RESTART_CHANGED_PRESERVED_SETTINGS')
        require(file_state(source) == financial_before, 'FINANCIAL_FILES_CHANGED_AFTER_RESTART')
        docker('exec', INGRESS, 'nginx', '-t')
        docker('exec', INGRESS, 'nginx', '-s', 'reload')
        phase('MEASURE_RESEARCH_API_IDLE_RESOURCES_AND_FINANCIAL_FILE_PRESERVATION')
        measurement = measure_idle(source, financial_before)
        pa_connections = price_action_connections()
        require(other_identities(identities()) == other_identities(original), 'UNRELATED_CONTAINER_CHANGED')
        receipt.update(status='STRATEGY_INTELLIGENCE_STAGING_RESEARCH_MODE_STARTED',
            source_commit=COMMIT, api_image=IMAGE, api_container_id=new['Id'],
            acceptance_read_only=True, price_action_lab_enabled=False, backup=backup,
            api_memory_limit_bytes=MEMORY, api_memory_swap_limit_bytes=MEMORY,
            financial_file_identities_unchanged=True, restart_verified=True,
            unrelated_container_identities_unchanged=True,
            resource_smoke=measurement,
            runtime_file_descriptor_check=pa_connections,
            authenticated_prd_acceptance='PENDING_OWNER_BROWSER_CHECK',
            full_supabase_platform_recovery_proven=False,
            receipt_directory=str(directory),
            next_action='Sign in to the existing staging workspace and open Strategy Intelligence.')
    except Exception as error:
        # Never restore an old execution mode or financial state automatically.
        if started:
            try:
                docker('stop', '--time', '20', API, timeout=90)
                receipt['staging_left_stopped'] = not inspect(API)['State']['Running']
            except Exception:
                receipt['staging_left_stopped'] = False
        receipt.update(status='BLOCKED', reason=str(error) if isinstance(error, Blocked) else type(error).__name__)
    finally:
        if lock is not None:
            lock.close()
    write_json(directory / 'startup-receipt.json', receipt)
    print(json.dumps(receipt, indent=2), flush=True)
    return 0 if receipt['status'] != 'BLOCKED' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', required=True, action='store_true')
    parser.add_argument('--run-directory', required=True)
    sys.exit(start(parser.parse_args()))
