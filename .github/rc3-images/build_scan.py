#!/usr/bin/env python3
"""Build accepted RC3 locally on an ephemeral GitHub runner. Never publish/deploy."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from urllib.request import Request, urlopen

COMMIT = '28acdbccc4f9b737c683d94e15bb16a09e50aa85'
SOURCE = '3ba76e44d9181ee1a8910667be645bfa477a7b3e4055c30538a29030e83caa4d'
REPO = 'Arjuunb/nexus-trading-bot'
VALIDATION_RUN = 38759339962
TRIVY = '0.69.3'
ALL_SEVERITIES = 'UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL'
BASES = {
    'node': 'node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0',
    'api': 'python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929',
    'worker': 'python:3.12-alpine3.24@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def command(args, *, cwd=None, output=None, timeout=1800):
    # No credentials are forwarded to builds or image probes.
    if output:
        with Path(output).open('w') as stream:
            result = subprocess.run(args, cwd=cwd, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=timeout)
        require(result.returncode == 0, 'COMMAND_FAILED: ' + args[0])
        return ''
    return subprocess.check_output(args, cwd=cwd, text=True, timeout=timeout).strip()


def hosted_guard(env, system, machine):
    require(env.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'GITHUB_HOSTED_RUNNER_REQUIRED')
    require(env.get('RUNNER_OS') == 'Linux' and env.get('RUNNER_ARCH') == 'X64'
            and system == 'Linux' and machine == 'x86_64', 'LINUX_AMD64_REQUIRED')
    require(env.get('GITHUB_REPOSITORY') == REPO, 'REPOSITORY_MISMATCH')
    event, ref = env.get('GITHUB_EVENT_NAME'), env.get('GITHUB_REF')
    require((event == 'push' and ref == 'refs/heads/ci/rc3-isolated-image-build')
            or (event == 'workflow_dispatch' and ref in {
                'refs/heads/main', 'refs/heads/ci/rc3-isolated-image-build'}), 'CONTROLLER_EVENT_DENIED')


def resource_gate(cpus, available_ram, disks):
    require(cpus >= 4, 'AT_LEAST_4_CPUS_REQUIRED')
    require(available_ram >= 8 * 1024**3, 'AT_LEAST_8_GIB_AVAILABLE_RAM_REQUIRED')
    require(disks and all(value >= 20 * 1024**3 for value in disks.values()),
            'AT_LEAST_20_GIB_FREE_DISK_REQUIRED')


def resources(evidence, phase, *, enforce=True):
    memory = dict(row.split(':', 1) for row in Path('/proc/meminfo').read_text().splitlines())
    available = int(memory['MemAvailable'].split()[0]) * 1024
    paths = {os.environ['GITHUB_WORKSPACE'], os.environ['RUNNER_TEMP'], '/var/lib/docker'}
    disks = {path: shutil.disk_usage(path).free for path in sorted(paths)}
    value = {'cpus': len(os.sched_getaffinity(0)), 'available_ram_bytes': available,
             'available_disk_bytes': disks, 'system': platform.system(), 'machine': platform.machine()}
    write(evidence / ('resources-' + phase + '.json'), value)
    if enforce:
        resource_gate(value['cpus'], available, disks)
    return value


def preflight(evidence):
    hosted_guard(os.environ, platform.system(), platform.machine())
    resources(evidence, 'before-sdk-cleanup', enforce=False)
    # These fixed paths contain unused SDKs on this disposable GitHub VM only.
    # Never delete Docker images, runtime volumes, repositories or user state.
    for path in ['/usr/local/lib/android', '/usr/share/dotnet', '/opt/ghc', '/opt/hostedtoolcache/CodeQL']:
        command(['sudo', 'rm', '-rf', '--', path])
    resources(evidence, 'pre-build')
    info = json.loads(command(['docker', 'info', '--format', '{{json .}}']))
    require(info['OSType'] == 'linux' and info['Architecture'] in {'x86_64', 'amd64'},
            'LOCAL_LINUX_AMD64_DAEMON_REQUIRED')
    require(not os.environ.get('DOCKER_HOST') and not os.environ.get('DOCKER_CONTEXT'),
            'REMOTE_DOCKER_FORBIDDEN')
    endpoint = json.loads(command(['docker', 'context', 'inspect']))[0]['Endpoints']['docker']['Host']
    require(endpoint == 'unix:///var/run/docker.sock', 'LOCAL_DOCKER_SOCKET_REQUIRED')
    write(evidence / 'docker.json', {
        'version': command(['docker', 'version', '--format', '{{json .}}']),
        'buildx_version': command(['docker', 'buildx', 'version']), 'endpoint': endpoint,
        'storage_driver': info['Driver'], 'docker_root': info['DockerRootDir']})
    def api(suffix):
        request = Request('https://api.github.com/repos/' + REPO + '/' + suffix,
                          headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'nexus-rc3-build-only'})
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    run = api('actions/runs/' + str(VALIDATION_RUN))
    jobs = api('actions/runs/' + str(VALIDATION_RUN) + '/jobs?per_page=100')['jobs']
    require(run['status'] == 'completed' and run['conclusion'] == 'success'
            and run['head_sha'] == '24b67bf43e1712b5566fa33b457814ceac5ec30b', 'VALIDATION_RUN_NOT_ACCEPTED')
    for minor in ('3.11', '3.12'):
        require(any(minor in job['name'] and job['conclusion'] == 'success' for job in jobs),
                'VALIDATION_MATRIX_INCOMPLETE')
    write(evidence / 'prior-validation.json', {
        'id': run['id'], 'conclusion': run['conclusion'], 'controller_commit': run['head_sha'],
        'url': run['html_url'], 'jobs': [{k: j[k] for k in ('id', 'name', 'conclusion')} for j in jobs]})


def sanitized_scan(data, exit_code, *, artifact_types, expected_image=None, vulnerabilities=False):
    require(isinstance(data, dict) and type(data.get('SchemaVersion')) is int
            and data['SchemaVersion'] == 2, 'SCANNER_SCHEMA_INVALID')
    require(data.get('ArtifactType') in artifact_types, 'SCANNER_ARTIFACT_INVALID')
    require(data.get('Trivy') == {'Version': TRIVY}, 'SCANNER_VERSION_MISMATCH')
    require(all(isinstance(data.get(k), str) and data[k].strip()
                for k in ('ReportID', 'CreatedAt', 'ArtifactName')), 'SCANNER_METADATA_INCOMPLETE')
    if expected_image:
        require(data.get('Metadata', {}).get('ImageID') == expected_image, 'SCANNED_IMAGE_ID_MISMATCH')
    require(isinstance(data.get('Results', []), list), 'SCANNER_RESULTS_INVALID')
    secrets, vulns, package_classes = [], [], set()
    for result in data.get('Results', []):
        require(isinstance(result, dict), 'SCANNER_RESULT_INVALID')
        package_classes.add(result.get('Class'))
        for key in ('Secrets', 'Vulnerabilities'):
            require(isinstance(result.get(key, []), list), 'SCANNER_FINDINGS_INVALID')
        for item in result.get('Secrets', []):
            require(isinstance(item, dict), 'SCANNER_SECRET_INVALID')
            # Never retain matches, surrounding code or secret-bearing raw reports.
            secrets.append({k: item.get(k) for k in ('RuleID', 'Severity', 'StartLine', 'EndLine')})
        for item in result.get('Vulnerabilities', []):
            require(isinstance(item, dict) and item.get('Severity') in ALL_SEVERITIES.split(','),
                    'SCANNER_VULNERABILITY_INVALID')
            vulns.append({k: item.get(k) for k in (
                'VulnerabilityID', 'PkgName', 'InstalledVersion', 'FixedVersion', 'Severity', 'Status')})
    if vulnerabilities:
        require('os-pkgs' in package_classes and data.get('Metadata', {}).get('OS', {}).get('Family') == 'alpine',
                'ALPINE_OS_SCAN_EVIDENCE_REQUIRED')
    high = sum(item['Severity'] in {'HIGH', 'CRITICAL'} for item in vulns)
    return {'scanner': 'Trivy ' + TRIVY, 'artifact_type': data['ArtifactType'],
            'created_at': data['CreatedAt'], 'report_id': data['ReportID'],
            'scanner_exit_code': exit_code, 'image_config_id': expected_image,
            'vulnerabilities': vulns, 'high_critical_count': high,
            'secret_count_all_severities': len(secrets), 'secrets': secrets,
            'passed': exit_code == 0 and high == 0 and not secrets}


def scan(kind, target, evidence, scratch, name, image_id=None):
    raw = scratch / (name + '-raw.json')
    args = ['trivy', kind, '--config', str(scratch / 'trivy.yaml'),
            '--ignorefile', str(scratch / 'empty.ignore'),
            '--secret-config', str(scratch / 'secret.yaml'),
            '--cache-dir', str(scratch / 'cache'), '--scanners', 'vuln,secret' if image_id else 'secret',
            '--severity', ALL_SEVERITIES, '--format', 'json', '--output', str(raw),
            '--exit-code', '0', '--timeout', '20m', '--disable-telemetry', '--no-progress']
    if image_id:
        args += ['--image-src', 'docker', '--image-config-scanners', 'secret', '--pkg-types', 'os,library']
    else:
        args += ['--skip-dirs', str(Path(target) / '.git')]
    args.append(target)
    try:
        result = subprocess.run(args, cwd=scratch, timeout=1250, capture_output=True)
        require(raw.is_file(), 'SCANNER_REPORT_MISSING')
        report = sanitized_scan(json.loads(raw.read_text()), result.returncode,
                                artifact_types={'container_image'} if image_id else {'repository', 'filesystem'},
                                expected_image=image_id, vulnerabilities=bool(image_id))
        report['raw_report_sha256'] = sha(raw)
        write(evidence / (name + '.json'), report)
        require(report['passed'], 'SCAN_GATE_FAILED: ' + name)
    finally:
        raw.unlink(missing_ok=True)


def image_identity(inspect, metadata):
    require(inspect['Os'] == 'linux' and inspect['Architecture'] == 'amd64', 'IMAGE_PLATFORM_MISMATCH')
    labels = inspect['Config'].get('Labels', {})
    require(labels.get('org.opencontainers.image.revision') == COMMIT
            and labels.get('nexus.source.sha256') == SOURCE, 'IMAGE_SOURCE_LABEL_MISMATCH')
    require(metadata.get('containerimage.config.digest') == inspect['Id'], 'BUILDKIT_CONFIG_ID_MISMATCH')
    digest = metadata.get('containerimage.digest', '')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', digest) is not None, 'LOCAL_MANIFEST_DIGEST_REQUIRED')
    return {'config_id': inspect['Id'], 'buildkit_local_manifest_digest': digest,
            'repo_digests': inspect.get('RepoDigests', []), 'tags': inspect['RepoTags'],
            'platform': 'linux/amd64', 'size_bytes': inspect['Size'], 'labels': labels,
            'registry_publication': False}


def build(source, evidence, scratch, archives):
    require(command(['git', 'rev-parse', 'HEAD'], cwd=source) == COMMIT, 'SOURCE_COMMIT_MISMATCH')
    require(not command(['git', 'status', '--porcelain=v1', '-uall'], cwd=source), 'DIRTY_SOURCE')
    frozen = scratch / 'frozen'
    command(['python3', 'ops/github-release.py', 'freeze', '--output', str(frozen),
             '--reviewed-commit', COMMIT, '--expected-source', SOURCE], cwd=source)
    manifest = json.loads((frozen / 'source-manifest.json').read_text())
    require(manifest['head'] == COMMIT and manifest['source_sha256'] == SOURCE
            and len(manifest['files']) == 1386, 'CANONICAL_SOURCE_MISMATCH')
    shutil.copyfile(frozen / 'source-manifest.json', evidence / 'source-manifest.json')
    context = frozen / 'context'
    for role in ('api', 'worker'):
        filename = 'api.alpine.Dockerfile' if role == 'api' else 'research-worker.alpine.Dockerfile'
        require(BASES[role] in (context / 'ops' / filename).read_text(), 'REVIEWED_ALPINE_BASE_MISMATCH')
    require(BASES['node'] in (context / 'ops/api.alpine.Dockerfile').read_text(), 'REVIEWED_NODE_BASE_MISMATCH')
    write(evidence / 'build-inputs.json', {'commit': COMMIT, 'source_sha256': SOURCE, 'bases': BASES,
        'files': {name: sha(context / name) for name in (
            'ops/api.alpine.Dockerfile', 'ops/research-worker.alpine.Dockerfile',
            'ops/api-requirements.lock', 'ops/prepare-api-alpine-inputs.py',
            'ops/prepare-shadow-worker-wheels.py', 'automation-hub-dashboard/package-lock.json',
            'tradexa-landing/package-lock.json')}})
    scan('fs', str(source), evidence, scratch, 'source-secrets')
    for name, ref in BASES.items():
        command(['docker', 'pull', '--platform', 'linux/amd64', ref], output=evidence / (name + '-base-pull.log'))
        inspect = json.loads(command(['docker', 'image', 'inspect', ref]))[0]
        write(evidence / (name + '-base.json'), {
            'reference': ref, 'config_id': inspect['Id'], 'platform': inspect['Os'] + '/' + inspect['Architecture'],
            'repo_digests': inspect.get('RepoDigests', [])})
    command(['python3', 'ops/prepare-api-alpine-inputs.py', str(scratch / 'api-inputs')],
            cwd=source, output=evidence / 'api-verified-inputs.json')
    command(['python3', 'ops/prepare-shadow-worker-wheels.py', str(scratch / 'worker-wheels'),
             '--platform', 'musllinux'], cwd=source, output=evidence / 'worker-verified-inputs.json')
    shutil.copyfile(scratch / 'worker-wheels/requirements-verified.txt', evidence / 'worker-requirements-verified.txt')
    # The bundled Docker driver belongs to this disposable VM's local daemon.
    # No remote builder, cache import/export or registry output.
    builder = command(['docker', 'buildx', 'inspect', 'default'])
    require(re.search(r'^Driver:\s+docker\s*$', builder, re.MULTILINE), 'BUNDLED_LOCAL_BUILDKIT_REQUIRED')
    (evidence / 'builder.txt').write_text(builder + '\n')
    tags, identities = {}, {}
    failures = []
    for role in ('api', 'worker'):
        try:
            resources(evidence, 'before-' + role)
            tag = 'nexus-rc3-' + role + ':28acdbccc4f9-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
            filename = 'api.alpine.Dockerfile' if role == 'api' else 'research-worker.alpine.Dockerfile'
            named = ('build_inputs=' + str(scratch / 'api-inputs') if role == 'api'
                     else 'verified_wheels=' + str(scratch / 'worker-wheels'))
            metadata_file = evidence / (role + '-buildkit.json')
            args = ['docker', 'buildx', 'build', '--builder', 'default', '--platform', 'linux/amd64',
                    '--no-cache', '--pull', '--load', '--provenance=false', '--progress=plain',
                    '--metadata-file', str(metadata_file), '--iidfile', str(evidence / (role + '-iid.txt')),
                    '--network=default' if role == 'api' else '--network=none',
                    '--build-context', named, '--label', 'org.opencontainers.image.revision=' + COMMIT,
                    '--label', 'nexus.source.sha256=' + SOURCE, '--tag', tag, '-f', str(context / 'ops' / filename)]
            if role == 'api':
                args += ['--build-arg', 'GIT_COMMIT=' + COMMIT, '--build-arg', 'GIT_BRANCH=accepted-rc3',
                         '--build-arg', 'NEXUS_SOURCE_SHA256=' + SOURCE]
            args.append(str(context))
            write(evidence / (role + '-build-command.json'), args)
            command(args, output=evidence / (role + '-build.log'), timeout=2400)
            inspect = json.loads(command(['docker', 'image', 'inspect', tag]))[0]
            identity = image_identity(inspect, json.loads(metadata_file.read_text()))
            identities[role] = identity
            tags[role] = tag
            write(evidence / (role + '-image.json'), identity)
            scan('image', identity['config_id'], evidence, scratch, role + '-scan', identity['config_id'])
        except (ValueError, subprocess.SubprocessError, OSError) as error:
            failures.append(role)
            write(evidence / (role + '-failure.json'), {'status': 'FAILED', 'error_type': type(error).__name__,
                  'reason': str(error) if isinstance(error, ValueError) else 'BUILD_OR_SCAN_COMMAND_FAILED'})
    require(not failures, 'BUILD_OR_SCAN_FAILED_FOR: ' + ','.join(failures))
    # Reuse the accepted source's bounded probes; application/worker entrypoints never run.
    command(['python3', 'ops/github-release.py', 'verify-images', '--require-alpine',
             '--source-manifest', str(frozen / 'source-manifest.json'), '--api-image', tags['api'],
             '--worker-image', tags['worker'], '--output', str(evidence / 'image-provenance.json')], cwd=source)
    for role, tag in tags.items():
        archive = archives / (role + '.docker.tar')
        command(['docker', 'image', 'save', '--output', str(archive), tag])
        identities[role]['docker_archive_sha256'] = sha(archive)
        identities[role]['archive_bytes'] = archive.stat().st_size
        write(evidence / (role + '-image.json'), identities[role])
    write(evidence / 'result.json', {'status': 'BUILD_AND_SCAN_PASSED', 'commit': COMMIT,
          'source_sha256': SOURCE, 'images': identities, 'publication_authorized': False,
          'deployment_authorized': False, 'application_started': False,
          'worker_activation_authorized': False, 'scope': 'STAGE_A_ONLY',
          'note': 'Local BuildKit manifest digests and config IDs; no registry digests or bit-identical rebuild claim.'})


def main():
    evidence = Path(os.environ['RUNNER_TEMP']) / 'rc3-image-evidence'
    evidence.mkdir(exist_ok=True)
    try:
        hosted_guard(os.environ, platform.system(), platform.machine())
        if sys.argv[1] == 'preflight':
            preflight(evidence)
            return
        scratch = Path(os.environ['RUNNER_TEMP']) / 'rc3-image-work'
        scratch.mkdir()
        for name, text in [('trivy.yaml', '{}\n'), ('secret.yaml', '{}\n'), ('empty.ignore', '')]:
            (scratch / name).write_text(text)
        archives = Path(os.environ['RUNNER_TEMP']) / 'rc3-image-archives'
        archives.mkdir()
        write(evidence / 'request.json', {'controller_commit': os.environ['GITHUB_SHA'],
              'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
              'commit': COMMIT, 'source_sha256': SOURCE, 'publication_authorized': False,
              'deployment_authorized': False})
        build(Path(os.environ['GITHUB_WORKSPACE']) / 'source', evidence, scratch, archives)
    except Exception as error:
        write(evidence / 'result.json', {'status': 'BLOCKED_OR_FAILED', 'error_type': type(error).__name__,
              'reason': str(error) if isinstance(error, ValueError) else 'COMMAND_OR_EVIDENCE_FAILED',
              'publication_authorized': False, 'deployment_authorized': False})
        raise


if __name__ == '__main__':
    main()
