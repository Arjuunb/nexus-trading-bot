"""Synthetic read-only fixtures, with no remote requests or application imports."""
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
SPEC=importlib.util.spec_from_file_location('startup_readonly',HERE/'vps-startup-readonly.py')
AUDIT=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(AUDIT)
FIRST='a'*20;SECOND='b'*20


class ProviderTests(unittest.TestCase):
    def test_canonical_aliases_identify_the_same_project(self):
        first={'SUPABASE_URL':'https://'+FIRST+'.supabase.co','SUPABASE_KEY':'synthetic-first-key'}
        second={'SUPABASE_URL':'HTTPS://'+FIRST.upper()+'.SUPABASE.CO.:443/','SUPABASE_KEY':'synthetic-second-key'}
        report=AUDIT.provider_identity(first,second)
        self.assertTrue(report['same_canonical_project'])
        self.assertFalse(report['distinct_canonical_projects_proven'])
        self.assertFalse(report['financial_credentials_equal'])

    def test_only_distinct_canonical_projects_prove_target_separation(self):
        first={'SUPABASE_URL':'https://'+FIRST+'.supabase.co/','SUPABASE_KEY':'synthetic-first-key'}
        second={'SUPABASE_URL':'https://'+SECOND+'.supabase.co','SUPABASE_KEY':'synthetic-second-key'}
        report=AUDIT.provider_identity(first,second)
        self.assertTrue(report['distinct_canonical_projects_proven'])
        self.assertFalse(report['remote_database_queries_performed'])
        self.assertEqual(report['effective_primary_instance_intent'],'NOT_CHECKED')
        serialized=json.dumps(report)
        self.assertNotIn(FIRST,serialized);self.assertNotIn('synthetic-first-key',serialized)

    def test_noncanonical_origins_never_prove_isolation(self):
        for value in ('http://'+FIRST+'.supabase.co','https://custom.example/',
                      'https://'+FIRST+'.supabase.co/rest/v1','https://'+FIRST+'.supabase.co?x=1',
                      'https://user:password@'+FIRST+'.supabase.co','https://'+FIRST+'.supabase.co:444',
                      'https://'+FIRST+'.supabase.co#fragment','https://'+FIRST+'.supabase.co:bad'):
            self.assertIsNone(AUDIT.canonical_project(value),value)

    def test_incomplete_pairs_remain_unproven(self):
        report=AUDIT.provider_identity({'SUPABASE_URL':'https://'+FIRST+'.supabase.co'}, {})
        self.assertFalse(report['distinct_canonical_projects_proven'])
        self.assertIn('INCOMPLETE',report['identity_status'])


class LocalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        for filename,prefix,mode in [('price_action_paper.db','pa','LIVE_PAPER'),('smc_strategy_paper.db','smc','HISTORICAL')]:
            with sqlite3.connect(self.root/filename) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE '+prefix+'_sessions(id TEXT,status TEXT,mode TEXT,private_owner TEXT)')
                db.execute('INSERT INTO '+prefix+'_sessions VALUES(?,?,?,?)',('synthetic-session','active',mode,'private-tenant-value'))
        with sqlite3.connect(self.root/'ledger.db') as db:
            db.execute('CREATE TABLE trading_instances(id TEXT,desired_running INTEGER)')
            db.execute("INSERT INTO trading_instances VALUES('synthetic-instance',1)")
        with sqlite3.connect(self.root/'shadow_research.db') as db:
            db.execute('CREATE TABLE shadow_registrations(desired_observing INTEGER)')
            db.execute('INSERT INTO shadow_registrations VALUES(0)')
        (self.root/'grid.json').write_text('{"running":false,"secret":"synthetic-private-secret"}')

    def tearDown(self):self.temp.cleanup()

    def run_child(self,extra_env=None,source=None):
        source=(source or AUDIT.CHILD).replace("root=Path('/var/lib/tradexa')",'root=Path('+repr(str(self.root))+')')
        env={**os.environ,'HUB_DATA_DIR':str(self.root)}
        for key in ('HUB_PRICE_ACTION_PAPER_DB','HUB_SMC_PAPER_DB','HUB_LEDGER_PATH','HUB_SHADOW_RESEARCH_DB','SUPABASE_URL','SUPABASE_KEY'):
            env.pop(key,None)
        env.update(extra_env or {})
        result=subprocess.run([sys.executable,'-B','-'],input=source,text=True,capture_output=True,env=env,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('private-tenant-value',result.stdout)
        self.assertNotIn('synthetic-private-secret',result.stdout)
        return json.loads(result.stdout)

    def test_live_intent_and_historical_session_are_distinguished(self):
        before={p.name:p.read_bytes() for p in self.root.glob('*.db')}
        report=self.run_child()
        self.assertTrue(report['price_action']['active_live_paper_observed'])
        self.assertFalse(report['smc']['active_live_paper_observed'])
        self.assertFalse(report['smc']['constructor_autoseed_requires_review'])
        self.assertTrue(report['local_instances']['desired_instance_observed'])
        self.assertFalse(report['local_shadow']['desired_observation_observed'])
        self.assertFalse(report['grid']['running'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.glob('*.db')})
        self.assertFalse(report['application_imported']);self.assertFalse(report['task6_complete'])

    def test_empty_sessions_flag_constructor_autoseeding_risk(self):
        with sqlite3.connect(self.root/'price_action_paper.db') as db:db.execute('DELETE FROM pa_sessions')
        report=self.run_child()
        self.assertTrue(report['price_action']['constructor_autoseed_requires_review'])
        self.assertFalse(report['price_action']['absence_of_new_paper_execution_proven'])

    def test_uncheckpointed_wal_state_is_read(self):
        with sqlite3.connect(self.root/'price_action_paper.db') as writer:
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("UPDATE pa_sessions SET mode='HISTORICAL'");writer.commit()
            self.assertGreater((self.root/'price_action_paper.db-wal').stat().st_size,0)
            self.assertFalse(self.run_child()['price_action']['active_live_paper_observed'])

    def test_interrupted_scan_is_unknown_rather_than_empty_or_safe(self):
        with sqlite3.connect(self.root/'price_action_paper.db') as db:
            db.execute('DELETE FROM pa_sessions')
            db.executemany('INSERT INTO pa_sessions VALUES(?,?,?,?)',
                          [(str(n),'stopped','HISTORICAL','synthetic-owner') for n in range(10000)])
        source=AUDIT.CHILD.replace('deadline=min(job_deadline,time.monotonic()+3)',
                                  'deadline=time.monotonic()-1')
        report=self.run_child(source=source)
        self.assertEqual(report['price_action']['status'],'UNVERIFIED')
        self.assertEqual(report['price_action']['sqlite_name'],'SQLITE_INTERRUPT')
        self.assertNotIn('active_session_observed',report['price_action'])

    def test_local_ledger_does_not_replace_configured_primary_authority(self):
        report=self.run_child({'SUPABASE_URL':'https://'+FIRST+'.supabase.co','SUPABASE_KEY':'synthetic-key'})
        self.assertFalse(report['local_instances']['is_primary_authority'])
        self.assertFalse(report['remote_database_queries_performed'])
        self.assertEqual(report['local_shadow']['postgres_or_other_effective_backend'],'NOT_CHECKED')

    def test_missing_file_is_not_created(self):
        path=self.root/'smc_strategy_paper.db';path.unlink()
        report=self.run_child()
        self.assertEqual(report['smc']['status'],'UNVERIFIED')
        self.assertFalse(path.exists())

    def test_external_path_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            target=Path(other)/'other.db';target.write_bytes(b'not a staging DB')
            report=self.run_child({'HUB_SMC_PAPER_DB':str(target)})
            self.assertEqual(report['smc']['status'],'UNVERIFIED')
            alias=self.root/'alias.db';alias.symlink_to(target)
            report=self.run_child({'HUB_SMC_PAPER_DB':str(alias)})
            self.assertEqual(report['smc']['status'],'UNVERIFIED')

    def test_grid_truthiness_and_remote_fallback_are_honest(self):
        (self.root/'grid.json').write_text('{"running":"false"}')
        self.assertTrue(self.run_child()['grid']['running'])
        (self.root/'grid.json').unlink()
        self.assertTrue(self.run_child()['grid']['remote_fallback_possible'])

    def test_read_only_connections_reject_an_attempted_write(self):
        source=AUDIT.CHILD.replace('rows=db.execute(sql).fetchall()',
             'db.execute("CREATE TABLE audit_must_not_write(id INTEGER)")\n            rows=db.execute(sql).fetchall()')
        report=self.run_child(source=source)
        self.assertEqual(report['price_action']['status'],'UNVERIFIED')
        self.assertEqual(report['price_action']['sqlite_name'],'SQLITE_READONLY')
        with sqlite3.connect(self.root/'price_action_paper.db') as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='audit_must_not_write'").fetchone())


class HostBoundaryTests(unittest.TestCase):
    def row(self,project,service,source):
        return {'Id':project+'-'+service,'Name':'/'+project+'-'+service+'-1',
                'Image':'synthetic-image','RestartCount':0,
                'Config':{'Labels':{'com.docker.compose.project':project,'com.docker.compose.service':service},
                          'Env':[k+'=0' for k in ('HUB_AUTO_ENGINE','HUB_ENABLE_EXTERNAL_LIVE','HUB_USE_LIVE_DATA','HUB_RESEARCH_AUTOSTART','NEXUS_SHADOW_SUPERVISED')]},
                'HostConfig':{'Memory':1610612736,'MemorySwap':1610612736},
                'State':{'StartedAt':'synthetic-start','Status':'running','Health':{'Status':'healthy'}},
                'Mounts':[{'Source':source}]}

    def test_shared_production_storage_blocks_before_any_exec(self):
        stage=self.row(AUDIT.STAGE,'api','same-source');prod=self.row(AUDIT.PROD,'app','same-source')
        with patch.object(sys,'argv',['audit.py']),patch.object(AUDIT,'project',side_effect=[[stage],[prod]]),patch.object(AUDIT,'docker') as call:
            self.assertRaisesRegex(AUDIT.Blocked,'SHARE_STORAGE',AUDIT.main)
            call.assert_not_called()

    def test_memory_override_is_enforced_before_any_exec(self):
        stage=self.row(AUDIT.STAGE,'api','staging-source');prod=self.row(AUDIT.PROD,'app','production-source')
        stage['HostConfig']['MemorySwap']=0
        with patch.object(sys,'argv',['audit.py']),patch.object(AUDIT,'project',side_effect=[[stage],[prod]]),patch.object(AUDIT,'docker') as call:
            self.assertRaisesRegex(AUDIT.Blocked,'1536_MIB',AUDIT.main)
            call.assert_not_called()

    def test_only_staging_exec_is_used_and_report_never_claims_acceptance(self):
        stage=self.row(AUDIT.STAGE,'api','staging-source');prod=self.row(AUDIT.PROD,'app','production-source')
        with patch.object(sys,'argv',['audit.py']),patch.object(AUDIT,'project',side_effect=[[stage],[prod],[stage],[prod]]), \
             patch.object(AUDIT,'docker',return_value='{"status":"synthetic-local-evidence"}') as call,redirect_stdout(io.StringIO()) as output:
            self.assertEqual(AUDIT.main(),0)
        self.assertEqual(call.call_count,1)
        self.assertEqual(call.call_args.args[0],['exec','-i',stage['Id'],'python','-B','-'])
        report=json.loads(output.getvalue());self.assertFalse(report['task6_complete'])
        self.assertFalse(report['services_updated']);self.assertTrue(report['production_container_identities_unchanged'])
        self.assertTrue(report['remaining_checks'])

    def test_stopped_replay_does_not_replace_or_ambiguate_running_production_app(self):
        stage=self.row(AUDIT.STAGE,'api','staging-source');prod=self.row(AUDIT.PROD,'app','production-source')
        replay=self.row(AUDIT.PROD,'app','production-source')
        replay.update(Id='synthetic-stopped-replay',Name='/synthetic-stopped-replay')
        replay['State']['Status']='exited'
        with patch.object(sys,'argv',['audit.py']), \
             patch.object(AUDIT,'project',side_effect=[[stage],[replay,prod],[stage],[replay,prod]]), \
             patch.object(AUDIT,'docker',return_value='{"status":"synthetic-local-evidence"}') as call, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(AUDIT.main(),0)
        self.assertEqual(call.call_args.args[0][2],stage['Id'])
        report=json.loads(output.getvalue())
        self.assertEqual(report['production_app_selection']['stopped_app_containers_ignored'],1)
        self.assertTrue(report['production_app_selection']['all_containers_retained_in_identity_snapshot'])

    def test_unexpected_running_app_does_not_substitute_for_canonical_production_app(self):
        stage=self.row(AUDIT.STAGE,'api','staging-source');alias=self.row(AUDIT.PROD,'app','production-source')
        alias['Name']='/synthetic-replay-running'
        with patch.object(sys,'argv',['audit.py']), \
             patch.object(AUDIT,'project',side_effect=[[stage],[alias]]),patch.object(AUDIT,'docker') as call:
            self.assertRaisesRegex(AUDIT.Blocked,'CANONICAL',AUDIT.main)
            call.assert_not_called()

    def test_second_running_app_remains_ambiguous_and_blocks_before_exec(self):
        stage=self.row(AUDIT.STAGE,'api','staging-source');prod=self.row(AUDIT.PROD,'app','production-source')
        extra=self.row(AUDIT.PROD,'app','other-production-source')
        extra.update(Id='synthetic-second-running-app',Name='/synthetic-second-running-app')
        with patch.object(sys,'argv',['audit.py']), \
             patch.object(AUDIT,'project',side_effect=[[stage],[prod,extra]]),patch.object(AUDIT,'docker') as call:
            self.assertRaises(AUDIT.Blocked,AUDIT.main)
            call.assert_not_called()

    def test_stopped_replay_is_still_included_in_change_detection(self):
        stage=self.row(AUDIT.STAGE,'api','staging-source');prod=self.row(AUDIT.PROD,'app','production-source')
        replay=self.row(AUDIT.PROD,'app','production-source')
        replay.update(Id='synthetic-replay',Name='/synthetic-stopped-replay');replay['State']['Status']='exited'
        changed=dict(replay,Image='changed-replay-image')
        with patch.object(sys,'argv',['audit.py']), \
             patch.object(AUDIT,'project',side_effect=[[stage],[prod,replay],[stage],[prod,changed]]), \
             patch.object(AUDIT,'docker',return_value='{"status":"synthetic-local-evidence"}') as call:
            self.assertRaisesRegex(AUDIT.Blocked,'CHANGED_DURING',AUDIT.main)
            self.assertEqual(call.call_count,1)


if __name__=='__main__':unittest.main()
