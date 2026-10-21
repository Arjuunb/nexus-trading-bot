"""Synthetic packet fixtures and Docker boundary tests; no customer evidence."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import zipfile

HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location('image_preload', HERE / 'vps-image-preload.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class PacketFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packet = self.root / 'fixture.zip'
        self.policy = deepcopy(P.POLICY)
        self.policy['commit'] = 'c' * 40
        files = [{'path': 'README.md', 'sha256': 'a'*64},
                 {'path': 'automation-hub/app.py', 'sha256': 'b'*64},
                 {'path': 'automation-hub/services/synthetic.py', 'sha256': 'd'*64}]
        self.policy['source_sha256'] = hashlib.sha256(encoded(files)).hexdigest()
        self.policy['source_files'] = 3
        self.data = {'source-manifest.json': encoded({'head': self.policy['commit'],
            'source_sha256': self.policy['source_sha256'], 'files': files})}
        images = {}
        for role in ('api', 'worker'):
            config = {'architecture': 'amd64', 'os': 'linux', 'config': {'Labels': {
                'org.opencontainers.image.revision': self.policy['commit'],
                'nexus.source.sha256': self.policy['source_sha256'], 'synthetic.role': role}}}
            raw_config = encoded(config)
            config_id = hashlib.sha256(raw_config).hexdigest()
            oci = encoded({'schemaVersion': 2, 'config': {'digest': 'sha256:'+config_id}, 'layers': []})
            oci_id = hashlib.sha256(oci).hexdigest()
            self.policy['images'][role]['config_id'] = 'sha256:' + config_id
            self.policy['images'][role]['oci_manifest_digest'] = 'sha256:' + oci_id
            self.policy['images'][role]['source_files'] = 3 if role == 'api' else 2
            index = encoded({'manifests': [{'digest': 'sha256:'+oci_id, 'platform': {'os': 'linux', 'architecture': 'amd64'}}]})
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode='w:gz') as archive:
                entries = {'index.json': index, 'blobs/sha256/'+oci_id: oci,
                    'blobs/sha256/'+config_id: raw_config,
                    'manifest.json': encoded([{'Config': 'blobs/sha256/'+config_id, 'Layers': []}])}
                for name, content in entries.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
            self.data[role + '.tar.gz'] = output.getvalue()
            images[role] = {'config_id': 'sha256:'+config_id,
                'source_files': self.policy['images'][role]['source_files']}
        self.data['build-proof.json'] = encoded({'commit': self.policy['commit'],
            'source_sha256': self.policy['source_sha256'], 'images': images})
        for name in P.SCAN_FILES:
            self.data[name] = encoded({'Results': [{'Vulnerabilities': [], 'Secrets': []}]})
        self.reseal()

    def reseal(self, extra=None):
        rows = [{'path': name, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                for name, content in sorted(self.data.items())]
        seal = encoded({'commit': self.policy['commit'], 'source_sha256': self.policy['source_sha256'], 'files': rows})
        with zipfile.ZipFile(self.packet, 'w') as output:
            for name, content in self.data.items():
                output.writestr(name, content)
            output.writestr('packet-integrity.json', seal)
            if extra:
                output.writestr(*extra)
        self.policy['zip_sha256'] = hashlib.sha256(self.packet.read_bytes()).hexdigest()
        self.policy['zip_bytes'] = self.packet.stat().st_size

    def tearDown(self):
        self.temp.cleanup()


class PacketTests(PacketFixture, unittest.TestCase):
    def test_complete_packet_and_oci_identity_chain_are_verified_without_docker(self):
        result = P.verify_packet(self.packet, self.policy)
        self.assertEqual(result['source']['head'], self.policy['commit'])
        self.assertEqual(set(result['exports']), {'api', 'worker'})
        self.assertEqual(result['exports']['api']['config_id'], self.policy['images']['api']['config_id'])

    def test_wrong_zip_hash_and_missing_file_are_rejected(self):
        self.policy['zip_sha256'] = '0'*64
        with self.assertRaisesRegex(P.Blocked, 'APPROVED_ZIP_IDENTITY_MISMATCH'):
            P.verify_packet(self.packet, self.policy)
        with self.assertRaisesRegex(P.Blocked, 'REGULAR_PACKET_FILE_REQUIRED'):
            P.verify_packet(self.root / 'missing.zip', self.policy)

    def test_incomplete_or_unsealed_packet_cannot_load(self):
        self.reseal(extra=('undeclared.txt', b'extra'))
        with self.assertRaisesRegex(P.Blocked, 'UNDECLARED_OR_MISSING_PACKET_INPUT'):
            P.verify_packet(self.packet, self.policy)

    def test_path_traversal_is_rejected_even_when_fixture_is_sealed(self):
        self.data['../escape'] = b'bad'
        self.reseal()
        with self.assertRaisesRegex(P.Blocked, 'UNSAFE_PACKET_PATH'):
            P.verify_packet(self.packet, self.policy)

    def test_duplicate_zip_entry_is_rejected(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            self.reseal(extra=('build-proof.json', b'duplicate'))
        with self.assertRaisesRegex(P.Blocked, 'DUPLICATE_PACKET_INPUT'):
            P.verify_packet(self.packet, self.policy)

    def test_scan_findings_block_images(self):
        for finding in ({'Secrets': [{'Severity': 'LOW'}]},
                        {'Vulnerabilities': [{'Severity': 'HIGH'}]}):
            with self.subTest(finding=finding):
                self.data['api-security.json'] = encoded({'Results': [finding]})
                self.reseal()
                with self.assertRaisesRegex(P.Blocked, 'CI_SECURITY_EVIDENCE_FAILED'):
                    P.verify_packet(self.packet, self.policy)

    def test_missing_scan_results_are_unknown_not_clean(self):
        self.data['api-security.json'] = encoded({})
        self.reseal()
        with self.assertRaisesRegex(P.Blocked, 'CI_SECURITY_EVIDENCE_INCOMPLETE'):
            P.verify_packet(self.packet, self.policy)

    def test_changed_frozen_source_inventory_is_rejected(self):
        source = json.loads(self.data['source-manifest.json'])
        source['files'][0]['sha256'] = 'f'*64
        self.data['source-manifest.json'] = encoded(source)
        self.reseal()
        with self.assertRaisesRegex(P.Blocked, 'FROZEN_SOURCE_IDENTITY_MISMATCH'):
            P.verify_packet(self.packet, self.policy)

    def test_wrong_export_manifest_digest_is_rejected(self):
        self.policy['images']['api']['oci_manifest_digest'] = 'sha256:' + '0'*64
        with self.assertRaisesRegex(P.Blocked, 'EXPORT_OCI_MANIFEST_IDENTITY_MISMATCH'):
            P.verify_packet(self.packet, self.policy)


class FakeDocker:
    def __init__(self, policy):
        self.policy = policy
        self.calls = []
        self.loads = 0
        self.changed = False
        self.fail_worker_load = False
        self.bad_probe = False
        self.api = {'Id': 'synthetic-stage', 'Name': '/' + P.STAGE + '-api-1', 'Image': 'sha256:old-stage',
            'RestartCount': 0, 'State': {'Status': 'running', 'Health': {'Status': 'healthy'}, 'OOMKilled': False, 'StartedAt': 'synthetic-start'},
            'HostConfig': {'Memory': P.API_MEMORY, 'MemorySwap': P.API_MEMORY},
            'Config': {'Env': [key+'=0' for key in P.DISABLED_CONTROLS]},
            'Mounts': [{'Source': '/synthetic-stage-state', 'Destination': '/var/lib/tradexa', 'RW': True, 'Type': 'volume', 'Name': P.STAGE+'_api-state'}]}
        self.prod = deepcopy(self.api)
        self.prod.update(Id='synthetic-prod', Name='/'+P.PROD+'-app-1', Image='sha256:old-prod')
        self.prod['Mounts'][0]['Source'] = '/synthetic-prod-state'

    def __call__(self, args, input_text=None, timeout=120):
        self.calls.append((args, input_text))
        if args[0] == 'ps':
            return 'synthetic-stage' if P.STAGE in args[-1] else 'synthetic-prod'
        if args[0] == 'inspect':
            row = deepcopy(self.api if args[1] == 'synthetic-stage' else self.prod)
            if self.changed and self.loads:
                row['RestartCount'] = 1
            return json.dumps([row])
        if args[:2] == ['image', 'load']:
            self.loads += 1
            if self.fail_worker_load and self.loads == 2:
                raise P.Blocked('DOCKER_OPERATION_FAILED:image')
            return 'Loaded image'
        if args[:2] == ['image', 'inspect']:
            role = 'api' if args[2] == self.policy['images']['api']['config_id'] else 'worker'
            return json.dumps([{'Id': args[2], 'Os': 'linux', 'Architecture': 'amd64', 'RepoDigests': [],
                'Config': {'Labels': {'org.opencontainers.image.revision': self.policy['commit'],
                    'nexus.source.sha256': self.policy['source_sha256']}}}])
        if args[0] == 'run':
            role = 'api' if self.policy['images']['api']['config_id'] in args else 'worker'
            return json.dumps({'source_files': len(json.loads(input_text)), 'mismatches': 1 if self.bad_probe else 0,
                'python': '3.11.17' if role == 'api' else '3.12.15', 'pq': 'binary', 'uid': 100,
                'boundary_ok': True})
        raise AssertionError('Unexpected Docker operation: '+repr(args))


class LoadingBoundaryTests(PacketFixture, unittest.TestCase):
    def test_loading_and_isolated_probes_never_update_services_or_mount_customer_data(self):
        fake = FakeDocker(self.policy)
        report = P.preload(self.packet, self.root, self.policy, fake)
        self.assertEqual(report['status'], 'APPROVED_IMAGES_LOADED_AND_VERIFIED_NO_SERVICES_UPDATED')
        self.assertTrue(report['production_container_identities_unchanged'])
        self.assertFalse(report['services_updated'])
        self.assertFalse(report['task6_complete'])
        self.assertEqual(fake.loads, 2)
        for args, payload in fake.calls:
            self.assertIn(args[0], ('ps', 'inspect', 'image', 'run'))
            if args[0] == 'run':
                self.assertIn('--read-only', args)
                self.assertEqual(args[args.index('--network')+1], 'none')
                self.assertEqual(args[args.index('--entrypoint')+1], 'python')
                self.assertNotIn('--volume', args)
                self.assertNotIn('--mount', args)
                self.assertIn('-i', args)
                self.assertTrue(json.loads(payload))

    def test_memory_override_and_shared_production_storage_block_before_load(self):
        for shared in (False, True):
            with self.subTest(shared=shared):
                fake = FakeDocker(self.policy)
                if shared:
                    fake.api['Mounts'][0]['Source'] = fake.prod['Mounts'][0]['Source']
                else:
                    fake.api['HostConfig']['Memory'] = 512*1024**2
                with self.assertRaises(P.Blocked):
                    P.preload(self.packet, self.root, self.policy, fake)
                self.assertEqual(fake.loads, 0)

    def test_packet_tampering_blocks_before_any_docker_operation(self):
        self.policy['zip_sha256'] = '0'*64
        fake = FakeDocker(self.policy)
        with self.assertRaises(P.Blocked):
            P.preload(self.packet, self.root, self.policy, fake)
        self.assertEqual(fake.calls, [])

    def test_low_disk_space_blocks_before_image_load(self):
        fake = FakeDocker(self.policy)
        with patch.object(P.shutil, 'disk_usage', return_value=type('D', (), {'free': P.RESERVE-1})()):
            with self.assertRaisesRegex(P.Blocked, 'IMAGE_LOAD_DISK_RESERVE'):
                P.preload(self.packet, self.root, self.policy, fake)
        self.assertEqual(fake.loads, 0)

    def test_disk_reserve_must_still_hold_after_loading(self):
        fake = FakeDocker(self.policy)
        def disk_usage(_):
            return type('D', (), {'free': P.RESERVE-1 if fake.loads == 2 else P.RESERVE+3*1024**3})()
        with patch.object(P.shutil, 'disk_usage', side_effect=disk_usage):
            with self.assertRaisesRegex(P.Blocked, 'IMAGE_LOAD_DISK_RESERVE'):
                P.preload(self.packet, self.root, self.policy, fake)

    def test_container_change_cannot_report_success(self):
        fake = FakeDocker(self.policy)
        fake.changed = True
        with self.assertRaisesRegex(P.Blocked, 'CONTAINER_IDENTITIES_CHANGED'):
            P.preload(self.packet, self.root, self.policy, fake)

    def test_failed_second_load_keeps_existing_containers(self):
        fake = FakeDocker(self.policy)
        fake.fail_worker_load = True
        with self.assertRaises(P.Blocked):
            P.preload(self.packet, self.root, self.policy, fake)
        self.assertEqual(fake.api['Image'], 'sha256:old-stage')
        self.assertEqual(fake.prod['Image'], 'sha256:old-prod')

    def test_probe_mismatch_never_becomes_acceptance(self):
        fake = FakeDocker(self.policy)
        fake.bad_probe = True
        with self.assertRaisesRegex(P.Blocked, 'ISOLATED_IMAGE_PROBE_FAILED'):
            P.preload(self.packet, self.root, self.policy, fake)

    def test_docker_uses_local_socket_and_never_exposes_inspect_stdout(self):
        response = subprocess.CompletedProcess([], 1, stdout='synthetic-private-credential-value', stderr='synthetic permission denied')
        with patch.object(P.subprocess, 'run', return_value=response) as runner:
            with self.assertRaises(P.Blocked) as failure:
                P.docker(['inspect', 'synthetic-id'])
        self.assertEqual(runner.call_args.args[0][:3], ['docker', '--host', 'unix:///var/run/docker.sock'])
        self.assertEqual(failure.exception.detail, 'synthetic permission denied')
        self.assertNotIn('synthetic-private-credential-value', str(failure.exception))

    def test_missing_docker_and_timeout_are_precise_blockers(self):
        for error, reason in ((FileNotFoundError(), 'DOCKER_CLIENT_NOT_AVAILABLE'),
                (subprocess.TimeoutExpired(['docker'], 1), 'DOCKER_OPERATION_TIMEOUT:ps')):
            with self.subTest(reason=reason), patch.object(P.subprocess, 'run', side_effect=error):
                with self.assertRaisesRegex(P.Blocked, reason):
                    P.docker(['ps'])


if __name__ == '__main__':
    unittest.main()
