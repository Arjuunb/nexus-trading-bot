"""Release inputs and readiness are evidence contracts, never trading gates."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from services.deployment_readiness import deployment_readiness
from services.release_source import bundle_source

T = datetime(2026, 10, 13, tzinfo=timezone.utc)


def metadata():
    return {'backend':'postgresql','research_authority':'original-authority',
            'shadow_authority':'original-authority','available':True}


def health_file(tmp_path, **changes):
    p=tmp_path/'worker.json'
    body={'schema_version':'shadow-worker-health.v1','recorded_at':T.isoformat(),
          'readiness':'READY','database_status':'AVAILABLE','backend':'postgresql',
          'metadata_authority_id':'original-authority','execution_runtime_loaded':False,
          'pid':123,'process_start_ticks':'original-generation'}
    p.write_text(json.dumps({**body,**changes}));return p


def test_http_liveness_does_not_manufacture_readiness_or_financial_evidence():
    r=deployment_readiness(owner_id='alice',auth_verified=True,now=T)
    assert r['checks']['HTTP_LIVE']['status']=='READY'
    assert r['checks']['AUTH_READY']['status']=='READY'
    assert r['checks']['POSTGRES_READY']['status']=='UNKNOWN'
    assert r['checks']['FEED_READY']['status']=='BLOCKED'
    assert r['checks']['FINANCIAL_EVIDENCE_READY']['status']=='UNKNOWN'
    assert r['execution_authorization'] is False and r['production_certification'] is False
    assert set(r['checks'])=={'HTTP_LIVE','API_READY','AUTH_READY','POSTGRES_READY',
        'RESEARCH_WORKER_READY','SHADOW_WORKER_READY','FEED_READY',
        'FINANCIAL_EVIDENCE_READY','OPERATIONAL_SPOOL_READY'}
    for row in r['checks'].values():
        assert set(row)>={'status','observed_at','source','authority_identity','reason','evidence_reference'}


def test_same_postgres_authority_is_required():
    r=deployment_readiness(owner_id='alice',auth_verified=True,metadata_probe=metadata,now=T)
    assert r['checks']['POSTGRES_READY']['status']=='READY'
    assert r['checks']['API_READY']['status']=='READY'
    bad=lambda:{**metadata(),'shadow_authority':'different-authority'}
    r=deployment_readiness(owner_id='alice',metadata_probe=bad,now=T)
    assert r['checks']['POSTGRES_READY']['status']=='BLOCKED'
    assert r['checks']['POSTGRES_READY']['reason']=='METADATA_AUTHORITY_MISMATCH'


def test_database_failure_is_sanitized_and_never_falls_back():
    calls=[]
    def unavailable():
        calls.append(1);raise RuntimeError('postgresql://secret:password@private-host/db')
    r=deployment_readiness(owner_id='alice',metadata_probe=unavailable,now=T)
    assert calls==[1] and r['checks']['POSTGRES_READY']['status']=='BLOCKED'
    assert 'password' not in json.dumps(r) and 'private-host' not in json.dumps(r)


@pytest.mark.parametrize('changes,expected',[
    ({},'READY'),({'recorded_at':(T-timedelta(seconds=30)).isoformat()},'BLOCKED'),
    ({'metadata_authority_id':'other'},'BLOCKED'),({'database_status':'UNAVAILABLE'},'BLOCKED'),
    ({'execution_runtime_loaded':True},'BLOCKED'),({'backend':'sqlite'},'BLOCKED'),
    ({'metadata_authority_id':None},'UNKNOWN'),({'readiness':'STOPPED'},'BLOCKED'),
])
def test_worker_heartbeat_requires_fresh_identity_and_matching_authority(tmp_path,changes,expected):
    p=health_file(tmp_path,**changes)
    r=deployment_readiness(owner_id='alice',metadata_probe=metadata,worker_paths={'research':p},now=T)
    row=r['checks']['RESEARCH_WORKER_READY'];assert row['status']==expected
    assert str(p) not in json.dumps(r) and 'pid' not in row
    if expected=='READY':assert row['process_verification']=='RECENT_SELF_REPORT; NOT_EXTERNAL_PID_CERTIFICATION'


def test_disabled_shadow_cannot_be_ready_even_with_a_health_file(tmp_path):
    p=health_file(tmp_path)
    r=deployment_readiness(owner_id='alice',metadata_probe=metadata,worker_paths={'shadow':p},now=T)
    assert r['checks']['SHADOW_WORKER_READY']['status']=='BLOCKED'
    assert r['checks']['SHADOW_WORKER_READY']['reason']=='SHADOW_NOT_ENABLED_OR_AUTHORIZED'


def test_spool_probe_is_read_only_and_owner_scoped(tmp_path):
    from services.operational_spool import DurableOperationalSpool
    p=tmp_path/'spool.sqlite';s=DurableOperationalSpool(p)
    s.register(owner_id='bob',source_id='b',scope={})
    s.enqueue({'owner_id':'bob','source_id':'b','event_type':'HEALTH_PROBE','occurred_at':T.isoformat(),'payload':{}},key='original')
    before=p.read_bytes()
    r=deployment_readiness(owner_id='alice',spool_path=p,now=T)
    assert r['checks']['OPERATIONAL_SPOOL_READY']['status']=='READY'
    assert p.read_bytes()==before
    assert deployment_readiness(owner_id='bob',spool_path=p,now=T)['checks']['OPERATIONAL_SPOOL_READY']['status']=='DEGRADED'
    with sqlite3.connect(p) as db:assert db.execute('SELECT COUNT(*) FROM spool_events').fetchone()[0]==1


def test_missing_spool_is_not_created(tmp_path):
    p=tmp_path/'missing.sqlite'
    r=deployment_readiness(owner_id='alice',spool_path=p,now=T)
    assert r['checks']['OPERATIONAL_SPOOL_READY']['status']=='UNKNOWN' and not p.exists()


def test_source_bundle_includes_untracked_implementation_and_freezes_bytes(tmp_path):
    root=tmp_path/'source';root.mkdir();(root/'Dockerfile').write_text('FROM scratch\n')
    p=root/'automation-hub/services/untracked.py';p.parent.mkdir(parents=True);p.write_text('original = 1\n')
    paths=['Dockerfile','automation-hub/services/untracked.py']
    first=bundle_source(root,tmp_path/'release1',paths,head='original-head',dirty_paths={paths[1]:'??'})
    second=bundle_source(root,tmp_path/'release2',list(reversed(paths)),head='original-head',dirty_paths={paths[1]:'??'})
    assert first['source_sha256']==second['source_sha256']
    assert first['required_untracked_files']==[paths[1]]
    assert (tmp_path/'release1/context'/paths[1]).read_bytes()==p.read_bytes()
    p.write_text('original = 2\n')
    third=bundle_source(root,tmp_path/'release3',paths,head='original-head',dirty_paths={paths[1]:'??'})
    assert first['source_sha256']!=third['source_sha256']
    assert (tmp_path/'release1/context'/paths[1]).read_text()=='original = 1\n'
    assert first['human_release_approval'] is False


@pytest.mark.parametrize('name',['.env','automation-hub/.env.production','automation-hub/logs/providers.json',
    'paper.sqlite','snapshot.db','snapshot.db-wal','.venv/secret','frontend/node_modules/secret'])
def test_release_excludes_private_and_financial_inputs_without_copying(tmp_path,name):
    root=tmp_path/'src';root.mkdir();p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('PRIVATE_SENTINEL')
    m=bundle_source(root,tmp_path/'release',[name],head='head',dirty_paths={})
    assert not (tmp_path/'release/context'/name).exists() and name in m['excluded_files']


@pytest.mark.parametrize('name',['../escape','/absolute','bad\\path'])
def test_release_rejects_path_escape(tmp_path,name):
    with pytest.raises(ValueError,match='SOURCE_PATH'):
        bundle_source(tmp_path,tmp_path/'release',[name],head='head',dirty_paths={})


def test_source_symlinks_cannot_read_unreviewed_external_data(tmp_path):
    root=tmp_path/'src';root.mkdir();(tmp_path/'private').write_text('SECRET');(root/'code.py').symlink_to(tmp_path/'private')
    with pytest.raises(ValueError,match='SYMLINK'):
        bundle_source(root,tmp_path/'release',['code.py'],head='head',dirty_paths={})


def test_actual_dockerfile_installs_hashed_dependencies_and_postgres_extra():
    root=Path(__file__).parents[2];text=(root/'Dockerfile').read_text()
    assert 'FROM node:20-slim' in text and 'FROM python:3.11-slim' in text
    assert '.[worker-postgres]' in text and '--require-hashes' in text
    assert 'ops/api-requirements.lock' in text and 'import psycopg' in text
