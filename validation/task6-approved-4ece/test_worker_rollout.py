"""Synthetic boundary and real SQLite tests; no customer data or VPS claims."""
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('worker_rollout', HERE/'vps-worker-rollout.py')
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)


def row():
    return {'Config': {'User': '100:100', 'Env': ['NEXUS_RESEARCH_WORKER_DSN=postgresql://nexus_staging_worker:fixture@postgres:5432/nexus_staging_metadata',
        'NEXUS_OPERATIONAL_SPOOL_PATH=/observation/research-operational-spool.sqlite'],
        'Entrypoint': ['python','-m','services.research_worker_main'],
        'Cmd': ['--backend','postgresql','--health-file','/observation/research-health.json']},
        'HostConfig': {'Memory': 536870912, 'MemorySwap': 1073741824, 'ReadonlyRootfs': True,
        'CapDrop': ['ALL'], 'Privileged': False, 'PidsLimit':64, 'NanoCpus':1000000000,
        'SecurityOpt':['no-new-privileges:true'], 'PortBindings':{}},
        'Mounts':[{'Type':'volume','Name':W.STAGE+'_research-state','Source':'fixture-research',
          'Destination':'/observation','RW':True}, {'Type':'volume','Name':W.STAGE+'_datasets',
          'Source':'fixture-datasets','Destination':'/datasets','RW':False}],
        'NetworkSettings':{'Networks':{W.STAGE+'_metadata':{}}}}


class Safety(unittest.TestCase):
    def test_existing_bounded_worker_is_accepted(self):
        self.assertEqual(W.validate_worker(row()), W.STAGE+'_metadata')

    def test_financial_mount_extra_network_root_and_credentials_are_rejected(self):
        mutations = [lambda c:c['Mounts'].append({'Destination':'/var/lib/tradexa'}),
            lambda c:c['NetworkSettings']['Networks'].update({'production':{}}),
            lambda c:c['Config'].update(User='0:0'),
            lambda c:c['Config']['Env'].append('SUPABASE_KEY=fixture-secret'),
            lambda c:c['HostConfig'].update(ReadonlyRootfs=False),
            lambda c:c['HostConfig'].update(Memory=0),
            lambda c:c['HostConfig'].update(Privileged=True),
            lambda c:c['HostConfig'].update(PortBindings={'8000/tcp':[]}),
            lambda c:c['Config'].update(Cmd=['--once'])]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                c=row();mutation(c)
                with self.assertRaises(W.Blocked):W.validate_worker(c)

    def test_credential_target_cannot_be_swapped(self):
        for dsn in ('postgresql://postgres:x@postgres/nexus_staging_metadata',
                    'postgresql://nexus_staging_worker:x@external/nexus_staging_metadata',
                    'postgresql://nexus_staging_worker:x@postgres/production'):
            c=row();c['Config']['Env'][0]='NEXUS_RESEARCH_WORKER_DSN='+dsn
            with self.assertRaises(W.Blocked):W.validate_worker(c)

    def test_only_worker_image_and_pull_policy_may_change(self):
        before={'name':W.STAGE,'services':{'api':{'mem_limit':1610612736,'memswap_limit':1610612736},
          'research-worker':{'image':'old','user':'100:100'}}}
        after=deepcopy(before);after['services']['research-worker'].update(image='new',pull_policy='never')
        W.validate_render(before,after,'new')
        for mutation in (lambda a:a['services']['api'].update(mem_limit=768*1024**2),
                         lambda a:a['services']['research-worker'].update(user='1000:1000'),
                         lambda a:a.update(name='another-project')):
            bad=deepcopy(after);mutation(bad)
            with self.assertRaises(W.Blocked):W.validate_render(before,bad,'new')

    def test_compose_json_byte_counts_are_decimal_strings(self):
        # compose-go UnitBytes.MarshalJSON emits quoted decimal byte counts.
        # Docker inspect separately emits integer HostConfig.Memory values.
        for memory,swap in [('1610612736','1610612736'),('1610612736',1610612736),
                            (1610612736,'1610612736')]:
            with self.subTest(memory=memory,swap=swap):
                before={'name':W.STAGE,'services':{'api':{'mem_limit':memory,'memswap_limit':swap},
                  'research-worker':{'image':'old'}}}
                after=deepcopy(before);after['services']['research-worker'].update(image='new',pull_policy='never')
                W.validate_render(before,after,'new')

    def test_normalizing_compose_bytes_cannot_accept_weaker_or_unlimited_memory(self):
        for value in ('805306368','0','-1','1536m','1.5g','1610612736.0',1610612736.0,True,None):
            for key in ('mem_limit','memswap_limit'):
                with self.subTest(value=value,key=key):
                    before={'name':W.STAGE,'services':{'api':{'mem_limit':'1610612736','memswap_limit':'1610612736'},
                      'research-worker':{'image':'old'}}}
                    before['services']['api'][key]=value
                    after=deepcopy(before);after['services']['research-worker'].update(image='new',pull_policy='never')
                    with self.assertRaises(W.Blocked):W.validate_render(before,after,'new')

    def test_normalizing_limits_does_not_hide_changes_to_rendered_api(self):
        before={'name':W.STAGE,'services':{'api':{'mem_limit':'1610612736','memswap_limit':'1610612736'},
          'research-worker':{'image':'old'}}}
        after=deepcopy(before);after['services']['research-worker'].update(image='new',pull_policy='never')
        after['services']['api']['mem_limit']=1610612736
        with self.assertRaisesRegex(W.Blocked,'ONLY_WORKER_IMAGE'):
            W.validate_render(before,after,'new')

    def test_targeted_commands_cannot_recreate_dependencies_or_pull(self):
        args=W.up_args(['compose','--project-name',W.STAGE])
        self.assertEqual(args[-1],'research-worker')
        self.assertIn('--no-deps',args);self.assertIn('--no-build',args)
        self.assertEqual(args[args.index('--pull')+1],'never')
        self.assertNotIn('down',args);self.assertNotIn('--volumes',args)

    def test_memory_override_is_last_for_deploy_and_rollback(self):
        for overlay in ('new.yaml','rollback.yaml'):
            args=W.compose_args(Path(overlay))
            self.assertEqual(args[-1],str(W.MEMORY))
            self.assertEqual(args[args.index('--project-name')+1],W.STAGE)

    def test_changed_compose_files_cannot_be_used_as_a_false_rollback(self):
        old=row();old['Config']['Labels']={'com.docker.compose.config-hash':'a'*64}
        W.validate_live_hash(old,'research-worker '+'a'*64+'\n')
        for output in ('research-worker '+'b'*64,'api '+'a'*64,''):
            with self.assertRaises(W.Blocked):W.validate_live_hash(old,output)

    def test_database_probe_rejects_queued_jobs_and_wrong_authority(self):
        good={'authority_id':'a'*32,'purpose':'research_shadow_metadata','version':1,
          'writer_role':'nexus_staging_worker','database':'nexus_staging_metadata',
          'role':'nexus_staging_worker','unsafe':False,'financial_tables':0,'tables':37,'pending':0,
          'read_only':True}
        W.validate_database(good)
        for key,value in [('pending',1),('unsafe',True),('financial_tables',1),('tables',1),('read_only',False)]:
            bad=good|{key:value}
            with self.assertRaises(W.Blocked):W.validate_database(bad)
        with self.assertRaises(W.Blocked):W.validate_database(good,'b'*32)

    def test_postcheck_rejects_changed_credentials_or_mounts(self):
        old=row();new=deepcopy(old)
        old_image={'Config':{'Env':[]}}
        new_image={'Config':{'Env':['PYTHON_VERSION=3.12.15']}}
        new['Config']['Env'].append('PYTHON_VERSION=3.12.15')
        W.compare_runtime(old,new,old_image,new_image)
        new['Config']['Env'].append('HUB_PASSWORD=secret')
        with self.assertRaises(W.Blocked):W.compare_runtime(old,new,old_image,new_image)


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir()
        namespace={'__name__':'unit_test'};exec(W.SNAPSHOT_CODE,namespace)
        self.capture=namespace['capture']
    def tearDown(self):self.temp.cleanup()

    def database(self):
        db=sqlite3.connect(self.source/'spool.sqlite')
        db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY, payload BLOB)')
        db.execute('INSERT INTO events VALUES(1,?)',(b'fixture',));db.commit()
        return db

    def test_online_backup_includes_uncheckpointed_wal_and_restores(self):
        db=self.database()
        (self.source/'research-health.json').write_text('{"fixture":true}')
        payload=self.capture(self.source)
        out=self.root/'restored';out.mkdir()
        proof=W.restore_snapshot(payload,out)
        with sqlite3.connect(out/'spool.sqlite') as restored:
            self.assertEqual(restored.execute('SELECT payload FROM events').fetchone()[0],b'fixture')
        self.assertEqual(proof['sqlite_databases'],1)
        self.assertTrue(proof['full_integrity_and_foreign_keys_passed'])
        self.assertTrue((self.source/'spool.sqlite-wal').exists());db.close()

    def test_missing_operational_database_blocks(self):
        with self.assertRaises(ValueError):self.capture(self.source)

    def test_external_symlink_is_rejected(self):
        db=self.database();(self.source/'escape').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):self.capture(self.source)
        db.close()

    def test_corrupt_copy_and_traversal_fail_before_restoration(self):
        db=self.database();payload=self.capture(self.source)
        output=self.root/'restore';output.mkdir()
        corrupt=deepcopy(payload);corrupt['files'][0]['sha256']='0'*64
        with self.assertRaises(W.Blocked):W.restore_snapshot(corrupt,output)
        corrupt=deepcopy(payload);corrupt['files'][0]['path']='../escape'
        with self.assertRaises(W.Blocked):W.restore_snapshot(corrupt,output)
        self.assertFalse((self.root/'escape').exists());db.close()


class Recovery(unittest.TestCase):
    def test_failed_update_attempt_is_followed_by_rollback(self):
        actions=[]
        def change():actions.append('update');raise W.Blocked('fixture-failure')
        def rollback():actions.append('rollback');return {'healthy':True}
        result=W.apply_with_rollback(change,rollback)
        self.assertEqual(actions,['update','rollback'])
        self.assertEqual(result['status'],'WORKER_UPDATE_FAILED_ROLLED_BACK')
        self.assertFalse(result['task6_complete'])

    def test_failed_rollback_is_reported_as_failure(self):
        def fail():raise W.Blocked('fixture')
        result=W.apply_with_rollback(fail,fail)
        self.assertEqual(result['status'],'WORKER_UPDATE_AND_ROLLBACK_BLOCKED')
        self.assertNotEqual(result.get('rollback_status'),'PASSED')

    def test_success_does_not_claim_full_prd_acceptance(self):
        result=W.apply_with_rollback(lambda:{'healthy':True},lambda:self.fail('unexpected rollback'))
        self.assertEqual(result['status'],'RESEARCH_WORKER_DEPLOYED_AND_RESTART_VERIFIED')
        self.assertFalse(result['task6_complete']);self.assertFalse(result['api_replaced'])


class Orchestration(unittest.TestCase):
    """Exercise the complete controller with a simulated Docker boundary."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.old=row()|{'Id':'old-worker','Name':'/'+W.WORKER,'Image':W.OLD_IMAGE,
          'RestartCount':0,'State':{'Status':'running','StartedAt':'before','OOMKilled':False,'Health':{'Status':'healthy'}}}
        self.old['Config']['Labels']={'com.docker.compose.service':'research-worker',
          'com.docker.compose.config-hash':'a'*64}
        self.worker=deepcopy(self.old);self.other={'Id':'untouched-api','Name':'/api','Config':{'Labels':{}}}
        self.calls=[];self.fail_update=False;self.drift=False
        self.image={'Id':W.NEW_IMAGE.split('@')[1],'RepoDigests':[W.NEW_IMAGE],'Config':{'Env':[]}}
        self.old_image={'Id':W.OLD_IMAGE,'Config':{'Env':[]}}
        self.proof={'authority_id':'a'*32,'purpose':'research_shadow_metadata','version':1,
          'writer_role':'nexus_staging_worker','database':'nexus_staging_metadata',
          'role':'nexus_staging_worker','unsafe':False,'financial_tables':0,'tables':37,'pending':0,'read_only':True}
        state=self.root/'source';state.mkdir()
        with sqlite3.connect(state/'spool.sqlite') as db:
            db.execute('CREATE TABLE fixture(id INTEGER PRIMARY KEY)');db.execute('INSERT INTO fixture VALUES(1)')
        ns={'__name__':'unit'};exec(W.SNAPSHOT_CODE,ns);self.snapshot=ns['capture'](state)
        self.preload=SimpleNamespace(verify_packet=lambda p:{'exports':{'worker':{'config_id':'config',
          'export_oci_manifest_digest':self.image['Id']}},'scans':{}},
          preflight=lambda command:{'production':'prod-unchanged'},
          containers=lambda project,command:[deepcopy(self.worker),deepcopy(self.other)],
          fingerprint=lambda rows:json.dumps(rows,sort_keys=True),inspect_loaded=lambda *args:(self.image,'OCI_MANIFEST'),
          POLICY={'commit':'fixture-commit','source_sha256':'fixture-source'},RESERVE=0)
        def backup():
            self.calls.append(['PG_BACKUP_AND_DETACHED_RESTORE'])
            print(json.dumps({'status':'POSTGRES_BACKUP_AND_DETACHED_RESTORE_PASSED'}));return 0
        self.pg=SimpleNamespace(main=backup)
        self.paths=[self.root/'base.yaml',self.root/'customer.yaml'];self.memory=self.root/'memory.yaml';self.env=self.root/'compose.env'
        for path in [*self.paths,self.memory,self.env]:path.write_text('fixture')
        self.patches=[patch.object(W,'native',self.native),patch.object(W,'CONFIGS',self.paths),
          patch.object(W,'MEMORY',self.memory),patch.object(W,'ENV_FILE',self.env),patch.object(W,'BASE',self.root)]
        for p in self.patches:p.start()
        self.directory=self.root/'receipt';self.directory.mkdir()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def native(self,args,**kwargs):
        self.calls.append(args)
        if args[:2]==['network','inspect']:
            return json.dumps([{'Internal':True,'Labels':{'com.docker.compose.project':W.STAGE}}])
        if args[:2]==['image','inspect']:return json.dumps([self.old_image])
        if args[0]=='inspect':return json.dumps([self.worker])
        if args[0]=='run':return json.dumps(self.proof)
        if args[0]=='exec':
            if args[-1]==W.DB_PROBE:return json.dumps(self.proof)
            if args[-1]==W.SNAPSHOT_CODE:return json.dumps(self.snapshot)
            return json.dumps({'readiness':'READY','database_status':'AVAILABLE','backend':'postgresql',
              'concurrency':1,'execution_runtime_loaded':False,'metadata_authority_id':'a'*32})
        if args[0]=='compose':
            if '--hash' in args:return 'research-worker '+('b' if self.drift else 'a')*64
            if 'config' in args:
                model={'name':W.STAGE,'services':{'api':{'mem_limit':'1610612736','memswap_limit':'1610612736'},
                  'research-worker':{'image':'old-reference','user':'100:100'}}}
                overlays=[Path(args[i+1]) for i,v in enumerate(args[:-1]) if v=='-f' and args[i+1] not in {str(p) for p in [*self.paths,self.memory]}]
                for overlay in overlays:model['services']['research-worker'].update(json.loads(overlay.read_text())['services']['research-worker'])
                return json.dumps(model)
            if 'up' in args:
                rollback=any(str(arg).endswith('compose.rollback.yaml') for arg in args)
                self.worker=deepcopy(self.old)
                if not rollback:
                    self.worker['Image']=self.image['Id'];self.worker['Id']='approved-worker';self.worker['State']['StartedAt']='deployed'
                    if self.fail_update:raise W.Blocked('SIMULATED_FAILURE_AFTER_REPLACEMENT')
                return ''
            if 'restart' in args:self.worker['State']['StartedAt']='restarted';return ''
        raise AssertionError('Unexpected Docker command: '+repr(args))

    def test_complete_flow_backups_precede_update_and_only_worker_is_restarted(self):
        result=W.rollout(self.root/'fixture.zip',self.preload,self.pg,self.directory)
        self.assertEqual(result['status'],'RESEARCH_WORKER_DEPLOYED_AND_RESTART_VERIFIED')
        self.assertTrue(result['restart_verified']);self.assertFalse(result['task6_complete'])
        commands=[c for c in self.calls if 'up' in c or 'restart' in c]
        self.assertEqual(len(commands),2)
        self.assertTrue(all(c[-1]=='research-worker' and '--no-deps' in c for c in commands))
        self.assertLess(self.calls.index(['PG_BACKUP_AND_DETACHED_RESTORE']),self.calls.index(commands[0]))
        self.assertTrue((self.directory/'research-state/spool.sqlite').is_file())
        self.assertTrue((self.directory/'post-restart-research-state/spool.sqlite').is_file())

    def test_failure_after_replacement_restores_original_worker_image(self):
        self.fail_update=True
        result=W.rollout(self.root/'fixture.zip',self.preload,self.pg,self.directory)
        self.assertEqual(result['status'],'WORKER_UPDATE_FAILED_ROLLED_BACK')
        self.assertEqual(result['rollback_status'],'PASSED');self.assertEqual(self.worker['Image'],W.OLD_IMAGE)

    def test_configuration_drift_blocks_before_backups_or_service_updates(self):
        self.drift=True
        with self.assertRaisesRegex(W.Blocked,'CONFIGURATION_DRIFT'):
            W.rollout(self.root/'fixture.zip',self.preload,self.pg,self.directory)
        self.assertFalse(any('up' in c or 'restart' in c or c[0]=='PG_BACKUP_AND_DETACHED_RESTORE' for c in self.calls))

    def test_new_image_wrong_credentials_or_authority_block_before_updates(self):
        self.proof['unsafe']=True
        with self.assertRaises(W.Blocked):W.rollout(self.root/'fixture.zip',self.preload,self.pg,self.directory)
        self.assertFalse(any('up' in c for c in self.calls))

    def test_failed_database_recovery_blocks_before_service_update(self):
        def fail():print('{"status":"BLOCKED","reason":"FIXTURE_RESTORE_FAILURE"}');return 1
        self.pg.main=fail
        with self.assertRaisesRegex(W.Blocked,'FRESH_POSTGRES_RECOVERY_FAILED'):
            W.rollout(self.root/'fixture.zip',self.preload,self.pg,self.directory)
        self.assertFalse(any('up' in c for c in self.calls))

    def test_unrelated_container_change_blocks_before_worker_update(self):
        real_backup=self.pg.main
        def drift():
            result=real_backup();self.other['Id']='changed-api';return result
        self.pg.main=drift
        with self.assertRaisesRegex(W.Blocked,'NON_WORKER_STAGING_CONTAINER_CHANGED'):
            W.rollout(self.root/'fixture.zip',self.preload,self.pg,self.directory)
        self.assertFalse(any('up' in c for c in self.calls))


class TerminalLoss(unittest.TestCase):
    def test_closed_terminal_does_not_abort_deployment_or_rollback_logging(self):
        import io
        class Closed:
            def write(self,_):raise BrokenPipeError('fixture-disconnect')
        private=io.StringIO();output=W.DurableOutput(Closed(),private)
        output.write('update\n');output.write('rollback\n');output.flush()
        self.assertEqual(private.getvalue(),'update\nrollback\n')


if __name__=='__main__':unittest.main()
