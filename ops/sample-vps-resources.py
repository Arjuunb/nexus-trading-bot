#!/usr/bin/env python3
"""Plan by default. Run manually on an explicitly authorized target, never SSH.

Only fixed read-only metadata commands are used. No environment, secret file,
financial database, cleanup, deployment or service-control capability exists.
"""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def validate_window(start, end, now, *, duration):
    if any(value.tzinfo is None for value in (start, end, now)):
        raise ValueError('AUTHORIZATION_WINDOW_REQUIRES_TIMEZONE')
    if not start <= now < end or duration < 0 or (end - now).total_seconds() < duration:
        raise ValueError('AUTHORIZATION_WINDOW_CLOSED_OR_TOO_SHORT')


def cpu_usage(before, after):
    # Linux user/nice counters already include guest/guest_nice.
    deltas = [b - a for a, b in zip(before[:8], after[:8])]
    if len(deltas) != 8 or min(deltas) < 0 or sum(deltas) <= 0:
        return {'status': 'UNKNOWN', 'reason': 'CPU_COUNTER_RESET_OR_EMPTY_INTERVAL'}
    total = sum(deltas)
    return {'busy_percent': round(100 * (total - deltas[3] - deltas[4]) / total, 3),
            'steal_percent': round(100 * deltas[7] / total, 3),
            'scope': 'WHOLE_HOST_INTERVAL; NOT_SINGLE_CONTAINER_CPU'}


def plan(samples, interval):
    if not 2 <= samples <= 60 or not 1 <= interval <= 30 or samples * interval > 300:
        raise ValueError('BOUNDED_SAMPLING_LIMIT')
    return {'schema_version': 'nexus_read_only_resource_sampling.v1', 'status': 'NOT_RUN',
            'samples': samples, 'interval_seconds': interval,
            'maximum_duration_seconds': min(300, 30 + samples * (interval + 5)),
            'remote_connection': False, 'deployment_authorization': False,
            'scope': 'MANUAL_OPERATOR_EXECUTION_ON_AUTHORIZED_TARGET_ONLY',
            'output_limit_bytes': 4 * 1024 * 1024,
            'limitation': 'SHORT_BOUNDED_SAMPLE; REPRESENTATIVE_LOAD_AND_LONGER_TERM_GROWTH_REMAIN_REVIEW_REQUIREMENTS'}


def metadata(command, *, json_lines=False, deadline=None, window_end=None):
    remaining = 5.0
    if deadline is not None:
        remaining = min(remaining, deadline - time.monotonic())
    if window_end is not None:
        remaining = min(remaining, (window_end - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        return {'status': 'UNKNOWN', 'reason': 'SAMPLING_BUDGET_OR_WINDOW_EXHAUSTED'}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=remaining,
                                env={key: value for key, value in os.environ.items()
                                     if key in {'PATH', 'DOCKER_HOST', 'DOCKER_CONFIG'}})
    except (OSError, subprocess.TimeoutExpired):
        return {'status': 'UNKNOWN', 'reason': 'COMMAND_UNAVAILABLE_OR_TIMEOUT'}
    if result.returncode:
        # Never retain untrusted daemon errors, credentials or full inspect output.
        return {'status': 'UNKNOWN', 'reason': 'READ_ONLY_COMMAND_FAILED', 'exit_code': result.returncode}
    if len(result.stdout.encode()) > 256 * 1024:
        return {'status': 'UNKNOWN', 'reason': 'COMMAND_OUTPUT_LIMIT'}
    if json_lines:
        try:
            return [json.loads(line) for line in result.stdout.splitlines() if line]
        except ValueError:
            return {'status': 'UNKNOWN', 'reason': 'COMMAND_FORMAT_UNAVAILABLE'}
    return result.stdout.strip()


def cpu_counters():
    return [int(value) for value in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]


def collect(args):
    start = datetime.fromisoformat(args.window_start)
    end = datetime.fromisoformat(args.window_end)
    budget = plan(args.samples, args.interval)['maximum_duration_seconds']
    validate_window(start, end, datetime.now(timezone.utc), duration=budget)
    deadline = time.monotonic() + budget
    def probe(command, **kwargs):
        return metadata(command, deadline=deadline, window_end=end, **kwargs)
    if os.environ.get('DOCKER_HOST') and not os.environ['DOCKER_HOST'].startswith('unix://'):
        raise ValueError('TARGET_LOCAL_DOCKER_ONLY')
    if os.environ.get('DOCKER_CONTEXT') not in (None, '', 'default'):
        raise ValueError('TARGET_LOCAL_DOCKER_ONLY')
    ipaddress.ip_address(args.target)
    addresses = probe(['ip', '-j', '-4', 'address', 'show'])
    try:
        local = {item['local'] for device in json.loads(addresses)
                 for item in device.get('addr_info', []) if item.get('family') == 'inet'}
    except (ValueError, TypeError, KeyError):
        raise ValueError('TARGET_ADDRESS_UNVERIFIED') from None
    if args.target not in local:
        raise ValueError('TARGET_ADDRESS_MISMATCH')
    storage_raw = probe(['docker', 'info', '--format',
        '{"driver":{{json .Driver}},"root":{{json .DockerRootDir}},"cpus":{{json .NCPU}},"memory_bytes":{{json .MemTotal}}}'])
    storage = {'status': 'UNKNOWN'}
    docker_filesystem = {'status': 'UNKNOWN'}
    try:
        storage = json.loads(storage_raw)
        directory = Path(storage['root'])
        if directory.is_absolute() and directory.is_dir():
            values = os.statvfs(directory)
            docker_filesystem = {'free_bytes': values.f_bavail * values.f_frsize,
                                'total_bytes': values.f_blocks * values.f_frsize,
                                'available_inodes': values.f_favail, 'total_inodes': values.f_files,
                                'path': str(directory), 'scope': 'DOCKER_ROOT_FILESYSTEM_METADATA_ONLY'}
    except (ValueError, TypeError, KeyError, OSError):
        pass
    result = {'schema_version': 'nexus_read_only_resource_sampling.v1', 'status': 'INCOMPLETE',
              'target': args.target, 'created_at': datetime.now(timezone.utc).isoformat(),
              'host_key_trust': 'OPERATOR_ATTESTED_INDEPENDENT_VERIFICATION; NOT_VERIFIED_BY_THIS_SAMPLER',
              'remote_connection': False, 'deployment_authorization': False, 'samples': [],
              'metadata': {
                  'kernel': probe(['uname', '-srvmo']),
                  'os': probe(['cat', '/etc/os-release']),
                  'docker_version': probe(['docker', 'version', '--format', '{{json .Server.Version}}']),
                  'compose_version': probe(['docker', 'compose', 'version', '--short']),
                  'docker_storage': storage,
                  'docker_root_filesystem': docker_filesystem,
                  'containers': probe(['docker', 'ps', '-a', '--format',
                      '{{json .Names}} {{json .Image}} {{json .Status}} {{json .Ports}}']),
                  'networks': probe(['docker', 'network', 'ls', '--format',
                      '{"name":{{json .Name}},"driver":{{json .Driver}},"scope":{{json .Scope}}}'], json_lines=True),
                  'volumes': probe(['docker', 'volume', 'ls', '--format',
                      '{"name":{{json .Name}},"driver":{{json .Driver}}}'], json_lines=True),
                  'ports': probe(['ss', '-lntup']),
                  'updates': probe(['apt', 'list', '--upgradable']),
                  'reboot_marker_present': Path('/run/reboot-required').exists(),
                  'host_cgroup_cpu_max': Path('/sys/fs/cgroup/cpu.max').read_text().strip()
                      if Path('/sys/fs/cgroup/cpu.max').is_file() else 'UNKNOWN',
                  'host_cgroup_memory_max': Path('/sys/fs/cgroup/memory.max').read_text().strip()
                      if Path('/sys/fs/cgroup/memory.max').is_file() else 'UNKNOWN',
                  'docker_socket_mode': oct(Path('/var/run/docker.sock').stat().st_mode & 0o777)
                      if Path('/var/run/docker.sock').exists() else 'UNKNOWN',
              }}
    previous = cpu_counters()
    for _ in range(args.samples):
        if time.monotonic() >= deadline:
            break
        validate_window(start, end, datetime.now(timezone.utc), duration=0)
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
        validate_window(start, end, datetime.now(timezone.utc), duration=0)
        current = cpu_counters()
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            if key in {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}:
                memory[key + '_bytes'] = int(value.strip().split()[0]) * 1024
        disk = shutil.disk_usage('/')
        fs = os.statvfs('/')
        result['samples'].append({'recorded_at': datetime.now(timezone.utc).isoformat(),
            'whole_host_cpu': cpu_usage(previous, current), 'memory': memory,
            'root_filesystem': {'total_bytes': disk.total, 'used_bytes': disk.used, 'free_bytes': disk.free,
                                'total_inodes': fs.f_files, 'available_inodes': fs.f_favail},
            'load_average': list(os.getloadavg()),
            'docker_resources': probe(['docker', 'stats', '--no-stream', '--format', '{{json .}}'], json_lines=True)})
        previous = current
        if time.monotonic() >= deadline:
            break
    validate_window(start, end, datetime.now(timezone.utc), duration=0)
    result['status'] = ('BOUNDED_SAMPLE_COMPLETE; NOT_SUSTAINED_CAPACITY_CERTIFICATION'
                        if len(result['samples']) == args.samples else 'PARTIAL; SAMPLING_BUDGET_EXHAUSTED')
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=12)
    parser.add_argument('--interval', type=int, default=5)
    parser.add_argument('--target', default='57.129.155.230')
    parser.add_argument('--window-start')
    parser.add_argument('--window-end')
    parser.add_argument('--authorize-read-only', action='store_true')
    parser.add_argument('--independent-host-key-verified', action='store_true')
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--output')
    output.add_argument('--stdout', action='store_true', help='Emit sanitized JSON without creating a target report file')
    args = parser.parse_args(argv)
    result = plan(args.samples, args.interval)
    if not args.authorize_read_only:
        print(json.dumps(result))
        return 0
    if not args.independent_host_key_verified or not args.window_start or not args.window_end:
        raise ValueError('AUTHORIZATION_WINDOW_AND_INDEPENDENT_HOST_KEY_REQUIRED')
    out = None
    if not args.stdout:
        if not args.output:
            raise ValueError('NEW_PRIVATE_REPORT_PATH_REQUIRED')
        out = Path(args.output)
        if not out.is_absolute() or out.exists() or out.is_symlink() or not out.parent.is_dir():
            raise ValueError('NEW_PRIVATE_REPORT_PATH_REQUIRED')
    # This is a local report path, not a remote transfer or executable input.
    start, end = datetime.fromisoformat(args.window_start), datetime.fromisoformat(args.window_end)
    validate_window(start, end, datetime.now(timezone.utc), duration=result['maximum_duration_seconds'])
    result = collect(args)
    payload = json.dumps(result, indent=2).encode()
    if len(payload) > 4 * 1024 * 1024:
        raise ValueError('REPORT_SIZE_LIMIT')
    if args.stdout:
        print(payload.decode())
        return 0
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
    print(json.dumps({'status': result['status'], 'report': str(out), 'deployment_authorization': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
