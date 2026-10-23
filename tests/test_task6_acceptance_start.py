"""Staging boundaries and failure handling; no real server is contacted."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = load('server_start', ROOT / 'ops/start-task6-staging-acceptance.py')
M = load('mac_start', ROOT / 'ops/run-task6-staging-from-mac.py')


def api_fixture():
    return {'Id': 'old-api', 'Image': S.IMAGE, 'Name': '/' + S.API,
        'Config': {'Env': ['KEEP_SECRET=synthetic-do-not-print', 'PORT=8000'],
            'Labels': {'com.docker.compose.config-hash': 'old-hash'},
            'User': '100:100', 'Cmd': ['python', 'app.py'], 'Hostname': 'old-host'},
        'HostConfig': {'Memory': S.MEMORY, 'MemorySwap': S.MEMORY,
            'NanoCpus': 1000000000, 'ReadonlyRootfs': True},
        'Mounts': [{'Name': S.VOLUME, 'Source': '/docker/staging-data',
                    'Destination': '/app/data', 'RW': True}],
        'NetworkSettings': {'Networks': {'staging-default': {}}},
        'State': {'Running': False, 'Status': 'exited', 'ExitCode': 0}}


def config_fixture():
    return {'services': {'api': {'image': 'nexus-ci-api@' + S.IMAGE,
        'environment': {'PORT': '8000'}, 'mem_limit': S.MEMORY, 'memswap_limit': S.MEMORY,
        'cpus': 1, 'volumes': ['state:/app/data']},
        'research-worker': {'image': 'already-deployed-worker', 'cpus': 1}},
        'networks': {'default': {}}, 'volumes': {'state': {}}}


class ComposeSafety(unittest.TestCase):
    def setUp(self):
        self.before = config_fixture()
        self.after = deepcopy(self.before)
        self.after['services']['api']['environment'][S.MODE_KEY] = '1'

    def test_only_one_staging_flag_is_permitted(self):
        S.validate_render(api_fixture(), self.before, self.after)

    def test_any_other_service_change_is_refused(self):
        self.after['services']['research-worker']['cpus'] = 2
        with self.assertRaisesRegex(S.Blocked, 'EXCEEDS_SINGLE'):
            S.validate_render(api_fixture(), self.before, self.after)

    def test_resource_or_image_or_network_changes_are_refused(self):
        for key, value in [('image', 'unscanned'), ('mem_limit', 768 * 1024**2),
                           ('memswap_limit', 2 * S.MEMORY), ('cpus', 2)]:
            after = deepcopy(self.after)
            after['services']['api'][key] = value
            with self.subTest(key=key), self.assertRaises(S.Blocked):
                S.validate_render(api_fixture(), self.before, after)
        self.after['networks']['default']['name'] = 'production'
        with self.assertRaises(S.Blocked):
            S.validate_render(api_fixture(), self.before, self.after)

    def test_additional_environment_change_is_refused(self):
        self.after['services']['api']['environment']['PORT'] = '9000'
        with self.assertRaises(S.Blocked):
            S.validate_render(api_fixture(), self.before, self.after)

    def test_stale_compose_environment_is_refused(self):
        self.before['services']['api']['environment']['PORT'] = '9000'
        self.after['services']['api']['environment']['PORT'] = '9000'
        with self.assertRaisesRegex(S.Blocked, 'DIFFERS_FROM_STOPPED'):
            S.validate_render(api_fixture(), self.before, self.after)

    def test_current_limits_must_already_be_1536_mib(self):
        self.before['services']['api']['mem_limit'] = 768 * 1024**2
        self.after['services']['api']['mem_limit'] = 768 * 1024**2
        with self.assertRaisesRegex(S.Blocked, 'MEMORY_WOULD_CHANGE'):
            S.validate_render(api_fixture(), self.before, self.after)

    def test_preserved_configuration_catches_hidden_environment_and_limits(self):
        before = api_fixture()
        after = deepcopy(before)
        after['Config']['Env'].append(S.MODE_KEY + '=1')
        after['Config']['Hostname'] = 'new-host'
        after['Config']['Labels']['com.docker.compose.config-hash'] = 'new-hash'
        self.assertEqual(S.preserved(before), S.preserved(after))
        after['Config']['Env'][0] = 'KEEP_SECRET=changed'
        self.assertNotEqual(S.preserved(before), S.preserved(after))
        after = deepcopy(before)
        after['HostConfig']['NanoCpus'] *= 2
        self.assertNotEqual(S.preserved(before), S.preserved(after))

    def test_manual_cpu_limit_drift_is_refused_before_recreation(self):
        api = api_fixture()
        api['HostConfig']['NanoCpus'] = 2_000_000_000
        with self.assertRaisesRegex(S.Blocked, 'CPU_LIMIT_WOULD_CHANGE'):
            S.validate_render(api, self.before, self.after)

    def test_duplicate_environment_keys_are_refused(self):
        c = api_fixture()
        c['Config']['Env'].append('PORT=9000')
        with self.assertRaisesRegex(S.Blocked, 'DUPLICATE'):
            S.environment(c)


class RetainedLedgerGate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'original'
        self.bundle.mkdir()
        self.receipt_path = self.root / 'recovery-receipt.json'
        seals = {}
        for name in ('application-ledger.dump', 'source-schema.json', 'source-fingerprints.json',
                     'restored-schema.json', 'blocked-receipt.json'):
            p = self.bundle / name
            p.write_text('synthetic-' + name)
            seals[name] = M.sha(p)
        self.receipt = {key: True for key in ('external_application_ledger_recovery_proven',
            'visible_column_order_and_all_recorded_definitions_match', 'object_ownership_and_grants_match',
            'all_selected_table_rows_match_recorded_snapshot', 'retained_input_files_unchanged',
            'local_restore_server_stopped')}
        self.receipt.update(status=M.LEDGER_STATUS, original_bundle=str(self.bundle), input_sha256=seals,
            scope='RECORDED_APPLICATION', snapshot_scope='RECORDED_NOT_LIVE')
        (self.root / 'restore-bootstrap.json').write_text('{}')
        self.save()

    def save(self):
        self.receipt_path.write_text(json.dumps(self.receipt))

    def test_real_gate_hashes_every_retained_input_without_reauthenticating(self):
        proof = M.ledger_gate(self.receipt_path)
        self.assertTrue(proof['external_application_ledger_recovery_proven'])
        self.assertFalse(proof['full_supabase_platform_recovery_proven'])
        self.assertNotIn('original_bundle', proof)
        self.assertNotIn('input_sha256', proof)

    def test_changed_dump_is_refused(self):
        (self.bundle / 'application-ledger.dump').write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'INPUT_CHANGED'):
            M.ledger_gate(self.receipt_path)

    def test_any_failed_verification_flag_is_refused(self):
        for key in ('all_selected_table_rows_match_recorded_snapshot', 'object_ownership_and_grants_match',
                    'local_restore_server_stopped', 'retained_input_files_unchanged'):
            with self.subTest(key=key):
                self.receipt[key] = False
                self.save()
                with self.assertRaisesRegex(RuntimeError, 'DID_NOT_PASS'):
                    M.ledger_gate(self.receipt_path)
                self.receipt[key] = True

    def test_missing_bootstrap_is_refused(self):
        (self.root / 'restore-bootstrap.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'BOOTSTRAP'):
            M.ledger_gate(self.receipt_path)

    def test_extra_or_missing_seals_are_refused(self):
        self.receipt['input_sha256']['../outside'] = 'a' * 64
        self.save()
        with self.assertRaisesRegex(RuntimeError, 'INPUTS_REQUIRED'):
            M.ledger_gate(self.receipt_path)

    def test_symlink_backup_is_refused(self):
        path = self.bundle / 'application-ledger.dump'
        path.unlink()
        path.symlink_to(self.bundle / 'source-schema.json')
        with self.assertRaisesRegex(RuntimeError, 'INPUT_CHANGED'):
            M.ledger_gate(self.receipt_path)


class FinancialPreservation(unittest.TestCase):
    def test_wal_write_and_deleted_pa_store_are_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ('account.db', 'smc_strategy_paper.db'):
                (root / name).write_bytes(b'synthetic')
            before = S.file_state(root)
            (root / 'smc_strategy_paper.db-wal').write_bytes(b'committed-write')
            self.assertNotEqual(S.file_state(root), before)
            (root / 'price_action_paper.db').write_bytes(b'recreated')
            with self.assertRaisesRegex(S.Blocked, 'REAPPEARED'):
                S.file_state(root)

    def test_symlink_and_missing_financial_stores_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaisesRegex(S.Blocked, 'STORES_REQUIRED'):
                S.file_state(root)
            (root / 'account.db').write_bytes(b'a')
            (root / 'smc_strategy_paper.db').symlink_to(root / 'account.db')
            with self.assertRaisesRegex(S.Blocked, 'UNSAFE_FINANCIAL_FILE'):
                S.file_state(root)

    def test_command_failures_do_not_print_environment_or_raw_stderr(self):
        failure = subprocess.CompletedProcess(['docker'], 1, '', 'synthetic-password-in-error')
        with patch.object(S.subprocess, 'run', return_value=failure) as call:
            with self.assertRaises(S.Blocked) as error:
                S.run(['docker', 'inspect', 'staging'])
            self.assertNotIn('password', str(error.exception))
            self.assertNotIn('PGPASSWORD', call.call_args.kwargs['env'])

    def test_outside_run_directory_is_rejected_before_mutation(self):
        with patch.object(S, 'regular', side_effect=lambda x: Path(x)):
            with self.assertRaises(S.Blocked):
                S.private_directory('/tmp/nexus-task6-acceptance-wrong')


class BaselineGates(unittest.TestCase):
    def baseline(self, mutate=None):
        api = api_fixture()
        api['Config']['Env'] += ['SUPABASE_URL=https://simgvasrjiyltnwtmyzt.supabase.co']
        api['Config']['Labels'].update({'com.docker.compose.project': S.PROJECT,
            'com.docker.compose.service': 'api',
            'com.docker.compose.project.config_files': str(S.ROOT / 'release/ops/base.yaml'),
            'com.docker.compose.project.working_dir': str(S.ROOT / 'release/ops')})
        ready = {'State': {'Running': True, 'Health': {'Status': 'healthy'}},
                 'Config': {'Env': [], 'Labels': {}}, 'Mounts': [], 'Id': 'dependency'}
        prod = deepcopy(ready)
        prod['Id'] = 'production'
        prod['Name'] = '/nexus-trading-bot-app-1'
        prod['Config'] = {'Labels': {'com.docker.compose.project': 'nexus-trading-bot'},
            'Env': ['SUPABASE_URL=https://separate-production.example']}
        initializer = deepcopy(ready)
        initializer['Id'] = 'initializer'
        initializer['State']['Running'] = False
        initializer['Mounts'] = deepcopy(api['Mounts'])
        initializer['Config']['Labels'] = {'com.docker.compose.project': S.PROJECT,
            'com.docker.compose.service': 'state-init'}
        objects = {S.API: api, S.IMAGE: {'Config': {'Labels': {'org.opencontainers.image.revision': S.COMMIT}}},
            S.VOLUME: {'Mountpoint': '/docker/staging-data', 'Labels': {'com.docker.compose.project': S.PROJECT}},
            'production': prod, 'initializer': initializer,
            S.WORKER: ready, S.PROJECT + '-postgres-1': ready, S.INGRESS: ready}
        if mutate:
            mutate(objects)
        with patch.object(S, 'inspect', side_effect=lambda name: objects[name]), \
             patch.object(S, 'docker', return_value=S.API + '\nproduction\ninitializer\n'), \
             patch.object(S, 'regular', side_effect=lambda path: Path(path)):
            return S.stopped_baseline()

    def test_existing_stopped_initializer_does_not_block_verified_separate_volume(self):
        api, source, _, _ = self.baseline()
        self.assertEqual(api['Image'], S.IMAGE)
        self.assertEqual(source, Path('/docker/staging-data'))

    def test_running_api_wrong_image_or_limits_are_refused(self):
        changes = [lambda o: o[S.API]['State'].update(Running=True),
            lambda o: o[S.API].update(Image='wrong-image'),
            lambda o: o[S.API]['HostConfig'].update(Memory=768 * 1024**2),
            lambda o: o[S.IMAGE]['Config']['Labels'].update({'org.opencontainers.image.revision': 'wrong'})]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(S.Blocked):
                self.baseline(change)

    def test_primary_overlap_with_production_is_refused(self):
        def change(objects):
            objects['production']['Config']['Env'] = ['SUPABASE_URL=https://simgvasrjiyltnwtmyzt.supabase.co']
        with self.assertRaisesRegex(S.Blocked, 'OVERLAPS_PRODUCTION'):
            self.baseline(change)

    def test_unhealthy_production_blocks_starting_extra_staging_load(self):
        def change(objects):
            objects['production']['State']['Health']['Status'] = 'unhealthy'
        with self.assertRaisesRegex(S.Blocked, 'PRODUCTION_API_MUST_ALREADY_BE_HEALTHY'):
            self.baseline(change)

    def test_production_or_running_initializer_cannot_share_staging_state(self):
        for target in ('production', 'initializer'):
            def change(objects):
                objects[target]['Mounts'] = deepcopy(objects[S.API]['Mounts'])
                objects[target]['State']['Running'] = True
            with self.subTest(target=target), self.assertRaisesRegex(S.Blocked, 'ANOTHER_CONSUMER'):
                self.baseline(change)

    def test_disabled_pa_and_staging_metadata_dependencies_are_mandatory(self):
        def enable_pa(objects):
            objects[S.API]['Config']['Env'].append('PRICE_ACTION_LAB_ENABLED=true')
        def stop_worker(objects):
            objects[S.WORKER]['State']['Running'] = False
        for change in (enable_pa, stop_worker):
            with self.subTest(change=change), self.assertRaises(S.Blocked):
                self.baseline(change)


class BackupProofGates(unittest.TestCase):
    def proof(self, mutate=None):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / 'state'
            source.mkdir()
            member = source / 'smc_strategy_paper.db'
            member.write_bytes(b'fixture')
            st = member.stat()
            archive = base / 'archive.zst'
            archive.write_bytes(b'synthetic-archive')
            manifest = {'source_volume': S.VOLUME, 'source': {member.name: {
                'inode': st.st_ino, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}},
                'archive': str(archive), 'archive_sha256': S.sha(archive),
                'restore_test': {'sha256_match': True, 'postgres_restored_counts': {'metadata': {'t': 1}},
                    'sqlite': {'smc_strategy_paper.db': {'integrity_check': 'ok', 'foreign_key_violations': 0}},
                    'at': 'synthetic-time'}}
            if mutate:
                mutate(manifest, member, archive)
            (base / 'manifest.json').write_text(json.dumps(manifest))
            (base / 'result.json').write_text(json.dumps({'status': 'OK'}))
            with patch.object(S, 'regular', side_effect=lambda path, **kwargs: Path(path)):
                return S.backup_gate(base, source)

    def test_verified_archive_and_fresh_original_identities_pass(self):
        self.assertEqual(self.proof()['files'], 1)

    def test_archive_tampering_or_changed_source_is_refused(self):
        for change in (lambda m, s, a: a.write_bytes(b'tampered'),
                       lambda m, s, a: s.write_bytes(b'new-source-data')):
            with self.subTest(change=change), self.assertRaises(S.Blocked):
                self.proof(change)

    def test_bad_sqlite_integrity_or_foreign_keys_are_refused(self):
        for key, value in [('integrity_check', 'bad'), ('foreign_key_violations', 1)]:
            def change(m, s, a):
                m['restore_test']['sqlite']['smc_strategy_paper.db'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(S.Blocked, 'SQLITE_INTEGRITY'):
                self.proof(change)

    def test_missing_postgres_restore_or_wrong_volume_is_refused(self):
        for change in (lambda m, s, a: m['restore_test'].update(postgres_restored_counts={}),
                       lambda m, s, a: m.update(source_volume='production')):
            with self.subTest(change=change), self.assertRaises(S.Blocked):
                self.proof(change)

    def test_manifest_cannot_traverse_outside_staging_volume(self):
        def change(m, s, a):
            m['source']['../not-staging'] = m['source'].pop(s.name)
        with self.assertRaisesRegex(S.Blocked, 'MEMBER_PATH'):
            self.proof(change)


class HealthGates(unittest.TestCase):
    def health(self, mutate=None):
        health = {'http': 200, 'json': {'status': 'ok', 'acceptance_read_only': True,
            'features': {'price_action_lab_enabled': False},
            'persistence': {'ledger_supabase': {'configured': True, 'connected': True}}}}
        if mutate:
            mutate(health)
        state = {'State': {'Running': True, 'Health': {'Status': 'healthy'}}}
        with patch.object(S, 'inspect', return_value=state), patch.object(S, 'api_request', return_value=health):
            return S.wait_healthy()

    def test_healthy_container_alone_does_not_clear_read_only_or_ledger_checks(self):
        self.health()
        for change in (lambda h: h['json'].update(acceptance_read_only=False),
                       lambda h: h['json']['features'].update(price_action_lab_enabled=True),
                       lambda h: h['json']['persistence']['ledger_supabase'].update(connected=False)):
            with self.subTest(change=change), self.assertRaises(S.Blocked):
                self.health(change)


class StartFlow(unittest.TestCase):
    def execute(self, *, backup_failure=False, health_failure=False, unsafe_http=False,
                changed_neighbor=False, changed_finance=False, backup_collision=False):
        with tempfile.TemporaryDirectory(prefix='nexus-task6-acceptance-') as temporary:
            directory = Path(temporary)
            source = directory / 'state'
            source.mkdir()
            for name in ('account.db', 'smc_strategy_paper.db'):
                (source / name).write_bytes(b'synthetic')
            files = [directory / 'base.yaml']
            files[0].write_text('synthetic-compose')
            env = directory / 'compose.env'
            env.write_text('synthetic-private-config')
            (directory / 'ledger-gate.json').write_text(json.dumps({'status': M.LEDGER_STATUS,
                'external_application_ledger_recovery_proven': True}))
            old = api_fixture()
            new = deepcopy(old)
            new['Id'] = 'new-api'
            new['State'] = {'Running': True, 'Status': 'running'}
            new['Config']['Env'].append(S.MODE_KEY + '=1')
            before_config = config_fixture()
            after_config = deepcopy(before_config)
            after_config['services']['api']['environment'][S.MODE_KEY] = '1'
            old_ids = {S.API: ['old-api'], 'production': ['prod-id', 'same-start']}
            if backup_collision:
                old_ids['nexus-ops-staging-backup-pgtest'] = ['preexisting-unknown-container']
            commands = []
            running = [False]

            def compose(paths, env_file, *args):
                commands.append(('compose', args))
                if args[:2] == ('config', '--format'):
                    return json.dumps(after_config if len(paths) > 1 else before_config)
                if args[:2] == ('config', '--hash'):
                    return 'api old-hash\nresearch-worker same\n'
                self.assertEqual(args, ('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'api'))
                running[0] = True
                return ''

            def current_ids():
                value = deepcopy(old_ids)
                if running[0]:
                    value[S.API] = ['new-api']
                    if changed_neighbor:
                        value['production'] = ['unexpected-prod']
                return value

            def docker(*args, **kwargs):
                commands.append(('docker', args))
                if args[0] == 'stop':
                    running[0] = False
                return ''

            def health():
                if health_failure:
                    raise S.Blocked('HEALTH_TEST_FAILURE')
                if changed_finance:
                    (source / 'smc_strategy_paper.db-wal').write_bytes(b'changed')
                return new

            def request(path, method='GET'):
                status = 200
                if path.endswith('/cohorts'):
                    status = 401
                elif method == 'POST':
                    status = 200 if unsafe_http else 423
                return {'http': status}

            with ExitStack() as guards:
                for name, value in [('LOCK_FILE', directory / 'lock')]:
                    guards.enter_context(patch.object(S, name, value))
                guards.enter_context(patch.object(S.os, 'geteuid', return_value=0))
                guards.enter_context(patch.object(S, 'private_directory', return_value=directory))
                guards.enter_context(patch.object(S, 'stopped_baseline', return_value=(old, source, files, env)))
                guards.enter_context(patch.object(S, 'identities', side_effect=current_ids))
                guards.enter_context(patch.object(S, 'compose', side_effect=compose))
                backup = guards.enter_context(patch.object(S, 'fresh_backup', return_value={'archive_sha256': 'fixture'}))
                if backup_failure:
                    backup.side_effect = S.Blocked('BACKUP_TEST_FAILURE')
                guards.enter_context(patch.object(S, 'wait_healthy', side_effect=health))
                guards.enter_context(patch.object(S, 'api_request', side_effect=request))
                guards.enter_context(patch.object(S, 'docker', side_effect=docker))
                guards.enter_context(patch.object(S, 'inspect', side_effect=lambda _: {'State': {'Running': running[0]}}))
                guards.enter_context(patch.object(S, 'measure_idle', return_value={'fixture': 'test-only'}))
                guards.enter_context(patch.object(S, 'price_action_connections', return_value={'price_action_file_descriptors': 0}))
                output = io.StringIO()
                with redirect_stdout(output):
                    code = S.start(SimpleNamespace(run_directory=str(directory)))
                receipt = json.loads((directory / 'startup-receipt.json').read_text())
                self.assertNotIn('synthetic-do-not-print', output.getvalue())
                return code, receipt, commands

    def test_success_keeps_task_open_until_authenticated_browser_acceptance(self):
        code, result, calls = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'STRATEGY_INTELLIGENCE_STAGING_RESEARCH_MODE_STARTED')
        self.assertFalse(result['task6_complete'])
        self.assertFalse(result['production_deployment_performed'])
        self.assertEqual(result['authenticated_prd_acceptance'], 'PENDING_OWNER_BROWSER_CHECK')
        self.assertTrue(result['restart_verified'])
        self.assertIn(('docker', ('restart', '--time', '20', S.API)), calls)

    def test_backup_failure_never_starts_api(self):
        code, result, calls = self.execute(backup_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'BACKUP_TEST_FAILURE')
        self.assertFalse(any(c[0] == 'compose' and c[1][0] == 'up' for c in calls))
        self.assertFalse(any(c[0] == 'docker' and c[1][0] == 'stop' for c in calls))

    def test_existing_backup_test_container_blocks_before_backup_or_recreation(self):
        code, result, calls = self.execute(backup_collision=True)
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'EXISTING_BACKUP_TEST_CONTAINER_REQUIRES_REVIEW')
        self.assertEqual(calls, [])

    def test_health_failure_stops_staging_without_starting_old_execution(self):
        code, result, calls = self.execute(health_failure=True)
        self.assertEqual(code, 1)
        self.assertTrue(result['staging_left_stopped'])
        self.assertIn(('docker', ('stop', '--time', '20', S.API)), calls)

    def test_execution_policy_failure_stops_staging(self):
        code, result, _ = self.execute(unsafe_http=True)
        self.assertEqual(code, 1)
        self.assertTrue(result['staging_left_stopped'])

    def test_neighbor_or_financial_change_blocks_and_stops_staging(self):
        for kwargs in ({'changed_neighbor': True}, {'changed_finance': True}):
            with self.subTest(kwargs=kwargs):
                code, result, _ = self.execute(**kwargs)
                self.assertEqual(code, 1)
                self.assertTrue(result['staging_left_stopped'])


if __name__ == '__main__':
    unittest.main()
