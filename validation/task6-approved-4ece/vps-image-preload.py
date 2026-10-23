"""Verify the approved CI ZIP and optionally preload images without deployment.

Only generic code and synthetic tests belong in git. Runtime receipts stay
private on the existing staging host. No Compose, service replacement, database
operation, application import, feed activation or paper execution is performed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import uuid
import zipfile

BASE = Path('/home/ubuntu/nexus-rc3-staging-28acdbc')
STAGE = 'nexus-remote-stage-rc3-28acdbc'
PROD = 'nexus-trading-bot'
API_MEMORY = 1610612736
RESERVE = 8 * 1024**3
DISABLED_CONTROLS = ('HUB_AUTO_ENGINE', 'HUB_ENABLE_EXTERNAL_LIVE', 'HUB_USE_LIVE_DATA',
                     'HUB_RESEARCH_AUTOSTART', 'NEXUS_SHADOW_SUPERVISED')
SCAN_FILES = ('source-security.json', 'api-security.json', 'api-all-secrets.json',
              'worker-security.json', 'worker-all-secrets.json')
POLICY = {
    'commit': '4eceb973e98e1350dd625153753637629453c401',
    'source_sha256': '82a41d03d5c049051d740d154a6c941efae01b4370f4b6d167b7167b54582ea8',
    'source_files': 1468,
    'zip_sha256': 'bcdcdcab3b3870cad1dbc6913e941979b2ec0b22e69e6726117d35e5bf0bb1da',
    'zip_bytes': 176365085,
    'images': {
        'api': {
            'config_id': 'sha256:09b524395a9706115b53c8c19e8cc4239b873dcb5450bd3d43700d3bff5f0321',
            'oci_manifest_digest': 'sha256:60b92270e1d2dbe21e84513b12602d35566ec65de4e5b4f3e853076d4f3a9ade',
            'source_files': 1468,
        },
        'worker': {
            'config_id': 'sha256:b16074411ac65ed2cc8c0d6981b0608eb25bebdf8445abdad1a04aa8913cda41',
            'oci_manifest_digest': 'sha256:6cfd023a115f8985b6298e6fd1f476064b40fda8781d604175e4d6cde20e0cc8',
            'source_files': 321,
        },
    },
}


class Blocked(RuntimeError):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.detail = detail


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def safe_name(name):
    path = PurePosixPath(name)
    require(bool(name) and not path.is_absolute() and '..' not in path.parts
            and '\\' not in name and str(path) == name, 'UNSAFE_PACKET_PATH')
    return path


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(archive, name):
    require(archive.getinfo(name).file_size <= 4*1024**2, 'PACKET_JSON_SIZE_LIMIT')
    return json.loads(archive.read(name))


def verify_export(stream, expected, policy):
    with tarfile.open(fileobj=stream, mode='r:gz') as archive:
        members = archive.getmembers()
        require(len(members) < 100 and len({m.name for m in members}) == len(members), 'EXPORT_MEMBER_LIMIT')
        for member in members:
            safe_name(member.name)
            require(member.isfile() or member.isdir(), 'EXPORT_LINK_OR_SPECIAL_FILE')
        def data(name):
            require(archive.getmember(name).size <= 4*1024**2, 'EXPORT_JSON_SIZE_LIMIT')
            return archive.extractfile(name).read()
        raw_index = data('index.json')
        index = json.loads(raw_index)
        require(len(index.get('manifests', [])) == 1, 'ONE_EXPORTED_IMAGE_REQUIRED')
        descriptor = index['manifests'][0]
        require(descriptor.get('digest') == expected['oci_manifest_digest'], 'EXPORT_OCI_MANIFEST_IDENTITY_MISMATCH')
        name = 'blobs/sha256/' + expected['oci_manifest_digest'].split(':')[1]
        raw_manifest = data(name)
        require('sha256:'+hashlib.sha256(raw_manifest).hexdigest() == expected['oci_manifest_digest'], 'EXPORT_MANIFEST_HASH_MISMATCH')
        manifest = json.loads(raw_manifest)
        require(manifest.get('config', {}).get('digest') == expected['config_id'], 'EXPORT_CONFIG_ID_MISMATCH')
        config_path = 'blobs/sha256/' + expected['config_id'].split(':')[1]
        raw_config = data(config_path)
        require('sha256:'+hashlib.sha256(raw_config).hexdigest() == expected['config_id'], 'EXPORT_CONFIG_HASH_MISMATCH')
        config = json.loads(raw_config)
        labels = config.get('config', {}).get('Labels', {})
        require(config.get('os') == 'linux' and config.get('architecture') == 'amd64'
                and labels.get('org.opencontainers.image.revision') == policy['commit']
                and labels.get('nexus.source.sha256') == policy['source_sha256'], 'EXPORT_SOURCE_OR_PLATFORM_MISMATCH')
        legacy = json.loads(data('manifest.json'))
        require(len(legacy) == 1 and legacy[0].get('Config') == config_path, 'EXPORT_DOCKER_CONFIG_ID_MISMATCH')
        rootfs = config.get('rootfs', {})
        diff_ids = rootfs.get('diff_ids')
        require(rootfs.get('type') == 'layers' and isinstance(diff_ids, list)
                and len(diff_ids) == len(manifest.get('layers', [])) == len(legacy[0].get('Layers', [])),
                'EXPORT_LAYER_INVENTORY_MISMATCH')
        # The entire ZIP is SHA-pinned, including all layer bytes. Docker's loader
        # handles those layers; do not confuse compressed OCI hashes with diff_ids.
        return {'config_id': expected['config_id'], 'export_oci_manifest_digest': expected['oci_manifest_digest'],
            'export_oci_index_digest': 'sha256:'+hashlib.sha256(raw_index).hexdigest(), 'rootfs_diff_ids': diff_ids}


def verify_packet(path, policy=POLICY):
    require(path.is_file() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode), 'REGULAR_PACKET_FILE_REQUIRED')
    require(path.stat().st_size == policy['zip_bytes'] and file_hash(path) == policy['zip_sha256'], 'APPROVED_ZIP_IDENTITY_MISMATCH')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'DUPLICATE_PACKET_INPUT')
        require(len(names) <= 1000 and sum(m.file_size for m in archive.infolist()) <= 256*1024**2, 'PACKET_RESOURCE_LIMIT')
        for member in archive.infolist():
            safe_name(member.filename)
            require(not member.is_dir() and not stat.S_ISLNK(member.external_attr >> 16), 'PACKET_LINK_OR_DIRECTORY')
        seal = read_json(archive, 'packet-integrity.json')
        require(seal.get('commit') == policy['commit'] and seal.get('source_sha256') == policy['source_sha256'], 'PACKET_SOURCE_IDENTITY_MISMATCH')
        rows = seal.get('files')
        require(isinstance(rows, list) and len(rows) == len({r['path'] for r in rows}), 'PACKET_SEAL_INCOMPLETE')
        require(set(names) == {'packet-integrity.json'} | {r['path'] for r in rows}, 'UNDECLARED_OR_MISSING_PACKET_INPUT')
        for row in rows:
            require(archive.getinfo(row['path']).file_size == row['bytes'], 'SEALED_FILE_SIZE_MISMATCH')
            with archive.open(row['path']) as stream:
                require(hashlib.file_digest(stream, 'sha256').hexdigest() == row['sha256'], 'SEALED_FILE_HASH_MISMATCH')
        source = read_json(archive, 'source-manifest.json')
        canonical = hashlib.sha256(json.dumps(source.get('files'), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        require(source.get('head') == policy['commit'] and source.get('source_sha256') == canonical == policy['source_sha256']
                and len(source['files']) == policy['source_files'], 'FROZEN_SOURCE_IDENTITY_MISMATCH')
        build = read_json(archive, 'build-proof.json')
        require(build.get('commit') == policy['commit'] and build.get('source_sha256') == policy['source_sha256'], 'CI_BUILD_SOURCE_MISMATCH')
        scans = {}
        for name in SCAN_FILES:
            scan = read_json(archive, name)
            results = scan.get('Results')
            require(isinstance(results, list) and bool(results) and all(isinstance(r, dict) for r in results), 'CI_SECURITY_EVIDENCE_INCOMPLETE')
            secrets = sum(len(r.get('Secrets', [])) for r in results)
            high = sum(v.get('Severity') in ('HIGH', 'CRITICAL') for r in results for v in r.get('Vulnerabilities', []))
            require(not secrets and not high, 'CI_SECURITY_EVIDENCE_FAILED')
            scans[name] = {'high_or_critical_vulnerabilities': high, 'secrets': secrets}
        exports = {}
        for role, expected in policy['images'].items():
            require(build.get('images', {}).get(role, {}).get('config_id') == expected['config_id']
                    and build['images'][role].get('source_files') == expected['source_files'], 'CI_IMAGE_IDENTITY_MISMATCH')
            with archive.open(role + '.tar.gz') as stream:
                exports[role] = verify_export(stream, expected, policy)
        return {'source': source, 'build': build, 'scans': scans, 'exports': exports,
                'files': rows, 'sealed_files_verified': len(rows)}


def docker(args, input_text=None, timeout=120):
    environment = {k: v for k, v in os.environ.items() if k not in
        ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')}
    try:
        result = subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', *args],
            input=input_text, text=True, capture_output=True, env=environment, timeout=timeout)
    except FileNotFoundError:
        raise Blocked('DOCKER_CLIENT_NOT_AVAILABLE') from None
    except subprocess.TimeoutExpired:
        raise Blocked('DOCKER_OPERATION_TIMEOUT:' + args[0]) from None
    if result.returncode:
        # Commands use only the local socket, public image inputs and fixed
        # probes. Never include inspect stdout (which contains credentials).
        raise Blocked('DOCKER_OPERATION_FAILED:' + args[0] + ':exit=' + str(result.returncode),
            detail=(result.stderr or '')[-1500:])
    return result.stdout


def containers(project, command):
    ids = command(['ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project]).split()
    require(bool(ids), 'EXISTING_PROJECT_REQUIRED:' + project)
    return json.loads(command(['inspect', *ids]))


def fingerprint(rows):
    # Compare all container configuration (including credentials) in memory;
    # never print or persist its contents or hashes in an operator receipt.
    values = [{k: c[k] for k in ('Id', 'Name', 'Image', 'Config', 'HostConfig', 'Mounts', 'RestartCount')}
              | {'started': c['State']['StartedAt'], 'status': c['State']['Status'],
                 'oom': c['State']['OOMKilled']} for c in rows]
    return json.dumps(sorted(values, key=lambda c: c['Id']), sort_keys=True, separators=(',', ':'))


def preflight(command):
    stage = containers(STAGE, command)
    production = containers(PROD, command)
    candidates = [c for c in stage if c['Name'] == '/' + STAGE + '-api-1']
    require(len(candidates) == 1, 'ONE_EXISTING_STAGE_API_REQUIRED')
    api = candidates[0]
    require(api['State']['Status'] == 'running' and api['State'].get('Health', {}).get('Status') == 'healthy', 'HEALTHY_EXISTING_STAGE_API_REQUIRED')
    require(api['HostConfig']['Memory'] == API_MEMORY and api['HostConfig']['MemorySwap'] == API_MEMORY, 'API_1536_MIB_MEMORY_OVERRIDE_REQUIRED')
    prod_sources = {m['Source'] for c in production for m in c['Mounts']}
    require(not any(m['Source'] in prod_sources for c in stage for m in c['Mounts']), 'SHARED_PRODUCTION_STORAGE')
    require(any(m['Destination'] == '/var/lib/tradexa' and m.get('Name') == STAGE+'_api-state' for m in api['Mounts']), 'EXISTING_STAGE_API_STATE_VOLUME_REQUIRED')
    env = dict(value.split('=', 1) for value in api['Config'].get('Env', []) if '=' in value)
    require(all(env.get(name) == '0' for name in DISABLED_CONTROLS), 'DISABLED_STAGING_CONTROLS_REQUIRED')
    return {'stage': fingerprint(stage), 'production': fingerprint(production), 'api': api}


PROBE = r'''
import hashlib,json,os,pathlib,sys,psycopg
expected=json.load(sys.stdin)
root=pathlib.Path('/app')
bad=[name for name,value in expected.items() if not (root/name).is_file() or hashlib.sha256((root/name).read_bytes()).hexdigest()!=value]
role=os.environ['NEXUS_TASK6_PROBE_ROLE']
boundary=all((root/'automation-hub'/folder/'index.html').is_file() for folder in ('webui','landing')) if role=='api' else not any((root/name).exists() for name in ('automation-hub/app.py','automation-hub/execution','automation-hub/webui','.env'))
print(json.dumps({'source_files':len(expected),'mismatches':len(bad),'python':sys.version.split()[0],
    'uid':os.geteuid(),'pq':psycopg.pq.__impl__,'boundary_ok':boundary}))
'''


def expected_files(source, role):
    rows = source['files'] if role == 'api' else [r for r in source['files'] if (
        r['path'].startswith(('bot/', 'tradexa/', 'automation-hub/strategies/', 'automation-hub/migrations/shadow/', 'automation-hub/migrations/workers/'))
        or r['path'].startswith(('automation-hub/services/', 'automation-hub/data/')) and r['path'].endswith('.py')
        or r['path'] in {'pyproject.toml', 'README.md', 'LICENSE', 'ops/staging-initialize.py', 'automation-hub/data/pr6_real_paper_freeze.json'})]
    return {r['path']: r['sha256'] for r in rows}


def inspect_loaded(export, policy, command):
    """Support both stores without accepting a tag or an unverified digest.

    The classic store resolves the config digest; containerd resolves a target
    descriptor (manifest/index). All candidates are bound to the SHA-pinned
    archive, with the full manifest -> config -> rootfs chain checked above.
    """
    allowed = {export['config_id']: 'CONFIG', export['export_oci_manifest_digest']: 'OCI_MANIFEST',
               export['export_oci_index_digest']: 'OCI_INDEX'}
    candidates = (export['export_oci_manifest_digest'], export['config_id'], export['export_oci_index_digest'])
    missing = []
    for reference in candidates:
        try:
            metadata = json.loads(command(['image', 'inspect', reference]))
        except Blocked as error:
            if 'no such image' in (error.detail or '').lower():
                missing.append(reference)
                continue
            raise
        require(len(metadata) == 1, 'ONE_LOADED_IMAGE_REQUIRED')
        image = metadata[0]
        labels = image.get('Config', {}).get('Labels', {})
        require(image.get('Id') in allowed and image.get('Os') == 'linux' and image.get('Architecture') == 'amd64'
                and labels.get('org.opencontainers.image.revision') == policy['commit']
                and labels.get('nexus.source.sha256') == policy['source_sha256'], 'LOADED_IMAGE_IDENTITY_MISMATCH')
        descriptor = image.get('Descriptor')
        require(descriptor is None or descriptor.get('digest') in allowed, 'LOADED_DESCRIPTOR_IDENTITY_MISMATCH')
        rootfs = image.get('RootFS', {})
        require(rootfs.get('Type') == 'layers' and (rootfs.get('Layers') or []) == export['rootfs_diff_ids'],
                'LOADED_ROOTFS_IDENTITY_MISMATCH')
        return image, allowed[image['Id']]
    raise Blocked('APPROVED_IMAGE_NOT_FOUND_AFTER_LOAD', detail='Unresolved approved digests: '+', '.join(missing))


def write_report(directory, report):
    path = directory / 'status.json'
    temporary = directory / 'status.tmp'
    with temporary.open('w') as output:
        json.dump(report, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def preload(packet, base=BASE, policy=POLICY, command=docker):
    verified = verify_packet(packet, policy)
    require(base.is_dir() and not base.is_symlink(), 'EXISTING_STAGE_BASE_REQUIRED')
    require(shutil.disk_usage(base).free >= RESERVE + 2*1024**3, 'IMAGE_LOAD_DISK_RESERVE')
    before = preflight(command)
    directory = base / ('task6-image-preload-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12])
    directory.mkdir(mode=0o700)
    report = {'status': 'RUNNING', 'scope': 'APPROVED_IMAGE_CACHE_AND_ISOLATED_PROBES_ONLY',
        'commit': policy['commit'], 'source_sha256': policy['source_sha256'],
        'zip_sha256': policy['zip_sha256'], 'sealed_files_verified': verified['sealed_files_verified'],
        'ci_security_reports': verified['scans'], 'security_scan_performed_on_vps': False,
        'services_updated': False, 'application_started': False, 'new_paper_execution': False,
        'deployment_performed': False, 'task6_complete': False, 'images': {}, 'receipt_directory': str(directory)}
    write_report(directory, report)
    try:
        with zipfile.ZipFile(packet) as archive:
            for role, expected in policy['images'].items():
                require(shutil.disk_usage(base).free >= RESERVE + 1024**3, 'IMAGE_LOAD_DISK_RESERVE')
                name = role + '.tar.gz'
                row = next(r for r in verified['files'] if r['path'] == name)
                target = directory / name
                with archive.open(name) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024**2)
                target.chmod(0o600)
                require(target.stat().st_size == row['bytes'] and file_hash(target) == row['sha256'], 'IMAGE_EXPORT_EXTRACTION_MISMATCH')
                print(json.dumps({'phase': 'LOAD_APPROVED_IMAGE_CACHE_ONLY', 'role': role}), flush=True)
                load_output = command(['image', 'load', '--input', str(target)], timeout=300)
                report['last_load_output'] = load_output[-1500:]
                write_report(directory, report)
                image, identity_kind = inspect_loaded(verified['exports'][role], policy, command)
                inventory = expected_files(verified['source'], role)
                require(len(inventory) == expected['source_files'], 'FROZEN_IMAGE_INVENTORY_COUNT_MISMATCH')
                print(json.dumps({'phase': 'ISOLATED_READ_ONLY_SOURCE_PROBE', 'role': role}), flush=True)
                probe = json.loads(command(['run', '--rm', '-i', '--pull=never', '--network', 'none', '--read-only',
                    '--cpus', '0.5', '--memory', '512m', '--memory-swap', '512m', '--pids-limit', '64', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges:true', '--env', 'NEXUS_TASK6_PROBE_ROLE='+role,
                    '--entrypoint', 'python', image['Id'], '-B', '-c', PROBE],
                    input_text=json.dumps(inventory), timeout=180))
                require(probe.get('source_files') == expected['source_files'] and probe.get('mismatches') == 0
                        and probe.get('boundary_ok') is True and probe.get('pq') == 'binary'
                        and isinstance(probe.get('uid'), int) and probe['uid'] > 0
                        and probe.get('python', '').startswith('3.11.' if role == 'api' else '3.12.'), 'ISOLATED_IMAGE_PROBE_FAILED')
                report['images'][role] = {**verified['exports'][role], 'repo_digests': image.get('RepoDigests', []),
                    'docker_image_id': image['Id'], 'docker_image_id_kind': identity_kind,
                    'archive_sha256': row['sha256'], 'probe': probe}
                write_report(directory, report)
        after = preflight(command)
        require(after['stage'] == before['stage'] and after['production'] == before['production'], 'CONTAINER_IDENTITIES_CHANGED')
        require(shutil.disk_usage(base).free >= RESERVE, 'IMAGE_LOAD_DISK_RESERVE')
        report.update(status='APPROVED_IMAGES_LOADED_AND_VERIFIED_NO_SERVICES_UPDATED',
            production_container_identities_unchanged=True, staging_container_identities_unchanged=True,
            api_health_after='healthy', api_memory_limit_bytes_after=API_MEMORY, api_memory_swap_limit_bytes_after=API_MEMORY,
            free_bytes_after=shutil.disk_usage(base).free, completed_at_utc=datetime.now(timezone.utc).isoformat())
        write_report(directory, report)
        return report
    except Exception as error:
        report.update(status='BLOCKED', error_type=type(error).__name__, reason=str(error) if isinstance(error, Blocked) else None,
            detail=getattr(error, 'detail', None))
        try:
            after = preflight(command)
            report['production_container_identities_unchanged'] = after['production'] == before['production']
            report['staging_container_identities_unchanged'] = after['stage'] == before['stage']
        except Exception:
            report['container_postcheck'] = 'UNVERIFIED'
        write_report(directory, report)
        if isinstance(error, Blocked):
            error.receipt_directory = str(directory)
        raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packet', type=Path)
    parser.add_argument('--load', action='store_true', help='Preload exact images and run isolated probes; never update services.')
    args = parser.parse_args()
    if args.load:
        report = preload(args.packet)
    else:
        verified = verify_packet(args.packet)
        report = {'status': 'APPROVED_PACKET_VERIFIED_NO_IMAGES_LOADED', 'commit': POLICY['commit'],
            'source_sha256': POLICY['source_sha256'], 'sealed_files_verified': verified['sealed_files_verified'],
            'exports': verified['exports'], 'services_updated': False, 'task6_complete': False}
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'BLOCKED', 'error_type': type(error).__name__,
            'reason': str(error) if isinstance(error, Blocked) else None,
            'detail': getattr(error, 'detail', None),
            'receipt_directory': getattr(error, 'receipt_directory', None),
            'services_updated': False, 'deployment_performed': False, 'task6_complete': False}), flush=True)
        raise SystemExit(1)
