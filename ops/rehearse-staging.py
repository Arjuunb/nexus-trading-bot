"""Explicit disposable-local rehearsal. Never calls scripts/deploy.sh.

Generates fresh fixture credentials and project-scoped resources. All subprocess
output is captured and secrets redacted before retained diagnostics. Shadow has
no registrations and is started only by --exercise-shadow. No trading requests.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid
import urllib.error
import urllib.request
import urllib.parse
import http.cookiejar
import ssl


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--output',required=True)
    p.add_argument('--api-image',default='nexus-api:sprint12-local');p.add_argument('--worker-image',default='nexus-workers:sprint12-local')
    p.add_argument('--run-disposable',action='store_true');p.add_argument('--exercise-shadow',action='store_true')
    p.add_argument('--browser-smoke',action='store_true')
    p.add_argument('--verify-remote-diagnostics',action='store_true',help='Read-only Sprint 13 endpoint check on a reviewed candidate image')
    a=p.parse_args(argv)
    if not a.api_image.startswith('nexus-api:sprint12-') or not a.worker_image.startswith('nexus-workers:sprint12-'):
        raise ValueError('DISPOSABLE_IMAGE_NAMESPACE_REQUIRED')
    root=Path(a.output);root.mkdir(mode=0o700,parents=True,exist_ok=False)
    project='nexus-s12-'+uuid.uuid4().hex[:12];profile=Path(__file__).with_name('staging.local.compose.yaml').resolve()
    generated={k:secrets.token_hex(32) for k in ('operator','runtime','password','session','control','webhook')}
    env={k:os.environ[k] for k in ('PATH','DOCKER_HOST','DOCKER_CONFIG') if k in os.environ}
    env.setdefault('DOCKER_HOST','unix:///var/run/docker.sock')
    if env.get('DOCKER_HOST') and not env['DOCKER_HOST'].startswith('unix://'):raise ValueError('LOCAL_DOCKER_SOCKET_REQUIRED')
    env.setdefault('DOCKER_CONFIG',str(root/'docker-client'));Path(env['DOCKER_CONFIG']).mkdir(exist_ok=True)
    def redact(text):
        for value in generated.values():text=text.replace(value,'[REDACTED_LOCAL_FIXTURE_SECRET]')
        return text
    def docker(args,*,binary=False):
        r=subprocess.run(['docker',*args],env=env,capture_output=True,text=not binary,timeout=120)
        if r.returncode:
            error=r.stderr.decode(errors='replace') if binary else r.stderr
            raise RuntimeError('LOCAL_DOCKER_OPERATION_FAILED: '+redact(error)[-2000:])
        return r.stdout
    uid=docker(['run','--rm','--network','none','--entrypoint','id',a.api_image,'-u']).strip()
    app_env=root/'app.env'
    app_env.write_text('\n'.join(['HUB_AUTH_MODE=legacy','HUB_EMERGENCY_ADMIN_ENABLED=1','HUB_USERNAME=staging-operator',
        'HUB_PASSWORD='+generated['password'],'HUB_SECRET='+generated['session'],
        'HUB_CONTROL_KEY='+generated['control'],'HUB_WEBHOOK_SECRET='+generated['webhook']])+'\n');app_env.chmod(0o600)
    tls=root/'tls';tls.mkdir(mode=0o700)
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1,DNS:localhost',
        '-keyout',str(tls/'tls.key'),'-out',str(tls/'tls.crt')],check=True,capture_output=True)
    (tls/'tls.key').chmod(0o600)
    controls={'NEXUS_STAGING_PROJECT':project,'NEXUS_STAGING_API_IMAGE':a.api_image,'NEXUS_STAGING_WORKER_IMAGE':a.worker_image,
        'NEXUS_STAGING_UID':uid,'NEXUS_STAGING_APP_ENV':str(app_env.resolve()),
        'NEXUS_STAGING_OPERATOR_PASSWORD':generated['operator'],'NEXUS_STAGING_RUNTIME_PASSWORD':generated['runtime'],
        'NEXUS_STAGING_SHADOW_ENABLED':'1' if a.exercise_shadow else '0'}
    controls['NEXUS_STAGING_TLS_DIR']=str(tls.resolve())
    control=root/'compose.env';control.write_text('\n'.join(k+'='+v for k,v in controls.items())+'\n');control.chmod(0o600)
    base=['compose','--env-file',str(control.resolve()),'-p',project,'-f',str(profile)]
    if a.exercise_shadow:base+=['--profile','shadow']
    def compose(args,**kwargs):return docker(base+args,**kwargs)
    config=json.loads(compose(['config','--format','json']))
    assert config['networks']['metadata']['internal'] is True
    for name in ('research-worker','shadow-worker'):
        assert not set(config['services'][name].get('environment',{})) & {'HUB_CONTROL_KEY','HUB_PASSWORD','SUPABASE_KEY','HUB_EXCHANGE_API_KEY'}
    report={'schema_version':'nexus_staging_rehearsal.v1','scope':'DISPOSABLE_LOCAL_STAGING','project':project,
        'created_at':datetime.now(timezone.utc).isoformat(),'status':'PLANNED','remote_deployment':False,
        'production_certification':False,'execution_authorization':False,'shadow_registered_candidates':0,
        'authentication_scope':'LOCAL_PRODUCTION_PROFILE_EMERGENCY_ADMIN; NOT_CUSTOMER_SUPABASE_JWT',
        'resources':{'internal_network':True,'postgres_public_port':False,'generated_fixture_credentials':True},'checks':{}}
    if not a.run_disposable:
        (root/'rehearsal.json').write_text(json.dumps(report,indent=2));return 0
    checks=report['checks'];started=False
    def api_python(code):return compose(['exec','-T','api','python','-B','-c',code])
    def financial():
        return json.loads(api_python('''import sqlite3,json,hashlib,pathlib
result={}
for p in pathlib.Path('/var/lib/tradexa').glob('*.db'):
 with sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) as db:
  for (n,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
   if n.startswith('v2_') or n in {'paper_trades','paper_positions','orders','positions','financial_journal','paper_trade_outbox'}:
    rows=db.execute('SELECT * FROM "'+n.replace('"','""')+'" ORDER BY rowid').fetchall()
    result[p.name+':'+n]=hashlib.sha256(json.dumps(rows,default=str).encode()).hexdigest()
print(json.dumps(result))'''))
    try:
        # VFS materializes a full root for each container. Initializers run
        # sequentially and are removed on success; their durable volumes stay.
        # --no-deps starts only reviewed services after initialization finished.
        started=True;compose(['up','-d','--wait','--wait-timeout','100','postgres'])
        compose(['run','--rm','--no-deps','state-init'])
        compose(['run','--rm','--no-deps','metadata-init'])
        services=['api','research-worker','ingress']+(['shadow-worker'] if a.exercise_shadow else [])
        # Bound VFS clone peaks as well as steady-state usage. This is an
        # operator rehearsal sequence, never a change to production execution.
        for service in services:
            compose(['up','--no-deps','-d','--wait','--wait-timeout','100',service])
        checks['sequential_runtime_startup']=services
        port=compose(['port','ingress','8443']).strip().rsplit(':',1)[1];url='https://127.0.0.1:'+port
        trust=ssl.create_default_context(cafile=str(tls/'tls.crt'))
        opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=trust))
        def get(path):return opener.open(url+path,timeout=5)
        assert json.load(get('/health'))['status']=='ok';checks['http_liveness']='PASS'
        with get('/') as response:
            public_html=response.read().decode()
            assert all(value not in public_html for value in generated.values())
        checks['public_frontend_has_no_server_credentials']='PASS'
        checks['local_https_certificate_verified']='PASS'
        try:get('/api/v2/strategy-intelligence/deployment-readiness')
        except urllib.error.HTTPError as e:assert e.code==401
        else:raise AssertionError('ANONYMOUS_API_ACCEPTED')
        checks['anonymous_authorization']='PASS'
        data=urllib.parse.urlencode({'username':'staging-operator','password':generated['password']}).encode()
        with opener.open(urllib.request.Request(url+'/login',data=data),timeout=5) as response:assert response.status==200
        for path in ('/','/app'):
            with get(path) as response:assert 'id="root"' in response.read().decode()
        checks['both_actual_frontend_bundles']='PASS'
        deadline=time.monotonic()+35
        while True:
            ready=json.load(get('/api/v2/strategy-intelligence/deployment-readiness'))
            if ready['checks']['RESEARCH_WORKER_READY']['status']=='READY':break
            if time.monotonic()>deadline:raise AssertionError('RESEARCH_READINESS_TIMEOUT')
            time.sleep(.5)
        assert ready['checks']['POSTGRES_READY']['status']=='READY'
        assert ready['checks']['FEED_READY']['status']=='BLOCKED' and ready['checks']['FINANCIAL_EVIDENCE_READY']['status']=='UNKNOWN'
        checks['same_api_worker_postgres_authority']='PASS';report['readiness']=ready
        if a.verify_remote_diagnostics:
            remote=json.load(get('/api/v2/strategy-intelligence/remote-staging-readiness'))
            assert remote['remote_staging']=='NOT_READY' and remote['deployment_authorization'] is False
            assert remote['live_public_feed_soak']=='NOT_RUN'
            checks['remote_diagnostics_fail_closed']='PASS';report['remote_readiness']=remote
        before=financial();assert before;checks['financial_state_captured']=len(before)
        # Restricted actual image connection, not a mock/SQLite substitute.
        privilege=json.loads(api_python("import os,json; from services.postgres_workers import PgResearchContext; c=PgResearchContext(os.environ['NEXUS_RESEARCH_WORKER_DSN']); print(json.dumps({'backend':c.backend,'authority':c.journal.authority_id,'role':dict(c.journal._connection.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone())}))"))
        assert not any(privilege['role'].values());checks['actual_runtime_role_restricted']='PASS'
        if a.browser_smoke:
            public=subprocess.check_output(['openssl','x509','-in',str(tls/'tls.crt'),'-pubkey','-noout'])
            der=subprocess.check_output(['openssl','pkey','-pubin','-outform','DER'],input=public)
            import base64
            spki=base64.b64encode(hashlib.sha256(der).digest()).decode()
            data=json.dumps({'url':url,'username':'staging-operator','password':generated['password'],'certificateSpki':spki}).encode()
            smoke=subprocess.run(['node',str(Path(__file__).with_name('staging-browser-smoke.cjs'))],
                input=data,capture_output=True,timeout=60)
            text=redact(smoke.stdout.decode()+smoke.stderr.decode());(root/'browser-smoke.log').write_text(text)
            assert smoke.returncode==0,'ACTUAL_PRODUCTION_BROWSER_SMOKE_FAILED'
            checks['actual_production_chromium_smoke']=json.loads(smoke.stdout)
        # Each service is independently supervised; recovery retains authority.
        for kind in ('research','shadow') if a.exercise_shadow else ('research',):
            service=kind+'-worker';path='/observation/'+kind+'-health.json'
            original=json.loads(compose(['exec','-T',service,'cat',path]))
            # Docker kill is an operator stop and disables automatic restart.
            # Kill the actual child inside its own namespace to simulate a crash.
            code="import os,json,signal; p=json.load(open("+repr(path)+")); os.kill(p['pid'],signal.SIGKILL)"
            recovery_start=time.monotonic()
            crashed=subprocess.run(['docker',*base,'exec','-T',service,'python','-B','-c',code],
                env=env,capture_output=True,timeout=10)
            assert crashed.returncode in (0,137), 'CRASH_INJECTION_FAILED'
            deadline=time.monotonic()+35
            while True:
                try:
                    new=json.loads(compose(['exec','-T',service,'cat',path]))
                    if new.get('process_start_ticks')!=original.get('process_start_ticks') and new['readiness']=='READY':break
                except RuntimeError:pass
                if time.monotonic()>deadline:raise AssertionError('WORKER_RESTART_TIMEOUT:'+kind)
                time.sleep(.5)
            assert new['metadata_authority_id']==original['metadata_authority_id']
            container=compose(['ps','-q',service]).strip()
            restarts=json.loads(docker(['inspect',container,'--format','{{.RestartCount}}']))
            assert 1<=restarts<=2
            checks[kind+'_supervised_crash_recovery']={'status':'PASS','restart_count':restarts,'new_generation':True,
                'recovery_seconds':time.monotonic()-recovery_start,'measurement':'LOCAL_SIGKILL_TO_READY; NOT_REMOTE_RTO'}
        if a.exercise_shadow:
            status=json.loads(compose(['exec','-T','shadow-worker','cat','/observation/shadow-health.json']))
            assert status['owned_registrations']==0 and status['execution_runtime_loaded'] is False
            checks['explicit_shadow_without_observation']='PASS'
        recovery_start=time.monotonic();compose(['restart','postgres']);time.sleep(2)
        deadline=time.monotonic()+35
        while True:
            r=json.load(get('/api/v2/strategy-intelligence/deployment-readiness'))
            if r['checks']['POSTGRES_READY']['status']=='READY':break
            if time.monotonic()>deadline:raise AssertionError('DATABASE_RESTART_TIMEOUT')
            time.sleep(.5)
        assert r['checks']['POSTGRES_READY']['authority_identity']==ready['checks']['POSTGRES_READY']['authority_identity']
        checks['postgres_restart_no_authority_fallback']='PASS'
        checks['postgres_restart_seconds']=time.monotonic()-recovery_start
        backup=json.loads(api_python("import json; from services.staging_backup import backup_sqlite_staging,restore_sqlite_staging; m=backup_sqlite_staging('/var/lib/tradexa','/var/lib/tradexa/verified-backup'); r=restore_sqlite_staging('/var/lib/tradexa/verified-backup','/var/lib/tradexa/verified-restore'); print(json.dumps({'backup_databases':len(m['originals']),'restore':r}))"))
        assert backup['restore']['restored_databases']==backup['backup_databases'];checks['consistent_sqlite_backup_restore']=backup
        backup_start=time.monotonic();backup_observed_at=datetime.now(timezone.utc).isoformat()
        dump=compose(['exec','-T','postgres','pg_dump','-U','postgres','-d','nexus_staging_metadata','-Fc'],binary=True)
        checks['postgres_backup_seconds']=time.monotonic()-backup_start
        (root/'metadata.dump').write_bytes(dump);checks['postgres_backup_sha256']=hashlib.sha256(dump).hexdigest()
        # Restore from the retained bytes into another disposable DB, never overwrite.
        restore_start=time.monotonic();compose(['exec','-T','postgres','createdb','-U','postgres','nexus_staging_restore'])
        restore=subprocess.run(['docker',*base,'exec','-T','postgres','pg_restore','-U','postgres','-d','nexus_staging_restore'],env=env,input=dump,capture_output=True,timeout=60)
        assert restore.returncode==0
        restored=compose(['exec','-T','postgres','psql','-U','postgres','-d','nexus_staging_restore','-Atc','SELECT authority_id FROM nexus_workers.worker_store_identity']).strip()
        assert restored==privilege['authority'];checks['postgres_restore_original_authority']='PASS'
        checks['postgres_restore_seconds']=time.monotonic()-restore_start
        checks['backup_recovery_objectives']={'snapshot_observed_at':backup_observed_at,'rpo_target':'UNVERIFIED',
            'rto_target':'UNVERIFIED','scope':'DISPOSABLE_LOCAL_FIXTURE; NOT_POWER_LOSS_OR_OFF_HOST_RECOVERY'}
        recovery_start=time.monotonic();compose(['restart','api']);deadline=time.monotonic()+35
        while True:
            try:
                if json.load(get('/health'))['status']=='ok':break
            except Exception:pass
            if time.monotonic()>deadline:raise AssertionError('API_RESTART_TIMEOUT')
            time.sleep(.5)
        checks['api_restart_seconds']=time.monotonic()-recovery_start
        assert financial()==before;checks['financial_state_unchanged_after_recovery']='PASS'
        assert json.loads(compose(['exec','-T','research-worker','cat','/observation/research-health.json']))['execution_runtime_loaded'] is False
        checks['no_trading_activation']='PASS';report['status']='VERIFIED_DISPOSABLE_LOCAL'
    except Exception as exc:
        report['status']='BLOCKED';report['failure_category']=type(exc).__name__;report['failure_reason']=redact(str(exc))
        raise
    finally:
        if started:
            try:(root/'service.log').write_text(redact(compose(['logs','--no-color','--tail','100'])))
            finally:compose(['down','--volumes','--remove-orphans','--timeout','15'])
        (root/'rehearsal.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({'status':report['status'],'project':project,'scope':report['scope'],'report':str(root/'rehearsal.json'),'deploy':False}))
    return 0


if __name__=='__main__':raise SystemExit(main())
