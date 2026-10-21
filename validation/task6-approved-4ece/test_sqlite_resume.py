"""Synthetic native SQLite recovery fixtures; never use customer data."""
import ast
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE=Path(__file__).parent
SPEC=importlib.util.spec_from_file_location('sqlite_resume',HERE/'vps-sqlite-resume.py')
RESUME=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESUME)
ORIGINAL=(HERE/'vps-sqlite-recovery.py').read_text()
TREE=ast.parse(ORIGINAL)
ORIGINAL_CHILD=ast.literal_eval(next(n.value for n in TREE.body if isinstance(n,ast.Assign)
    and any(isinstance(t,ast.Name) and t.id=='CHILD' for t in n.targets)))
TOKEN='0123456789abcdef0123456789abcdef'
GIB=1024**3


class ResumeFixtures(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.base=Path(self.temporary.name)
        self.root=self.base/'api';self.root.mkdir()
        self.research=self.base/'research';self.research.mkdir()
        self.shadow=self.base/'shadow';self.shadow.mkdir()
        self.source=self.root/'evidence.db'
        self.db=sqlite3.connect(self.source)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE paper_trades(id INTEGER PRIMARY KEY,owner TEXT,pnl REAL,proof BLOB)')
        self.db.executemany('INSERT INTO paper_trades VALUES(?,?,?,?)',
                            [(n,'tenant-A' if n%2 else 'tenant-B',n/10,b'\x00\xff') for n in range(1,1001)])
        self.db.execute('CREATE VIRTUAL TABLE notes USING fts5(text)')
        self.db.execute("INSERT INTO notes VALUES('preserve full text index')")
        self.db.execute('CREATE INDEX financial_owner ON paper_trades(owner)')
        self.db.commit()
        self.work=self.root/('.task6-sqlite-recovery-'+TOKEN)
        self.raw=self.work/'owned-snapshot.sqlite'

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def fixture_child(self,child,free=100*GIB):
        child=child.replace("ROOT=Path('/var/lib/tradexa')",'ROOT=Path('+repr(str(self.root))+')')
        child=child.replace("Path('/workers/research')",'Path('+repr(str(self.research))+')')
        child=child.replace("Path('/workers/shadow')",'Path('+repr(str(self.shadow))+')')
        # Synthetic disk counters only; the native SQLite/gzip/restore operations are real.
        child=child.replace('from pathlib import Path\n',
            'from pathlib import Path\nfrom types import SimpleNamespace\n'
            +'shutil.disk_usage=lambda _: SimpleNamespace(free='+str(free)+')\n',1)
        return child

    def execute(self,child,archive_limit=4*GIB):
        environment={**os.environ,'NEXUS_TASK6_RECOVERY_TOKEN':TOKEN,
                     'NEXUS_TASK6_RECOVERY_RESERVE':str(8*GIB),
                     'NEXUS_TASK6_RECOVERY_ARCHIVE_LIMIT':str(archive_limit)}
        result=subprocess.run([sys.executable,'-B','-'],input=child,text=True,
                               capture_output=True,env=environment,timeout=30)
        return result

    def blocked_first_snapshot(self):
        child=self.fixture_child(ORIGINAL_CHILD)
        child=child.replace("        phase('FULL_INTEGRITY_CHECK')",
            "        phase('FULL_INTEGRITY_CHECK')\n        raise RuntimeError('PHASE_TIME_BOUND_EXCEEDED')",1)
        result=self.execute(child)
        self.assertEqual(result.returncode,1,result.stderr+result.stdout)
        self.assertTrue(self.raw.is_file())
        manifest=json.loads((self.work/'manifest.json').read_text())
        self.assertEqual(manifest['reason'],'PHASE_TIME_BOUND_EXCEEDED')
        return hashlib.sha256(self.raw.read_bytes()).hexdigest()

    def resumed(self,**kwargs):
        return self.execute(self.fixture_child(RESUME.build_child(ORIGINAL),**kwargs))

    def test_original_hash_and_compilation(self):
        self.assertEqual(hashlib.sha256(ORIGINAL.encode()).hexdigest(),RESUME.ORIGINAL_SHA256)
        compile(RESUME.build_child(ORIGINAL),'<resume>','exec')
        self.assertRaises(RESUME.Blocked,RESUME.build_child,ORIGINAL.replace('cache_size=-2048','cache_size=-999'))

    def test_wal_snapshot_is_reused_and_full_restore_matches(self):
        raw_hash=self.blocked_first_snapshot()
        blocked_bytes=(self.work/'manifest.json').read_bytes()
        child=self.fixture_child(RESUME.build_child(ORIGINAL))
        # There is only one DB in this fixture. Re-copying it must fail the test.
        child=child.replace('original.backup(destination,pages=256,progress=backup_progress,sleep=0.05)',
                            "raise RuntimeError('UNEXPECTED_SECOND_SOURCE_BACKUP')")
        result=self.execute(child)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        manifest=json.loads((self.work/'manifest.json').read_text())
        self.assertEqual(manifest['status'],'SQLITE_BACKUP_AND_RESTORE_PASSED')
        self.assertEqual(manifest['entries'][0]['snapshot_sha256'],raw_hash)
        self.assertEqual(manifest['entries'][0]['financial_records']['paper_trades']['rows'],1000)
        self.assertTrue(manifest['entries'][0]['restored_bytes_schema_and_financial_records_match'])
        self.assertEqual((self.work/'manifest.before-resume.json').read_text(),json.dumps(json.loads(blocked_bytes),indent=2))
        self.assertIn('EXISTING_OWNED_SNAPSHOT_REUSED',result.stdout)
        self.assertFalse(self.raw.exists())
        self.assertEqual(self.db.execute('SELECT count(*) FROM paper_trades').fetchone()[0],1000)

    def test_current_financial_changes_block_before_integrity(self):
        self.blocked_first_snapshot()
        self.db.execute('UPDATE paper_trades SET pnl=999 WHERE id=1');self.db.commit()
        result=self.resumed()
        self.assertEqual(result.returncode,1,result.stdout)
        self.assertIn('RESUME_SNAPSHOT_SCHEMA_OR_FINANCIAL_RECORDS_CHANGED',result.stdout)
        self.assertTrue(self.raw.exists())
        self.assertEqual(self.db.execute('SELECT pnl FROM paper_trades WHERE id=1').fetchone()[0],999)

    def test_second_database_and_other_state_are_preserved(self):
        with sqlite3.connect(self.research/'research.sqlite') as db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,state TEXT)')
            db.execute("INSERT INTO jobs VALUES(1,'pending')")
        (self.root/'customer.json').write_text('{"owner":"synthetic-tenant"}')
        self.blocked_first_snapshot()
        result=self.resumed()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        manifest=json.loads((self.work/'manifest.json').read_text())
        self.assertEqual(len(manifest['entries']),2)
        self.assertEqual(len(manifest['non_sqlite_entries']),1)
        self.assertEqual(manifest['non_sqlite_entries'][0]['source_relative_path'],'customer.json')
        self.assertTrue(all(e['restored_bytes_schema_and_financial_records_match'] for e in manifest['entries']))

    def test_time_bound_remains_enforced_and_preserves_snapshot(self):
        before=self.blocked_first_snapshot()
        child=self.fixture_child(RESUME.build_child(ORIGINAL))
        child=child.replace("        phase('FULL_INTEGRITY_CHECK')",
            "        phase('FULL_INTEGRITY_CHECK')\n        deadline=time.monotonic()-1\n        guard(True)",1)
        result=self.execute(child)
        self.assertEqual(result.returncode,1)
        self.assertIn('PHASE_TIME_BOUND_EXCEEDED',result.stdout)
        self.assertEqual(hashlib.sha256(self.raw.read_bytes()).hexdigest(),before)

    def test_wrong_ownership_preserves_snapshot_and_failure_evidence(self):
        before=self.blocked_first_snapshot()
        previous=(self.work/'manifest.json').read_bytes()
        (self.work/'ownership.json').write_text(json.dumps({'token':'bad'}))
        result=self.resumed()
        self.assertEqual(result.returncode,1)
        self.assertIn('OWNED_RESUME_MARKER_MISMATCH',result.stdout)
        self.assertEqual(hashlib.sha256(self.raw.read_bytes()).hexdigest(),before)
        self.assertEqual((self.work/'manifest.json').read_bytes(),previous)

    def test_disk_reserve_stops_without_removing_snapshot(self):
        before=self.blocked_first_snapshot()
        result=self.resumed(free=7*GIB)
        self.assertEqual(result.returncode,1)
        self.assertIn('FREE_DISK_RESERVE_REACHED',result.stdout)
        self.assertEqual(hashlib.sha256(self.raw.read_bytes()).hexdigest(),before)

    def test_invalid_snapshot_never_passes(self):
        self.blocked_first_snapshot()
        self.raw.write_bytes(b'not a valid SQLite backup')
        result=self.resumed()
        self.assertEqual(result.returncode,1)
        self.assertIn('SQLITE_NOTADB',result.stdout)
        self.assertTrue(self.raw.exists())
        self.assertEqual(self.db.execute('SELECT count(*) FROM paper_trades').fetchone()[0],1000)

    def test_archive_cap_stops_before_deleting_raw_copy(self):
        before=self.blocked_first_snapshot()
        result=self.execute(self.fixture_child(RESUME.build_child(ORIGINAL)),archive_limit=1)
        self.assertEqual(result.returncode,1)
        self.assertIn('COMPRESSED_ARCHIVE_LIMIT_REACHED',result.stdout)
        self.assertEqual(hashlib.sha256(self.raw.read_bytes()).hexdigest(),before)

    def test_progress_has_work_counters_and_resource_bounds(self):
        self.blocked_first_snapshot()
        child=self.fixture_child(RESUME.build_child(ORIGINAL)).replace('now-last_log>15','now-last_log>0')
        result=self.execute(child)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        events=[json.loads(line) for line in result.stdout.splitlines()]
        ticks=[e for e in events if e.get('sqlite_vm_steps',0)>0]
        self.assertTrue(ticks)
        self.assertTrue(all('elapsed_seconds' in e and 'cpu_seconds' in e and 'peak_rss_kib' in e for e in ticks))
        manifest=json.loads((self.work/'manifest.json').read_text())
        self.assertEqual(manifest['sqlite_cache_mib'],32)
        self.assertEqual(manifest['process_address_space_limit_bytes'],256*1024**2)
        self.assertEqual(manifest['phase_time_limit_seconds'],14400)

    def test_original_runner_hash_is_checked_before_execution(self):
        directory=self.base/'control';directory.mkdir()
        runner=directory/'runner.py'
        runner.write_text("raise AssertionError('must not execute untrusted code')")
        self.assertRaisesRegex(RESUME.Blocked,'CHECKSUM_MISMATCH',RESUME.load_original,directory)
        runner.write_text(ORIGINAL)
        module,_=RESUME.load_original(directory)
        self.assertIs(module.Blocked,RESUME.Blocked)

    def test_directory_and_token_boundary(self):
        directory=self.base/('task6-sqlite-recovery-20000101T000000Z-'+TOKEN[:12])
        directory.mkdir(mode=0o700)
        (directory/'job.json').write_text(json.dumps({'token':TOKEN,'api_id':'synthetic'}))
        (directory/'status.json').write_text(json.dumps({'status':'BLOCKED','reason':'DOCKER_OPERATION_FAILED:exec:exit=1'}))
        with patch.object(RESUME,'BASE',self.base):
            config,_=RESUME.validate_directory(directory)
            self.assertEqual(config['token'],TOKEN)
            directory.chmod(0o755)
            self.assertRaisesRegex(RESUME.Blocked,'PERMISSIONS',RESUME.validate_directory,directory)

    def host_fixture(self):
        directory=self.base/('task6-sqlite-recovery-20000101T000000Z-'+TOKEN[:12])
        directory.mkdir(mode=0o700)
        config={'token':TOKEN,'api_id':'synthetic-api'}
        (directory/'job.json').write_text(json.dumps(config))
        (directory/'status.json').write_text('{"status":"BLOCKED","original":"preserved"}')
        (directory/'runner.py').write_text(ORIGINAL)
        module,_=RESUME.load_original(directory)
        module.BASE=self.base
        api={'Id':'synthetic-api','Image':'synthetic-image','RestartCount':0,
             'State':{'StartedAt':'synthetic-time','Health':{'Status':'healthy'}}}
        prod={'Id':'synthetic-production','Image':'unchanged','RestartCount':0,
              'State':{'StartedAt':'unchanged'},'Mounts':[]}
        module.preflight=lambda:(api,module.selected_snapshot([prod]),{})
        def containers(project):
            self.assertEqual(project,module.PROD)
            return [prod]
        module.project_containers=containers
        def docker(args,*,input_text=None,stream=False):
            if args[0]=='exec':
                if input_text is not None:
                    self.assertEqual(args[-4],config['api_id'])
                    result=self.execute(self.fixture_child(input_text))
                    with (directory/'resume.log').open('a') as log:
                        log.write(result.stdout+result.stderr)
                    if result.returncode:
                        raise RESUME.Blocked('DOCKER_OPERATION_FAILED:exec:exit=1')
                    return ''
                self.assertEqual(args[1],config['api_id'])
                return (self.work/'manifest.json').read_text()
            if args[0]=='cp':
                self.assertEqual(args[1],config['api_id']+':/var/lib/tradexa/.task6-sqlite-recovery-'+TOKEN)
                import shutil
                shutil.copytree(self.work,Path(args[2]))
                return ''
            self.fail('Unexpected Docker operation: '+args[0])
        module.docker=docker
        module.CHILD=RESUME.build_child(ORIGINAL)
        return directory,module,config

    def test_host_pipeline_copies_verified_archives_and_keeps_original_failure(self):
        self.blocked_first_snapshot()
        directory,module,config=self.host_fixture()
        original_status=(directory/'status.json').read_bytes()
        with patch.object(RESUME,'prepare',return_value=(module,config)), \
             patch.object(module.shutil,'disk_usage',return_value=types.SimpleNamespace(free=100*GIB)):
            result=RESUME.run(directory)
        self.assertEqual(result,0)
        report=json.loads((directory/'resume-status.json').read_text())
        self.assertEqual(report['status'],'SQLITE_BACKUP_AND_RESTORE_PASSED')
        self.assertEqual(report['production_container_identities'],'UNCHANGED')
        self.assertTrue(report['owned_snapshot_reused'])
        self.assertFalse(report['task6_complete'])
        self.assertEqual((directory/'status.json').read_bytes(),original_status)
        self.assertTrue((directory/'sqlite'/'001.sqlite.gz').is_file())

    def test_host_failure_reports_specific_child_reason(self):
        self.blocked_first_snapshot()
        self.db.execute('DELETE FROM paper_trades WHERE id=1');self.db.commit()
        directory,module,config=self.host_fixture()
        with patch.object(RESUME,'prepare',return_value=(module,config)):
            result=RESUME.run(directory)
        self.assertEqual(result,1)
        report=json.loads((directory/'resume-status.json').read_text())
        self.assertEqual(report['reason'],'RESUME_SNAPSHOT_SCHEMA_OR_FINANCIAL_RECORDS_CHANGED')
        self.assertEqual(report['parent_reason'],'DOCKER_OPERATION_FAILED:exec:exit=1')
        self.assertFalse(report['owned_snapshot_reused'])

    def test_background_launch_is_private_and_duplicate_launch_is_rejected(self):
        directory,module,config=self.host_fixture()
        previous=(directory/'status.json').read_bytes()
        with patch.object(sys,'argv',['resume.py',str(directory)]), \
             patch.object(RESUME,'prepare',return_value=(module,config)), \
             patch.object(RESUME.subprocess,'Popen',return_value=types.SimpleNamespace(pid=123)) as launch, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(RESUME.main(),0)
            self.assertRaises(FileExistsError,RESUME.main)
        self.assertEqual(launch.call_count,1)
        self.assertTrue(launch.call_args.kwargs['start_new_session'])
        self.assertEqual(launch.call_args.kwargs['stdin'],subprocess.DEVNULL)
        self.assertEqual(json.loads(output.getvalue())['status'],'SQLITE_RESUME_JOB_STARTED')
        for name in ('resume-runner.py','resume-status.json','resume.log'):
            self.assertEqual((directory/name).stat().st_mode & 0o077,0)
        self.assertEqual((directory/'status.json').read_bytes(),previous)


if __name__=='__main__':
    unittest.main()
