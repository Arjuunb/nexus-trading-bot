#!/usr/bin/env python3
"""Create NEW private staging bindings or check their prerequisites; never deploy.

No production dotenv, SSH identity, database, exchange client or remote target
is opened. Customer authentication and TLS trust still require operator review.
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess


ROOT = Path(__file__).resolve().parents[1]
# Legacy local config IDs and approved immutable registry references are distinct
# forms. Mutable tags and arbitrary registries remain invalid preparation input.
IMAGE = re.compile(r'(?:sha256:|(?:ghcr\.io/[a-z0-9][a-z0-9._/-]*|postgres|nginx)@sha256:)[0-9a-f]{64}')
PROJECT = re.compile(r'nexus-remote-stage-[a-z0-9][a-z0-9-]{0,40}')
SCOPE = 'NEW_INDEPENDENT_NEXUS_STAGING_BINDINGS'
PROFILES = {'remote': 'staging.remote.compose.yaml', 'alpine': 'staging.alpine.compose.yaml'}


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)


def prepare(output, *, project, api_id, worker_id, postgres_id, ingress_id, uid, port, compose_profile='remote'):
    if compose_profile not in PROFILES:
        raise ValueError('ALLOWLISTED_COMPOSE_PROFILE_REQUIRED')
    if not PROJECT.fullmatch(project):
        raise ValueError('INDEPENDENT_PROJECT_REQUIRED')
    if port in {80, 443, 5432} or not 1024 <= port <= 65535:
        raise ValueError('UNUSED_LOOPBACK_PORT_REQUIRED')
    if not 1 <= uid <= 65534 or not all(IMAGE.fullmatch(value) for value in (api_id, worker_id, postgres_id, ingress_id)):
        raise ValueError('REVIEWED_IMAGES_AND_NONROOT_UID_REQUIRED')
    out = Path(output)
    if not out.is_absolute() or out.exists() or out.is_symlink() or ROOT in out.resolve().parents:
        raise ValueError('NEW_PRIVATE_DIRECTORY_OUTSIDE_REPOSITORY_REQUIRED')
    if any(parent.is_symlink() for parent in out.parents):
        raise ValueError('NEW_PRIVATE_DIRECTORY_SYMLINK_FORBIDDEN')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    credentials = [secrets.token_hex(32) for _ in range(5)]
    controls = {
        'NEXUS_REMOTE_STAGING_PROJECT': project, 'NEXUS_STAGING_PROJECT': project,
        'NEXUS_STAGING_API_IMAGE': api_id, 'NEXUS_STAGING_WORKER_IMAGE': worker_id,
        'NEXUS_REMOTE_POSTGRES_IMAGE': postgres_id, 'NEXUS_REMOTE_INGRESS_IMAGE': ingress_id,
        'NEXUS_STAGING_UID': str(uid), 'NEXUS_REMOTE_HTTPS_PORT': str(port),
        'NEXUS_STAGING_APP_ENV': str(out / 'app.env'), 'NEXUS_STAGING_TLS_DIR': str(out / 'tls'),
        'NEXUS_STAGING_OPERATOR_PASSWORD': credentials[0],
        'NEXUS_STAGING_RUNTIME_PASSWORD': credentials[1], 'NEXUS_STAGING_SHADOW_ENABLED': '0',
    }
    application = {
        'HUB_AUTH_MODE': 'supabase', 'HUB_EMERGENCY_ADMIN_ENABLED': '0',
        'HUB_SECRET': credentials[2], 'HUB_CONTROL_KEY': credentials[3], 'HUB_WEBHOOK_SECRET': credentials[4],
        'HUB_AUTO_ENGINE': '0', 'HUB_ENABLE_EXTERNAL_LIVE': '0', 'HUB_USE_LIVE_DATA': '0',
        'HUB_RESEARCH_AUTOSTART': '0', 'HUB_MONITOR_AGENT': '0', 'HUB_DAILY_REPORT_HOUR': '-1',
    }
    (out / 'tls').mkdir(mode=0o700)
    _write_private(out / 'compose.env', ''.join(key + '=' + value + '\n' for key, value in controls.items()))
    _write_private(out / 'app.env', ''.join(key + '=' + value + '\n' for key, value in application.items()))
    _write_private(out / 'staging-bindings-scope.json', json.dumps({'schema_version': 'staging-bindings.v1',
        'scope': SCOPE, 'project': project, 'compose_profile': compose_profile, 'purpose': 'PREPARATION_ONLY; NOT_REMOTE_PROVISIONING',
        'production_secrets_copied': False, 'customer_authentication_configured': False}, indent=2))
    return {'status': 'PREPARED_INCOMPLETE', 'directory': str(out), 'project': project,
            'gates': {'SEPARATE_STAGING_CREDENTIALS': 'PASS', 'CUSTOMER_AUTHENTICATION': 'BLOCKED',
                      'TLS_TRUST': 'UNKNOWN', 'TARGET_PORT_AVAILABILITY': 'UNKNOWN', 'DEPLOYMENT_AUTHORIZATION': 'BLOCKED'},
            'missing_secure_bindings': ['SUPABASE_URL', 'SUPABASE_KEY'],
            'deployment_authorization': False, 'remote_provisioning': False}


def read_bindings(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or key in result or any(c in value for c in '\r\0$`'):
            raise ValueError('BINDINGS_FORMAT_INVALID')
        result[key] = value
    return result


def _private_regular(path):
    return (path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1
            and not any(parent.is_symlink() for parent in path.parents)
            and path.stat().st_mode & 0o077 == 0 and path.stat().st_uid == os.getuid())


def checked_compose_args(release_dir, bindings_path, project):
    if not bindings_path or not release_dir or not PROJECT.fullmatch(project):
        raise ValueError('BINDINGS_AND_EXPLICIT_PROJECT_REQUIRED')
    path, release = Path(bindings_path), Path(release_dir)
    if not path.is_absolute() or path.name != 'compose.env' or not _private_regular(path):
        raise ValueError('MARKED_PRIVATE_BINDINGS_FILE_REQUIRED')
    marker = path.parent / 'staging-bindings-scope.json'
    if not _private_regular(marker):
        raise ValueError('MARKED_PRIVATE_BINDINGS_FILE_REQUIRED')
    scope = json.loads(marker.read_text())
    if scope.get('scope') != SCOPE or scope.get('project') != project:
        raise ValueError('BINDINGS_SCOPE_MISMATCH')
    profile = scope.get('compose_profile', 'remote')
    if profile not in PROFILES:
        raise ValueError('ALLOWLISTED_COMPOSE_PROFILE_REQUIRED')
    if not release.is_absolute() or not (release / 'ops' / PROFILES[profile]).is_file():
        raise ValueError('REVIEWED_RELEASE_DIRECTORY_REQUIRED')
    controls = read_bindings(path)
    if any(controls.get(key) != project for key in ('NEXUS_STAGING_PROJECT', 'NEXUS_REMOTE_STAGING_PROJECT')):
        raise ValueError('BINDINGS_PROJECT_MISMATCH')
    if controls.get('NEXUS_STAGING_SHADOW_ENABLED') != '0':
        raise ValueError('BINDINGS_SHADOW_MUST_REMAIN_DISABLED')
    for key in ('NEXUS_STAGING_API_IMAGE', 'NEXUS_STAGING_WORKER_IMAGE', 'NEXUS_REMOTE_POSTGRES_IMAGE', 'NEXUS_REMOTE_INGRESS_IMAGE'):
        if not IMAGE.fullmatch(controls.get(key, '')):
            raise ValueError('BINDINGS_IMMUTABLE_IMAGES_REQUIRED')
    for key in ('NEXUS_STAGING_OPERATOR_PASSWORD', 'NEXUS_STAGING_RUNTIME_PASSWORD'):
        if not re.fullmatch('[0-9a-f]{64}', controls.get(key, '')):
            raise ValueError('BINDINGS_NEW_STRONG_CREDENTIAL_REQUIRED')
    if controls['NEXUS_STAGING_OPERATOR_PASSWORD'] == controls['NEXUS_STAGING_RUNTIME_PASSWORD']:
        raise ValueError('BINDINGS_DISTINCT_DATABASE_ROLES_REQUIRED')
    if controls.get('NEXUS_STAGING_APP_ENV') != str(path.parent / 'app.env') or controls.get('NEXUS_STAGING_TLS_DIR') != str(path.parent / 'tls'):
        raise ValueError('BINDINGS_SEPARATE_STAGING_PATHS_REQUIRED')
    port = int(controls.get('NEXUS_REMOTE_HTTPS_PORT', '0'))
    if not 1024 <= port <= 65535 or port in {80, 443, 5432}:
        raise ValueError('BINDINGS_LOOPBACK_PORT_REQUIRED')
    return ['docker', 'compose', '--env-file', str(path), '-p', project,
            '-f', str(release / 'ops' / PROFILES[profile])]


def check(release_dir, bindings_path, project):
    checked_compose_args(release_dir, bindings_path, project)
    root = Path(bindings_path).parent
    app = root / 'app.env'
    if not _private_regular(app):
        raise ValueError('BINDINGS_PRIVATE_STAGING_APPLICATION_FILE_REQUIRED')
    values = read_bindings(app)
    forbidden = {'HUB_EXCHANGE_API_KEY', 'HUB_EXCHANGE_API_SECRET', 'BINANCE_API_KEY', 'BINANCE_API_SECRET', 'SSH_PRIVATE_KEY'}
    if forbidden & set(values):
        raise ValueError('EXECUTION_CREDENTIAL_IN_STAGING_BINDINGS')
    for key in ('HUB_AUTO_ENGINE', 'HUB_ENABLE_EXTERNAL_LIVE', 'HUB_USE_LIVE_DATA', 'HUB_RESEARCH_AUTOSTART', 'HUB_MONITOR_AGENT'):
        if values.get(key) != '0':
            raise ValueError('STAGING_EXECUTION_MUST_REMAIN_DISABLED')
    if values.get('HUB_AUTH_MODE') != 'supabase' or values.get('HUB_EMERGENCY_ADMIN_ENABLED') != '0':
        raise ValueError('STAGING_CUSTOMER_AUTH_REQUIRED')
    blockers = ['DEPLOYMENT_AUTHORIZATION']
    if not values.get('SUPABASE_URL', '').startswith('https://') or not values.get('SUPABASE_KEY'):
        blockers.append('CUSTOMER_AUTHENTICATION')
    if not (root / 'tls/tls.crt').is_file() or not _private_regular(root / 'tls/tls.key'):
        blockers.append('STAGING_TLS_FILES')
    return {'status': 'BLOCKED', 'blockers': blockers, 'project': project,
            'customer_authentication_functional_test': 'UNKNOWN', 'tls_customer_trust': 'UNKNOWN',
            'target_capacity': 'UNKNOWN', 'target_port_availability': 'UNKNOWN', 'deployment_authorization': False}


def local_fixture_tls(root):
    tls = Path(root) / 'tls'
    if (tls / 'tls.key').exists() or (tls / 'tls.crt').exists():
        raise ValueError('NEW_LOCAL_TLS_FILES_REQUIRED')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
                    '-keyout', str(tls / 'tls.key'), '-out', str(tls / 'tls.crt')],
                   capture_output=True, check=True, timeout=15)
    (tls / 'tls.key').chmod(0o600)
    (tls / 'tls.crt').chmod(0o600)
    return {'status': 'LOCAL_SELF_SIGNED_FIXTURE_ONLY', 'customer_tls_trust': 'UNKNOWN'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('prepare')
    create.add_argument('--output', required=True)
    create.add_argument('--project', required=True)
    for name in ('api', 'worker', 'postgres', 'ingress'):
        create.add_argument('--' + name + '-image', required=True)
    create.add_argument('--uid', type=int, default=1000)
    create.add_argument('--port', type=int, default=18443)
    create.add_argument('--compose-profile', choices=tuple(PROFILES), default='remote')
    create.add_argument('--local-fixture-tls', action='store_true')
    validate = sub.add_parser('check')
    validate.add_argument('--release-dir', required=True)
    validate.add_argument('--bindings', required=True)
    validate.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    if args.action == 'prepare':
        result = prepare(args.output, project=args.project, api_id=args.api_image, worker_id=args.worker_image,
                         postgres_id=args.postgres_image, ingress_id=args.ingress_image, uid=args.uid, port=args.port, compose_profile=args.compose_profile)
        if args.local_fixture_tls:
            result['tls'] = local_fixture_tls(args.output)
    else:
        result = check(args.release_dir, args.bindings, args.project)
    print(json.dumps(result))
    return 2 if result['status'] == 'BLOCKED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
