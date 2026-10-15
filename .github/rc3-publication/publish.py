#!/usr/bin/env python3
"""Publish only the exact previously scanned RC3 archives. No build or execution."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler

REPO = 'Arjuunb/nexus-trading-bot'
COMMIT = '28acdbccc4f9b737c683d94e15bb16a09e50aa85'
SOURCE = '3ba76e44d9181ee1a8910667be645bfa477a7b3e4055c30538a29030e83caa4d'
BUILD_RUN = 38767616216
CONTROLLER = '6c0a6b22d20601bc1b15d0bd0cf3c39198b57943'
TAG = 'rc3-28acdbccc4f9-build-38767616216'
EXPECTED = {
    'api': {
        'archive_sha256': '00925c3630805b106e2e84fbcaeef5bbb62a4aae5ade04480ecda4a4b7f97896',
        'config_id': 'sha256:8803f66d1abb938c2a7aceb1f7d7cd947bf52f2f724cd1091d1d264acd75cd26'},
    'worker': {
        'archive_sha256': '4df0a36e74f4d9c38d033794f6d707d9e9674d65862bdf284a1abf8c5a7be7b3',
        'config_id': 'sha256:654bc9deb2841183835f4a7b4563fe37d62d9fdb1295a3bd4107a12710e602db'},
}


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


def archive_inventory(path, expected, tag):
    with Path(path).open('rb') as stream:
        require(digest(stream) == expected['archive_sha256'], 'ARCHIVE_SHA256_MISMATCH')
    with tarfile.open(path) as tar:
        rows = json.load(tar.extractfile('manifest.json'))
        require(len(rows) == 1, 'ONE_IMAGE_PER_ARCHIVE_REQUIRED')
        row = rows[0]
        payload = tar.extractfile(row['Config']).read()
        require('sha256:' + hashlib.sha256(payload).hexdigest() == expected['config_id'], 'CONFIG_ID_MISMATCH')
        config = json.loads(payload)
        require(config['os'] == 'linux' and config['architecture'] == 'amd64', 'PLATFORM_MISMATCH')
        labels = config['config']['Labels']
        require(labels.get('org.opencontainers.image.revision') == COMMIT
                and labels.get('nexus.source.sha256') == SOURCE, 'SOURCE_LABEL_MISMATCH')
        require(row['RepoTags'] == [tag], 'ARCHIVED_TAG_MISMATCH')
        layers = config['rootfs']['diff_ids']
        require(layers and len(layers) == len(row['Layers']), 'LAYER_COUNT_MISMATCH')
        for name, layer_hash in zip(row['Layers'], layers):
            require('sha256:' + digest(tar.extractfile(name)) == layer_hash, 'LAYER_HASH_MISMATCH')
    return {**expected, 'local_tag': tag, 'layer_diff_ids': layers, 'archive_verified': True}


def validate_scan(report, image_id):
    require(report.get('scanner') == 'Trivy 0.69.3' and report.get('scanner_exit_code') == 0
            and report.get('passed') is True and report.get('high_critical_count') == 0
            and report.get('secret_count_all_severities') == 0 and report.get('secrets') == []
            and report.get('image_config_id') == image_id, 'SCANNED_IMAGE_GATE_FAILED')
    findings = report.get('vulnerabilities')
    require(isinstance(findings, list) and not any(
        item.get('Severity') not in {'UNKNOWN', 'LOW', 'MEDIUM'} for item in findings),
        'HIGH_CRITICAL_OR_MALFORMED_FINDING')


def verify(archives, evidence, out):
    source = read(evidence / 'source-manifest.json')
    canonical = hashlib.sha256(json.dumps(source['files'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    require(source['head'] == COMMIT and source['source_sha256'] == SOURCE and canonical == SOURCE
            and len(source['files']) == 1386 and not source['working_tree'], 'CANONICAL_SOURCE_MISMATCH')
    result = read(evidence / 'result.json')
    require(result['status'] == 'BUILD_AND_SCAN_PASSED' and result['commit'] == COMMIT
            and result['source_sha256'] == SOURCE, 'ORIGINAL_BUILD_GATE_FAILED')
    validate_scan(read(evidence / 'source-secrets.json'), None)
    images = {}
    for role, expected in EXPECTED.items():
        original = result['images'][role]
        require(original['config_id'] == expected['config_id']
                and original['docker_archive_sha256'] == expected['archive_sha256'], 'BUILD_EVIDENCE_MISMATCH')
        validate_scan(read(evidence / (role + '-scan.json')), expected['config_id'])
        tag = 'nexus-rc3-' + role + ':28acdbccc4f9-38767616216-1'
        images[role] = archive_inventory(archives / (role + '.docker.tar'), expected, tag)
    write(out / 'verified-inputs.json', {'commit': COMMIT, 'source_sha256': SOURCE,
          'original_build_run': BUILD_RUN, 'images': images, 'build': False,
          'application_started': False, 'deployment_authorized': False})
    return images


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


def inspect_image(ref, expected):
    image = json.loads(command(['docker', 'image', 'inspect', ref]))[0]
    labels = image['Config'].get('Labels') or {}
    require(image['Id'] == expected['config_id'] and image['Os'] == 'linux'
            and image['Architecture'] == 'amd64' and image['RootFS']['Layers'] == expected['layer_diff_ids']
            and labels.get('org.opencontainers.image.revision') == COMMIT
            and labels.get('nexus.source.sha256') == SOURCE, 'LOADED_OR_PULLED_IMAGE_CHANGED')
    return image


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


def publish(archives, evidence, out):
    guard(os.environ)
    endpoint = json.loads(command(['docker', 'context', 'inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint == 'unix:///var/run/docker.sock', 'LOCAL_DOCKER_SOCKET_REQUIRED')
    images = verify(archives, evidence, out)
    for run_id, head in [(BUILD_RUN, CONTROLLER), (38769139448, '161404df82cb7ec77e97eeaf1a2621af8d9afe87')]:
        run = github('repos/' + REPO + '/actions/runs/' + str(run_id))
        require(run['status'] == 'completed' and run['conclusion'] == 'success'
                and run['head_sha'] == head, 'ORIGINAL_WORKFLOW_NOT_ACCEPTED')
    # Prepare both images and check every destination before pushing either.
    for role, expected in images.items():
        command(['docker', 'image', 'load', '--input', str(archives / (role + '.docker.tar'))],
                log=out / (role + '-load.log'))
        inspect_image(expected['local_tag'], expected)
        name = 'nexus-trading-bot-' + role
        package = github('users/Arjuunb/packages/container/' + name, missing_ok=True)
        write(out / (role + '-package-before.json'), {
            'visible_to_workflow': package is not None,
            'id': package.get('id') if package else None,
            'visibility': package.get('visibility') if package else None})
        target = 'ghcr.io/arjuunb/' + name + ':' + TAG
        existing = remote_manifest(target) if package else None
        if existing is not None:
            check_existing(existing, expected)
        expected.update({'tag': target, 'existing_same_config': existing is not None})
    write(out / 'publication.json', {'status': 'PREPARED', 'images': images, 'deployment_authorized': False})
    published = {}
    for role, expected in images.items():
        command(['docker', 'image', 'tag', expected['local_tag'], expected['tag']])
        if not expected['existing_same_config']:
            command(['docker', 'image', 'push', expected['tag']], log=out / (role + '-push.log'))
        descriptor = json.loads(command(['docker', 'buildx', 'imagetools', 'inspect', expected['tag'],
                                         '--format', '{{json .Manifest}}']))
        registry_digest = descriptor.get('digest', '')
        require(re.fullmatch(r'sha256:[a-f0-9]{64}', registry_digest), 'REGISTRY_DIGEST_REQUIRED')
        ref = expected['tag'].rsplit(':', 1)[0] + '@' + registry_digest
        manifest = remote_manifest(ref)
        require(manifest is not None, 'PUBLISHED_MANIFEST_MISSING')
        check_existing(manifest, expected)
        write(out / (role + '-registry-manifest.json'), manifest)
        write(out / (role + '-registry-descriptor.json'), descriptor)
        command(['docker', 'image', 'pull', '--platform', 'linux/amd64', ref], log=out / (role + '-pull-by-digest.log'))
        inspect_image(ref, expected)
        package = github('users/Arjuunb/packages/container/nexus-trading-bot-' + role)
        published[role] = {**expected, 'reference': ref, 'registry_manifest_digest': registry_digest,
                           'pull_by_digest_verified': True, 'package_visibility': package['visibility'],
                           'package_url': package['html_url']}
        write(out / 'publication.json', {'status': 'PUBLISHED_PARTIAL' if len(published) < 2 else 'PUBLISHED_VERIFIED',
              'schema_version': 'nexus_rc3_publication.v1', 'commit': COMMIT, 'source_sha256': SOURCE,
              'original_build_run': BUILD_RUN, 'publication_run': os.environ['GITHUB_RUN_ID'],
              'controller_commit': os.environ['GITHUB_SHA'], 'created_at': datetime.now(timezone.utc).isoformat(),
              'authorization': 'DIRECT_USER_APPROVAL_FOR_GHCR_PUBLICATION_ONLY',
              'github_environment': 'ghcr-staging-release', 'images': published,
              'rebuilt': False, 'application_started': False, 'vps_access': False,
              'deployment_authorized': False, 'worker_activation_authorized': False, 'scope': 'STAGE_A_ONLY'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('verify', 'publish'))
    parser.add_argument('--archives', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(exist_ok=True, parents=True)
    try:
        if args.action == 'verify':
            verify(args.archives, args.evidence, args.out)
        else:
            publish(args.archives, args.evidence, args.out)
    except Exception as error:
        write(args.out / 'failure.json', {'status': 'FAILED', 'error_type': type(error).__name__,
              'reason': str(error) if isinstance(error, ValueError) else 'COMMAND_OR_EVIDENCE_ERROR',
              'possible_partial_publication': (args.out / 'publication.json').exists(), 'deployment_authorized': False})
        raise


if __name__ == '__main__':
    main()
