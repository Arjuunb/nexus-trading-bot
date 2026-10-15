"""Publication guards and archive integrity tests; no registry or Docker required."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('publish', Path(__file__).with_name('publish.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class PublicationGuards(unittest.TestCase):
    def test_event_host_and_attempt(self):
        env = {'GITHUB_REPOSITORY': m.REPO, 'GITHUB_EVENT_NAME': 'push',
               'GITHUB_REF': 'refs/heads/ci/rc3-isolated-image-build', 'GITHUB_RUN_ATTEMPT': '1',
               'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_OS': 'Linux', 'RUNNER_ARCH': 'X64'}
        m.guard(env)
        for key, value in [('GITHUB_REPOSITORY', 'other/repo'), ('GITHUB_EVENT_NAME', 'pull_request'),
                           ('GITHUB_REF', 'refs/heads/main'), ('GITHUB_RUN_ATTEMPT', '2'),
                           ('RUNNER_ENVIRONMENT', 'self-hosted'), ('RUNNER_OS', 'macOS'),
                           ('RUNNER_ARCH', 'ARM64'), ('DOCKER_HOST', 'ssh://example'),
                           ('DOCKER_CONTEXT', 'remote')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.guard({**env, key: value})

    def test_unauthorized_host_never_invokes_docker(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'other/repo'}), patch.object(m, 'command') as cmd:
            with self.assertRaises(ValueError):
                m.publish(Path('/unused'), Path('/unused'), Path('/unused'))
            cmd.assert_not_called()

    def clean_scan(self):
        return {'scanner': 'Trivy 0.69.3', 'scanner_exit_code': 0, 'passed': True,
                'high_critical_count': 0, 'secret_count_all_severities': 0, 'secrets': [],
                'image_config_id': 'sha256:fixture', 'vulnerabilities': []}

    def test_failed_or_different_scan_blocks(self):
        m.validate_scan(self.clean_scan(), 'sha256:fixture')
        for key, value in [('scanner', 'other'), ('scanner_exit_code', 1), ('passed', False),
                           ('high_critical_count', 1), ('secret_count_all_severities', 1),
                           ('secrets', [{}]), ('image_config_id', 'wrong')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.validate_scan({**self.clean_scan(), key: value}, 'sha256:fixture')

    def test_high_critical_cannot_hide_behind_zero_summary(self):
        for severity in ('HIGH', 'CRITICAL', 'unexpected'):
            scan = self.clean_scan()
            scan['vulnerabilities'] = [{'Severity': severity}]
            with self.subTest(severity=severity), self.assertRaises(ValueError):
                m.validate_scan(scan, 'sha256:fixture')
        scan['vulnerabilities'] = [{'Severity': 'MEDIUM'}, {'Severity': 'LOW'}]
        m.validate_scan(scan, 'sha256:fixture')

    def test_existing_different_tag_is_not_overwritten(self):
        expected = {'config_id': 'sha256:fixture'}
        m.check_existing({'schemaVersion': 2, 'config': {'digest': 'sha256:fixture'}}, expected)
        for manifest in ({'schemaVersion': 2, 'config': {'digest': 'wrong'}},
                         {'schemaVersion': 2, 'manifests': []}, {'schemaVersion': 1}):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                m.check_existing(manifest, expected)

    def test_missing_package_metadata_still_checks_registry_before_any_push(self):
        images = {'api': {'config_id': 'sha256:expected', 'local_tag': 'fixture:accepted'}}
        def api(path, **kwargs):
            if '/actions/runs/' in path:
                return {'status': 'completed', 'conclusion': 'success', 'head_sha': (
                    m.CONTROLLER if str(m.BUILD_RUN) in path else '161404df82cb7ec77e97eeaf1a2621af8d9afe87')}
            return None
        def command(args, **kwargs):
            if args[:3] == ['docker', 'context', 'inspect']:
                return json.dumps([{'Endpoints': {'docker': {'Host': 'unix:///var/run/docker.sock'}}}])
            return ''
        with tempfile.TemporaryDirectory() as directory, patch.object(m, 'guard'), \
                patch.object(m, 'verify', return_value=images), patch.object(m, 'github', side_effect=api), \
                patch.object(m, 'command', side_effect=command) as cmd, patch.object(m, 'inspect_image'), \
                patch.object(m, 'remote_manifest', return_value={'schemaVersion': 2, 'config': {'digest': 'wrong'}}) as remote:
            with self.assertRaisesRegex(ValueError, 'REFUSE_OVERWRITE'):
                m.publish(Path('/archives'), Path('/evidence'), Path(directory))
            remote.assert_called_once()
            self.assertFalse(any(call.args[0][:3] in (['docker', 'image', 'push'], ['docker', 'image', 'tag'])
                                 for call in cmd.call_args_list))

    def fixture_archive(self, root, *, config_change=None, stored_layer=b'layer payload', tag='fixture:accepted'):
        layers = ['sha256:' + hashlib.sha256(b'layer payload').hexdigest()]
        config = {'os': 'linux', 'architecture': 'amd64',
                  'config': {'Labels': {'org.opencontainers.image.revision': m.COMMIT,
                                        'nexus.source.sha256': m.SOURCE}}, 'rootfs': {'diff_ids': layers}}
        if config_change:
            config_change(config)
        payload = json.dumps(config).encode()
        config_id = 'sha256:' + hashlib.sha256(payload).hexdigest()
        rows = [{'Config': 'config.json', 'RepoTags': [tag], 'Layers': ['layer.tar']}]
        path = root / 'image.tar'
        with tarfile.open(path, 'w') as tar:
            for name, data in [('config.json', payload), ('manifest.json', json.dumps(rows).encode()),
                               ('layer.tar', stored_layer)]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with path.open('rb') as stream:
            archive_hash = m.digest(stream)
        return path, {'archive_sha256': archive_hash, 'config_id': config_id}

    def test_archive_success_and_exact_byte_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path, expected = self.fixture_archive(Path(directory))
            result = m.archive_inventory(path, expected, 'fixture:accepted')
            self.assertTrue(result['archive_verified'])
            with self.assertRaises(ValueError):
                m.archive_inventory(path, {**expected, 'archive_sha256': 'wrong'}, 'fixture:accepted')
            with self.assertRaises(ValueError):
                m.archive_inventory(path, {**expected, 'config_id': 'wrong'}, 'fixture:accepted')
            with self.assertRaises(ValueError):
                m.archive_inventory(path, expected, 'fixture:other')

    def test_tampered_layer_fails_even_with_matching_outer_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            path, expected = self.fixture_archive(Path(directory), stored_layer=b'corrupted bytes')
            with self.assertRaisesRegex(ValueError, 'LAYER_HASH_MISMATCH'):
                m.archive_inventory(path, expected, 'fixture:accepted')

    def test_wrong_source_platform_and_layer_count_fail(self):
        changes = [lambda c: c.update(architecture='arm64'), lambda c: c.update(os='windows'),
                   lambda c: c['config']['Labels'].update({'nexus.source.sha256': 'wrong'}),
                   lambda c: c['config']['Labels'].update({'org.opencontainers.image.revision': 'wrong'}),
                   lambda c: c['rootfs'].update(diff_ids=[])]
        for index, change in enumerate(changes):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                path, expected = self.fixture_archive(Path(directory), config_change=change)
                with self.assertRaises(ValueError):
                    m.archive_inventory(path, expected, 'fixture:accepted')


if __name__ == '__main__':
    unittest.main(verbosity=2)
