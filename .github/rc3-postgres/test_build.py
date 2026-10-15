import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('support_build', Path(__file__).with_name('build.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SupportBuildTests(unittest.TestCase):
    def environment(self):
        return {'GITHUB_REPOSITORY': 'Arjuunb/nexus-trading-bot', 'RUNNER_ENVIRONMENT': 'github-hosted',
            'RUNNER_OS': 'Linux', 'RUNNER_ARCH': 'X64', 'GITHUB_EVENT_NAME': 'push',
            'GITHUB_REF': 'refs/heads/ci/rc3-isolated-image-build', 'GITHUB_SHA': 'a' * 40}

    def report(self):
        return {'SchemaVersion': 2, 'ArtifactType': 'container_image', 'Trivy': {'Version': m.INPUTS['trivy_version']},
            'Metadata': {'ImageID': 'sha256:fixture', 'OS': {'Family': 'alpine'}},
            'Results': [{'Class': 'os-pkgs', 'Type': 'alpine'},
                {'Type': 'gobinary', 'Target': 'usr/local/bin/gosu', 'Packages': [
                    {'Name': 'stdlib', 'Version': 'v' + m.INPUTS['go_version']}]}]}

    def test_only_hosted_linux_can_build(self):
        m.guard(self.environment(), 'Linux', 'x86_64')
        for key, value in [('RUNNER_ENVIRONMENT', 'self-hosted'), ('GITHUB_REPOSITORY', 'other/repo'),
                           ('GITHUB_REF', 'refs/heads/main'), ('GITHUB_EVENT_NAME', 'pull_request')]:
            env = self.environment(); env[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.guard(env, 'Linux', 'x86_64')

    def test_remote_docker_is_rejected(self):
        env = self.environment(); env['DOCKER_HOST'] = 'ssh://ubuntu@57.129.155.230'
        with self.assertRaisesRegex(ValueError, 'REMOTE_DOCKER_FORBIDDEN'):
            m.guard(env, 'Linux', 'x86_64')

    def test_mac_is_rejected(self):
        with self.assertRaises(ValueError):
            m.guard(self.environment(), 'Darwin', 'arm64')

    def test_capacity_floors(self):
        gib = 1024**3
        m.resource_gate(4, 8*gib, {'disk': 20*gib})
        for cpus, ram, disks in [(3, 8*gib, {'disk': 20*gib}), (4, 6*gib, {'disk': 20*gib}),
                                 (4, 8*gib, {'disk': 19*gib}), (4, 8*gib, {})]:
            with self.subTest(cpus=cpus, ram=ram, disks=disks), self.assertRaises(ValueError):
                m.resource_gate(cpus, ram, disks)

    def test_scan_success_has_no_publication_effect(self):
        self.assertTrue(m.scan_gate(self.report(), 0, 'sha256:fixture')['passed'])
        self.assertFalse(m.INPUTS['publication_authorized'])
        self.assertFalse(m.INPUTS['deployment_authorized'])

    def test_unfixed_high_is_blocked(self):
        r = self.report(); r['Results'][0]['Vulnerabilities'] = [{'Severity': 'HIGH', 'FixedVersion': ''}]
        self.assertFalse(m.scan_gate(r, 0, 'sha256:fixture')['passed'])

    def test_critical_is_blocked(self):
        r = self.report(); r['Results'][0]['Vulnerabilities'] = [{'Severity': 'CRITICAL'}]
        self.assertFalse(m.scan_gate(r, 0, 'sha256:fixture')['passed'])

    def test_every_secret_severity_is_blocked(self):
        for severity in m.SEVERITIES:
            r = self.report(); r['Results'][0]['Secrets'] = [{'Severity': severity}]
            with self.subTest(severity=severity):
                self.assertFalse(m.scan_gate(r, 0, 'sha256:fixture')['passed'])

    def test_lower_vulnerabilities_are_retained_and_allowed(self):
        r = self.report(); r['Results'][0]['Vulnerabilities'] = [{'Severity': 'MEDIUM'}, {'Severity': 'LOW'}]
        result = m.scan_gate(r, 0, 'sha256:fixture')
        self.assertTrue(result['passed']); self.assertEqual(result['vulnerability_counts'], {'MEDIUM': 1, 'LOW': 1})

    def test_scanner_error_fails_closed(self):
        with self.assertRaises(ValueError):
            m.scan_gate(self.report(), 1, 'sha256:fixture')

    def test_wrong_image_is_rejected(self):
        with self.assertRaises(ValueError):
            m.scan_gate(self.report(), 0, 'sha256:different')

    def test_missing_helper_inventory_is_rejected(self):
        r = self.report(); r['Results'].pop()
        with self.assertRaises(ValueError):
            m.scan_gate(r, 0, 'sha256:fixture')

    def test_old_compiler_inventory_is_rejected(self):
        r = self.report(); r['Results'][1]['Packages'][0]['Version'] = 'v1.24.6'
        with self.assertRaises(ValueError):
            m.scan_gate(r, 0, 'sha256:fixture')

    def test_raw_secret_matches_are_not_retained(self):
        r = self.report(); r['Results'][0]['Secrets'] = [{'Severity': 'LOW', 'Match': 'sensitive fixture',
            'Code': {'Lines': 'surrounding fixture'}, 'RuleID': 'fixture'}]
        sanitized = json.dumps(m.sanitize(r))
        self.assertNotIn('sensitive fixture', sanitized); self.assertNotIn('surrounding fixture', sanitized)

    def test_archive_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'bad.tar'
            with tarfile.open(path, 'w') as t:
                entry = tarfile.TarInfo('../outside'); entry.size = 1
                t.addfile(entry, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                m.extract(path, Path(d) / 'out')

    def test_archive_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'bad.tar'
            with tarfile.open(path, 'w') as t:
                entry = tarfile.TarInfo('link'); entry.type = tarfile.SYMTYPE; entry.linkname = '/outside'
                t.addfile(entry)
            with self.assertRaises(ValueError):
                m.extract(path, Path(d) / 'out')

    def test_modified_postgres_entrypoint_is_rejected(self):
        base = {'Id': m.INPUTS['base_config_id'], 'RootFS': {'Layers': ['sha256:base']},
                'Config': {'Entrypoint': ['docker-entrypoint.sh']}}
        image = {'Id': 'sha256:fixture', 'Os': 'linux', 'Architecture': 'amd64',
                 'RootFS': {'Layers': ['sha256:base', 'sha256:replacement']},
                 'Config': {'Entrypoint': ['docker-entrypoint.sh'], 'Labels': {
                     'nexus.gosu.binary.sha256': 'helper', 'org.opencontainers.image.revision': 'controller',
                     'nexus.gosu.source.commit': m.INPUTS['gosu_commit']}}}
        metadata = {'containerimage.config.digest': image['Id']}
        m.image_provenance(base, image, metadata, 'helper', 'controller')
        image['Config']['Entrypoint'] = ['unreviewed-entrypoint']
        with self.assertRaises(ValueError):
            m.image_provenance(base, image, metadata, 'helper', 'controller')


if __name__ == '__main__':
    unittest.main(verbosity=2)
