import copy
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from services.remote_staging import validate_remote_profile, remote_readiness, verify_review_archive


def profile():
    return {'name':'nexus-remote-stage-fixture','services':{
        'api':{'image':'sha256:'+'a'*64,'environment':{'HUB_AUTO_ENGINE':'0','HUB_ENABLE_EXTERNAL_LIVE':'0','HUB_USE_LIVE_DATA':'0','HUB_RESEARCH_AUTOSTART':'0','HUB_MONITOR_AGENT':'0'},
            'networks':{'metadata':{},'ingress':{}},'volumes':[{'type':'volume','source':'api-state','target':'/var/lib/tradexa'}]},
        'postgres':{'image':'sha256:'+'b'*64,'networks':{'metadata':{}},'volumes':[{'type':'volume','source':'postgres-state','target':'/var/lib/postgresql/data'}]},
        'ingress':{'image':'sha256:'+'c'*64,'ports':[{'host_ip':'127.0.0.1','published':'18443','target':8443}], 'networks':{'ingress':{}}},
        'research-worker':{'image':'sha256:'+'d'*64,'networks':{'metadata':{}},'volumes':[{'type':'volume','source':'research-state','target':'/observation'}]},
        'state-init':{'image':'sha256:'+'d'*64,'network_mode':'none'},
        'metadata-init':{'image':'sha256:'+'d'*64,'networks':{'metadata':{}}}},
        'volumes':{k:{} for k in ('api-state','postgres-state','research-state')},
        'networks':{'metadata':{'internal':True},'ingress':{}}}


@pytest.mark.parametrize('mutation,reason',[
    (lambda x:x.update(name='tradexa'),'PROJECT'),
    (lambda x:x['services']['postgres'].update(ports=[{'published':'5432'}]),'DATABASE'),
    (lambda x:x['services']['ingress']['ports'][0].update(host_ip='0.0.0.0'),'LOOPBACK'),
    (lambda x:x['services']['ingress']['ports'][0].update(published='443'),'PRODUCTION_PORT'),
    (lambda x:x['volumes']['api-state'].update(external=True),'EXTERNAL'),
    (lambda x:x['services']['api'].update(image='nexus-api:latest'),'IMMUTABLE'),
    (lambda x:x['services']['api']['environment'].update(HUB_AUTO_ENGINE='1'),'EXECUTION'),
    (lambda x:x['services']['research-worker']['networks'].update(ingress={}),'WORKER_NETWORK'),
    (lambda x:x['services']['api']['volumes'].append({'type':'bind','source':'/production','target':'/var/lib/tradexa'}),'STATE_BIND'),
])
def test_unsafe_profiles_fail_closed(mutation,reason):
    value=profile();mutation(value)
    with pytest.raises(ValueError,match=reason):validate_remote_profile(value)


def test_profile_validation_is_design_not_deployment_authorization():
    x=validate_remote_profile(profile())
    assert x['status']=='LOCAL_DESIGN_VALIDATED'
    assert x['deployment_authorization'] is False


def test_unknown_remote_measurements_never_become_ready():
    x=remote_readiness(owner_id='alice')
    assert x['remote_staging']=='NOT_READY'
    assert x['gates']['REMOTE_CAPACITY']['status']=='UNKNOWN'
    assert x['gates']['LIVE_FEED_SOAK']['status']=='BLOCKED'
    assert x['execution_authorization'] is False


def test_readiness_scope_and_freshness_are_verified():
    now=datetime.now(timezone.utc)
    x=remote_readiness(owner_id='alice',now=now,diagnostics={
        'backup':{'owner_id':'alice','recorded_at':(now-timedelta(days=5)).isoformat(),'verified':True},
        'feed':{'owner_id':'bob','recorded_at':now.isoformat(),'verified':True},
        'resource':{'owner_id':'alice','recorded_at':now.isoformat(),'free_bytes':100,'memory_pressure':None}})
    assert x['gates']['BACKUP_FRESHNESS']['status']=='BLOCKED'
    assert x['gates']['LIVE_FEED_SOAK']['status']=='BLOCKED'
    assert x['gates']['LIVE_FEED_SOAK']['reason']=='AUTHORIZED_LIVE_PUBLIC_SOAK_NOT_RUN'
    assert x['gates']['DISK_CAPACITY']['status']=='BLOCKED'
    assert x['gates']['MEMORY_PRESSURE']['status']=='UNKNOWN'
    assert 'bob' not in json.dumps(x)


def test_review_archive_changed_bytes_cannot_verify(tmp_path):
    import hashlib
    (tmp_path/'proof').write_text('original')
    m={'review_id':'release-review:fixture','files':[{'path':'proof','sha256':hashlib.sha256(b'original').hexdigest(),'bytes':8}]}
    (tmp_path/'artifact-integrity.json').write_text(json.dumps(m))
    assert verify_review_archive(tmp_path)['verified_files']==1
    (tmp_path/'proof').write_text('different')
    with pytest.raises(ValueError,match='INTEGRITY'):verify_review_archive(tmp_path)


def test_review_archive_rejects_escape_and_symlinks(tmp_path):
    (tmp_path/'artifact-integrity.json').write_text(json.dumps({'review_id':'x','files':[{'path':'../private','sha256':'a'*64,'bytes':4}]}))
    with pytest.raises(ValueError,match='PATH'):verify_review_archive(tmp_path)


def test_remote_diagnostics_do_not_echo_private_material():
    assert 'PASSWORD_SENTINEL' not in json.dumps(remote_readiness(owner_id='alice',diagnostics={'backup':{'owner_id':'alice','password':'PASSWORD_SENTINEL'}}))
    with pytest.raises(PermissionError):remote_readiness(owner_id='')


def test_real_compose_resolution_keeps_independent_volume_names_and_ports(tmp_path):
    import os,subprocess
    root=Path(__file__).parents[2]
    app=tmp_path/'fixture.env';app.write_text('HUB_SECRET=fixture-only')
    tls=tmp_path/'tls';tls.mkdir()
    variables={'NEXUS_REMOTE_STAGING_PROJECT':'nexus-remote-stage-fixture','NEXUS_STAGING_PROJECT':'nexus-remote-stage-fixture',
        'NEXUS_STAGING_API_IMAGE':'sha256:'+'a'*64,'NEXUS_STAGING_WORKER_IMAGE':'sha256:'+'d'*64,
        'NEXUS_REMOTE_POSTGRES_IMAGE':'sha256:'+'b'*64,'NEXUS_REMOTE_INGRESS_IMAGE':'sha256:'+'c'*64,
        'NEXUS_REMOTE_HTTPS_PORT':'18443','NEXUS_STAGING_UID':'1000','NEXUS_STAGING_APP_ENV':str(app),
        'NEXUS_STAGING_OPERATOR_PASSWORD':'fixture-operator','NEXUS_STAGING_RUNTIME_PASSWORD':'fixture-runtime',
        'NEXUS_STAGING_TLS_DIR':str(tls)}
    bindings=tmp_path/'compose.env';bindings.write_text('\n'.join(k+'='+v for k,v in variables.items()))
    r=subprocess.run(['docker','compose','--env-file',str(bindings),'-f',str(root/'ops/staging.remote.compose.yaml'),'--profile','shadow','config','--format','json'],
        env={'PATH':os.environ['PATH']},capture_output=True,text=True,check=True,timeout=15)
    config=json.loads(r.stdout);x=validate_remote_profile(config)
    assert x['project']=='nexus-remote-stage-fixture'
    assert config['services']['api']['environment']['HUB_EMERGENCY_ADMIN_ENABLED']=='0'
    assert config['services']['api']['environment']['HUB_AUTH_MODE']=='supabase'
    assert all(v['name'].startswith(x['project']+'_') for v in config['volumes'].values())
    assert config['services']['shadow-worker']['command'][-1]=='/observation/feed-cursors.sqlite'
    assert '--feed-journal' in config['services']['shadow-worker']['command']

