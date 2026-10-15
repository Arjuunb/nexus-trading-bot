#!/usr/bin/env python3
"""Build/scan a staging support image on a disposable CI VM; never publish/deploy."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import subprocess
import tarfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
INPUTS = json.loads((ROOT / 'inputs.json').read_text())
SEVERITIES = {'UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def run(args, *, env=None, cwd=None, output=None, timeout=900, success=True):
    result = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if output:
        Path(output).write_text(result.stdout + result.stderr)
    if success:
        require(result.returncode == 0, 'COMMAND_FAILED: ' + Path(args[0]).name)
    return result


def guard(env, system, machine):
    require(env.get('GITHUB_REPOSITORY') == 'Arjuunb/nexus-trading-bot', 'REPOSITORY_MISMATCH')
    require(env.get('RUNNER_ENVIRONMENT') == 'github-hosted' and env.get('RUNNER_OS') == 'Linux'
            and env.get('RUNNER_ARCH') == 'X64' and system == 'Linux' and machine == 'x86_64',
            'ISOLATED_GITHUB_LINUX_AMD64_REQUIRED')
    require(env.get('GITHUB_EVENT_NAME') in {'push', 'workflow_dispatch'} and
            env.get('GITHUB_REF') == 'refs/heads/ci/rc3-isolated-image-build', 'CONTROLLER_EVENT_DENIED')
    require(not env.get('DOCKER_HOST') and not env.get('DOCKER_CONTEXT'), 'REMOTE_DOCKER_FORBIDDEN')
    require(re.fullmatch('[0-9a-f]{40}', env.get('GITHUB_SHA', '')), 'CONTROLLER_SHA_REQUIRED')


def resource_gate(cpus, ram, disks):
    require(cpus >= 4, 'AT_LEAST_4_CPUS_REQUIRED')
    require(ram >= 8 * 1024**3, 'AT_LEAST_8_GIB_AVAILABLE_RAM_REQUIRED')
    require(disks and all(v >= 20 * 1024**3 for v in disks.values()), 'AT_LEAST_20_GIB_FREE_DISK_REQUIRED')


def preflight(evidence):
    guard(os.environ, platform.system(), platform.machine())
    endpoint = json.loads(run(['docker', 'context', 'inspect']).stdout)[0]['Endpoints']['docker']['Host']
    require(endpoint == 'unix:///var/run/docker.sock', 'LOCAL_DISPOSABLE_DOCKER_SOCKET_REQUIRED')
    info = json.loads(run(['docker', 'info', '--format', '{{json .}}']).stdout)
    require(info['OSType'] == 'linux' and info['Architecture'] in {'x86_64', 'amd64'}, 'DAEMON_PLATFORM_MISMATCH')
    # Fixed unused SDK paths on this verified disposable VM only. No Docker prune.
    for path in ('/usr/local/lib/android', '/usr/share/dotnet', '/opt/ghc', '/opt/hostedtoolcache/CodeQL'):
        run(['sudo', 'rm', '-rf', '--', path])
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    ram = int(memory['MemAvailable'].split()[0]) * 1024
    disks = {p: shutil.disk_usage(p).free for p in
             (os.environ['GITHUB_WORKSPACE'], os.environ['RUNNER_TEMP'], '/var/lib/docker')}
    cpus = len(os.sched_getaffinity(0))
    write(evidence / 'resources.json', {'cpus': cpus, 'available_ram_bytes': ram,
        'available_disk_bytes': disks, 'platform': 'linux/amd64', 'docker_endpoint': endpoint,
        'docker_version': json.loads(run(['docker', 'version', '--format', '{{json .}}']).stdout)})
    resource_gate(cpus, ram, disks)


def fetch(url, target, expected):
    with urlopen(url, timeout=120) as response, Path(target).open('wb') as stream:
        shutil.copyfileobj(response, stream)
    require(sha(target) == expected, 'DOWNLOAD_SHA256_MISMATCH: ' + Path(target).name)


def extract(archive, output, *, unused_symlinks=None):
    unused_symlinks = unused_symlinks or {}
    with tarfile.open(archive) as tar:
        regular_members = []
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and '..' not in name.parts and '\\' not in member.name,
                    'UPSTREAM_ARCHIVE_PATH_INVALID')
            if member.issym() and unused_symlinks.get(member.name) == member.linkname:
                # The pinned source's .dockerignore -> .gitignore link is not
                # needed by Go. Validate its exact identity and never create it.
                continue
            require(member.isfile() or member.isdir(), 'UPSTREAM_ARCHIVE_LINK_OR_DEVICE_FORBIDDEN')
            regular_members.append(member)
        tar.extractall(output, members=regular_members)


def compile_helper(scratch, evidence):
    fetch(INPUTS['go_archive_url'], scratch / 'go.tar.gz', INPUTS['go_archive_sha256'])
    fetch(INPUTS['gosu_archive_url'], scratch / 'gosu.tar.gz', INPUTS['gosu_archive_sha256'])
    extract(scratch / 'go.tar.gz', scratch)
    original = scratch / 'upstream'
    original.mkdir()
    extract(scratch / 'gosu.tar.gz', original, unused_symlinks={
        'gosu-' + INPUTS['gosu_commit'] + '/.dockerignore': '.gitignore'})
    original = original / ('gosu-' + INPUTS['gosu_commit'])
    require(sha(original / 'go.mod') == INPUTS['go_mod_sha256'] and
            sha(original / 'go.sum') == INPUTS['go_sum_sha256'], 'UPSTREAM_DEPENDENCY_LOCK_MISMATCH')
    home, out = scratch / 'home', scratch / 'bin'
    home.mkdir(); out.mkdir()
    env = {'PATH': str(scratch / 'go/bin') + ':' + os.defpath, 'HOME': str(home),
        'GOCACHE': str(scratch / 'go-cache'), 'GOMODCACHE': str(scratch / 'go-modules'),
        'GOBIN': str(out), 'GOTOOLCHAIN': 'local', 'GOWORK': 'off', 'GOPROXY': 'https://proxy.golang.org',
        'GOSUMDB': 'sum.golang.org', 'GOFLAGS': '-mod=readonly', 'CGO_ENABLED': '0',
        'GOOS': 'linux', 'GOARCH': 'amd64', 'GOTELEMETRY': 'off'}
    go = str(scratch / 'go/bin/go')
    version = run([go, 'version'], env=env).stdout.strip()
    require(version == 'go version go' + INPUTS['go_version'] + ' linux/amd64', 'GO_TOOLCHAIN_MISMATCH')
    module = 'github.com/tianon/gosu@' + INPUTS['gosu_module_version']
    downloaded = json.loads(run([go, 'mod', 'download', '-json', module], env=env, cwd=home).stdout)
    require(downloaded['Version'] == INPUTS['gosu_module_version'] and
            downloaded.get('Origin', {}).get('Hash') == INPUTS['gosu_commit'], 'GO_MODULE_SOURCE_COMMIT_MISMATCH')
    cached = Path(downloaded['Dir'])
    compared = {}
    for source in original.rglob('*'):
        if source.is_file() and (source.suffix == '.go' or source.name in {'go.mod', 'go.sum'}):
            relative = source.relative_to(original)
            require((cached / relative).is_file() and sha(source) == sha(cached / relative), 'GO_MODULE_SOURCE_BYTES_MISMATCH')
            compared[str(relative)] = sha(source)
    run([go, 'install', '-trimpath', '-ldflags=-w', '-buildvcs=false', module], env=env, cwd=home,
        output=evidence / 'gosu-compile.log')
    metadata = run([go, 'version', '-m', str(out / 'gosu')], env=env).stdout
    require('go' + INPUTS['go_version'] in metadata and INPUTS['gosu_module_version'] in metadata
            and 'github.com/moby/sys/user\tv0.1.0' in metadata and 'golang.org/x/sys\tv0.1.0' in metadata,
            'BINARY_BUILD_METADATA_MISMATCH')
    (evidence / 'gosu-build-metadata.txt').write_text(metadata)
    write(evidence / 'verified-inputs.json', {'inputs': INPUTS, 'compiler': version,
        'upstream_source_files': compared, 'module_sumdb_record': {k: downloaded.get(k) for k in
            ('Version', 'Sum', 'GoModSum', 'Origin')}, 'gosu_binary_sha256': sha(out / 'gosu')})
    return out / 'gosu'


def image_provenance(base, image, metadata, helper_sha, controller):
    require(base['Id'] == INPUTS['base_config_id'], 'BASE_CONFIG_ID_MISMATCH')
    require(image['Os'] == 'linux' and image['Architecture'] == 'amd64', 'IMAGE_PLATFORM_MISMATCH')
    require(image['RootFS']['Layers'][:-1] == base['RootFS']['Layers'], 'UPSTREAM_LAYERS_CHANGED')
    require(metadata.get('containerimage.config.digest') == image['Id'], 'BUILDKIT_CONFIG_ID_MISMATCH')
    labels = image['Config'].get('Labels', {})
    require(labels.get('nexus.gosu.binary.sha256') == helper_sha and
            labels.get('org.opencontainers.image.revision') == controller and
            labels.get('nexus.gosu.source.commit') == INPUTS['gosu_commit'], 'PROVENANCE_LABEL_MISMATCH')
    for key in ('Env', 'Entrypoint', 'Cmd', 'User', 'WorkingDir', 'Volumes', 'ExposedPorts', 'StopSignal', 'Healthcheck'):
        require(base['Config'].get(key) == image['Config'].get(key), 'UPSTREAM_RUNTIME_CONFIG_CHANGED: ' + key)


def smoke_test(image, evidence, run_id):
    # Every object is created fresh on the verified ephemeral VM, with no host
    # bind mounts, external networks, public ports or production credentials.
    name = 'nexus-rc3-pg-test-' + run_id
    volume = name + '-data'
    password = secrets.token_hex(32)
    created_volume = created_container = False
    tests = {}
    try:
        run(['docker', 'volume', 'create', '--label', 'nexus.scope=disposable-ci-test', volume])
        created_volume = True
        run(['docker', 'create', '--name', name, '--network', 'none', '--memory', '512m', '--cpus', '1',
             '--pids-limit', '128', '--security-opt', 'no-new-privileges:true', '--log-driver', 'local',
             '--log-opt', 'max-size=1m', '--log-opt', 'max-file=2',
             '-e', 'POSTGRES_PASSWORD=' + password, '-e', 'POSTGRES_DB=nexus_staging_metadata',
             '-e', 'POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256',
             '--mount', 'type=volume,src=' + volume + ',dst=/var/lib/postgresql/data', image])
        created_container = True
        def execute(args, good=True):
            return run(['docker', 'exec', name] + args, success=good)
        def ready():
            for _ in range(90):
                if execute(['pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'nexus_staging_metadata'], good=False).returncode == 0:
                    return
                time.sleep(.5)
            raise ValueError('DISPOSABLE_POSTGRES_NOT_READY')
        def sql(query, supplied_password=password, good=True):
            return execute(['env', 'PGPASSWORD=' + supplied_password, 'psql', '-h', '127.0.0.1',
                '-U', 'postgres', '-d', 'nexus_staging_metadata', '-v', 'ON_ERROR_STOP=1', '-Atc', query], good=good)
        run(['docker', 'start', name]); ready()
        tests['version'] = execute(['postgres', '--version']).stdout.strip()
        require(tests['version'].endswith(INPUTS['postgres_version']), 'POSTGRES_VERSION_CHANGED')
        require('gosu version ' + INPUTS['gosu_version'] in execute(['gosu', '--version']).stdout,
                'GOSU_VERSION_CHANGED')
        require(execute(['gosu', 'nobody', 'id', '-u']).stdout.strip() == '65534', 'GOSU_NOBODY_TRANSITION_FAILED')
        require(execute(['gosu', 'postgres', 'id', '-u']).stdout.strip() == '70', 'GOSU_POSTGRES_TRANSITION_FAILED')
        uid_line = next(s for s in execute(['cat', '/proc/1/status']).stdout.splitlines() if s.startswith('Uid:'))
        require(uid_line.split()[1:] == ['70'] * 4, 'POSTGRES_MUST_RUN_AS_POSTGRES_UID')
        require(sql('SELECT current_user').stdout.strip() == 'postgres', 'AUTHENTICATED_TCP_SQL_FAILED')
        require(sql('SELECT 1', supplied_password='intentionally-wrong-fixture', good=False).returncode != 0,
                'INCORRECT_PASSWORD_ACCEPTED')
        sql("CREATE TABLE rc3_probe (value text NOT NULL); INSERT INTO rc3_probe VALUES ('isolated-persistent-fixture');")
        run(['docker', 'restart', '--time', '15', name]); ready()
        require(sql('SELECT value FROM rc3_probe').stdout.strip() == 'isolated-persistent-fixture', 'RESTART_PERSISTENCE_FAILED')
        helper_hash = execute(['sha256sum', '/usr/local/bin/gosu']).stdout.split()[0]
        tests.update({'status': 'PASS', 'gosu_version': INPUTS['gosu_version'], 'gosu_binary_sha256': helper_hash,
            'privilege_drop': 'PASS; nobody=65534, postgres=70, database process uid=70',
            'tcp_authentication': 'PASS; correct password accepted, incorrect password rejected',
            'fresh_volume_init': 'PASS', 'restart_persistence': 'PASS', 'network': 'none', 'published_ports': [],
            'volume_scope': 'NEW_DISPOSABLE_CI_ONLY; never production', 'production_state_accessed': False})
        write(evidence / 'disposable-postgres-tests.json', tests)
        return helper_hash
    finally:
        if created_container:
            run(['docker', 'rm', '-f', name], success=False)
        if created_volume:
            run(['docker', 'volume', 'rm', volume], success=False)


def scan_gate(report, code, expected_id):
    require(code == 0 and report.get('SchemaVersion') == 2 and report.get('ArtifactType') == 'container_image',
            'SCANNER_FAILED_OR_SCHEMA_INVALID')
    require(report.get('Trivy', {}).get('Version') == INPUTS['trivy_version'], 'SCANNER_VERSION_MISMATCH')
    require(report.get('Metadata', {}).get('ImageID') == expected_id, 'SCANNED_IMAGE_ID_MISMATCH')
    require(report.get('Metadata', {}).get('OS', {}).get('Family') == 'alpine', 'ALPINE_SCAN_REQUIRED')
    results = report.get('Results')
    require(isinstance(results, list) and any(r.get('Class') == 'os-pkgs' for r in results), 'OS_RESULTS_REQUIRED')
    gosu = [r for r in results if r.get('Type') == 'gobinary' and r.get('Target', '').lstrip('/') == 'usr/local/bin/gosu']
    require(len(gosu) == 1, 'REBUILT_HELPER_SCAN_REQUIRED')
    packages = gosu[0].get('Packages', [])
    require(any(p.get('Name') == 'stdlib' and p.get('Version') == 'v' + INPUTS['go_version'] for p in packages),
            'PATCHED_GO_STDLIB_INVENTORY_REQUIRED')
    counts, secrets_count = Counter(), 0
    for result in results:
        require(isinstance(result.get('Vulnerabilities', []), list) and isinstance(result.get('Secrets', []), list),
                'SCANNER_FINDINGS_MALFORMED')
        secrets_count += len(result.get('Secrets', []))
        for v in result.get('Vulnerabilities', []):
            require(v.get('Severity') in SEVERITIES, 'UNKNOWN_SCANNER_SEVERITY')
            counts[v['Severity']] += 1
    return {'passed': not counts['HIGH'] and not counts['CRITICAL'] and not secrets_count,
        'vulnerability_counts': dict(counts), 'high_critical': counts['HIGH'] + counts['CRITICAL'],
        'secrets_all_severities': secrets_count, 'image_config_id': expected_id,
        'scanner': 'Trivy ' + INPUTS['trivy_version']}


def sanitize(report):
    for result in report.get('Results', []):
        if 'Secrets' in result:
            result['Secrets'] = [{k: item.get(k) for k in ('RuleID', 'Severity', 'StartLine', 'EndLine')}
                                 for item in result['Secrets']]
    return report


def scan_image(tag, image_id, scratch, evidence):
    version = INPUTS['trivy_version']
    fetch('https://github.com/aquasecurity/trivy/releases/download/v' + version + '/trivy_' + version + '_Linux-64bit.tar.gz',
          scratch / 'trivy.tar.gz', INPUTS['trivy_archive_sha256'])
    with tarfile.open(scratch / 'trivy.tar.gz') as tar:
        member = tar.getmember('trivy')
        require(member.isfile(), 'TRIVY_BINARY_REQUIRED')
        (scratch / 'trivy').write_bytes(tar.extractfile(member).read())
    (scratch / 'trivy').chmod(0o755)
    (scratch / 'trivy.yaml').write_text('{}\n')
    (scratch / 'empty.ignore').write_text('')
    (scratch / 'secret.yaml').write_text('{}\n')
    raw = scratch / 'scan-raw.json'
    args = [str(scratch / 'trivy'), 'image', '--config', str(scratch / 'trivy.yaml'),
        '--cache-dir', str(scratch / 'cache'), '--image-src', 'docker', '--platform', 'linux/amd64',
        '--ignorefile', str(scratch / 'empty.ignore'), '--secret-config', str(scratch / 'secret.yaml'),
        '--scanners', 'vuln,secret', '--image-config-scanners', 'secret', '--pkg-types', 'os,library',
        '--severity', 'UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL', '--list-all-pkgs', '--exit-code', '0',
        '--format', 'json', '--output', str(raw), '--timeout', '15m', '--no-progress',
        '--disable-telemetry', '--skip-version-check', tag]
    write(evidence / 'scan-command.json', args)
    code = run(args, cwd=scratch, timeout=1000, success=False).returncode
    require(raw.is_file(), 'SCAN_REPORT_MISSING')
    try:
        report = json.loads(raw.read_text())
        write(evidence / 'postgres-scan.json', sanitize(report))
        result = scan_gate(report, code, image_id)
        result.update({'raw_report_sha256': sha(raw), 'no_custom_exclusions': True,
            'secret_scope': 'Default Trivy detectors and built-in path exclusions; includes image configuration'})
        write(evidence / 'scan-result.json', result)
        shutil.copyfile(scratch / 'cache/db/metadata.json', evidence / 'trivy-db-metadata.json')
        require(result['passed'], 'HIGH_CRITICAL_OR_SECRET_SECURITY_GATE_FAILED')
        return result
    finally:
        raw.unlink(missing_ok=True)


def archive_gate(archive, image_id, helper_sha):
    with tarfile.open(archive) as tar:
        manifests = json.load(tar.extractfile('manifest.json'))
        require(len(manifests) == 1, 'EXACTLY_ONE_ARCHIVED_IMAGE_REQUIRED')
        manifest = manifests[0]
        config_raw = tar.extractfile(manifest['Config']).read()
        require('sha256:' + hashlib.sha256(config_raw).hexdigest() == image_id, 'ARCHIVE_CONFIG_ID_MISMATCH')
        config = json.loads(config_raw)
        require(len(manifest['Layers']) == len(config['rootfs']['diff_ids']), 'ARCHIVE_LAYER_COUNT_MISMATCH')
        for layer, digest in zip(manifest['Layers'], config['rootfs']['diff_ids']):
            stream = tar.extractfile(layer)
            h = hashlib.sha256()
            for block in iter(lambda: stream.read(1024**2), b''):
                h.update(block)
            require('sha256:' + h.hexdigest() == digest, 'ARCHIVE_LAYER_HASH_MISMATCH')
        with tarfile.open(fileobj=tar.extractfile(manifest['Layers'][-1])) as last:
            regular = [m for m in last.getmembers() if m.isfile()]
            require(len(regular) == 1 and regular[0].name.lstrip('./') == 'usr/local/bin/gosu',
                    'ONLY_GOSU_MAY_BE_REPLACED')
            require(hashlib.sha256(last.extractfile(regular[0]).read()).hexdigest() == helper_sha,
                    'ARCHIVED_HELPER_HASH_MISMATCH')
    return {'archive_sha256': sha(archive), 'archive_bytes': archive.stat().st_size,
            'config_id': image_id, 'verified_layers': len(manifest['Layers']),
            'replacement_regular_files': ['usr/local/bin/gosu']}


def main():
    temp = Path(os.environ['RUNNER_TEMP'])
    evidence, scratch, archives = (temp / n for n in ('rc3-postgres-evidence', 'rc3-postgres-scratch', 'rc3-postgres-archive'))
    for directory in (evidence, scratch, archives):
        directory.mkdir(parents=True, exist_ok=False)
    write(evidence / 'request.json', {'scope': INPUTS['scope'], 'controller_commit': os.environ['GITHUB_SHA'],
        'human_authorization': 'User approved correcting and scanning the staging PostgreSQL support image',
        'publication_authorized': False, 'deployment_authorized': False, 'vps_access': False,
        'accepted_rc3_application_images_modified': False})
    preflight(evidence)
    helper = compile_helper(scratch, evidence)
    context = scratch / 'context'; context.mkdir()
    shutil.copyfile(helper, context / 'gosu')
    shutil.copyfile(ROOT / 'Dockerfile', context / 'Dockerfile')
    run(['docker', 'pull', '--platform', 'linux/amd64', INPUTS['base']], output=evidence / 'base-pull.log')
    base = json.loads(run(['docker', 'image', 'inspect', INPUTS['base']]).stdout)[0]
    write(evidence / 'base-image.json', base)
    run_id = os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
    tag = 'nexus-rc3-postgres:pg16.15-gosu1.19-go1.27.2-' + run_id
    command = ['docker', 'buildx', 'build', '--builder', 'default', '--platform', 'linux/amd64',
        '--pull', '--no-cache', '--network', 'none', '--load', '--provenance=false',
        '--build-arg', 'CONTROLLER_COMMIT=' + os.environ['GITHUB_SHA'],
        '--build-arg', 'GOSU_SHA256=' + sha(helper), '--tag', tag,
        '--metadata-file', str(scratch / 'buildkit.json'), str(context)]
    write(evidence / 'build-command.json', command)
    run(command, output=evidence / 'build.log')
    image = json.loads(run(['docker', 'image', 'inspect', tag]).stdout)[0]
    metadata = json.loads((scratch / 'buildkit.json').read_text())
    write(evidence / 'image.json', image); write(evidence / 'buildkit.json', metadata)
    image_provenance(base, image, metadata, sha(helper), os.environ['GITHUB_SHA'])
    require(smoke_test(tag, evidence, run_id) == sha(helper), 'RUNNING_HELPER_HASH_MISMATCH')
    scan_result = scan_image(tag, image['Id'], scratch, evidence)
    archive = archives / 'postgres.docker.tar'
    run(['docker', 'image', 'save', '--output', str(archive), tag])
    archive_info = archive_gate(archive, image['Id'], sha(helper))
    write(evidence / 'result.json', {'status': 'BUILD_TEST_SCAN_PASS', 'local_tag': tag,
        'config_id': image['Id'], 'buildkit_reported_local_digest': metadata.get('containerimage.digest'),
        'repo_digests': image.get('RepoDigests', []), 'registry_manifest_digest': None,
        'archive': archive_info, 'scan': scan_result, 'gosu_binary_sha256': sha(helper),
        'controller_commit': os.environ['GITHUB_SHA'], 'run_id': os.environ['GITHUB_RUN_ID'],
        'platform': 'linux/amd64', 'publication_authorized': False, 'published': False,
        'vps_access': False, 'deployment_authorized': False, 'production_state_accessed': False,
        'accepted_rc3_application_images_modified': False})
    print('PASS: corrected PostgreSQL support image built, tested, scanned and archived; no publication or deployment')


if __name__ == '__main__':
    main()
