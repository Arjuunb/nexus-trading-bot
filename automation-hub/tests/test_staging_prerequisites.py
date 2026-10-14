"""Local operator tools never authorize a target deployment or read secrets."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest
import yaml

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 10, 13, 16, 6, tzinfo=timezone.utc)
IMAGE = 'sha256:' + 'a' * 64


def operator(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'ops' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_initializers_have_cpu_memory_and_pid_limits_without_execution_flags():
    services = yaml.safe_load((ROOT / 'ops/staging.local.compose.yaml').read_text())['services']
    for name in ('state-init', 'metadata-init'):
        assert 0 < services[name]['cpus'] <= 1
        assert services[name]['mem_limit'] == '256m'
        assert 0 < services[name]['pids_limit'] <= 128
        assert services[name]['restart'] == 'no'
    assert services['api']['environment']['HUB_AUTO_ENGINE'] == '0'


def test_sampler_default_is_a_bounded_plan_without_command_execution(capsys, monkeypatch):
    module = operator('sample-vps-resources')
    monkeypatch.setattr(module, 'collect', lambda *a, **k: pytest.fail('plan opened an inspection'))
    assert module.main(['--samples', '3', '--interval', '2']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'NOT_RUN'
    assert result['maximum_duration_seconds'] <= 300
    assert result['remote_connection'] is False


@pytest.mark.parametrize('start,end', [
    (NOW + timedelta(seconds=1), NOW + timedelta(minutes=5)),
    (NOW - timedelta(minutes=5), NOW),
    (NOW.replace(tzinfo=None), NOW + timedelta(minutes=5)),
])
def test_sampling_rejects_closed_or_ambiguous_authorization_windows(start, end):
    with pytest.raises(ValueError, match='AUTHORIZATION_WINDOW'):
        operator('sample-vps-resources').validate_window(start, end, NOW, duration=10)


def test_sampling_will_not_overrun_the_authorized_window():
    module = operator('sample-vps-resources')
    with pytest.raises(ValueError, match='AUTHORIZATION_WINDOW'):
        module.validate_window(NOW - timedelta(seconds=1), NOW + timedelta(seconds=5), NOW, duration=10)
    assert module.validate_window(NOW - timedelta(seconds=1), NOW + timedelta(seconds=20), NOW, duration=10) is None


def test_cpu_sampling_excludes_guest_double_counting_and_reports_steal():
    result = operator('sample-vps-resources').cpu_usage(
        [100, 0, 100, 700, 50, 0, 0, 50, 20, 0],
        [120, 0, 110, 750, 60, 0, 0, 60, 30, 0],
    )
    assert result['busy_percent'] == 40
    assert result['steal_percent'] == 10
    assert result['scope'] == 'WHOLE_HOST_INTERVAL; NOT_SINGLE_CONTAINER_CPU'


def test_sampling_exhausted_budget_never_executes_another_command(monkeypatch):
    module = operator('sample-vps-resources')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('expired budget executed a command'))
    result = module.metadata(['docker', 'stats'], deadline=0)
    assert result['status'] == 'UNKNOWN'
    assert result['reason'] == 'SAMPLING_BUDGET_OR_WINDOW_EXHAUSTED'


def test_sampling_errors_do_not_echo_credentials_or_daemon_stderr(monkeypatch):
    module = operator('sample-vps-resources')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout='', stderr='CREDENTIAL_SENTINEL'))
    assert 'CREDENTIAL_SENTINEL' not in json.dumps(module.metadata(['docker', 'info']))


def prepare(module, out):
    return module.prepare(out, project='nexus-remote-stage-test', api_id=IMAGE,
                          worker_id=IMAGE, postgres_id=IMAGE, ingress_id=IMAGE, uid=1000, port=18443)


def test_staging_bindings_generate_unique_private_secrets_but_do_not_claim_auth_or_tls(tmp_path):
    module = operator('prepare-staging-bindings')
    out = tmp_path / 'new-private-settings'
    result = prepare(module, out)
    controls = module.read_bindings(out / 'compose.env')
    application = module.read_bindings(out / 'app.env')
    secrets = [controls['NEXUS_STAGING_OPERATOR_PASSWORD'], controls['NEXUS_STAGING_RUNTIME_PASSWORD'],
               application['HUB_SECRET'], application['HUB_CONTROL_KEY'], application['HUB_WEBHOOK_SECRET']]
    assert len(set(secrets)) == len(secrets) and all(len(value) >= 64 for value in secrets)
    assert (out / 'compose.env').stat().st_mode & 0o777 == 0o600
    assert (out / 'app.env').stat().st_mode & 0o777 == 0o600
    assert application['HUB_AUTH_MODE'] == 'supabase'
    assert application['HUB_EMERGENCY_ADMIN_ENABLED'] == '0'
    assert 'SUPABASE_KEY' not in application
    assert controls['NEXUS_STAGING_SHADOW_ENABLED'] == '0'
    assert result['gates']['CUSTOMER_AUTHENTICATION'] == 'BLOCKED'
    assert result['gates']['TLS_TRUST'] == 'UNKNOWN'
    assert not any(value in json.dumps(result) for value in secrets)
    with pytest.raises(ValueError, match='NEW_PRIVATE_DIRECTORY'):
        prepare(module, out)


@pytest.mark.parametrize('project,port', [('production', 18443), ('nexus-remote-stage-test', 443),
                                        ('nexus-remote-stage-test', 5432)])
def test_binding_preparation_rejects_production_namespace_and_ports(tmp_path, project, port):
    with pytest.raises(ValueError, match='PROJECT|PORT'):
        operator('prepare-staging-bindings').prepare(tmp_path / 'private', project=project,
            api_id=IMAGE, worker_id=IMAGE, postgres_id=IMAGE, ingress_id=IMAGE, uid=1000, port=port)
    assert not (tmp_path / 'private').exists()


def test_empty_or_unmarked_binding_paths_never_reach_compose(tmp_path):
    module = operator('prepare-staging-bindings')
    for path in ('', str(tmp_path), str(tmp_path / '.env')):
        with pytest.raises(ValueError, match='BINDINGS'):
            module.checked_compose_args(str(ROOT), path, 'nexus-remote-stage-test')


def test_binding_guard_reports_missing_auth_and_tls_without_secret_values(tmp_path):
    module = operator('prepare-staging-bindings')
    out = tmp_path / 'private'
    prepare(module, out)
    result = module.check(str(ROOT), str(out / 'compose.env'), 'nexus-remote-stage-test')
    assert result['status'] == 'BLOCKED'
    assert 'CUSTOMER_AUTHENTICATION' in result['blockers']
    assert 'STAGING_TLS_FILES' in result['blockers']
    assert 'DEPLOYMENT_AUTHORIZATION' in result['blockers']


def test_blocked_binding_cli_returns_failure_so_shell_chains_cannot_start_services(tmp_path, capsys):
    module = operator('prepare-staging-bindings')
    out = tmp_path / 'private'
    prepare(module, out)
    assert module.main(['check', '--release-dir', str(ROOT), '--bindings', str(out / 'compose.env'),
                        '--project', 'nexus-remote-stage-test']) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'BLOCKED'


def test_binding_guard_rejects_symlinked_parent_without_opening_alias_files(tmp_path):
    module = operator('prepare-staging-bindings')
    out = tmp_path / 'private'
    prepare(module, out)
    alias = tmp_path / 'alias'
    alias.symlink_to(out, target_is_directory=True)
    with pytest.raises(ValueError, match='BINDINGS'):
        module.checked_compose_args(str(ROOT), str(alias / 'compose.env'), 'nexus-remote-stage-test')


def row(name, value):
    payload = value.encode()
    return {'path': name, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(), 'mode': 0o644}


def test_runtime_correspondence_accepts_operator_changes_and_preserves_configuration_identity():
    module = operator('recover-staging-release')
    old = {'files': [row('automation-hub/app.py', 'same'), row('ops/staging.local.compose.yaml', 'old')]}
    new = {'files': [row('automation-hub/app.py', 'same'), row('ops/staging.local.compose.yaml', 'new'),
                     row('ops/sample-vps-resources.py', 'operator only')]}
    result = module.source_correspondence(old, new)
    assert result['runtime_source_matches'] is True
    assert result['changed_operator_files'] == ['ops/staging.local.compose.yaml']


@pytest.mark.parametrize('name', ['automation-hub/app.py', 'automation-hub/strategies/active.py',
                                'ops/api-requirements.lock', 'automation-hub/migrations/workers/0001.sql',
                                'Dockerfile'])
def test_runtime_rule_lock_migration_and_build_changes_cannot_reuse_old_images(name):
    module = operator('recover-staging-release')
    with pytest.raises(ValueError, match='SOURCE_IMAGE_MISMATCH'):
        module.source_correspondence({'files': [row(name, 'original')]}, {'files': [row(name, 'changed')]})


def docker_archive(tmp_path, *, secret=False, corrupt=False, traversal=False):
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode='w') as archive:
        payload = b'fixture data'
        info = tarfile.TarInfo('app/.env' if secret else 'app/automation-hub/app.py')
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    layer = layer.getvalue()
    digest = hashlib.sha256(layer).hexdigest()
    config = json.dumps({'rootfs': {'diff_ids': ['sha256:' + ('0' * 64 if corrupt else digest)]},
                         'config': {'Env': ['PATH=/bin']}}).encode()
    config_id = hashlib.sha256(config).hexdigest()
    items = {config_id + '.json': config, 'layer.tar': layer,
             'manifest.json': json.dumps([{'Config': config_id + '.json', 'Layers': ['layer.tar']}]).encode()}
    if traversal:
        items['../outside'] = b'forbidden'
    out = tmp_path / 'image.tar'
    with tarfile.open(out, 'w') as archive:
        for name, payload in items.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return out, 'sha256:' + config_id


def test_image_export_verifies_configuration_and_every_uncompressed_layer(tmp_path):
    path, identity = docker_archive(tmp_path)
    result = operator('recover-staging-release').verify_docker_archive(path, identity)
    assert result['config_id'] == identity and result['verified_layers'] == 1
    assert result['private_application_inputs'] == []


@pytest.mark.parametrize('option,reason', [('secret', 'PRIVATE_IMAGE_INPUT'),
                                        ('corrupt', 'LAYER_HASH_MISMATCH'), ('traversal', 'DOCKER_PATH')])
def test_corrupt_private_or_unsafe_image_exports_cannot_be_sealed(tmp_path, option, reason):
    path, identity = docker_archive(tmp_path, **{option: True})
    with pytest.raises(ValueError, match=reason):
        operator('recover-staging-release').verify_docker_archive(path, identity)
