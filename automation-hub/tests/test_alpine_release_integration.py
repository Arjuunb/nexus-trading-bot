"""Local release guards only: no registry publication, service startup or SSH."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'ops' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inputs():
    return load('prepare-api-alpine-inputs')


def wheel(filename, payload=b'reviewed public fixture wheel'):
    return {'filename': filename, 'url': 'https://files.pythonhosted.org/packages/' + filename,
            'size': len(payload), 'digests': {'sha256': hashlib.sha256(payload).hexdigest()}, 'yanked': False}


def test_musl_selection_preserves_python_abi_and_rejects_glibc(inputs):
    good = wheel('fixture-1.0-cp311-cp311-musllinux_1_2_x86_64.whl')
    glibc = wheel('fixture-1.0-cp311-cp311-manylinux_2_17_x86_64.whl')
    other = wheel('fixture-1.0-cp312-cp312-musllinux_1_2_x86_64.whl')
    assert inputs.select_wheel([glibc, other, good], {good['digests']['sha256']}) == good


@pytest.mark.parametrize('filename', [
    'fixture-1.0-cp312-cp312-musllinux_1_2_x86_64.whl',
    'fixture-1.0-cp311-cp311-manylinux_2_17_x86_64.whl',
    'fixture-1.0-cp311-cp311-musllinux_1_2_aarch64.whl',
    'fixture-1.0-cp311-cp311-musllinux_1_3_x86_64.whl',
    'fixture-1.0.tar.gz',
])
def test_unavailable_compatible_wheel_fails_closed(inputs, filename):
    with pytest.raises(ValueError, match='WHEEL'):
        inputs.select_wheel([wheel(filename)], set())


def test_abi3_selection_uses_highest_compatible_reviewed_abi(inputs):
    old = wheel('fixture-1.0-cp39-abi3-musllinux_1_2_x86_64.whl')
    current = wheel('fixture-1.0-cp311-abi3-musllinux_1_2_x86_64.whl')
    assert inputs.select_wheel([old, current], {current['digests']['sha256']}) == current


def test_hash_not_in_lock_is_not_accepted(inputs):
    with pytest.raises(ValueError, match='LOCK_HASH'):
        inputs.select_wheel([wheel('fixture-1.0-py3-none-any.whl')], {'0' * 64})


def test_lock_parser_requires_hashes_and_unique_exact_versions(inputs):
    text = 'fixture==1.0 \\\n    --hash=sha256:' + 'a' * 64 + '\n'
    assert inputs.parse_lock(text) == {'fixture': ('1.0', {'a' * 64})}
    for bad in ('fixture>=1.0\n', 'fixture==1.0\n', text + text, text + '--index-url https://evil.example\n'):
        with pytest.raises(ValueError, match='LOCK'):
            inputs.parse_lock(bad)


def test_existing_input_directory_is_preserved_without_network(inputs, tmp_path, monkeypatch):
    directory = tmp_path / 'existing'; directory.mkdir(); (directory / 'keep').write_bytes(b'preserve')
    monkeypatch.setattr(inputs, 'urlopen', lambda *a, **k: pytest.fail('network attempted'))
    with pytest.raises(ValueError, match='NEW'):
        inputs.prepare(directory)
    assert (directory / 'keep').read_bytes() == b'preserve'


def test_yanked_ambiguous_and_untrusted_wheel_origins_are_rejected(inputs):
    row = wheel('fixture-1.0-py3-none-any.whl')
    with pytest.raises(ValueError, match='WHEEL'):
        inputs.select_wheel([{**row, 'yanked': True}], {row['digests']['sha256']})
    with pytest.raises(ValueError, match='WHEEL'):
        inputs.select_wheel([row, dict(row)], {row['digests']['sha256']})
    with pytest.raises(ValueError, match='ORIGIN'):
        inputs.select_wheel([{**row, 'url': 'https://evil.example/pkg.whl'}], {row['digests']['sha256']})


def test_alpine_bindings_use_separate_stage_a_profile(tmp_path):
    module = load('prepare-staging-bindings')
    out = tmp_path / 'private'
    image = 'sha256:' + '1' * 64
    module.prepare(out, project='nexus-remote-stage-alpine-test', api_id=image,
                   worker_id=image, postgres_id=image, ingress_id=image, uid=100,
                   port=18443, compose_profile='alpine')
    args = module.checked_compose_args(str(ROOT), str(out / 'compose.env'), 'nexus-remote-stage-alpine-test')
    assert args[-1] == str(ROOT / 'ops/staging.alpine.compose.yaml')
    assert '--profile' not in args
    assert module.read_bindings(out / 'compose.env')['NEXUS_STAGING_SHADOW_ENABLED'] == '0'


@pytest.mark.parametrize('profile', ['../../production', '/etc/private', 'live', ''])
def test_only_allowlisted_staging_profiles_can_be_prepared(tmp_path, profile):
    module = load('prepare-staging-bindings'); out = tmp_path / 'private'; image = 'sha256:' + '1' * 64
    with pytest.raises(ValueError, match='PROFILE'):
        module.prepare(out, project='nexus-remote-stage-test', api_id=image, worker_id=image,
                       postgres_id=image, ingress_id=image, uid=100, port=18443, compose_profile=profile)
    assert not out.exists()


def valid_runtime(role='api'):
    return {'os_id': 'alpine', 'os_version': '3.24.2', 'python': '3.11.17' if role == 'api' else '3.12.15',
            'uid': 100 if role == 'api' else 1000, 'pq': 'binary', 'ssl_required': True,
            'ssl_hostname': True, 'native_imports_ok': True,
            'packages': {'fixture': '1.0'}, 'execution_flags_disabled': True,
            'worker_execution_support': 'BLOCKED_DEPENDENCY_PANDAS' if role == 'worker' else 'NOT_APPLICABLE'}


@pytest.mark.parametrize('field,bad', [
    ('os_id', 'debian'), ('os_version', '3.23.9'), ('python', '3.12.15'), ('uid', 0),
    ('pq', 'ctypes'), ('ssl_required', False), ('ssl_hostname', False),
    ('native_imports_ok', False), ('packages', {'fixture': '2.0'}),
    ('execution_flags_disabled', False),
])
def test_native_release_safety_failures_block_image_verification(field, bad):
    module = load('github-release'); value = valid_runtime(); value[field] = bad
    with pytest.raises(ValueError, match='ALPINE'):
        module.validate_alpine_runtime('api', value, {'fixture': '1.0'})


def test_worker_support_gap_is_preserved_and_stage_a_is_explicit():
    module = load('github-release'); value = valid_runtime('worker')
    result = module.validate_alpine_runtime('worker', value, {'fixture': '1.0'})
    assert result['release_scope'] == 'STAGE_A_ONLY'
    assert result['worker_execution_support'] == 'BLOCKED_DEPENDENCY_PANDAS'
    assert result['worker_activation_authorized'] is False


def test_config_packet_contains_every_extended_alpine_profile():
    module = load('github-release')
    assert 'ops/staging.alpine.compose.yaml' in module.CONFIG_FILES
    assert {'ops/staging.local.compose.yaml', 'ops/staging.remote.compose.yaml'} <= set(module.CONFIG_FILES)


def test_ci_uses_reviewed_alpine_recipes_and_all_severity_secret_gates():
    value = yaml.load((ROOT / '.github/workflows/staging-release.yml').read_text(), Loader=yaml.BaseLoader)
    steps = value['jobs']['build']['steps']; runs = '\n'.join(s.get('run', '') for s in steps)
    assert 'ops/api.alpine.Dockerfile' in runs and 'ops/research-worker.alpine.Dockerfile' in runs
    assert '--platform musllinux' in runs and '--require-alpine' in runs
    scans = [s['with'] for s in steps if 'trivy-action@' in s.get('uses', '')]
    for role in ('api', 'worker'):
        secrets = [s for s in scans if s.get('image-ref', '').startswith('nexus-ci-' + role + ':') and s.get('scanners') == 'secret']
        assert len(secrets) == 1
        assert secrets[0]['severity'] == 'UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL' and secrets[0]['exit-code'] == '1'


def test_stage_a_does_not_activate_research_or_shadow_workers():
    value = yaml.load((ROOT / 'ops/staging.alpine.compose.yaml').read_text(), Loader=yaml.BaseLoader)
    assert value['services']['research-worker']['profiles'] == ['research']
    assert value['services']['shadow-worker']['profiles'] == ['shadow']
    assert value['services']['api']['extends']['file'] == 'staging.remote.compose.yaml'

@pytest.mark.parametrize('reference', ['nexus-api:latest', 'ghcr.io/example/api:latest', 'ssh://server', 'sha256:short'])
def test_native_test_runner_accepts_only_local_immutable_ids(reference):
    module = load('run-api-image-regressions')
    with pytest.raises(ValueError, match='IMAGE'):
        module.validate_image(reference)


def test_native_test_tool_extraction_rejects_path_traversal(tmp_path):
    import zipfile
    module = load('run-api-image-regressions'); archive = tmp_path / 'fixture.whl'
    with zipfile.ZipFile(archive, 'w') as z: z.writestr('../escape.py', 'untrusted')
    out = tmp_path / 'tools'; out.mkdir()
    with pytest.raises(ValueError, match='TOOL'):
        module.extract_tool(archive, out)
    assert not (tmp_path / 'escape.py').exists()


def test_native_test_tool_extraction_preserves_existing_files(tmp_path):
    import zipfile
    module = load('run-api-image-regressions'); archive = tmp_path / 'fixture.whl'
    with zipfile.ZipFile(archive, 'w') as z: z.writestr('package/__init__.py', 'new')
    out = tmp_path / 'tools'; (out / 'package').mkdir(parents=True)
    (out / 'package/__init__.py').write_text('original')
    with pytest.raises(FileExistsError): module.extract_tool(archive, out)
    assert (out / 'package/__init__.py').read_text() == 'original'


def test_verified_tool_root_is_readable_by_nonroot_image_user(tmp_path):
    import zipfile
    module = load('run-api-image-regressions'); archive = tmp_path / 'fixture.whl'
    with zipfile.ZipFile(archive, 'w') as z: z.writestr('package/__init__.py', 'PUBLIC = True')
    out = tmp_path / 'tools'; out.mkdir(mode=0o700)
    module.extract_tool(archive, out)
    assert out.stat().st_mode & 0o005 == 0o005
    assert (out / 'package/__init__.py').stat().st_mode & 0o004


@pytest.mark.parametrize('name', ['api.tar', 'worker.tar.gz', 'release.tgz', 'candidate.oci'])
def test_local_image_archives_are_excluded_before_release_source_read(name):
    assert load('github-release').denied_source(name)


def test_separate_evaluation_manifests_cannot_be_recorded_as_a_single_publication(tmp_path):
    module = load('github-release'); head = 'a' * 40; source_hash = 'b' * 64
    values = {'source': {'head': head, 'source_sha256': source_hash, 'files': []},
              'proof': {'commit': head, 'source_sha256': source_hash,
                        'source_mode': 'SEPARATE_EVALUATED_SOURCE_MANIFESTS',
                        'worker_source_sha256': 'c' * 64},
              'approval': {'verification': 'CONFIGURED_REVIEWER_APPROVED_THIS_RUN'}}
    for name, value in values.items(): (tmp_path / (name + '.json')).write_text(json.dumps(value))
    out = tmp_path / 'publication.json'
    with pytest.raises(ValueError, match='COMMITTED_BUILD'):
        module.main(['record-publication', '--source-manifest', str(tmp_path / 'source.json'),
                     '--build-proof', str(tmp_path / 'proof.json'), '--approval', str(tmp_path / 'approval.json'),
                     '--api-ref', 'ghcr.io/arjuunb/nexus-trading-bot-api@sha256:' + '1' * 64,
                     '--worker-ref', 'ghcr.io/arjuunb/nexus-trading-bot-worker@sha256:' + '2' * 64,
                     '--output', str(out)])
    assert not out.exists()
