"""Failure-path tests for the build-only controller; no container runtime needed."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('build_scan', Path(__file__).with_name('build_scan.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def report():
    return {'SchemaVersion': 2, 'ArtifactType': 'container_image', 'Trivy': {'Version': '0.69.3'},
            'ReportID': 'fixture', 'CreatedAt': '2026-10-15T00:00:00Z', 'ArtifactName': 'sha256:' + 'a' * 64,
            'Metadata': {'ImageID': 'sha256:' + 'a' * 64, 'OS': {'Family': 'alpine'}},
            'Results': [{'Class': 'os-pkgs', 'Type': 'alpine', 'Vulnerabilities': []}]}


class Gates(unittest.TestCase):
    def test_prior_validation_is_for_exact_correction(self):
        run = {'id': m.VALIDATION_RUN, 'status': 'completed', 'conclusion': 'success',
               'head_sha': m.COMMIT, 'event': 'pull_request',
               'head_branch': 'fix/rc3-customer-workspace', 'path': '.github/workflows/release-validation.yml',
               'pull_requests': [{'number': 39, 'head': {'sha': m.COMMIT}, 'base': {'sha': m.ACCEPTED_BASE}}]}
        jobs = [{'name': 'backend-and-ui (' + v + ')', 'conclusion': 'success'} for v in ('3.11', '3.12')]
        m.validate_prior_validation(run, jobs)
        for key, value in [('id', 1), ('status', 'in_progress'), ('conclusion', 'failure'),
                           ('head_sha', 'a'*40), ('event', 'push'), ('head_branch', 'main'),
                           ('path', '.github/workflows/ci.yml')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.validate_prior_validation({**run, key: value}, jobs)
        for key in ('head', 'base'):
            changed = copy.deepcopy(run)
            changed['pull_requests'][0][key]['sha'] = 'b'*40
            with self.subTest(pull_request_ref=key), self.assertRaises(ValueError):
                m.validate_prior_validation(changed, jobs)
        with self.assertRaises(ValueError):
            m.validate_prior_validation({**run, 'pull_requests': []}, jobs)
        for rows in (jobs[:1], [dict(j, conclusion='skipped') for j in jobs]):
            with self.assertRaises(ValueError):
                m.validate_prior_validation(run, rows)

    def test_all_exact_head_source_ci_runs_are_required(self):
        jobs = [{'name': 'landing-build', 'conclusion': 'success'}] + [
            {'name': 'test (' + v + ')', 'conclusion': 'success'} for v in ('3.10', '3.11', '3.12')]
        for expected_id, event in ((39061054072, 'pull_request'), (39061049627, 'push')):
            run = {'id': expected_id, 'head_sha': m.COMMIT, 'status': 'completed',
                   'conclusion': 'success', 'event': event, 'path': '.github/workflows/ci.yml',
                   'head_branch': 'fix/rc3-customer-workspace'}
            m.validate_additional_ci(run, jobs, expected_id)
            for key, value in [('id', 1), ('status', 'in_progress'), ('conclusion', 'cancelled'),
                               ('head_sha', 'c'*40), ('path', 'other.yml'), ('event', 'workflow_dispatch')]:
                with self.subTest(id=expected_id, key=key), self.assertRaises(ValueError):
                    m.validate_additional_ci({**run, key:value}, jobs, expected_id)
            with self.assertRaises(ValueError):
                m.validate_additional_ci(run, jobs[:-1], expected_id)

    def test_resource_boundaries(self):
        m.resource_gate(4, 8 * 1024**3, {'docker': 20 * 1024**3})
        for cpus, ram, disks in [(3, 8 * 1024**3, {'d': 20 * 1024**3}),
                                  (4, 8 * 1024**3 - 1, {'d': 20 * 1024**3}),
                                  (4, 8 * 1024**3, {'d': 20 * 1024**3 - 1}),
                                  (4, 8 * 1024**3, {})]:
            with self.subTest(cpus=cpus, ram=ram, disks=disks), self.assertRaises(ValueError):
                m.resource_gate(cpus, ram, disks)

    def test_host_and_event_boundaries(self):
        env = {'RUNNER_ENVIRONMENT': 'github-hosted', 'RUNNER_OS': 'Linux', 'RUNNER_ARCH': 'X64',
               'GITHUB_REPOSITORY': m.REPO, 'GITHUB_EVENT_NAME': 'push',
               'GITHUB_REF': 'refs/heads/ci/rc3-workspace-build'}
        m.hosted_guard(env, 'Linux', 'x86_64')
        for key, value in [('RUNNER_ENVIRONMENT', 'self-hosted'), ('RUNNER_ARCH', 'ARM64'),
                           ('GITHUB_REPOSITORY', 'other/repo'), ('GITHUB_EVENT_NAME', 'pull_request'),
                           ('GITHUB_REF', 'refs/heads/main')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.hosted_guard({**env, key: value}, 'Linux', 'x86_64')

    def test_denied_host_never_runs_cleanup(self):
        with patch.dict(os.environ, {'RUNNER_ENVIRONMENT': 'self-hosted'}), patch.object(m, 'command') as run:
            with self.assertRaises(ValueError):
                m.preflight(Path('/unused'))
            run.assert_not_called()

    def checked(self, data=None, code=0):
        return m.sanitized_scan(data or report(), code, artifact_types={'container_image'},
                                expected_image='sha256:' + 'a' * 64, vulnerabilities=True)

    def test_success_requires_os_evidence(self):
        self.assertTrue(self.checked()['passed'])
        for key in ('Results', 'Metadata'):
            data = report()
            data.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.checked(data)

    def test_vulnerability_severities(self):
        for severity in m.ALL_SEVERITIES.split(','):
            data = report()
            data['Results'][0]['Vulnerabilities'] = [{'VulnerabilityID': 'CVE-fixture', 'Severity': severity}]
            with self.subTest(severity=severity):
                self.assertEqual(self.checked(data)['passed'], severity not in {'HIGH', 'CRITICAL'})

    def test_secrets_at_every_severity_block_and_are_redacted(self):
        for severity in m.ALL_SEVERITIES.split(','):
            data = report()
            data['Results'][0]['Secrets'] = [{'Severity': severity, 'RuleID': 'fixture',
                                             'Match': 'private-fixture-value', 'Code': 'sensitive-source'}]
            value = self.checked(data)
            with self.subTest(severity=severity):
                self.assertFalse(value['passed'])
                self.assertEqual(value['secret_count_all_severities'], 1)
                self.assertNotIn('private-fixture-value', json.dumps(value))
                self.assertNotIn('sensitive-source', json.dumps(value))

    def test_failure_exit_cannot_pass_clean_report(self):
        self.assertFalse(self.checked(code=1)['passed'])

    def test_malformed_reports_cannot_pass(self):
        for key, value in [('SchemaVersion', True), ('SchemaVersion', '2'), ('Trivy', {'Version': 'other'}),
                           ('ReportID', ''), ('ArtifactType', 'filesystem'), ('Results', None)]:
            data = report()
            data[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.checked(data)
        for key in ('Secrets', 'Vulnerabilities'):
            data = report()
            data['Results'][0][key] = None
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.checked(data)

    def test_wrong_image_id_rejected(self):
        data = report()
        data['Metadata']['ImageID'] = 'sha256:' + 'b' * 64
        with self.assertRaises(ValueError):
            self.checked(data)

    def test_native_clean_source_encoding(self):
        for kind in ('repository', 'filesystem'):
            data = report()
            data['ArtifactType'] = kind
            del data['Results']
            value = m.sanitized_scan(data, 0, artifact_types={'repository', 'filesystem'})
            self.assertTrue(value['passed'])

    def test_image_identity_binds_config_exporter_digest_and_source(self):
        inspect = {'Os': 'linux', 'Architecture': 'amd64', 'Id': 'sha256:' + 'a' * 64,
                   'RepoTags': ['nexus:fixture'], 'Size': 100, 'RepoDigests': [],
                   'Config': {'Labels': {'org.opencontainers.image.revision': m.COMMIT,
                                         'nexus.source.sha256': m.SOURCE}}}
        metadata = {'containerimage.config.digest': inspect['Id'], 'containerimage.digest': 'sha256:' + 'b' * 64}
        self.assertEqual(m.image_identity(inspect, metadata)['repo_digests'], [])
        for target, key, value in [('inspect', 'Architecture', 'arm64'), ('inspect', 'Os', 'windows'),
                                   ('metadata', 'containerimage.config.digest', 'wrong'),
                                   ('metadata', 'containerimage.digest', 'wrong')]:
            i, b = copy.deepcopy(inspect), copy.deepcopy(metadata)
            (i if target == 'inspect' else b)[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.image_identity(i, b)
        for label in ('org.opencontainers.image.revision', 'nexus.source.sha256'):
            i = copy.deepcopy(inspect)
            i['Config']['Labels'][label] = 'wrong'
            with self.assertRaises(ValueError):
                m.image_identity(i, metadata)


if __name__ == '__main__':
    unittest.main(verbosity=2)
