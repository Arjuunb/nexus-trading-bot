"""Native point-in-time restore tests; all records and paths are synthetic."""
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location('snapshot_preserve', HERE / 'vps-sqlite-snapshot-preserve.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)
TOKEN = '0123456789abcdef0123456789abcdef'


class SnapshotPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / ('.task6-sqlite-recovery-' + TOKEN)
        self.work.mkdir(mode=0o700)
        self.raw = self.work / 'owned-snapshot.sqlite'
        (self.work / 'ownership.json').write_text(json.dumps({'token': TOKEN, 'purpose': 'TASK6_OWNED_TEMPORARIES_ONLY'}))
        self.source = self.root / 'live.db'
        self.live = sqlite3.connect(self.source, isolation_level=None)
        self.live.execute('PRAGMA journal_mode=WAL')
        self.live.execute('PRAGMA wal_autocheckpoint=0')
        self.live.executescript('''
            CREATE TABLE v2_account(id INTEGER PRIMARY KEY,balance REAL);
            CREATE TABLE pa_funding_events(id INTEGER PRIMARY KEY,amount REAL);
            CREATE TABLE pa_sessions(id TEXT PRIMARY KEY,state_json TEXT,updated_at TEXT);
            INSERT INTO v2_account VALUES(1,10000.25);
            INSERT INTO pa_funding_events VALUES(1,-0.1);
            INSERT INTO pa_sessions VALUES('synthetic-session','sensitive synthetic configuration','old');
        ''')
        with sqlite3.connect(self.raw) as dst:
            self.live.backup(dst)
            dst.execute('PRAGMA journal_mode=DELETE')
        self.old_bytes = self.raw.read_bytes()
        self.history = {'status': 'BLOCKED', 'reason': 'RESUME_SNAPSHOT_SCHEMA_OR_FINANCIAL_RECORDS_CHANGED'}
        (self.work / 'manifest.json').write_text(json.dumps(self.history))
        self.guard = P.Guard(self.root, reserve=0, phase_seconds=20, job_seconds=30, check_memory=False)

    def tearDown(self):
        self.live.close()
        self.temp.cleanup()

    def preserve(self, **kwargs):
        return P.preserve(self.root, TOKEN, self.guard, archive_limit=1024**2, **kwargs)

    def test_later_funding_and_session_changes_do_not_invalidate_old_restore(self):
        self.live.executescript('''
            INSERT INTO pa_funding_events VALUES(2,-0.2);
            INSERT INTO pa_funding_events VALUES(3,-0.3);
            UPDATE pa_sessions SET updated_at='new',state_json='new configuration';
        ''')
        report = self.preserve()
        self.assertEqual(report['status'], 'OWNED_POINT_IN_TIME_SNAPSHOT_RESTORED')
        self.assertFalse(report['current_live_financial_records_backed_up'])
        self.assertFalse(report['full_sqlite_recovery_passed'])
        self.assertFalse(report['task6_complete'])
        self.assertEqual(self.live.execute('SELECT count(*) FROM pa_funding_events').fetchone()[0], 3)
        self.assertEqual(report['financial_records']['pa_funding_events']['rows'], 1)
        with gzip.open(self.work / 'preservation' / 'snapshot.sqlite.gz', 'rb') as src:
            self.assertEqual(src.read(), self.old_bytes)
        self.assertFalse(self.raw.exists())
        self.assertFalse((self.work / 'preservation' / 'restored.sqlite').exists())
        self.assertEqual(json.loads((self.work / 'manifest.json').read_text()), self.history)

    def test_byte_roundtrip_includes_json_blobs_triggers_and_foreign_keys(self):
        with sqlite3.connect(self.raw) as db:
            db.executescript('''
                CREATE TABLE parent(id INTEGER PRIMARY KEY);
                CREATE TABLE child(id INTEGER REFERENCES parent(id),proof BLOB);
                INSERT INTO parent VALUES(9);
                INSERT INTO child VALUES(9,x'0001ff');
                CREATE TRIGGER frozen_delete BEFORE DELETE ON parent BEGIN SELECT RAISE(ABORT,'frozen'); END;
            ''')
        before = self.raw.read_bytes()
        report = self.preserve()
        self.assertTrue(report['integrity_check_ok'])
        self.assertTrue(report['foreign_key_check_ok'])
        self.assertTrue(report['schema_and_financial_records_match'])
        with gzip.open(self.work / 'preservation' / 'snapshot.sqlite.gz', 'rb') as src:
            self.assertEqual(src.read(), before)

    def test_wrong_ownership_never_removes_snapshot(self):
        (self.work / 'ownership.json').write_text('{}')
        with self.assertRaisesRegex(P.Blocked, 'OWNED_SNAPSHOT_MARKER_MISMATCH'):
            self.preserve()
        self.assertEqual(self.raw.read_bytes(), self.old_bytes)

    def test_symlink_and_hardlink_are_rejected(self):
        self.raw.unlink()
        self.raw.symlink_to(self.source)
        with self.assertRaisesRegex(P.Blocked, 'REGULAR_SINGLE_LINK'):
            self.preserve()
        self.raw.unlink()
        os.link(self.source, self.raw)
        with self.assertRaisesRegex(P.Blocked, 'REGULAR_SINGLE_LINK'):
            self.preserve()

    def test_archive_limit_failure_keeps_original_owned_snapshot(self):
        with self.assertRaisesRegex(P.Blocked, 'ARCHIVE_LIMIT'):
            P.preserve(self.root, TOKEN, self.guard, archive_limit=8)
        self.assertEqual(self.raw.read_bytes(), self.old_bytes)

    def test_disk_reserve_failure_keeps_original_owned_snapshot(self):
        with patch.object(P.shutil, 'disk_usage', return_value=type('D', (), {'free': 0})()):
            self.guard.reserve = 1
            with self.assertRaisesRegex(P.Blocked, 'FREE_DISK_RESERVE'):
                self.preserve()
        self.assertEqual(self.raw.read_bytes(), self.old_bytes)

    def test_corrupt_compressed_archive_keeps_original_owned_snapshot(self):
        real = P.verify_archive
        def corrupt_then_verify(path, *args, **kwargs):
            path.write_bytes(b'not gzip')
            return real(path, *args, **kwargs)
        with patch.object(P, 'verify_archive', side_effect=corrupt_then_verify):
            with self.assertRaises((OSError, EOFError)):
                self.preserve()
        self.assertEqual(self.raw.read_bytes(), self.old_bytes)

    def test_foreign_key_failure_retains_verified_archive(self):
        with sqlite3.connect(self.raw) as db:
            db.executescript('CREATE TABLE p(id INTEGER PRIMARY KEY);CREATE TABLE c(id INTEGER REFERENCES p(id));INSERT INTO c VALUES(999);')
        before = self.raw.read_bytes()
        with self.assertRaisesRegex(P.Blocked, 'FOREIGN_KEY_CHECK_FAILED'):
            self.preserve()
        with gzip.open(self.work / 'preservation' / 'snapshot.sqlite.gz', 'rb') as src:
            self.assertEqual(src.read(), before)

    def test_preexisting_preservation_directory_blocks_duplicate_job(self):
        (self.work / 'preservation').mkdir()
        with self.assertRaisesRegex(P.Blocked, 'PRESERVATION_ALREADY_EXISTS'):
            self.preserve()
        self.assertEqual(self.raw.read_bytes(), self.old_bytes)

    def test_wal_dependent_snapshot_is_not_treated_as_standalone(self):
        db = sqlite3.connect(self.raw)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('INSERT INTO pa_funding_events VALUES(7,7)')
            db.commit()
            with self.assertRaisesRegex(P.Blocked, 'STANDALONE_DELETE_MODE_REQUIRED'):
                self.preserve()
        finally:
            db.close()
        self.assertTrue(self.raw.exists())

    def test_timeout_preserves_snapshot(self):
        self.guard.started -= 100
        with self.assertRaisesRegex(P.Blocked, 'TIME_BOUND_EXCEEDED'):
            self.preserve()
        self.assertEqual(self.raw.read_bytes(), self.old_bytes)

    def test_report_has_no_row_values(self):
        report = self.preserve()
        self.assertNotIn('sensitive synthetic configuration', json.dumps(report))
        self.assertNotIn('synthetic-session', json.dumps(report))

    def test_native_integrity_rejects_broken_index_and_retains_archive(self):
        with sqlite3.connect(self.raw) as db:
            db.executescript("CREATE TABLE indexed_data(value TEXT);CREATE INDEX proof_index ON indexed_data(value);INSERT INTO indexed_data VALUES('old-index-key');")
        data = self.raw.read_bytes()
        self.assertEqual(data.count(b'old-index-key'), 2)
        damaged = data.replace(b'old-index-key', b'new-index-key', 1)
        self.raw.write_bytes(damaged)
        with self.assertRaisesRegex(P.Blocked, 'FULL_INTEGRITY_CHECK_FAILED'):
            self.preserve()
        report = json.loads((self.work / 'preservation' / 'manifest.json').read_text())
        self.assertEqual(report['status'], 'BLOCKED')
        self.assertFalse(report['full_sqlite_recovery_passed'])
        with gzip.open(self.work / 'preservation' / 'snapshot.sqlite.gz', 'rb') as src:
            self.assertEqual(src.read(), damaged)


class HostPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.directory = self.base / ('task6-sqlite-recovery-20260101T000000Z-' + TOKEN[:12])
        self.directory.mkdir(mode=0o700)
        source = '''\
def preflight():
    return {'Id':'synthetic-stage-id'}, [{'id':'synthetic-prod-id'}], {}
'''
        (self.directory / 'runner.py').write_text(source)
        (self.directory / 'job.json').write_text(json.dumps({'token': TOKEN, 'api_id': 'synthetic-stage-id'}))
        (self.directory / 'status.json').write_text(json.dumps({'status': 'BLOCKED', 'production_before': [{'id': 'synthetic-prod-id'}]}))
        (self.directory / 'resume-status.json').write_text(json.dumps({'status': 'BLOCKED', 'reason': 'RESUME_SNAPSHOT_SCHEMA_OR_FINANCIAL_RECORDS_CHANGED'}))
        self.base_patch = patch.object(P, 'BASE', self.base)
        self.hash_patch = patch.object(P, 'ORIGINAL_SHA256', hashlib.sha256(source.encode()).hexdigest())
        self.base_patch.start()
        self.hash_patch.start()

    def tearDown(self):
        self.hash_patch.stop()
        self.base_patch.stop()
        self.temp.cleanup()

    def test_expected_failed_job_and_unchanged_containers_are_required(self):
        _, config, baseline = P.prepare(self.directory)
        self.assertEqual(config['api_id'], 'synthetic-stage-id')
        self.assertEqual(baseline, [{'id': 'synthetic-prod-id'}])

    def test_modified_original_runner_is_rejected_before_execution(self):
        (self.directory / 'runner.py').write_text('raise AssertionError("must not execute")')
        with self.assertRaisesRegex(P.Blocked, 'ORIGINAL_RUNNER_CHECKSUM_MISMATCH'):
            P.prepare(self.directory)

    def test_changed_production_identity_is_rejected(self):
        (self.directory / 'status.json').write_text(json.dumps({'status': 'BLOCKED', 'production_before': []}))
        with self.assertRaisesRegex(P.Blocked, 'PRODUCTION_CONTAINER_IDENTITIES_CHANGED'):
            P.prepare(self.directory)

    def test_changed_staging_api_is_rejected(self):
        (self.directory / 'job.json').write_text(json.dumps({'token': TOKEN, 'api_id': 'replacement-id'}))
        with self.assertRaisesRegex(P.Blocked, 'ORIGINAL_STAGE_API_REQUIRED'):
            P.prepare(self.directory)

    def test_inconsistent_token_and_control_file_symlink_are_rejected(self):
        (self.directory / 'job.json').write_text(json.dumps({'token': 'f'*32, 'api_id': 'synthetic-stage-id'}))
        with self.assertRaisesRegex(P.Blocked, 'ORIGINAL_RECOVERY_TOKEN_REQUIRED'):
            P.prepare(self.directory)
        (self.directory / 'job.json').unlink()
        (self.directory / 'job.json').symlink_to(self.directory / 'status.json')
        with self.assertRaisesRegex(P.Blocked, 'ORIGINAL_RECOVERY_CONTROL_FILES_REQUIRED'):
            P.prepare(self.directory)


if __name__ == '__main__':
    unittest.main()
