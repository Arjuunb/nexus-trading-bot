#!/usr/bin/env python3
"""Recover a NEW local review packet from retained images and frozen source.

No rebuild, remote Docker context, publication, transfer, deployment or Git
mutation. Completed packets are never overwritten. Original absent archives
and test logs are not silently recreated under their historical identities.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import selectors
import subprocess
import sys
import tarfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'automation-hub'))
from services.release_source import bundle_source, excluded
from services.remote_staging import sha256_file, verify_review_archive

API = 'sha256:2a06435876742ee690521ed21828d6ae093c92f0474834d71ccc68e7a7b531c3'
WORKER = 'sha256:6ef0a094f2aa599bc5411d7f151512df28f3974912b51996d90611c1216728a5'
POSTGRES = 'sha256:81bd698b4594e751a3269e4dcd3e03a4a0ec0daf7b72e7aa1abd43cce9887542'
INGRESS = 'sha256:6769dc3a703c719c1d2756bda113659be28ae16cf0da58dd5fd823d6b9a050ea'
BUILD_SOURCE = 'e7de53f84034262433699c91588f1b60f8915e82861cbd0fd02de6b73cd44dc1'
PRIOR_REVIEW = 'release-review:d2e56e5a57000551e18ea9a6be43668b6824ff40b9b5ecc83981a06a27091b8c'
OPERATOR_CHANGES = {'ops/staging.local.compose.yaml', 'ops/staging.remote.compose.yaml',
                    'ops/recover-staging-release.py', 'ops/sample-vps-resources.py',
                    'ops/prepare-staging-bindings.py'}
SECRET_ENV = {'HUB_SECRET', 'HUB_PASSWORD', 'HUB_CONTROL_KEY', 'HUB_WEBHOOK_SECRET',
              'SUPABASE_KEY', 'SUPABASE_SERVICE_ROLE_KEY', 'HUB_EXCHANGE_API_KEY',
              'HUB_EXCHANGE_API_SECRET', 'BINANCE_API_KEY', 'BINANCE_API_SECRET', 'SSH_PRIVATE_KEY'}


def private_path(name):
    path = PurePosixPath(name)
    return (excluded(path) or any(part.lower().startswith('.env') or part.lower() in {'.ssh', '.aws', '.codex'} for part in path.parts)
            or path.suffix.lower() in {'.pem', '.key', '.p12', '.pfx'}
            or path.name in {'id_ed25519', 'id_rsa', 'compose.env', 'app.env', 'tls.key'})


def _operator_only(name):
    return name in OPERATOR_CHANGES or name.startswith(('automation-hub/docs/', 'automation-hub/tests/', 'tests/'))


def source_correspondence(build, current):
    old = {row['path']: row for row in build['files']}
    new = {row['path']: row for row in current['files']}
    changed = sorted(name for name in old if name not in new or old[name]['sha256'] != new[name]['sha256'])
    added = sorted(set(new) - set(old))
    if any(not _operator_only(name) for name in changed + added):
        raise ValueError('SOURCE_IMAGE_MISMATCH; FROZEN_RUNTIME_LOCKS_RULES_AND_BUILD_INPUTS_MUST_MATCH')
    return {'runtime_source_matches': True, 'changed_operator_files': changed,
            'added_operator_files': added, 'matched_build_source_files': len(old) - len(changed),
            'scope': 'OPERATOR_CONFIGURATION_DOCS_TESTS_MAY_DIFFER; RUNTIME_IMAGE_UNCHANGED'}


def _docker_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or not name:
        raise ValueError('INVALID_DOCKER_PATH')
    return path


def verify_docker_archive(path, expected_id):
    with tarfile.open(path, 'r:*') as archive:
        entries = archive.getmembers()
        if len(entries) > 10000 or sum(row.size for row in entries) > 4 * 1024 ** 3:
            raise ValueError('DOCKER_EXPORT_SIZE_LIMIT')
        names = set()
        for row in entries:
            _docker_path(row.name)
            if row.name in names or row.issym() or row.islnk():
                raise ValueError('INVALID_DOCKER_PATH')
            names.add(row.name)
        manifests = json.load(archive.extractfile('manifest.json'))
        if len(manifests) != 1:
            raise ValueError('ONE_IMMUTABLE_IMAGE_PER_EXPORT_REQUIRED')
        manifest = manifests[0]
        _docker_path(manifest['Config'])
        raw = archive.extractfile(manifest['Config']).read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024 or 'sha256:' + hashlib.sha256(raw).hexdigest() != expected_id:
            raise ValueError('IMAGE_CONFIG_ID_MISMATCH')
        config = json.loads(raw)
        if any(line.partition('=')[0] in SECRET_ENV for line in config.get('config', {}).get('Env', [])):
            raise ValueError('PRIVATE_IMAGE_INPUT_IN_CONFIG')
        diffs = config['rootfs']['diff_ids']
        if len(diffs) != len(manifest['Layers']):
            raise ValueError('LAYER_HASH_MISMATCH')
        layer_sizes = []
        for name, expected in zip(manifest['Layers'], diffs):
            _docker_path(name)
            value = hashlib.sha256()
            with archive.extractfile(name) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    value.update(chunk)
            if 'sha256:' + value.hexdigest() != expected:
                raise ValueError('LAYER_HASH_MISMATCH')
            layer_sizes.append(archive.getmember(name).size)
            # Inspect names only; do not extract base-system or private values.
            with archive.extractfile(name) as stream, tarfile.open(fileobj=stream, mode='r|*') as layer:
                for entry in layer:
                    normalized = str(PurePosixPath(entry.name))
                    if normalized.startswith('app/') and entry.isfile() and private_path(normalized):
                        raise ValueError('PRIVATE_IMAGE_INPUT_IN_LAYER')
        return {'config_id': expected_id, 'verified_layers': len(diffs), 'layer_diff_ids': diffs,
                'uncompressed_layer_bytes': layer_sizes, 'private_application_inputs': [],
                'uncompressed_archive_bytes': sum(row.size for row in entries)}


def _git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True)


def _docker_env(directory):
    if os.environ.get('DOCKER_HOST') and not os.environ['DOCKER_HOST'].startswith('unix://'):
        raise ValueError('LOCAL_DOCKER_SOCKET_REQUIRED')
    if os.environ.get('DOCKER_CONTEXT') not in (None, '', 'default'):
        raise ValueError('LOCAL_DOCKER_SOCKET_REQUIRED')
    directory.mkdir(mode=0o700)
    return {'PATH': os.environ['PATH'], 'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONFIG': str(directory)}


def _docker(command, env):
    result = subprocess.run(['docker', *command], env=env, capture_output=True, check=True, timeout=90)
    return result.stdout


def _image_python(image, code, env):
    return json.loads(_docker(['run', '--rm', '--pull=never', '--network', 'none', '--read-only',
        '--cpus', '.5', '--memory', '256m', '--pids-limit', '64', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges:true', '--entrypoint', 'python', image, '-B', '-c', code], env))


def _save(image, path, env):
    with path.open('xb') as output, gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressed:
        with subprocess.Popen(['docker', 'save', image], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            deadline = time.monotonic() + 300
            size = 0
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ValueError('LOCAL_IMAGE_EXPORT_TIMEOUT')
                        ready = selector.select(min(5, remaining))
                        if not ready:
                            continue
                        chunk = os.read(process.stdout.fileno(), 1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > 4 * 1024 ** 3:
                            raise ValueError('LOCAL_IMAGE_EXPORT_SIZE_LIMIT')
                        compressed.write(chunk)
                if process.wait(timeout=max(1, deadline - time.monotonic())):
                    raise ValueError('LOCAL_IMAGE_EXPORT_FAILED')
            except BaseException:
                process.kill()
                process.wait(timeout=10)
                raise


def _source_tar(context, path):
    with path.open('xb') as output, gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w|') as archive:
            for source in sorted(context.rglob('*')):
                if not source.is_file():
                    continue
                info = archive.gettarinfo(str(source), arcname=str(source.relative_to(context)))
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ''
                with source.open('rb') as stream:
                    archive.addfile(info, stream)


def reconstruct(output, *, evidence=()):
    out = Path(output)
    if not out.is_absolute() or out.exists() or out.is_symlink() or ROOT in out.resolve().parents:
        raise ValueError('NEW_PACKET_OUTSIDE_REPOSITORY_REQUIRED')
    if any(parent.is_symlink() for parent in out.parents):
        raise ValueError('PACKET_SYMLINK_FORBIDDEN')
    temporary = out.with_name(out.name + '.' + uuid.uuid4().hex + '.partial')
    temporary.mkdir(mode=0o700, parents=True)
    env = _docker_env(temporary / 'docker-client')
    discovered = [name for name in _git('ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0') if name]
    paths = [name for name in discovered if not private_path(name)]
    status = _git('status', '--porcelain=v1', '-uall')
    head = _git('rev-parse', 'HEAD').strip()
    source = bundle_source(ROOT, temporary / 'source', paths, head=head,
                           dirty_paths={row[3:]: row[:2] for row in status.splitlines()})
    build_proof = _image_python(API, """import hashlib,json,pathlib,sys,importlib.metadata as metadata,psycopg
root=pathlib.Path('/app'); m=json.loads((root/'release-source.json').read_text())
bad=[r['path'] for r in m['files'] if hashlib.sha256((root/r['path']).read_bytes()).hexdigest()!=r['sha256']]
bundles={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('automation-hub/webui','automation-hub/landing') for p in (root/folder).rglob('*') if p.is_file()}
print(json.dumps({'manifest':m,'conflicts':bad,'bundles':bundles,'python':sys.version,'pq_implementation':psycopg.pq.__impl__,'dependencies':{d.metadata['Name'].lower().replace('_','-'):d.version for d in metadata.distributions()}}))""", env)
    build = build_proof['manifest']
    canonical = hashlib.sha256(json.dumps(build['files'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if build_proof['conflicts'] or canonical != build['source_sha256'] or canonical != BUILD_SOURCE:
        raise ValueError('RETAINED_IMAGE_BUILD_SOURCE_MISMATCH')
    correspondence = source_correspondence(build, source)
    import re
    required = {}
    for line in (ROOT / 'ops/api-requirements.lock').read_text().splitlines():
        match = re.match(r'^([A-Za-z0-9_.-]+)==([^\s;\\]+)', line)
        if match and ';' not in line:
            required[match.group(1).lower().replace('_', '-')] = match.group(2)
    if (not build_proof['python'].startswith('3.11.') or build_proof['pq_implementation'] != 'binary'
            or any(build_proof['dependencies'].get(name) != value for name, value in required.items())):
        raise ValueError('RETAINED_RUNTIME_DEPENDENCY_LOCK_MISMATCH')
    (temporary / 'recovered-build-source-manifest.json').write_text(json.dumps(build, indent=2, sort_keys=True))
    (temporary / 'image-source-proof.json').write_text(json.dumps(build_proof, indent=2, sort_keys=True))
    worker = _image_python(WORKER, """import hashlib,json,pathlib
root=pathlib.Path('/app'); paths=[]
for name in ('bot','tradexa','automation-hub/services','automation-hub/data','automation-hub/strategies','automation-hub/migrations','ops'):
 paths.extend(p for p in (root/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
paths.extend(root/name for name in ('pyproject.toml','README.md','LICENSE'))
rows={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
print(json.dumps({'files':rows,'forbidden':[n for n in ('automation-hub/execution','automation-hub/app.py','automation-hub/webui') if (root/n).exists()]}))""", env)
    expected = {row['path']: row['sha256'] for row in build['files']}
    if worker['forbidden'] or any(expected.get(name) != value for name, value in worker['files'].items()):
        raise ValueError('RETAINED_WORKER_SOURCE_MISMATCH')
    (temporary / 'worker-source-proof.json').write_text(json.dumps(worker, indent=2, sort_keys=True))
    image_dir = temporary / 'images'
    image_dir.mkdir()
    images = []
    for role, reference in (('api', API), ('worker', WORKER), ('postgres', POSTGRES), ('ingress', INGRESS)):
        info = json.loads(_docker(['image', 'inspect', reference], env))[0]
        identity = info['Id']
        if info['Os'] != 'linux' or info['Architecture'] != 'amd64':
            raise ValueError('REVIEWED_LINUX_AMD64_IMAGE_REQUIRED')
        archive = image_dir / (role + '.tar.gz')
        _save(identity, archive, env)
        proof = verify_docker_archive(archive, identity)
        images.append({'role': role, 'config_id': identity, 'registry_repo_digests': info.get('RepoDigests', []),
                       'architecture': info['Architecture'], 'os': info['Os'], 'virtual_bytes': info['Size'],
                       'export': str(archive.relative_to(temporary)), 'export_bytes': archive.stat().st_size,
                       'export_sha256': sha256_file(archive), 'layer_proof': proof})
    _source_tar(temporary / 'source/context', temporary / 'operator-source.tar.gz')
    retained = temporary / 'evidence'
    retained.mkdir()
    for index, path in enumerate(evidence):
        path = Path(path)
        if not path.is_file() or path.is_symlink() or path.suffix not in {'.json', '.xml', '.log', '.md'} or private_path(path.name):
            raise ValueError('EXPLICIT_PUBLIC_REPORT_REQUIRED')
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('EVIDENCE_SIZE_LIMIT')
        shutil.copyfile(path, retained / (str(index) + '-' + path.name))
    # Detect source changes during lengthy exports without replacing any source.
    current_paths = {name for name in _git('ls-files', '--cached', '--others', '--exclude-standard', '-z').split('\0') if name and not private_path(name)}
    if current_paths != {row['path'] for row in source['files']}:
        raise ValueError('SOURCE_CHANGED_DURING_PACKET')
    if any(sha256_file(ROOT / row['path']) != row['sha256'] for row in source['files']):
        raise ValueError('SOURCE_CHANGED_DURING_PACKET')
    manifest = {'schema_version': 'nexus_reconstructed_release.v1', 'status': 'RECONSTRUCTED_VERIFIED',
        'created_at': datetime.now(timezone.utc).isoformat(), 'prior_review_reference': PRIOR_REVIEW,
        'original_archive_recovered': False, 'head': head, 'branch': _git('branch', '--show-current').strip(),
        'operator_source_sha256': source['source_sha256'], 'api_build_source_sha256': BUILD_SOURCE,
        'source_correspondence': correspondence, 'images': images,
        'source_archive_sha256': sha256_file(temporary / 'operator-source.tar.gz'),
        'required_untracked_files': source['required_untracked_files'],
        'denied_private_inputs': sorted(name for name in discovered if private_path(name)),
        'dependency_locks': {row['path']: row['sha256'] for row in source['files'] if row['path'].endswith(('.lock', 'package-lock.json'))},
        'migration_hashes': {row['path']: row['sha256'] for row in source['files'] if '/migrations/' in row['path'] and row['path'].endswith('.sql')},
        'frontend_bundle_hashes': build_proof['bundles'], 'worker_input_files_verified': len(worker['files']),
        'runtime_lock_packages_verified': len(required),
        'build_performed': False, 'runtime_images_reused_without_modification': True,
        'original_raw_test_logs_available': False, 'offline_rebuild_public_package_cache_retained': False,
        'human_release_approval': False, 'deployment_authorization': False, 'vps_access': False,
        'retention_scope': 'WORKSPACE_REVIEW_FILES; OFF_HOST_DURABILITY_NOT_CERTIFIED',
        'limitations': ['NOT_THE_ORIGINAL_ARCHIVE_BYTES', 'CONFIG_IDS_ARE_NOT_REGISTRY_MANIFEST_DIGESTS',
                        'CURRENT_PUBLIC_REGISTRY_RETRIEVABILITY_NOT_TESTED', 'TARGET_STAGING_GATES_STILL_REQUIRED']}
    unique_layers = {}
    unique_prefixes = {}
    for image in images:
        chain = image['layer_proof']['layer_diff_ids']
        sizes = image['layer_proof']['uncompressed_layer_bytes']
        for index, (layer, size) in enumerate(zip(chain, sizes)):
            if layer in unique_layers and unique_layers[layer] != size:
                raise ValueError('LAYER_SIZE_CONFLICT')
            unique_layers[layer] = size
            unique_prefixes[tuple(chain[:index + 1])] = sum(sizes[:index + 1])
    manifest['storage_model'] = {
        'compressed_exports_bytes': sum(image['export_bytes'] for image in images),
        'distinct_uncompressed_layer_archive_bytes': sum(unique_layers.values()),
        'vfs_cumulative_prefix_tar_bytes': sum(unique_prefixes.values()),
        'standard_runtime_virtual_image_bytes': sum(image['virtual_bytes'] for image in images),
        'maximum_one_initializer_virtual_image_bytes': next(image['virtual_bytes'] for image in images if image['role'] == 'worker'),
        'scope': 'PLANNING_ESTIMATES_ONLY; NOT_MEASURED_VPS_PHYSICAL_ALLOCATION',
        'excluded_costs': ['FILESYSTEM_METADATA_AND_BLOCK_ROUNDING', 'POSTGRES_WAL_AND_STATE',
                           'DATASETS_SPOOLS_LOGS_BACKUPS_AND_RESTORE', 'PRODUCTION_GROWTH_RESERVE'],
        'driver_on_target': 'UNKNOWN',
    }
    review = 'release-review:' + hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    manifest['review_id'] = review
    (temporary / 'release-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True))
    inventory = [{'path': str(path.relative_to(temporary)), 'bytes': path.stat().st_size, 'sha256': sha256_file(path)}
                 for path in sorted(temporary.rglob('*')) if path.is_file()]
    (temporary / 'artifact-integrity.json').write_text(json.dumps({'review_id': review, 'files': inventory}, indent=2, sort_keys=True))
    verify_review_archive(temporary)
    temporary.rename(out)
    return {'status': manifest['status'], 'review_id': review, 'source_sha256': source['source_sha256'],
            'directory': str(out), 'verified_artifacts': len(inventory), 'deployment_authorization': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--evidence', action='append', default=[])
    args = parser.parse_args(argv)
    print(json.dumps(reconstruct(args.output, evidence=args.evidence)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
