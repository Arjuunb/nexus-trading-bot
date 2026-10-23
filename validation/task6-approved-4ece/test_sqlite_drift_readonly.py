"""Native SQLite fixtures for redacted, incomplete drift diagnosis."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

HERE=Path(__file__).parent
SPEC=importlib.util.spec_from_file_location('sqlite_drift',HERE/'vps-sqlite-drift-readonly.py')
DRIFT=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(DRIFT)
TOKEN='0123456789abcdef0123456789abcdef'


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'price_action_paper.db'
        self.work=self.root/('.task6-sqlite-recovery-'+TOKEN);self.work.mkdir()
        self.raw=self.work/'owned-snapshot.sqlite'
        (self.work/'ownership.json').write_text(json.dumps({'token':TOKEN,'purpose':'TASK6_OWNED_TEMPORARIES_ONLY'}))
        self.db=sqlite3.connect(self.source,isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA wal_autocheckpoint=0')
        self.db.executescript('''
          CREATE TABLE v2_account(id INTEGER PRIMARY KEY,balance REAL,updated_at TEXT);
          CREATE TABLE v2_quote_cursor(symbol TEXT PRIMARY KEY,sequence INTEGER);
          CREATE TABLE v2_positions(symbol TEXT PRIMARY KEY,size REAL);
          CREATE TABLE v2_orders(id TEXT PRIMARY KEY,status TEXT);
          CREATE TABLE v2_fills(id INTEGER PRIMARY KEY,realized_pnl REAL,proof BLOB);
          CREATE TABLE v2_funding_events(id TEXT PRIMARY KEY,amount REAL);
          CREATE TABLE pa_sessions(id TEXT PRIMARY KEY,status TEXT,mode TEXT,updated_at TEXT,state_json TEXT);
          CREATE TABLE pa_order_meta(id TEXT PRIMARY KEY,status TEXT,config_json TEXT);
          CREATE TABLE pa_funding_events(id TEXT PRIMARY KEY,amount REAL);
          INSERT INTO v2_account VALUES(1,10000.5,'synthetic-old-time');
          INSERT INTO v2_quote_cursor VALUES('SYNTHETIC_SYMBOL',1);
          INSERT INTO pa_sessions VALUES('private-session-id','active','LIVE_PAPER','synthetic-old-time','private-json-proof');
          INSERT INTO pa_order_meta VALUES('private-order-id','PENDING','private-configuration');
        ''')
        self.snapshot()

    def snapshot(self):
        with sqlite3.connect(self.raw) as target:
            self.db.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')

    def tearDown(self):self.db.close();self.temp.cleanup()

    def run_child(self,source=None):
        code=(source or DRIFT.CHILD).replace("ROOT=Path('/var/lib/tradexa')",'ROOT=Path('+repr(str(self.root))+')')
        env={**os.environ,'NEXUS_TASK6_DRIFT_TOKEN':TOKEN,'HUB_DATA_DIR':str(self.root),
             'HUB_PRICE_ACTION_PAPER_DB':str(self.source)}
        result=subprocess.run([sys.executable,'-B','-'],input=code,text=True,capture_output=True,env=env,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        for value in ('private-session-id','private-order-id','private-json-proof','private-configuration','SYNTHETIC_SYMBOL'):
            self.assertNotIn(value,result.stdout)
        report=json.loads(result.stdout)
        self.assertFalse(report['recovery_passed']);self.assertFalse(report['complete_financial_evidence_proven'])
        self.assertFalse(report['database_writes_performed']);self.assertFalse(report['task6_complete'])
        return report

    def test_identical_selected_columns_never_become_recovery_acceptance(self):
        before=self.raw.read_bytes()
        report=self.run_child()
        self.assertTrue(report['schema_matches'])
        self.assertTrue(all(row['status']=='SELECTED_SAMPLE_COLUMNS_MATCH' for row in report['selected_table_diagnostics'].values()))
        self.assertEqual(self.raw.read_bytes(),before)

    def test_control_timestamps_and_quote_cursor_drift_are_reported_by_column(self):
        self.db.execute("UPDATE v2_account SET updated_at='synthetic-new-time'")
        self.db.execute('UPDATE v2_quote_cursor SET sequence=2')
        self.db.execute("UPDATE pa_sessions SET updated_at='synthetic-new-time'")
        report=self.run_child()['selected_table_diagnostics']
        self.assertEqual(report['v2_account']['changed_columns'],['updated_at'])
        self.assertEqual(report['v2_quote_cursor']['changed_columns'],['sequence'])
        self.assertEqual(report['pa_sessions']['changed_columns'],['updated_at'])

    def test_financial_balance_change_is_reported_without_printing_value(self):
        self.db.execute('UPDATE v2_account SET balance=777.875')
        report=self.run_child();row=report['selected_table_diagnostics']['v2_account']
        self.assertEqual(row['status'],'SAMPLED_DIFFERENCES_FOUND')
        self.assertIn('balance',row['changed_columns'])
        self.assertNotIn('777.875',json.dumps(report))

    def test_new_fills_change_sample_keys_and_never_pass_as_equal(self):
        self.db.execute('INSERT INTO v2_fills VALUES(1,123.25,?)',(b'private-fill-proof',))
        report=self.run_child()['selected_table_diagnostics']['v2_fills']
        self.assertEqual(report['sampled_keys_only_in_live'],1)
        self.assertEqual(report['status'],'SAMPLED_DIFFERENCES_FOUND')

    def test_schema_difference_is_separate_from_row_drift(self):
        self.db.execute('CREATE INDEX new_synthetic_index ON v2_account(updated_at)')
        report=self.run_child()
        self.assertFalse(report['schema_matches']);self.assertEqual(report['schema_objects_added_count'],1)

    def test_large_tables_remain_explicitly_truncated(self):
        self.db.executemany('INSERT INTO v2_fills VALUES(?,?,?)',[(n,1.0,b'proof') for n in range(150)])
        self.snapshot()
        report=self.run_child()['selected_table_diagnostics']['v2_fills']
        self.assertTrue(report['sample_truncated']);self.assertEqual(report['live_sample_rows'],128)

    def test_json_columns_are_explicitly_uncompared(self):
        self.db.execute("UPDATE pa_sessions SET state_json='different-private-json'")
        report=self.run_child()['selected_table_diagnostics']['pa_sessions']
        self.assertIn('state_json',report['excluded_json_or_payload_columns_not_compared'])
        self.assertEqual(report['status'],'SELECTED_SAMPLE_COLUMNS_MATCH')
        self.assertEqual(report['row_scope'],'ACTIVE_SESSIONS_ONLY')

    def test_missing_table_is_unknown_not_empty_or_matching(self):
        self.db.execute('DROP TABLE v2_funding_events')
        report=self.run_child()['selected_table_diagnostics']['v2_funding_events']
        self.assertEqual(report['status'],'UNVERIFIED_TABLE_MISSING')

    def test_marker_mismatch_and_snapshot_symlink_are_rejected(self):
        (self.work/'ownership.json').write_text('{}')
        report=self.run_child();self.assertEqual(report['reason'],'OWNED_SNAPSHOT_MARKER_MISMATCH')
        self.raw.unlink();self.raw.symlink_to(self.source)
        report=self.run_child();self.assertEqual(report['reason'],'REGULAR_STAGING_SOURCE_AND_OWNED_SNAPSHOT_REQUIRED')

    def test_uncheckpointed_wal_changes_are_seen(self):
        self.db.execute('UPDATE v2_account SET balance=999.5')
        self.assertGreater((self.root/'price_action_paper.db-wal').stat().st_size,0)
        report=self.run_child()['selected_table_diagnostics']['v2_account']
        self.assertIn('balance',report['changed_columns'])

    def test_mutation_is_rejected_by_read_only_connection(self):
        code=DRIFT.CHILD.replace("db.execute('BEGIN')","db.execute('BEGIN')\n    db.execute('CREATE TABLE must_not_write(id INTEGER)')")
        report=self.run_child(source=code)
        self.assertEqual(report['status'],'BLOCKED');self.assertEqual(report['sqlite_name'],'SQLITE_READONLY')
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='must_not_write'").fetchone())


class HostTests(unittest.TestCase):
    def test_only_original_staging_api_is_executed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);directory=base/('task6-sqlite-recovery-20000101T000000Z-'+TOKEN[:12])
            directory.mkdir(mode=0o700)
            (directory/'job.json').write_text(json.dumps({'token':TOKEN,'api_id':'synthetic-stage-api'}))
            def row(project,service,identifier):
                return {'Id':identifier,'Name':'/'+project+'-'+service+'-1','Image':'synthetic-image','RestartCount':0,
                    'Config':{'Labels':{'com.docker.compose.project':project,'com.docker.compose.service':service}},
                    'State':{'Status':'running','StartedAt':'synthetic-time','Health':{'Status':'healthy'}},
                    'HostConfig':{'Memory':1610612736,'MemorySwap':1610612736},'Mounts':[{'Source':project+'-state'}]}
            stage=row(DRIFT.STAGE,'api','synthetic-stage-api');prod=row(DRIFT.PROD,'app','synthetic-production-api')
            with patch.object(DRIFT,'BASE',base),patch.object(sys,'argv',['drift.py',str(directory)]), \
                 patch.object(DRIFT,'project',side_effect=[[stage],[prod],[stage],[prod]]), \
                 patch.object(DRIFT,'docker',return_value='{"recovery_passed":false}') as call,redirect_stdout(io.StringIO()) as output:
                self.assertEqual(DRIFT.main(),0)
            self.assertEqual(call.call_count,1)
            args=call.call_args.args[0]
            self.assertEqual(args[0],'exec');self.assertEqual(args[4],stage['Id'])
            self.assertNotIn(prod['Id'],args)
            self.assertTrue(json.loads(output.getvalue())['production_container_identities_unchanged'])


if __name__=='__main__':unittest.main()
