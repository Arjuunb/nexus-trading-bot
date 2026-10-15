"""No Docker or registry mutation: publication failure and provenance guards."""
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

spec = importlib.util.spec_from_file_location('publisher', Path(__file__).with_name('publish.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class PublicationTests(unittest.TestCase):
    def test_authorized_branch_host_and_attempt(self):
        env = {'GITHUB_REPOSITORY': m.REPO, 'GITHUB_EVENT_NAME': 'push',
            'GITHUB_REF': 'refs/heads/ci/rc3-isolated-image-build', 'GITHUB_RUN_ATTEMPT': '1',
            'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_OS': 'Linux', 'RUNNER_ARCH': 'X64'}
        m.guard(env)
        for key, value in [('GITHUB_REPOSITORY', 'other/repo'), ('GITHUB_EVENT_NAME', 'pull_request'),
                           ('GITHUB_REF', 'refs/heads/main'), ('GITHUB_RUN_ATTEMPT', '2'),
                           ('RUNNER_ENVIRONMENT', 'self-hosted'), ('RUNNER_OS', 'macOS'),
                           ('RUNNER_ARCH', 'ARM64'), ('DOCKER_HOST', 'ssh://57.129.155.230'),
                           ('DOCKER_CONTEXT', 'remote')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.guard({**env, key: value})

    def test_denied_host_never_invokes_docker(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'other/repo'}), patch.object(m, 'command') as cmd:
            with self.assertRaises(ValueError):
                m.publish(Path('/unused'), Path('/unused'), Path('/unused'))
            cmd.assert_not_called()

    def scan(self):
        return {'SchemaVersion': 2, 'ArtifactType': 'container_image', 'Trivy': {'Version': '0.75.0'},
            'Metadata': {'ImageID': m.PINS['config_id'], 'OS': {'Family': 'alpine'}},
            'Results': [{'Class': 'os-pkgs', 'Vulnerabilities': []}, {'Type': 'gobinary',
                'Target': 'usr/local/bin/gosu', 'Packages': [{'Name': 'stdlib', 'Version': 'v1.27.2'}]}]}

    def test_clean_scan_is_accepted(self):
        m.validate_scan(self.scan(), m.PINS['config_id'])

    def test_high_critical_or_malformed_severity_cannot_hide(self):
        for severity in ('HIGH', 'CRITICAL', 'unexpected'):
            r = self.scan(); r['Results'][0]['Vulnerabilities'] = [{'Severity': severity}]
            with self.subTest(severity=severity), self.assertRaises(ValueError):
                m.validate_scan(r, m.PINS['config_id'])

    def test_secret_at_every_severity_blocks(self):
        for severity in ('UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'):
            r = self.scan(); r['Results'][0]['Secrets'] = [{'Severity': severity}]
            with self.subTest(severity=severity), self.assertRaises(ValueError):
                m.validate_scan(r, m.PINS['config_id'])

    def test_wrong_scan_image_fails(self):
        with self.assertRaises(ValueError):
            m.validate_scan(self.scan(), 'sha256:wrong')

    def test_missing_or_old_helper_inventory_fails(self):
        for packages in ([], [{'Name': 'stdlib', 'Version': 'v1.24.6'}]):
            r = self.scan(); r['Results'][1]['Packages'] = packages
            with self.subTest(packages=packages), self.assertRaises(ValueError):
                m.validate_scan(r, m.PINS['config_id'])

    def test_existing_different_image_is_not_overwritten(self):
        expected = {'config_id': m.PINS['config_id']}
        m.check_existing({'schemaVersion': 2, 'config': {'digest': m.PINS['config_id']}}, expected)
        for manifest in ({'schemaVersion': 2, 'config': {'digest': 'different'}},
                         {'schemaVersion': 2, 'manifests': []}):
            with self.assertRaises(ValueError):
                m.check_existing(manifest, expected)

    def test_missing_package_metadata_still_checks_tag_before_push(self):
        expected = {'config_id': m.PINS['config_id'], 'local_tag': m.PINS['local_tag']}
        def github(path, **kwargs):
            if '/actions/runs/' in path:
                return {'status': 'completed', 'conclusion': 'success', 'head_sha': m.PINS['build_controller']}
            if '/actions/artifacts/' in path:
                key = 'image' if str(m.PINS['image_artifact_id']) in path else 'evidence'
                return {'expired': False, 'workflow_run': {'id': m.PINS['build_run'],
                    'head_sha': m.PINS['build_controller']}, 'digest': m.PINS[key + '_artifact_zip_digest']}
            return None
        def command(args, **kwargs):
            return json.dumps([{'Endpoints': {'docker': {'Host': 'unix:///var/run/docker.sock'}}}]) if args[:3] == ['docker', 'context', 'inspect'] else ''
        with tempfile.TemporaryDirectory() as d, patch.object(m, 'guard'), patch.object(m, 'verify', return_value=expected), \
                patch.object(m, 'github', side_effect=github), patch.object(m, 'command', side_effect=command) as cmd, \
                patch.object(m, 'inspect_image'), patch.object(m, 'remote_manifest', return_value={'schemaVersion': 2, 'config': {'digest': 'different'}}) as remote:
            with self.assertRaisesRegex(ValueError, 'REFUSE_OVERWRITE'):
                m.publish(Path('/archive'), Path('/evidence'), Path(d))
            remote.assert_called_once()
            self.assertFalse(any(call.args[0][:3] in (['docker', 'image', 'tag'], ['docker', 'image', 'push']) for call in cmd.call_args_list))

    def labels(self):
        return {'org.opencontainers.image.revision': m.PINS['build_controller'],
            'nexus.gosu.source.commit': m.PINS['gosu_commit'], 'nexus.gosu.go.version': m.PINS['go_version'],
            'nexus.gosu.binary.sha256': m.PINS['gosu_binary_sha256']}

    def fixture(self, root, *, wrong_layer=False, extra_file=False, wrong_labels=False):
        helper = b'fixture helper bytes'
        layer = io.BytesIO()
        with tarfile.open(fileobj=layer, mode='w') as tar:
            entry = tarfile.TarInfo('usr/local/bin/gosu'); entry.size = len(helper)
            tar.addfile(entry, io.BytesIO(helper))
            if extra_file:
                entry = tarfile.TarInfo('unreviewed'); entry.size = 1
                tar.addfile(entry, io.BytesIO(b'x'))
        actual_layer = layer.getvalue()
        labels = self.labels()
        if wrong_labels: labels['nexus.gosu.go.version'] = '1.24.6'
        config = {'os': 'linux', 'architecture': 'amd64', 'config': {'Labels': labels},
            'rootfs': {'diff_ids': ['sha256:' + hashlib.sha256(actual_layer).hexdigest()]}}
        payload = json.dumps(config).encode(); image_id = 'sha256:' + hashlib.sha256(payload).hexdigest()
        manifest = [{'Config': 'config.json', 'RepoTags': [m.PINS['local_tag']], 'Layers': ['layer.tar']}]
        archive = root / 'image.tar'
        with tarfile.open(archive, 'w') as tar:
            for name, data in [('manifest.json', json.dumps(manifest).encode()), ('config.json', payload),
                               ('layer.tar', b'corrupted' if wrong_layer else actual_layer)]:
                entry = tarfile.TarInfo(name); entry.size = len(data); tar.addfile(entry, io.BytesIO(data))
        expected = {'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
            'archive_bytes': archive.stat().st_size, 'config_id': image_id, 'local_tag': m.PINS['local_tag'],
            'gosu_binary_sha256': hashlib.sha256(helper).hexdigest()}
        return archive, expected

    def test_archive_success_and_outer_hash_binding(self):
        with tempfile.TemporaryDirectory() as d:
            path, expected = self.fixture(Path(d))
            self.assertEqual(m.archive_inventory(path, expected)['config_id'], expected['config_id'])
            for key, value in [('archive_sha256', 'wrong'), ('archive_bytes', 1), ('config_id', 'wrong'), ('gosu_binary_sha256', 'wrong')]:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    m.archive_inventory(path, {**expected, key: value})

    def test_tampered_layer_fails_even_with_updated_outer_hash(self):
        with tempfile.TemporaryDirectory() as d:
            path, expected = self.fixture(Path(d), wrong_layer=True)
            with self.assertRaisesRegex(ValueError, 'LAYER_HASH_MISMATCH'):
                m.archive_inventory(path, expected)

    def test_extra_replacement_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path, expected = self.fixture(Path(d), extra_file=True)
            with self.assertRaisesRegex(ValueError, 'ONLY_REVIEWED_HELPER'):
                m.archive_inventory(path, expected)

    def test_wrong_source_labels_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path, expected = self.fixture(Path(d), wrong_labels=True)
            with self.assertRaisesRegex(ValueError, 'PROVENANCE_LABEL'):
                m.archive_inventory(path, expected)

    def test_changed_original_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / 'result.json').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'ORIGINAL_EVIDENCE_HASH_MISMATCH'):
                m.verify(root / 'image.tar', root, root)


if __name__ == '__main__':
    unittest.main(verbosity=2)
