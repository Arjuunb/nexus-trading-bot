#!/usr/bin/env python3
"""Trusted release preparation and CI guards. No push, SSH, or deploy command.

Source fingerprints reuse services.release_source. Approval is obtained from
GitHub's actual environment/review APIs, never from a caller's boolean.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'automation-hub'))
from services.release_source import (bundle_source, excluded, generated_research_journal,
                                     GENERATED_RESEARCH_JOURNALS, EXCLUSION_POLICY_VERSION)
from services.remote_staging import sha256_file

REPOSITORY = 'arjuunb/nexus-trading-bot'
ENVIRONMENT = 'ghcr-staging-release'
INDEPENDENT_APPROVAL = 'independent-review.v1'
STAGING_OWNER_APPROVAL = 'staging-owner.v1'
STAGING_OWNER_ID = 190428336
STAGING_OWNER_LOGIN = 'arjuunb'
HEX40 = re.compile(r'[0-9a-f]{40}')
HEX64 = re.compile(r'[0-9a-f]{64}')
CONFIG_ID = re.compile(r'sha256:[0-9a-f]{64}')
PRIVATE_PARTS = {'.ssh', '.aws', '.codex', '.agents', 'secrets', 'state', 'runtime',
                 'backups', 'letsencrypt', 'certbot-webroot', '.github-credentials'}
PRIVATE_NAMES = {'credentials.json', '.netrc', 'id_rsa', 'id_ed25519'}
CONFIG_FILES = ('ops/staging.remote.compose.yaml', 'ops/staging.local.compose.yaml',
                'ops/staging.remote.nginx.conf', 'ops/prepare-staging-bindings.py',
                'ops/staging.alpine.compose.yaml')
UPSTREAM = {
    'postgres': 'postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea',
    'ingress': 'nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10',
}


def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name:
        raise ValueError('INVALID_RELEASE_PATH')
    return path


def denied_source(name, *, allow_image_exports=False):
    path = safe_name(name)
    return (excluded(path) or any(x in PRIVATE_PARTS for x in path.parts)
            or path.name in PRIVATE_NAMES
            or path.name.endswith(('.pem', '.key', '.crt', '.p12', '.pfx', '.db-journal'))
            or path.name.endswith(('.tar', '.tar.gz', '.tgz', '.oci')) and not (
                allow_image_exports and name in {'api.tar', 'worker.tar', 'api.tar.gz', 'worker.tar.gz'}))


def harmless_exclusion(name):
    path = PurePosixPath(name)
    return path.name in {'.env.example', '.gitkeep'} or any(
        x in {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', 'dist'} for x in path.parts)


def freeze_source(root, output, paths, *, head, dirty, tracked=(), expected_source=None):
    root = Path(root).resolve(); output = Path(output).absolute()
    if not HEX40.fullmatch(head) or expected_source is not None and not HEX64.fullmatch(expected_source):
        raise ValueError('INVALID_SOURCE_IDENTITY')
    if output.exists() or output.is_symlink() or root == output or root in output.parents:
        raise ValueError('NEW_FREEZE_OUTSIDE_CHECKOUT_REQUIRED')
    allowed = []; denied = []
    for name in sorted(set(paths)):
        safe_name(name)
        if denied_source(name):
            reviewed_journal = (generated_research_journal(name)
                                and PurePosixPath(name).parent == PurePosixPath('automation-hub/data'))
            if name in tracked and not harmless_exclusion(name) and not reviewed_journal:
                raise ValueError('TRACKED_PRIVATE_OR_STATE_INPUT')
            denied.append(name)
        else:
            allowed.append(name)
    partial = output.with_name(output.name + '.partial-' + uuid.uuid4().hex)
    value = bundle_source(root, partial, allowed, head=head, dirty_paths=dirty)
    if expected_source and value['source_sha256'] != expected_source:
        raise ValueError('SOURCE_IDENTITY_MISMATCH; PARTIAL_FREEZE_NOT_A_RELEASE')
    write_new(partial / 'source-policy.json', {'excluded_without_reading': denied,
        'source_exclusion_policy': EXCLUSION_POLICY_VERSION,
        'generated_journal_exclusion_preserves_original_files': True,
        'credentials_or_financial_state_included': False, 'publication_authorization': False})
    partial.rename(output)
    return value


def validate_release_request(*, repository, commit, actual_commit, source_sha, ref, event, run_attempt):
    if repository.lower() != REPOSITORY or event != 'workflow_dispatch' or run_attempt != '1':
        raise ValueError('MANUAL_FIRST_ATTEMPT_IN_APPROVED_REPOSITORY_REQUIRED')
    if not HEX40.fullmatch(commit) or commit != actual_commit or not HEX64.fullmatch(source_sha):
        raise ValueError('REVIEWED_COMMIT_AND_SOURCE_IDENTITY_REQUIRED')
    if not re.fullmatch(r'refs/heads/release/[a-z0-9][a-z0-9._/-]*|refs/tags/nexus-staging-[a-z0-9][a-z0-9._-]*', ref):
        raise ValueError('REVIEWED_RELEASE_BRANCH_OR_TAG_REQUIRED')
    return {'repository': REPOSITORY, 'commit': commit, 'source_sha256': source_sha, 'ref': ref}


def validate_publication_approval(environment, reviews, *, initiator_ids, commit, source_sha, run_id, run_attempt,
                                  approval_policy=INDEPENDENT_APPROVAL):
    if not HEX40.fullmatch(commit) or not HEX64.fullmatch(source_sha) or not str(run_id).isdigit() or run_attempt != '1':
        raise ValueError('FRESH_RELEASE_RUN_REQUIRED')
    if environment.get('name') != ENVIRONMENT or not isinstance(environment.get('id'), int):
        raise ValueError('APPROVED_ENVIRONMENT_REQUIRED')
    rules = [r for r in environment.get('protection_rules', []) if r.get('type') == 'required_reviewers']
    if approval_policy == INDEPENDENT_APPROVAL:
        reviewers = {x['reviewer']['id'] for r in rules if r.get('prevent_self_review') is True
                     for x in r.get('reviewers', []) if x.get('type') == 'User'
                     and isinstance(x.get('reviewer', {}).get('id'), int)}
        eligible_reviewers = reviewers - set(initiator_ids)
    elif approval_policy == STAGING_OWNER_APPROVAL:
        # Explicit staging exception; it is never independent review. A real
        # GitHub environment approval is still required after the build/scans.
        if set(initiator_ids) != {STAGING_OWNER_ID}:
            raise ValueError('STAGING_OWNER_INITIATOR_REQUIRED')
        reviewers = {x['reviewer']['id'] for r in rules if r.get('prevent_self_review') is False
                     for x in r.get('reviewers', []) if x.get('type') == 'User'
                     and x.get('reviewer', {}).get('id') == STAGING_OWNER_ID
                     and str(x['reviewer'].get('login', '')).lower() == STAGING_OWNER_LOGIN}
        eligible_reviewers = reviewers
    else:
        raise ValueError('UNSUPPORTED_STAGING_APPROVAL_POLICY')
    branch_policy = environment.get('deployment_branch_policy') or {}
    if not reviewers or not any(branch_policy.get(k) is True for k in ('protected_branches', 'custom_branch_policies')):
        raise ValueError('PROTECTED_ENVIRONMENT_WITH_DISTINCT_USER_REVIEWER_REQUIRED'
                         if approval_policy == INDEPENDENT_APPROVAL else
                         'PROTECTED_STAGING_ENVIRONMENT_WITH_OWNER_REVIEWER_REQUIRED')
    accepted = [r for r in reviews if r.get('state') == 'approved'
                and r.get('user', {}).get('id') in eligible_reviewers
                and (approval_policy != STAGING_OWNER_APPROVAL
                     or str(r.get('user', {}).get('login', '')).lower() == STAGING_OWNER_LOGIN)
                and any(e.get('id') == environment['id'] and e.get('name') == ENVIRONMENT
                        for e in r.get('environments', []))]
    if not accepted:
        raise ValueError('ACTUAL_APPROVAL_BY_CONFIGURED_DISTINCT_REVIEWER_REQUIRED'
                         if approval_policy == INDEPENDENT_APPROVAL else
                         'ACTUAL_APPROVAL_BY_CONFIGURED_STAGING_OWNER_REQUIRED')
    return {'verification': 'CONFIGURED_REVIEWER_APPROVED_THIS_RUN', 'environment': ENVIRONMENT,
            'environment_id': environment['id'], 'reviewer_id': accepted[-1]['user']['id'],
            'reviewed_commit': commit, 'source_sha256': source_sha, 'run_id': str(run_id),
            'run_attempt': run_attempt, 'approval_policy': approval_policy,
            'independent_review': approval_policy == INDEPENDENT_APPROVAL,
            'deployment_authorization': False}


def registry_ref(value, role):
    if role not in {'api', 'worker'} or not re.fullmatch(
            r'ghcr\.io/arjuunb/nexus-trading-bot-' + role + r'@sha256:[0-9a-f]{64}', value):
        raise ValueError('APPROVED_IMMUTABLE_GHCR_REFERENCE_REQUIRED')
    return value


def write_new(path, value):
    path = Path(path)
    with path.open('x') as f:
        json.dump(value, f, sort_keys=True, indent=2); f.write('\n')
    path.chmod(0o600)


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('BOUNDED_REGULAR_PUBLIC_METADATA_REQUIRED')
    return json.loads(path.read_text())


def validate_source_journal_exclusion(source):
    rows = source.get('files')
    if not isinstance(rows, list):
        raise ValueError('SOURCE_JOURNAL_POLICY_UNVERIFIED')
    if any(generated_research_journal(safe_name(row['path'])) for row in rows):
        raise ValueError('GENERATED_JOURNAL_IN_RELEASE_SOURCE')


def validate_image_journal_exclusion(paths):
    if paths != []:
        raise ValueError('GENERATED_JOURNAL_IN_IMAGE_OR_UNVERIFIED')
    return {'generated_journals_absent': True}


def seal_packet(root, *, commit, source_sha):
    root = Path(root); rows = []
    if not HEX40.fullmatch(commit) or not HEX64.fullmatch(source_sha):
        raise ValueError('INVALID_PACKET_IDENTITY')
    if (root / 'source-manifest.json').is_file():
        validate_source_journal_exclusion(read_json(root / 'source-manifest.json'))
    for path in sorted(root.rglob('*')):
        name = str(path.relative_to(root)); safe_name(name)
        if path.is_symlink(): raise ValueError('PACKET_SYMLINK')
        if not path.is_file(): continue
        if denied_source(name, allow_image_exports=True) or path.name == 'packet-integrity.json': raise ValueError('PRIVATE_OR_EXISTING_PACKET_INPUT')
        rows.append({'path': name, 'bytes': path.stat().st_size, 'sha256': sha256_file(path)})
    if not rows or len(rows) > 1000 or sum(x['bytes'] for x in rows) > 8 * 1024**3:
        raise ValueError('PACKET_RESOURCE_LIMIT')
    write_new(root / 'packet-integrity.json', {'commit': commit, 'source_sha256': source_sha, 'files': rows})


def verify_packet(root, *, commit, source_sha):
    root = Path(root); value = read_json(root / 'packet-integrity.json')
    if value.get('commit') != commit or value.get('source_sha256') != source_sha:
        raise ValueError('PACKET_IDENTITY_MISMATCH')
    seen = set(); total = 0
    if not isinstance(value.get('files'), list) or len(value['files']) > 1000:
        raise ValueError('PACKET_RESOURCE_LIMIT')
    for row in value['files']:
        name = str(safe_name(row['path'])); path = root / name
        if name in seen or any((root / Path(*PurePosixPath(name).parts[:n])).is_symlink()
                               for n in range(1, len(PurePosixPath(name).parts)+1)):
            raise ValueError('PACKET_DUPLICATE_OR_SYMLINK')
        if not path.is_file() or denied_source(name, allow_image_exports=True): raise ValueError('PACKET_PRIVATE_OR_MISSING_INPUT')
        total += path.stat().st_size
        if total > 8 * 1024**3 or path.stat().st_size != row['bytes'] or sha256_file(path) != row['sha256']:
            raise ValueError('PACKET_INTEGRITY_MISMATCH')
        seen.add(name)
    actual = {str(x.relative_to(root)) for x in root.rglob('*') if x.is_file() and x.name != 'packet-integrity.json'}
    if actual != seen: raise ValueError('UNDECLARED_PACKET_INPUT')
    source_path = root / 'source-manifest.json'
    source = read_json(source_path if source_path.is_file() else root / 'publication.json')
    if source_path.is_file():
        validate_source_journal_exclusion(source)
    if source.get('head', source.get('commit')) != commit or source.get('source_sha256') != source_sha:
        raise ValueError('PACKET_SOURCE_IDENTITY_MISMATCH')
    return {'verified_files': len(seen), 'commit': commit, 'source_sha256': source_sha}


def publication_manifest(source, approval, *, api_ref, worker_ref, api_config_id, worker_config_id,
                         api_uid, api_version, worker_version):
    validate_source_journal_exclusion(source)
    if approval.get('reviewed_commit') != source.get('head') or approval.get('source_sha256') != source.get('source_sha256'):
        raise ValueError('APPROVAL_SOURCE_IDENTITY_MISMATCH')
    if (approval.get('verification') != 'CONFIGURED_REVIEWER_APPROVED_THIS_RUN'
            or approval.get('run_attempt') != '1' or not approval.get('reviewer_id')
            or not str(approval.get('run_id', '')).isdigit()):
        raise ValueError('PUBLICATION_APPROVAL_REFERENCE_REQUIRED')
    if not CONFIG_ID.fullmatch(api_config_id) or not CONFIG_ID.fullmatch(worker_config_id):
        raise ValueError('ACTUAL_IMAGE_CONFIG_IDS_REQUIRED')
    if not 1 <= api_uid <= 65534 or not api_version.startswith('3.11.') or not worker_version.startswith('3.12.'):
        raise ValueError('PRODUCTION_RUNTIME_IDENTITY_MISMATCH')
    files = {r['path']: r['sha256'] for r in source['files']}
    return {'schema_version': 'nexus_ghcr_publication.v1', 'repository': REPOSITORY,
            'commit': source['head'], 'source_sha256': source['source_sha256'],
            'created_at': datetime.now(timezone.utc).isoformat(), 'approval': approval,
            'images': {'api': {'reference': registry_ref(api_ref, 'api'), 'config_id': api_config_id},
                       'worker': {'reference': registry_ref(worker_ref, 'worker'), 'config_id': worker_config_id},
                       **{k: {'reference': v} for k, v in UPSTREAM.items()}},
            'api_uid': api_uid, 'configuration_hashes': {p: files[p] for p in CONFIG_FILES if p in files},
            'dependency_locks': {p: h for p, h in files.items() if p.endswith('.lock') or p.endswith('package-lock.json')},
            'migration_hashes': {p: h for p, h in files.items() if '/migrations/' in p and p.endswith('.sql')},
            'build_recipe_hashes': {p: files[p] for p in ('ops/api.alpine.Dockerfile',
                                    'ops/research-worker.alpine.Dockerfile') if p in files},
            'release_scope': 'STAGE_A_ONLY', 'worker_activation_authorized': False,
            'deployment_authorization': False, 'trading_authorization': False}


def config_packet(root, output, publication):
    """Copy only reviewed public configuration/operator helper; no private files."""
    root = Path(root); output = Path(output)
    if output.exists() or output.is_symlink(): raise ValueError('NEW_CONFIGURATION_PACKET_REQUIRED')
    for role in ('api', 'worker'): registry_ref(publication['images'][role]['reference'], role)
    if publication.get('deployment_authorization') is not False:
        raise ValueError('PUBLICATION_IS_NOT_DEPLOYMENT_AUTHORIZATION')
    for name in CONFIG_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != publication.get('configuration_hashes', {}).get(name):
            raise ValueError('REVIEWED_CONFIGURATION_HASH_MISMATCH')
    output.mkdir(mode=0o700, parents=True)
    for name in CONFIG_FILES:
        target = output / name; target.parent.mkdir(exist_ok=True)
        target.write_bytes((root / name).read_bytes()); target.chmod(0o600)
    write_new(output / 'publication.json', publication)
    seal_packet(output, commit=publication['commit'], source_sha=publication['source_sha256'])
    # Configuration packets are verified by their files; they contain no full source manifest.
    return {'directory': str(output), 'configuration_files': list(CONFIG_FILES), 'deployment_authorization': False}


def parse_status(payload):
    """Read porcelain -z without stripping the significant leading status space."""
    rows = payload.split('\0'); index = 0; result = {}
    while index < len(rows):
        row = rows[index]; index += 1
        if not row: continue
        if len(row) < 4 or row[2] != ' ': raise ValueError('GIT_STATUS_FORMAT_INVALID')
        result[row[3:]] = row[:2]
        if 'R' in row[:2] or 'C' in row[:2]: index += 1
    return result


def run(command, *, timeout=120, strip=True, input=None):
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout,
                            input=input, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
    return result.stdout.strip() if strip else result.stdout


def github_json(path, token):
    request = Request('https://api.github.com/repos/' + REPOSITORY + '/' + path,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urlopen(request, timeout=15) as response: payload = response.read(2*1024*1024+1)
        if len(payload) > 2*1024*1024: raise ValueError('GITHUB_METADATA_LIMIT')
        return json.loads(payload)
    except Exception:
        raise ValueError('GITHUB_APPROVAL_METADATA_UNAVAILABLE') from None


def validate_alpine_runtime(role, value, packages):
    minor = '3.11.' if role == 'api' else '3.12.'
    uid = 100 if role == 'api' else 1000
    if (role not in {'api', 'worker'} or value.get('os_id') != 'alpine'
            or not value.get('os_version', '').startswith('3.24.')
            or not value.get('python', '').startswith(minor) or value.get('uid') != uid
            or value.get('pq') != 'binary' or value.get('ssl_required') is not True
            or value.get('ssl_hostname') is not True or value.get('native_imports_ok') is not True
            or value.get('execution_flags_disabled') is not True or value.get('packages') != packages):
        raise ValueError('ALPINE_NATIVE_RUNTIME_OR_SAFETY_MISMATCH')
    if role == 'worker' and value.get('worker_execution_support') not in {
            'BLOCKED_DEPENDENCY_PANDAS', 'UNKNOWN'}:
        raise ValueError('ALPINE_WORKER_EXECUTION_NOT_CERTIFIED')
    return {**value, 'release_scope': 'STAGE_A_ONLY', 'worker_activation_authorized': False}


def _alpine_packages(role):
    path = ROOT / 'ops' / ('prepare-api-alpine-inputs.py' if role == 'api' else 'prepare-shadow-worker-wheels.py')
    spec = importlib.util.spec_from_file_location('locked_' + role, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return ({name: version for name, (version, _) in module.parse_lock((ROOT / 'ops/api-requirements.lock').read_text()).items()}
            if role == 'api' else dict(module.PACKAGES))


def _alpine_probe(role):
    modules = (['numpy', 'pandas', 'cryptography.hazmat.bindings._rust', 'coincurve',
                'uvloop', 'httptools', 'aiohttp', 'jiter', 'orjson', 'pydantic_core',
                'yaml', 'supabase', 'ccxt', 'zlib_ng', 'watchfiles', 'yarl']
               if role == 'api' else ['websockets.speedups', 'requests', 'certifi'])
    return """
import importlib,importlib.metadata as metadata,json,os,platform,ssl,sys
sys.path.insert(0,'/app/automation-hub')
import psycopg
for name in MODULES: importlib.import_module(name)
support='NOT_APPLICABLE'
if ROLE=='worker':
 try: importlib.import_module('services.strategy_presets')
 except ModuleNotFoundError as exc:
  if exc.name!='pandas': raise
  support='BLOCKED_DEPENDENCY_PANDAS'
 else: support='UNKNOWN'
flags=['HUB_AUTO_ENGINE','HUB_ENABLE_EXTERNAL_LIVE','HUB_USE_LIVE_DATA',
       'HUB_RESEARCH_AUTOSTART','HUB_MONITOR_AGENT','NEXUS_SHADOW_SUPERVISED']
names=json.load(sys.stdin); release=platform.freedesktop_os_release()
from bot.data.indicators import ema,rsi
prices=[100+i*.25 for i in range(40)]
print(json.dumps({'os_id':release['ID'],'os_version':release['VERSION_ID'],
 'python':sys.version.split()[0],'uid':os.getuid(),'pq':psycopg.pq.__impl__,
 'ssl_required':ssl.create_default_context().verify_mode==ssl.CERT_REQUIRED,
 'ssl_hostname':ssl.create_default_context().check_hostname,'native_imports_ok':True,
 'packages':{name:metadata.version(name) for name in names},
 'execution_flags_disabled':all(os.environ.get(name)=='0' for name in flags),
 'worker_execution_support':support,'math_fixture':{'ema':ema(prices,14),'rsi':rsi(prices,14)}}))
""".replace('MODULES', repr(modules)).replace('ROLE', repr(role))


def verify_local_images(source, api, worker, *, require_alpine=False, worker_source=None):
    """Run only bounded read-only image probes; never start app/worker entrypoints."""
    validate_source_journal_exclusion(source)
    if worker_source is not None:
        validate_source_journal_exclusion(worker_source)
    probes = {}; refs = {'api': api, 'worker': worker}
    if worker_source is not None:
        if source['head'] != worker_source['head']: raise ValueError('EVALUATED_SOURCE_HEAD_MISMATCH')
        api_files = {r['path']: r['sha256'] for r in source['files']}
        for row in worker_source['files']:
            if row['path'].startswith(('bot/', 'tradexa/', 'automation-hub/services/',
                                       'automation-hub/data/', 'automation-hub/strategies/', 'automation-hub/migrations/')):
                if api_files.get(row['path']) != row['sha256']:
                    raise ValueError('EVALUATED_RUNTIME_SOURCE_MISMATCH')
    for role, ref in refs.items():
        role_source = worker_source if role == 'worker' and worker_source is not None else source
        metadata = json.loads(run(['docker', 'image', 'inspect', ref, '--format', '{{json .}}']))
        labels = metadata['Config'].get('Labels') or {}
        if metadata['Os'] != 'linux' or metadata['Architecture'] != 'amd64' or labels.get('org.opencontainers.image.revision') != role_source['head'] or labels.get('nexus.source.sha256') != role_source['source_sha256']:
            raise ValueError('BUILT_IMAGE_SOURCE_OR_PLATFORM_MISMATCH')
        command = ['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--read-only',
                   '--cpus', '0.5', '--memory', '512m' if require_alpine else '256m', '--pids-limit', '64',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                   '--env', 'OPENBLAS_NUM_THREADS=1', '--env', 'OMP_NUM_THREADS=1',
                   '--entrypoint', 'python', ref, '-B', '-c']
        code = "import json,sys,psycopg; print(json.dumps({'python':sys.version.split()[0],'pq':psycopg.pq.__impl__}))"
        runtime = json.loads(run(command + [code]))
        if runtime['pq'] != 'binary' or not runtime['python'].startswith('3.11.' if role == 'api' else '3.12.'):
            raise ValueError('LOCKED_RUNTIME_MISMATCH')
        expected = role_source['files'] if role == 'api' else [r for r in role_source['files'] if (
            r['path'].startswith(('bot/', 'tradexa/', 'automation-hub/strategies/',
                                 'automation-hub/migrations/shadow/', 'automation-hub/migrations/workers/'))
            or r['path'].startswith(('automation-hub/services/', 'automation-hub/data/')) and r['path'].endswith('.py')
            or r['path'] in {'pyproject.toml', 'README.md', 'LICENSE', 'ops/staging-initialize.py',
                            'automation-hub/data/pr6_real_paper_freeze.json'})]
        data = json.dumps({r['path']: r['sha256'] for r in expected})
        if len(data.encode()) > 4 * 1024 * 1024:
            raise ValueError('IMAGE_SOURCE_MANIFEST_LIMIT')
        # Large source inventories exceed Linux's single-argument limit. The
        # receiver is fixed code; only bounded public hash metadata uses stdin.
        code = "import pathlib,hashlib,json,sys; expected=json.load(sys.stdin); root=pathlib.Path('/app'); bad=[n for n,h in expected.items() if not (root/n).is_file() or hashlib.sha256((root/n).read_bytes()).hexdigest()!=h]; assert not bad,'IMAGE_SOURCE_MISMATCH'; print(json.dumps({'source_files':len(expected)}))"
        files = json.loads(run(command[:2] + ['-i'] + command[2:] + [code], input=data))
        # Fixed read-only presence probe: do not read journal contents or accept
        # an undeclared historical file merely because its name is not hashed.
        code = ("import os,json; names=" + repr(sorted(GENERATED_RESEARCH_JOURNALS))
                + "; print(json.dumps([os.path.relpath(os.path.join(folder,name),'/app') "
                  "for folder,dirs,files in os.walk('/app') for name in files if name in names]))")
        journal_boundary = validate_image_journal_exclusion(json.loads(run(command + [code])))
        if role == 'api':
            code = "import pathlib,json; root=pathlib.Path('/app/automation-hub'); assert all((root/n/'index.html').is_file() for n in ('webui','landing')); print(json.dumps({'frontend_bundles':True}))"
        else:
            code = "import pathlib,json; root=pathlib.Path('/app'); assert not any((root/n).exists() for n in ('automation-hub/app.py','automation-hub/execution','automation-hub/webui','.env')); print(json.dumps({'code_only_boundary':True}))"
        boundary = json.loads(run(command + [code]))
        uid = int(run(['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--read-only',
                       '--memory', '64m', '--pids-limit', '16', '--entrypoint', 'id', ref, '-u']))
        if uid == 0: raise ValueError('NONROOT_RUNTIME_REQUIRED')
        probes[role] = {'config_id': metadata['Id'], 'uid': uid, **runtime, **files, **boundary, **journal_boundary}
        if require_alpine:
            packages = _alpine_packages(role)
            value = json.loads(run(command[:2] + ['-i'] + command[2:] + [_alpine_probe(role)], input=json.dumps(list(packages))))
            probes[role]['alpine'] = validate_alpine_runtime(role, value, packages)
    if require_alpine and probes['api']['alpine']['math_fixture'] != probes['worker']['alpine']['math_fixture']:
        raise ValueError('ALPINE_SHARED_NUMERICAL_FIXTURE_MISMATCH')
    return {'commit': source['head'], 'source_sha256': source['source_sha256'], 'images': probes,
            'source_mode': 'SINGLE_FROZEN_RELEASE_SOURCE' if worker_source is None else 'SEPARATE_EVALUATED_SOURCE_MANIFESTS',
            'worker_source_sha256': worker_source['source_sha256'] if worker_source is not None else source['source_sha256'],
            'application_started': False, 'deployment_authorization': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    freeze = sub.add_parser('freeze'); freeze.add_argument('--output', required=True)
    freeze.add_argument('--expected-source'); freeze.add_argument('--reviewed-commit'); freeze.add_argument('--preview', action='store_true')
    request = sub.add_parser('check-request'); request.add_argument('--reviewed-commit', required=True); request.add_argument('--source-sha', required=True)
    approval = sub.add_parser('check-approval'); approval.add_argument('--reviewed-commit', required=True); approval.add_argument('--source-sha', required=True); approval.add_argument('--output', required=True)
    approval.add_argument('--approval-policy', choices=(INDEPENDENT_APPROVAL, STAGING_OWNER_APPROVAL),
                          default=INDEPENDENT_APPROVAL)
    for name in ('seal', 'verify-packet'):
        p = sub.add_parser(name); p.add_argument('--directory', required=True); p.add_argument('--reviewed-commit', required=True); p.add_argument('--source-sha', required=True)
    verify = sub.add_parser('verify-images'); verify.add_argument('--source-manifest', required=True); verify.add_argument('--api-image', required=True); verify.add_argument('--worker-image', required=True); verify.add_argument('--output', required=True)
    verify.add_argument('--require-alpine', action='store_true')
    verify.add_argument('--evaluated-worker-source-manifest', help='Local reference-pair check only; cannot authorize publication')
    publish = sub.add_parser('record-publication')
    for name in ('source-manifest', 'build-proof', 'approval', 'api-ref', 'worker-ref', 'output'): publish.add_argument('--'+name, required=True)
    config = sub.add_parser('config-packet'); config.add_argument('--publication', required=True); config.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if args.action == 'freeze':
        head = run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'])
        if args.reviewed_commit and args.reviewed_commit != head: raise ValueError('REVIEWED_COMMIT_MISMATCH')
        status = run(['git', '-C', str(ROOT), 'status', '--porcelain=v1', '-uall', '-z'], strip=False)
        if status and not args.preview: raise ValueError('CLEAN_REVIEWED_GIT_SOURCE_REQUIRED')
        paths = run(['git', '-C', str(ROOT), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], strip=False).split('\0')
        tracked = set(run(['git', '-C', str(ROOT), 'ls-files', '-z'], strip=False).split('\0'))
        dirty = parse_status(status)
        value = freeze_source(ROOT, args.output, [p for p in paths if p], head=head,
            dirty=dirty, tracked=tracked, expected_source=args.expected_source)
        print(json.dumps({'source_sha256': value['source_sha256'], 'files': len(value['files']),
            'required_untracked': len(value['required_untracked_files']), 'preview': args.preview,
            'publication_authorization': False}))
    elif args.action in ('check-request', 'check-approval'):
        value = validate_release_request(repository=os.environ.get('GITHUB_REPOSITORY', ''),
            commit=args.reviewed_commit, actual_commit=os.environ.get('GITHUB_SHA', ''), source_sha=args.source_sha,
            ref=os.environ.get('GITHUB_REF', ''), event=os.environ.get('GITHUB_EVENT_NAME', ''), run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT', ''))
        if args.action == 'check-approval':
            token = os.environ.get('GH_TOKEN'); rid = os.environ.get('GITHUB_RUN_ID', '')
            if not token or not rid.isdigit(): raise ValueError('WORKFLOW_APPROVAL_METADATA_REQUIRED')
            environment = github_json('environments/'+ENVIRONMENT, token)
            history = github_json('actions/runs/'+rid+'/approvals', token)
            workflow_run = github_json('actions/runs/'+rid, token)
            if workflow_run.get('head_sha') != args.reviewed_commit: raise ValueError('APPROVAL_RUN_COMMIT_MISMATCH')
            initiators = {workflow_run.get(k, {}).get('id') for k in ('actor', 'triggering_actor')}
            value = validate_publication_approval(environment, history, initiator_ids=initiators,
                commit=args.reviewed_commit, source_sha=args.source_sha, run_id=rid,
                run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT', ''), approval_policy=args.approval_policy)
            write_new(args.output, value)
        print(json.dumps(value))
    elif args.action in ('seal', 'verify-packet'):
        value = (seal_packet if args.action == 'seal' else verify_packet)(args.directory,
            commit=args.reviewed_commit, source_sha=args.source_sha)
        print(json.dumps(value or {'sealed': True}))
    elif args.action == 'verify-images':
        value = verify_local_images(read_json(args.source_manifest), args.api_image, args.worker_image,
            require_alpine=args.require_alpine,
            worker_source=read_json(args.evaluated_worker_source_manifest) if args.evaluated_worker_source_manifest else None)
        write_new(args.output, value); print(json.dumps(value))
    elif args.action == 'record-publication':
        source = read_json(args.source_manifest); proof = read_json(args.build_proof); approval = read_json(args.approval)
        if (proof.get('commit') != source['head'] or proof.get('source_sha256') != source['source_sha256']
                or proof.get('source_mode') != 'SINGLE_FROZEN_RELEASE_SOURCE'
                or proof.get('worker_source_sha256') != source['source_sha256']
                or approval.get('verification') != 'CONFIGURED_REVIEWER_APPROVED_THIS_RUN'):
            raise ValueError('COMMITTED_BUILD_AND_ACTUAL_APPROVAL_REQUIRED')
        value = publication_manifest(source, approval, api_ref=args.api_ref, worker_ref=args.worker_ref,
            api_config_id=proof['images']['api']['config_id'], worker_config_id=proof['images']['worker']['config_id'],
            api_uid=proof['images']['api']['uid'], api_version=proof['images']['api']['python'], worker_version=proof['images']['worker']['python'])
        write_new(args.output, value); print(json.dumps({'manifest': args.output, 'images': value['images'], 'deployment_authorization': False}))
    else:
        value = read_json(args.publication)
        if run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']) != value['commit']:
            raise ValueError('APPROVED_CONFIG_CHECKOUT_REQUIRED')
        print(json.dumps(config_packet(ROOT, args.output, value)))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Never expose token-bearing HTTP requests, daemon stderr or secret values.
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(json.dumps({'status': 'BLOCKED', 'reason': reason, 'deployment_authorization': False}), file=sys.stderr)
        raise SystemExit(2)
