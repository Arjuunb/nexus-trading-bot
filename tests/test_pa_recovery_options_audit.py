"""Synthetic recovery-option evidence; never contacts real Docker or a VPS."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SPEC=importlib.util.spec_from_file_location('options_audit',Path(__file__).parents[1]/'ops/audit-pa-recovery-options.py')
M=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(M)


def containers():
    rows=[]
    for scope,(project,service) in M.AUDIT.PROJECTS.items():
        rows.append({'Id':scope,'Name':'/'+scope,'Image':'sha256:'+scope,
            'Config':{'Labels':{'com.docker.compose.project':project,'com.docker.compose.service':service},
                      'Env':['BACKUP_PASSWORD=PRIVATE_PASSWORD']},
            'HostConfig':{'Memory':1610612736,'MemorySwap':1610612736,'RestartPolicy':{'Name':'unless-stopped'}},
            'State':{'Status':'running','StartedAt':'synthetic'},
            'Mounts':[{'Source':'/synthetic/'+scope,'Destination':'/var/lib/tradexa','Name':scope+'_volume','RW':True}]})
    replay=deepcopy(rows[1]);replay.update(Id='replay',Name='/pa-replay-2025')
    replay['State']['Status']='exited';rows.append(replay)
    return rows


def fake_docker(rows,calls,*,missing=False,changed=False):
    inspections=0
    def command(args):
        nonlocal inspections
        calls.append(args)
        if args[0]=='ps':return 'staging production replay'
        if args[0]=='inspect':
            inspections+=1
            result=deepcopy(rows)
            if changed and inspections>1:result[0]['State']['StartedAt']='different'
            return json.dumps(result)
        if args[0]=='exec':return json.dumps({'databases':[],'financial_rows_read':False})
        if args[:2]==['image','inspect']:
            if missing:raise RuntimeError('synthetic image missing')
            return json.dumps([{'Id':args[2],'Config':{'Labels':{'org.opencontainers.image.revision':'reviewed-source',
                'nexus.source.sha256':'source-fingerprint','private':'PRIVATE_LABEL'}}}])
        raise AssertionError('Unexpected Docker operation')
    return command


def test_readonly_evidence_checks_image_and_retains_stopped_consumers_without_secrets():
    calls=[];report=M.collect(fake_docker(containers(),calls),host=lambda:{'snapshot_support':'UNVERIFIED'})
    assert report['container_identities_and_mounts_unchanged'] is True
    assert report['backup_restore_proven'] is False
    assert report['services_changed'] is False and report['files_deleted'] is False
    assert report['provisioning_performed'] is False
    assert report['environments'][0]['image']['available'] is True
    assert report['environments'][0]['memory_limit_bytes']==1610612736
    assert report['environments'][1]['consumers'][1]['status']=='exited'
    assert not any(c[0]=='exec' and c[1]=='replay' for c in calls)
    assert {c[0] for c in calls}=={'ps','inspect','exec','image'}
    assert 'PRIVATE_PASSWORD' not in json.dumps(report)
    assert 'PRIVATE_LABEL' not in json.dumps(report)


def test_missing_rollback_image_is_reported_unverified_without_pull():
    calls=[];report=M.collect(fake_docker(containers(),calls,missing=True),host=lambda:{})
    assert all(r['image']['available'] is False for r in report['environments'])
    assert all(c[0] in {'ps','inspect','exec','image'} for c in calls)


def test_changed_container_identity_blocks_report():
    with pytest.raises(RuntimeError,match='CONTAINER_STATE_CHANGED'):
        M.collect(fake_docker(containers(),[],changed=True),host=lambda:{})


def test_two_running_apis_are_not_selected():
    rows=containers();rows[-1]['State']['Status']='running'
    with pytest.raises(RuntimeError,match='ONE_RUNNING_EXISTING_API_REQUIRED'):
        M.collect(fake_docker(rows,[]),host=lambda:{})


def test_schedule_scan_reports_counts_without_printing_commands_or_keys():
    result=M.schedule_summary('# private comment\n0 2 * * * nexus price_action --token=SECRET_TOKEN\n')
    assert result['explicit_pa_matches']==1 and result['nexus_or_shared_matches']==1
    assert result['commands_printed'] is False
    assert 'SECRET_TOKEN' not in json.dumps(result)


def test_real_child_reads_live_wal_metadata_and_backup_sizes_without_financial_rows(tmp_path):
    paper=tmp_path/'price_action_paper.db'
    db=sqlite3.connect(paper);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
    db.execute('CREATE TABLE balances(value TEXT)');db.execute("INSERT INTO balances VALUES('PRIVATE_BALANCE')");db.commit()
    backup=tmp_path/'backups'/'20261022T000000Z';backup.mkdir(parents=True)
    (backup/'existing.sqlite.gz').write_bytes(b'unchanged-private-archive')
    module=tmp_path/'backup_module.py';module.write_text('KEEP=7\nraise RuntimeError("DO_NOT_IMPORT")\n')
    code=M.CHILD.replace("root=Path('/var/lib/tradexa')",'root=Path('+repr(str(tmp_path))+')')
    code=code.replace("Path('/app/automation-hub/services/backup.py')",'Path('+repr(str(module))+')')
    prefix="import urllib.request\ndef offline(*args,**kwargs):raise OSError('offline boundary')\nurllib.request.urlopen=offline\n"
    env={k:v for k,v in os.environ.items() if not k.startswith(('HUB_','SUPABASE_','BACKUP_'))}
    env.update(BACKUP_PASSWORD='PRIVATE_PASSWORD',SUPABASE_URL='https://synthetic.invalid',SUPABASE_KEY='PRIVATE_REMOTE_KEY')
    try:
        result=subprocess.run([sys.executable,'-B','-c',prefix+code],env=env,text=True,capture_output=True,timeout=15,check=True)
        report=json.loads(result.stdout)
        row=next(r for r in report['databases'] if r['binding']=='HUB_PRICE_ACTION_PAPER_DB')
        assert row['logical_bytes']>row['bytes'] and row['wal_bytes']>0
        assert report['retention_policy']['default_keep']==7
        assert report['backup_directories'][0]['files']==1
        assert report['backup_directories'][0]['bytes']==len(b'unchanged-private-archive')
        assert report['financial_rows_read'] is False and report['application_imported'] is False
        assert report['backup_or_restore_performed'] is False
        assert not (tmp_path/'ledger.db').exists()
        assert all(secret not in result.stdout for secret in ('PRIVATE_BALANCE','PRIVATE_PASSWORD','PRIVATE_REMOTE_KEY'))
    finally:db.close()
