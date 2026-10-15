#!/usr/bin/env python3
"""Publish the separately approved, exact scanned PostgreSQL archive; never build/start."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

REPO = 'Arjuunb/nexus-trading-bot'
PINS = json.loads(Path(__file__).with_name('pins.json').read_text())

def require(condition, message):
    if not condition:
        raise ValueError(message)

def digest(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        value.update(block)
    return value.hexdigest()

def write(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')

def read(path):
    return json.loads(Path(path).read_text())

def guard(env):
    require(env.get('GITHUB_REPOSITORY') == REPO, 'REPOSITORY_MISMATCH')
    require(env.get('GITHUB_EVENT_NAME') in {'push', 'workflow_dispatch'}
            and env.get('GITHUB_REF') == 'refs/heads/ci/rc3-isolated-image-build'
            and env.get('GITHUB_RUN_ATTEMPT') == '1', 'FRESH_AUTHORIZED_BRANCH_RUN_REQUIRED')
    require(env.get('RUNNER_ENVIRONMENT') == 'github-hosted'
            and env.get('RUNNER_OS') == 'Linux' and env.get('RUNNER_ARCH') == 'X64',
            'ISOLATED_GITHUB_LINUX_AMD64_REQUIRED')
    require(not env.get('DOCKER_HOST') and not env.get('DOCKER_CONTEXT'), 'REMOTE_DOCKER_FORBIDDEN')

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('UNEXPECTED_GITHUB_API_REDIRECT')

def github(path, *, missing_ok=False):
    # Only GitHub API reads; tokens are never printed or sent to other origins.
    request = Request('https://api.github.com/' + path, headers={
        'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'nexus-rc3-publication-only'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            data = response.read(4 * 1024 * 1024 + 1)
        require(len(data) <= 4 * 1024 * 1024, 'API_METADATA_LIMIT')
        return json.loads(data)
    except HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise ValueError('GITHUB_API_READ_FAILED_HTTP_' + str(error.code)) from None

def command(args, *, log=None, timeout=900):
    if log:
        with Path(log).open('w') as stream:
            result = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        require(result.returncode == 0, 'COMMAND_FAILED_SEE_' + Path(log).name)
        return ''
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    require(result.returncode == 0, 'COMMAND_FAILED_' + args[0])
    return result.stdout.strip()

def remote_manifest(ref):
    result = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', '--raw', ref],
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        reason = result.stderr.lower()
        if 'not found' in reason or 'manifest unknown' in reason:
            return None
        raise ValueError('REGISTRY_TAG_READ_FAILED')
    return json.loads(result.stdout)

def check_existing(manifest, expected):
    require(manifest.get('schemaVersion') == 2 and manifest.get('config', {}).get('digest') == expected['config_id'],
            'EXISTING_TAG_HAS_DIFFERENT_IMAGE_REFUSE_OVERWRITE')


def validate_labels(labels):
    require(labels.get('org.opencontainers.image.revision') == PINS['build_controller']
            and labels.get('nexus.gosu.source.commit') == PINS['gosu_commit']
            and labels.get('nexus.gosu.go.version') == PINS['go_version']
            and labels.get('nexus.gosu.binary.sha256') == PINS['gosu_binary_sha256'], 'SOURCE_PROVENANCE_LABEL_MISMATCH')


def validate_scan(report, expected_id):
    require(report.get('SchemaVersion') == 2 and report.get('ArtifactType') == 'container_image'
            and report.get('Trivy', {}).get('Version') == '0.75.0'
            and report.get('Metadata', {}).get('ImageID') == expected_id, 'SCAN_IDENTITY_MISMATCH')
    require(report.get('Metadata', {}).get('OS', {}).get('Family') == 'alpine', 'ALPINE_SCAN_REQUIRED')
    results = report.get('Results')
    require(isinstance(results, list) and any(r.get('Class') == 'os-pkgs' for r in results), 'OS_SCAN_RESULTS_REQUIRED')
    gosu = [r for r in results if r.get('Type') == 'gobinary' and r.get('Target', '').lstrip('/') == 'usr/local/bin/gosu']
    require(len(gosu) == 1 and any(p.get('Name') == 'stdlib' and p.get('Version') == 'v1.27.2'
            for p in gosu[0].get('Packages', [])), 'PATCHED_HELPER_INVENTORY_REQUIRED')
    for result in results:
        require(isinstance(result.get('Secrets', []), list) and not result.get('Secrets'), 'SECRET_GATE_FAILED')
        vulns = result.get('Vulnerabilities', [])
        require(isinstance(vulns, list) and all(v.get('Severity') in {'UNKNOWN', 'LOW', 'MEDIUM'} for v in vulns),
                'HIGH_CRITICAL_OR_MALFORMED_FINDING')


def archive_inventory(path, expected):
    require(path.stat().st_size == expected['archive_bytes'], 'ARCHIVE_SIZE_MISMATCH')
    with path.open('rb') as stream:
        require(digest(stream) == expected['archive_sha256'], 'ARCHIVE_SHA256_MISMATCH')
    with tarfile.open(path) as tar:
        rows = json.load(tar.extractfile('manifest.json'))
        require(len(rows) == 1 and rows[0]['RepoTags'] == [expected['local_tag']], 'EXACT_ARCHIVED_IMAGE_REQUIRED')
        row = rows[0]; payload = tar.extractfile(row['Config']).read()
        require('sha256:' + hashlib.sha256(payload).hexdigest() == expected['config_id'], 'ARCHIVE_CONFIG_ID_MISMATCH')
        config = json.loads(payload)
        require(config['os'] == 'linux' and config['architecture'] == 'amd64', 'IMAGE_PLATFORM_MISMATCH')
        validate_labels(config['config'].get('Labels', {}))
        layers = config['rootfs']['diff_ids']
        require(layers and len(layers) == len(row['Layers']), 'LAYER_COUNT_MISMATCH')
        for name, diff_id in zip(row['Layers'], layers):
            require('sha256:' + digest(tar.extractfile(name)) == diff_id, 'LAYER_HASH_MISMATCH')
        with tarfile.open(fileobj=tar.extractfile(row['Layers'][-1])) as last:
            files = [entry for entry in last.getmembers() if not entry.isdir()]
            require(len(files) == 1 and files[0].isfile() and files[0].name.lstrip('./') == 'usr/local/bin/gosu',
                    'ONLY_REVIEWED_HELPER_REPLACEMENT_ALLOWED')
            require(digest(last.extractfile(files[0])) == expected['gosu_binary_sha256'], 'REPLACEMENT_BINARY_HASH_MISMATCH')
    return {k: expected[k] for k in ('archive_sha256', 'archive_bytes', 'config_id', 'local_tag')} | {
        'layer_diff_ids': layers, 'config': config}


def verify(archive, evidence, out):
    for name, expected in PINS['evidence_sha256'].items():
        with (evidence / name).open('rb') as stream:
            require(digest(stream) == expected, 'ORIGINAL_EVIDENCE_HASH_MISMATCH: ' + name)
    result = read(evidence / 'result.json')
    require(result['status'] == 'BUILD_TEST_SCAN_PASS' and result['run_id'] == str(PINS['build_run'])
            and result['controller_commit'] == PINS['build_controller']
            and result['config_id'] == PINS['config_id'] and not result['published']
            and result['archive']['archive_sha256'] == PINS['archive_sha256'], 'ORIGINAL_BUILD_RESULT_MISMATCH')
    scan = read(evidence / 'scan-result.json')
    require(scan['passed'] is True and scan['high_critical'] == 0 and scan['secrets_all_severities'] == 0,
            'ORIGINAL_SCAN_FAILED')
    validate_scan(read(evidence / 'postgres-scan.json'), PINS['config_id'])
    tests = read(evidence / 'disposable-postgres-tests.json')
    require(tests['status'] == 'PASS' and tests['gosu_binary_sha256'] == PINS['gosu_binary_sha256']
            and tests['network'] == 'none' and tests['published_ports'] == [], 'ORIGINAL_DATABASE_TESTS_FAILED')
    image = archive_inventory(archive, PINS)
    base = read(evidence / 'base-image.json')
    require(base['Id'] == PINS['base_config_id'] and image['layer_diff_ids'][:-1] == base['RootFS']['Layers'],
            'UPSTREAM_BASE_CHANGED')
    for key in ('Env', 'Entrypoint', 'Cmd', 'User', 'WorkingDir', 'Volumes', 'ExposedPorts', 'StopSignal', 'Healthcheck'):
        default = '' if key == 'User' else None
        require(image['config']['config'].get(key, default) == base['Config'].get(key, default),
                'UPSTREAM_RUNTIME_CONFIG_CHANGED: ' + key)
    image.pop('config')
    write(out / 'verified-inputs.json', {'image': image, 'pins': PINS,
        'publication_authorization': 'DIRECT_USER_APPROVAL_FOR_THIS_POSTGRES_IMAGE_PUBLICATION_ONLY',
        'rebuilt': False, 'application_started': False, 'vps_access': False, 'deployment_authorized': False})
    return image


def inspect_image(reference, expected):
    image = json.loads(command(['docker', 'image', 'inspect', reference]))[0]
    require(image['Id'] == expected['config_id'] and image['Os'] == 'linux' and image['Architecture'] == 'amd64'
            and image['RootFS']['Layers'] == expected['layer_diff_ids'], 'LOADED_OR_PULLED_IMAGE_CHANGED')
    validate_labels(image['Config'].get('Labels') or {})
    return image


class RegistryNoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_registry_bytes(reference, expected, out):
    package = 'arjuunb/nexus-trading-bot-postgres'
    opener = build_opener(RegistryNoRedirect())
    basic = base64.b64encode((os.environ['GITHUB_ACTOR'] + ':' + os.environ['GH_TOKEN']).encode()).decode()
    request = Request('https://ghcr.io/token?service=ghcr.io&scope=repository:' + package + ':pull',
                      headers={'Authorization': 'Basic ' + basic})
    with opener.open(request, timeout=30) as response:
        token = json.load(response)['token']
    headers = {'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json'}
    registry = 'https://ghcr.io/v2/' + package
    digest_value = reference.split('@', 1)[1]
    with opener.open(Request(registry + '/manifests/' + digest_value, headers=headers), timeout=30) as response:
        raw = response.read(1024**2)
        require(response.headers.get('Docker-Content-Digest') == digest_value, 'REGISTRY_DIGEST_HEADER_MISMATCH')
    require('sha256:' + hashlib.sha256(raw).hexdigest() == digest_value, 'REGISTRY_MANIFEST_BYTES_MISMATCH')
    manifest = json.loads(raw); check_existing(manifest, expected)
    (out / 'registry-manifest.raw.json').write_bytes(raw)
    with opener.open(Request(registry + '/manifests/' + PINS['target'].rsplit(':', 1)[1], headers=headers), timeout=30) as response:
        require('sha256:' + hashlib.sha256(response.read(1024**2)).hexdigest() == digest_value, 'REGISTRY_TAG_CHANGED')
    try:
        response = opener.open(Request(registry + '/blobs/' + expected['config_id'], headers=headers), timeout=30)
    except HTTPError as error:
        require(error.code in {301, 302, 303, 307, 308}, 'REGISTRY_CONFIG_READ_FAILED')
        location = error.headers['Location']; target = urlsplit(location)
        require(target.scheme == 'https' and target.hostname and not target.username and not target.password
                and (target.hostname == 'ghcr.io' or target.hostname.endswith(('.githubusercontent.com', '.blob.core.windows.net'))),
                'UNTRUSTED_BLOB_REDIRECT')
        # Do not forward registry credentials to a signed blob/CDN URL.
        response = opener.open(Request(location), timeout=30)
    with response:
        payload = response.read(4 * 1024**2)
    require('sha256:' + hashlib.sha256(payload).hexdigest() == expected['config_id'], 'REGISTRY_CONFIG_BYTES_MISMATCH')
    config = json.loads(payload)
    require(config['os'] == 'linux' and config['architecture'] == 'amd64'
            and config['rootfs']['diff_ids'] == expected['layer_diff_ids'], 'REGISTRY_ROOTFS_OR_PLATFORM_MISMATCH')
    validate_labels(config['config'].get('Labels', {}))
    (out / 'registry-config.raw.json').write_bytes(payload)
    proof = {'manifest_sha256_verified': True, 'config_bytes_sha256_verified': True,
        'tag_resolves_to_expected_digest': True, 'source_labels_verified': True,
        'rootfs_diff_ids_verified': True, 'authentication': 'SCOPED_WORKFLOW_TOKEN; NO_CREDENTIALS_RETAINED'}
    write(out / 'registry-http-verification.json', proof)
    return proof


def publish(archive, evidence, out):
    guard(os.environ)
    endpoint = json.loads(command(['docker', 'context', 'inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint == 'unix:///var/run/docker.sock', 'LOCAL_DOCKER_SOCKET_REQUIRED')
    expected = verify(archive, evidence, out)
    original = github('repos/' + REPO + '/actions/runs/' + str(PINS['build_run']))
    require(original['status'] == 'completed' and original['conclusion'] == 'success'
            and original['head_sha'] == PINS['build_controller'], 'ORIGINAL_BUILD_RUN_NOT_ACCEPTED')
    for key in ('image', 'evidence'):
        artifact = github('repos/' + REPO + '/actions/artifacts/' + str(PINS[key + '_artifact_id']))
        require(not artifact['expired'] and artifact['workflow_run']['id'] == PINS['build_run']
                and artifact['workflow_run']['head_sha'] == PINS['build_controller']
                and artifact['digest'] == PINS[key + '_artifact_zip_digest'], 'ORIGINAL_ARTIFACT_IDENTITY_MISMATCH')
    command(['docker', 'image', 'load', '--input', str(archive)], log=out / 'image-load.log')
    inspect_image(expected['local_tag'], expected)
    package_path = 'users/Arjuunb/packages/container/nexus-trading-bot-postgres'
    before = github(package_path, missing_ok=True)
    write(out / 'package-before.json', {'visible_to_workflow': before is not None,
        'visibility': before.get('visibility') if before else None})
    existing = remote_manifest(PINS['target'])
    if existing is not None:
        check_existing(existing, expected)
    state = {'schema_version': 'nexus_rc3_postgres_publication.v1', 'status': 'PREPARED',
        'original_build_run': PINS['build_run'], 'publication_run': os.environ['GITHUB_RUN_ID'],
        'controller_commit': os.environ['GITHUB_SHA'], 'created_at': datetime.now(timezone.utc).isoformat(),
        'image': {**expected, 'tag': PINS['target']},
        'authorization': 'DIRECT_USER_APPROVAL_FOR_THIS_POSTGRES_IMAGE_PUBLICATION_ONLY',
        'github_environment': 'ghcr-staging-release', 'rebuilt': False, 'application_started': False,
        'vps_access': False, 'deployment_authorized': False, 'worker_activation_authorized': False}
    write(out / 'publication.json', state)
    if existing is None:
        command(['docker', 'image', 'tag', expected['local_tag'], PINS['target']])
        command(['docker', 'image', 'push', PINS['target']], log=out / 'image-push.log')
    descriptor = json.loads(command(['docker', 'buildx', 'imagetools', 'inspect', PINS['target'], '--format', '{{json .Manifest}}']))
    registry_digest = descriptor.get('digest', '')
    require(re.fullmatch('sha256:[0-9a-f]{64}', registry_digest), 'REGISTRY_DIGEST_REQUIRED')
    reference = PINS['target'].rsplit(':', 1)[0] + '@' + registry_digest
    command(['docker', 'image', 'pull', '--platform', 'linux/amd64', reference], log=out / 'pull-by-digest.log')
    inspect_image(reference, expected)
    proof = verify_registry_bytes(reference, expected, out)
    package = github(package_path, missing_ok=True)
    state.update(status='PUBLISHED_VERIFIED', publication_authorized=True,
        image={**expected, 'tag': PINS['target'], 'reference': reference, 'registry_manifest_digest': registry_digest,
            'pull_by_digest_verified': True, 'registry_http_verification': proof,
            'package_visibility': package.get('visibility') if package else 'UNKNOWN',
            'package_url': package.get('html_url') if package else None},
        package_visibility_changed=False, existing_same_image=existing is not None)
    write(out / 'registry-descriptor.json', descriptor)
    write(out / 'publication.json', state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('verify', 'publish'))
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(); args.out.mkdir(parents=True, exist_ok=True)
    try:
        if args.action == 'verify':
            verify(args.archive, args.evidence, args.out)
        else:
            publish(args.archive, args.evidence, args.out)
    except Exception as error:
        write(args.out / 'failure.json', {'status': 'FAILED', 'error_type': type(error).__name__,
            'reason': str(error) if isinstance(error, ValueError) else 'COMMAND_OR_EVIDENCE_ERROR',
            'possible_publication': (args.out / 'publication.json').exists(), 'deployment_authorized': False})
        raise


if __name__ == '__main__':
    main()
