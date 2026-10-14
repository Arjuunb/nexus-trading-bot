"""Release publication boundaries; all artifacts and approvals are local fixtures."""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HEAD = 'a' * 40
SOURCE = 'b' * 64
API = 'ghcr.io/arjuunb/nexus-trading-bot-api@sha256:' + '1' * 64
WORKER = 'ghcr.io/arjuunb/nexus-trading-bot-worker@sha256:' + '2' * 64


@pytest.fixture
def release():
    path = ROOT / 'ops/github-release.py'
    assert path.is_file(), 'release guard not implemented'
    spec = importlib.util.spec_from_file_location('github_release', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def policy():
    return {'id': 17, 'name': 'ghcr-staging-release',
            'protection_rules': [{'type': 'required_reviewers', 'prevent_self_review': True,
                                  'reviewers': [{'type': 'User', 'reviewer': {'id': 22}}]}],
            'deployment_branch_policy': {'protected_branches': True, 'custom_branch_policies': False}}


def history(user=22, state='approved', environment=17):
    return [{'state': state, 'user': {'id': user},
             'environments': [{'id': environment, 'name': 'ghcr-staging-release'}]}]


def test_freeze_includes_untracked_source_without_reading_denied_dependencies(release, tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'tracked.py').write_text('VALUE = 1\n')
    (root / 'untracked.py').write_text('VALUE = 2\n')
    (root / '.venv').symlink_to(tmp_path / 'unavailable')
    (root / '.env.example').write_text('PUBLIC_EXAMPLE=1\n')
    value = release.freeze_source(root, tmp_path / 'frozen',
        ['tracked.py', 'untracked.py', '.venv', '.env.example'], head=HEAD,
        dirty={'untracked.py': '??'}, tracked={'tracked.py', '.env.example'})
    assert {x['path'] for x in value['files']} == {'tracked.py', 'untracked.py'}
    assert value['required_untracked_files'] == ['untracked.py']
    assert not (tmp_path / 'frozen/context/.env.example').exists()


def test_same_source_hash_and_changed_source_identity(release, tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'code.py').write_text('VALUE = 1\n')
    def freeze(name):
        return release.freeze_source(root, tmp_path / name, ['code.py'], head=HEAD, dirty={}, tracked={'code.py'})
    first = freeze('one'); second = freeze('two')
    assert first['source_sha256'] == second['source_sha256']
    (root / 'code.py').write_text('VALUE = 2\n')
    assert freeze('three')['source_sha256'] != first['source_sha256']


@pytest.mark.parametrize('name', ['.env', 'account.sqlite', 'private.key', '.ssh/id_ed25519',
                                 'credentials.json', 'secrets/token', 'state/orders.json'])
def test_tracked_private_or_state_paths_fail_before_file_read(release, tmp_path, monkeypatch, name):
    root = tmp_path / 'source'; root.mkdir()
    original = Path.read_bytes
    def guarded(path):
        if path == root / name:
            pytest.fail('private input was read')
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', guarded)
    with pytest.raises(ValueError, match='PRIVATE|STATE'):
        release.freeze_source(root, tmp_path / 'out', [name], head=HEAD, dirty={}, tracked={name})


def test_source_symlink_cannot_escape_checkout(release, tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    (root / 'code.py').symlink_to(tmp_path / 'outside.py')
    with pytest.raises(ValueError, match='SYMLINK'):
        release.freeze_source(root, tmp_path / 'out', ['code.py'], head=HEAD, dirty={}, tracked={'code.py'})


def test_source_mismatch_is_not_a_completed_freeze(release, tmp_path):
    root = tmp_path / 'source'; root.mkdir(); (root / 'code.py').write_text('VALUE=1\n')
    with pytest.raises(ValueError, match='SOURCE_IDENTITY'):
        release.freeze_source(root, tmp_path / 'out', ['code.py'], head=HEAD, dirty={},
                              tracked={'code.py'}, expected_source=SOURCE)
    assert not (tmp_path / 'out').exists()


def test_git_status_preserves_leading_status_and_renamed_paths(release):
    value = release.parse_status(' M Dockerfile\0?? ops/new file.py\0R  renamed.py\0old.py\0')
    assert value == {'Dockerfile': ' M', 'ops/new file.py': '??', 'renamed.py': 'R '}


def test_approval_requires_configured_review_and_actual_distinct_reviewer(release):
    value = release.validate_publication_approval(policy(), history(),
        initiator_ids={11}, commit=HEAD, source_sha=SOURCE, run_id='123', run_attempt='1')
    assert value['reviewer_id'] == 22 and value['reviewed_commit'] == HEAD
    assert value['source_sha256'] == SOURCE
    with pytest.raises(ValueError, match='APPROVAL'):
        release.validate_publication_approval(policy(), [], initiator_ids={11},
            commit=HEAD, source_sha=SOURCE, run_id='123', run_attempt='1')


@pytest.mark.parametrize('kind', ['missing_rules', 'self_review_allowed', 'self_approval',
                                 'wrong_reviewer', 'wrong_environment', 'bypassed', 'rerun'])
def test_approval_fail_closed(release, kind):
    p = policy(); h = history(); attempt = '1'
    if kind == 'missing_rules': p['protection_rules'] = []
    if kind == 'self_review_allowed': p['protection_rules'][0]['prevent_self_review'] = False
    if kind == 'self_approval': h = history(user=11)
    if kind == 'wrong_reviewer': h = history(user=999)
    if kind == 'wrong_environment': h = history(environment=99)
    if kind == 'bypassed': h = history(state='bypassed')
    if kind == 'rerun': attempt = '2'
    with pytest.raises(ValueError):
        release.validate_publication_approval(p, h, initiator_ids={11}, commit=HEAD,
            source_sha=SOURCE, run_id='123', run_attempt=attempt)


@pytest.mark.parametrize('ref', ['refs/heads/main', 'refs/pull/1/merge', 'refs/heads/release/x;echo nope'])
def test_only_reviewed_release_refs(release, ref):
    with pytest.raises(ValueError):
        release.validate_release_request(repository='Arjuunb/nexus-trading-bot', commit=HEAD,
            actual_commit=HEAD, source_sha=SOURCE, ref=ref, event='workflow_dispatch', run_attempt='1')


def test_release_request_ties_workflow_to_exact_commit(release):
    assert release.validate_release_request(repository='Arjuunb/nexus-trading-bot', commit=HEAD,
        actual_commit=HEAD, source_sha=SOURCE, ref='refs/heads/release/nexus-s14-ghcr',
        event='workflow_dispatch', run_attempt='1')['repository'] == 'arjuunb/nexus-trading-bot'
    with pytest.raises(ValueError):
        release.validate_release_request(repository='Arjuunb/nexus-trading-bot', commit=HEAD,
            actual_commit='c'*40, source_sha=SOURCE, ref='refs/heads/release/nexus-s14-ghcr',
            event='workflow_dispatch', run_attempt='1')


def test_config_id_and_mutable_tag_are_not_registry_digests(release):
    for value in ('sha256:'+'1'*64, 'ghcr.io/arjuunb/nexus-trading-bot-api:latest',
                  'ghcr.io/other/other@sha256:'+'1'*64):
        with pytest.raises(ValueError): release.registry_ref(value, 'api')
    assert release.registry_ref(API, 'api') == API


def test_build_packet_detects_changed_and_undeclared_artifacts(release, tmp_path):
    root = tmp_path / 'packet'; root.mkdir()
    (root / 'api.tar.gz').write_bytes(b'fixture API archive')
    (root / 'worker.tar.gz').write_bytes(b'fixture worker archive')
    (root / 'source-manifest.json').write_text(json.dumps({'head': HEAD, 'source_sha256': SOURCE, 'files': []}))
    release.seal_packet(root, commit=HEAD, source_sha=SOURCE)
    assert release.verify_packet(root, commit=HEAD, source_sha=SOURCE)['verified_files'] == 3
    (root / 'extra.env').write_text('FIXTURE_ONLY=1\n')
    with pytest.raises(ValueError, match='UNDECLARED'):release.verify_packet(root, commit=HEAD, source_sha=SOURCE)
    (root / 'extra.env').unlink()
    (root / 'api.tar.gz').write_bytes(b'changed')
    with pytest.raises(ValueError, match='INTEGRITY'):release.verify_packet(root, commit=HEAD, source_sha=SOURCE)


def test_publication_keeps_registry_and_config_identities_separate(release):
    source = {'head': HEAD, 'source_sha256': SOURCE, 'files': []}
    proof = {'reviewed_commit': HEAD, 'source_sha256': SOURCE, 'reviewer_id': 22, 'run_id': '123',
             'verification': 'CONFIGURED_REVIEWER_APPROVED_THIS_RUN', 'run_attempt': '1'}
    value = release.publication_manifest(source, proof, api_ref=API, worker_ref=WORKER,
        api_config_id='sha256:'+'3'*64, worker_config_id='sha256:'+'4'*64,
        api_uid=101, api_version='3.11.14', worker_version='3.12.14')
    assert value['images']['api']['reference'] == API
    assert value['images']['api']['config_id'] == 'sha256:'+'3'*64
    assert value['deployment_authorization'] is False and value['api_uid'] == 101


def test_publication_rejects_unverified_approval_reference(release):
    source = {'head': HEAD, 'source_sha256': SOURCE, 'files': []}
    proof = {'reviewed_commit': HEAD, 'source_sha256': SOURCE, 'reviewer_id': 22, 'run_id': '123'}
    with pytest.raises(ValueError, match='PUBLICATION_APPROVAL_REFERENCE_REQUIRED'):
        release.publication_manifest(source, proof, api_ref=API, worker_ref=WORKER,
            api_config_id='sha256:'+'3'*64, worker_config_id='sha256:'+'4'*64,
            api_uid=101, api_version='3.11.14', worker_version='3.12.14')


def test_configuration_packet_has_only_hash_verified_public_inputs(release, tmp_path):
    root = tmp_path / 'source'; root.mkdir()
    hashes = {}
    import hashlib
    for name in release.CONFIG_FILES:
        path = root / name; path.parent.mkdir(exist_ok=True)
        path.write_text('reviewed public fixture\n')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    value = {'commit': HEAD, 'source_sha256': SOURCE, 'configuration_hashes': hashes,
             'images': {'api': {'reference': API}, 'worker': {'reference': WORKER}},
             'deployment_authorization': False}
    out = tmp_path / 'packet'
    release.config_packet(root, out, value)
    assert release.verify_packet(out, commit=HEAD, source_sha=SOURCE)['verified_files'] == len(release.CONFIG_FILES) + 1
    assert (out / 'ops/staging.alpine.compose.yaml').read_bytes() == (root / 'ops/staging.alpine.compose.yaml').read_bytes()
    assert not any(p.suffix in {'.env', '.key', '.db'} for p in out.rglob('*'))
    (root / release.CONFIG_FILES[0]).write_text('unreviewed change\n')
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        release.config_packet(root, tmp_path / 'changed', value)


def test_staging_bindings_accept_pinned_registry_references_without_mutable_tags(tmp_path):
    path = ROOT / 'ops/prepare-staging-bindings.py'
    spec = importlib.util.spec_from_file_location('staging_registry_bindings', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    out = tmp_path / 'private'
    module.prepare(out, project='nexus-remote-stage-registry', api_id=API, worker_id=WORKER,
        postgres_id='postgres@sha256:'+'5'*64, ingress_id='nginx@sha256:'+'6'*64, uid=101, port=18443)
    controls = module.read_bindings(out / 'compose.env')
    assert controls['NEXUS_STAGING_API_IMAGE'] == API
    with pytest.raises(ValueError):
        module.prepare(tmp_path / 'bad', project='nexus-remote-stage-registry', api_id=API+':latest',
            worker_id=WORKER, postgres_id='postgres@sha256:'+'5'*64,
            ingress_id='nginx@sha256:'+'6'*64, uid=101, port=18443)


def test_publication_workflow_cannot_run_on_push_or_before_approval():
    path = ROOT / '.github/workflows/staging-release.yml'
    assert path.is_file(), 'publication workflow not implemented'
    value = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert set(value['on']) == {'workflow_dispatch'}
    publish = value['jobs']['publish']
    assert publish['environment'] == 'ghcr-staging-release'
    runs = [s.get('run', '') for s in publish['steps']]
    approval = next(i for i,s in enumerate(runs) if 'check-approval' in s)
    login = next(i for i,s in enumerate(runs) if 'docker login' in s)
    assert approval < login
    assert all('ssh ' not in text and 'compose up' not in text for text in runs)


def test_large_image_source_manifest_is_passed_as_bounded_stdin(release, monkeypatch):
    files = [{'path': 'bot/source_' + str(n) + '.py', 'sha256': 'f' * 64} for n in range(2000)]
    source = {'head': HEAD, 'source_sha256': SOURCE, 'files': files}
    expected = {r['path']: r['sha256'] for r in files}
    received = []

    def docker(command, **options):
        if command[:3] == ['docker', 'image', 'inspect']:
            return json.dumps({'Os': 'linux', 'Architecture': 'amd64', 'Id': 'sha256:'+'3'*64,
                               'Config': {'Labels': {'org.opencontainers.image.revision': HEAD,
                                                    'nexus.source.sha256': SOURCE}}})
        if 'id' in command:
            return '101'
        code = command[-1]
        assert len(code.encode()) < 65536, 'manifest exceeds a safe process argument size'
        if 'IMAGE_SOURCE_MISMATCH' in code:
            assert '-i' in command and 'sys.stdin' in code
            value = json.loads(options['input'])
            assert value == expected
            received.append(value)
            return json.dumps({'source_files': len(value)})
        if "'pq'" in code:
            return json.dumps({'python': '3.11.17' if 'api' in command else '3.12.14', 'pq': 'binary'})
        if "os.walk('/app')" in code:
            return '[]'
        return json.dumps({'frontend_bundles': True} if 'api' in command else {'code_only_boundary': True})

    monkeypatch.setattr(release, 'run', docker)
    proof = release.verify_local_images(source, 'api', 'worker')
    assert len(received) == 2
    assert proof['images']['api']['source_files'] == 2000
    assert proof['images']['worker']['source_files'] == 2000
    assert all(proof['images'][role]['generated_journals_absent'] is True for role in ('api', 'worker'))


def test_release_subprocess_streams_large_public_metadata_without_argument_overflow(release):
    import sys
    payload = json.dumps({'source': 'x' * 200000})
    response = release.run([sys.executable, '-c',
                            'import sys,json; print(len(json.load(sys.stdin)["source"]))'], input=payload)
    assert response == '200000'
