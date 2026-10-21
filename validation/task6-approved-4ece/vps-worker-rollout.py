"""Update only the existing staging research worker, with backups and rollback.

This is operator tooling, not a new application build. The approved API is NOT
started: its saved paper-session startup path remains a separate blocker.
Actual receipts and backup contents stay private on the existing VPS.
"""
import argparse
import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

BASE=Path('/home/ubuntu/nexus-rc3-staging-28acdbc')
STAGE='nexus-remote-stage-rc3-28acdbc'
WORKER=STAGE+'-research-worker-1'
RELEASE=BASE/'release'
MEMORY=RELEASE/'intelligence-recovery-20261018/compose.memory.yaml'
CONFIGS=[RELEASE/'ops/staging.alpine.compose.yaml',
         RELEASE/'customer-workspace/compose.customer-workspace.yaml']
ENV_FILE=BASE/'private/compose.env'
OLD_IMAGE='sha256:25daa1ca6ea70c493053ab60635844d82ea5b147daf8ae64da7b58a508277fb1'
NEW_IMAGE='nexus-ci-worker@sha256:6cfd023a115f8985b6298e6fd1f476064b40fda8781d604175e4d6cde20e0cc8'
DEPENDENCIES={
 'vps-image-preload.py':'e86f615966ed1c7cb467337c93024d49f008d41516755ab2775336220b9cf9fa',
 'vps-postgres-recovery.py':'6975f9b9bd2219871bd5ec91935e9afff7c226bd5dda8a649c03ebbb0e9e43f7'}
MAX_SNAPSHOT=32*1024**2
FORBIDDEN={'BINANCE_API_KEY','BINANCE_API_SECRET','SUPABASE_KEY','ALPACA_API_KEY',
 'ALPACA_SECRET_KEY','BYBIT_API_KEY','BYBIT_API_SECRET','OANDA_API_KEY','HUB_EXCHANGE_API_KEY',
 'HUB_EXCHANGE_API_SECRET','SUPABASE_SERVICE_ROLE_KEY','HUB_CONTROL_KEY','HUB_WEBHOOK_SECRET',
 'HUB_PASSWORD','HUB_SECRET','HUB_API_KEY','ANTHROPIC_API_KEY','TELEGRAM_BOT_TOKEN'}


class Blocked(RuntimeError):pass


def require(value,reason):
    if not value:raise Blocked(reason)


def envmap(config):return dict(v.split('=',1) for v in config.get('Env',[]) if '=' in v)


def native(args,*,input_text=None,stdin=None,stdout=None,timeout=180):
    # An inherited shell variable must not override the existing Compose env file.
    environment={k:v for k,v in os.environ.items() if k in ('PATH','HOME','USER','LANG','LC_ALL')}
    try:
        result=subprocess.run(['docker','--host','unix:///var/run/docker.sock',*args],
          input=input_text.encode() if input_text is not None else None,stdin=stdin,
          stdout=stdout or subprocess.PIPE,stderr=subprocess.PIPE,env=environment,timeout=timeout)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise Blocked('LOCAL_DOCKER_'+type(exc).__name__.upper()+':'+args[0]) from None
    # Compose/SQL errors may include interpolated credentials. Never echo them.
    require(result.returncode==0,'LOCAL_DOCKER_FAILED:'+args[0]+':exit='+str(result.returncode))
    return result.stdout.decode() if stdout is None else None


def dependency(name):
    path=Path(__file__).parent/name
    require(path.is_file() and not path.is_symlink() and
      hashlib.sha256(path.read_bytes()).hexdigest()==DEPENDENCIES[name],'PINNED_HELPER_REQUIRED:'+name)
    spec=importlib.util.spec_from_file_location(name.replace('-','_'),path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def inspect(name):return json.loads(native(['inspect',name]))[0]


def validate_worker(worker):
    config=worker['Config'];host=worker['HostConfig'];env=envmap(config)
    require(config.get('User')=='100:100','EXISTING_WORKER_USER_100_REQUIRED')
    require(not any(env.get(k) for k in FORBIDDEN),'WORKER_EXECUTION_CREDENTIAL_FORBIDDEN')
    dsn=urlsplit(env.get('NEXUS_RESEARCH_WORKER_DSN',''))
    require(dsn.scheme in ('postgres','postgresql') and dsn.hostname=='postgres' and
      dsn.port in (None,5432) and dsn.username=='nexus_staging_worker' and bool(dsn.password) and
      dsn.path=='/nexus_staging_metadata' and not dsn.fragment and not dsn.query,'EXISTING_METADATA_DSN_REQUIRED')
    require(env.get('NEXUS_OPERATIONAL_SPOOL_PATH')=='/observation/research-operational-spool.sqlite',
      'EXISTING_DURABLE_OPERATIONAL_SPOOL_REQUIRED')
    require(config.get('Entrypoint')==['python','-m','services.research_worker_main'] and
      config.get('Cmd')==['--backend','postgresql','--health-file','/observation/research-health.json'],
      'RESEARCH_ONLY_COMMAND_REQUIRED')
    mounts=worker['Mounts']
    require(len(mounts)==2 and {(m.get('Type'),m.get('Name'),m.get('Destination'),m.get('RW')) for m in mounts}==
      {('volume',STAGE+'_research-state','/observation',True),('volume',STAGE+'_datasets','/datasets',False)},
      'RESEARCH_ONLY_VOLUMES_REQUIRED')
    require(host.get('Memory')==536870912 and host.get('MemorySwap')==1073741824 and
      host.get('ReadonlyRootfs') is True and not host.get('Privileged') and not host.get('PortBindings') and
      host.get('PidsLimit')==64 and host.get('NanoCpus')==1000000000 and
      set(host.get('CapDrop') or [])=={'ALL'} and
      any(v in ('no-new-privileges','no-new-privileges:true') for v in host.get('SecurityOpt') or []),
      'EXISTING_WORKER_RESOURCE_AND_SECURITY_BOUNDARY_REQUIRED')
    networks=list(worker['NetworkSettings']['Networks'])
    require(networks==[STAGE+'_metadata'],'ONLY_EXISTING_METADATA_NETWORK_ALLOWED')
    return networks[0]


def compose_args(overlay=None):
    args=['compose','--project-name',STAGE,'--project-directory',str(RELEASE/'ops'),
      '--env-file',str(ENV_FILE),'--profile','research']
    for path in [*CONFIGS,*([overlay] if overlay else []),MEMORY]:args+=['-f',str(path)]
    return args


def up_args(prefix):return prefix+['up','-d','--no-deps','--no-build','--pull','never','research-worker']


def validate_render(before,after,image):
    require(before.get('name')==after.get('name')==STAGE,'EXISTING_PROJECT_ONLY')
    api=after['services']['api']
    require(api.get('mem_limit')==api.get('memswap_limit')==1610612736,'API_1536_OVERRIDE_LAST_REQUIRED')
    expected=json.loads(json.dumps(before))
    expected['services']['research-worker'].update(image=image,pull_policy='never')
    require(after==expected,'ONLY_WORKER_IMAGE_AND_PULL_POLICY_MAY_CHANGE')


def validate_live_hash(worker,output):
    words=output.split()
    require(len(words)==2 and words[0]=='research-worker' and re.fullmatch(r'[0-9a-f]{64}',words[1]) and
      words[1]==(worker['Config'].get('Labels') or {}).get('com.docker.compose.config-hash'),
      'EXISTING_WORKER_COMPOSE_CONFIGURATION_DRIFT')


def compare_runtime(old,new,old_image,new_image):
    validate_worker(new)
    require(old['HostConfig']==new['HostConfig'],'WORKER_HOST_CONFIGURATION_CHANGED')
    require(sorted(old['Mounts'],key=lambda m:m['Destination'])==sorted(new['Mounts'],key=lambda m:m['Destination']),
      'WORKER_PERSISTENT_MOUNTS_CHANGED')
    for key in ('User','Entrypoint','Cmd','WorkingDir','Healthcheck','StopSignal'):
        require(old['Config'].get(key)==new['Config'].get(key),'WORKER_RUNTIME_CONFIGURATION_CHANGED:'+key)
    old_defaults=envmap(old_image['Config']);new_defaults=envmap(new_image['Config'])
    actual=envmap(old['Config'])
    # Only inherited image defaults may differ; every operator binding is retained.
    bindings={k:v for k,v in actual.items() if k not in old_defaults or v!=old_defaults[k]}
    require(envmap(new['Config'])==new_defaults|bindings,'WORKER_ENVIRONMENT_CHANGED')


DB_PROBE=r'''
import json,os,sys,psycopg
payload=json.load(sys.stdin)
dsn=payload.get('dsn') or os.environ['NEXUS_RESEARCH_WORKER_DSN']
with psycopg.connect(dsn,connect_timeout=5,autocommit=True) as db:
 with db.transaction():
  db.execute('SET TRANSACTION READ ONLY')
  db.execute("SET LOCAL statement_timeout='10s'")
  identity=db.execute('SELECT purpose,authority_id,version,writer_role FROM nexus_workers.worker_store_identity').fetchall()
  if len(identity)!=1:raise ValueError('ONE_METADATA_IDENTITY_REQUIRED')
  purpose,authority,version,writer=identity[0]
  name,role,readonly=db.execute('SELECT current_database(),current_user,current_setting(\'transaction_read_only\')').fetchone()
  unsafe=db.execute("SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls OR has_schema_privilege(current_user,'nexus_workers','CREATE') FROM pg_roles WHERE rolname=current_user").fetchone()[0]
  dangerous=db.execute("SELECT coalesce(bool_or(pg_has_role(current_user,rolname,'MEMBER')),false) FROM pg_roles WHERE rolname IN ('pg_read_server_files','pg_write_server_files','pg_execute_server_program','pg_read_all_data','pg_write_all_data','pg_signal_backend')").fetchone()[0]
  tables=db.execute("SELECT count(*) FROM pg_tables WHERE schemaname='nexus_workers'").fetchone()[0]
  financial=db.execute("SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') AND (tablename IN ('paper_trades','paper_positions','trade_decision_journal','orders','positions','balances','pa_sessions','smc_sessions','financial_journal') OR left(tablename,3)='v2_')").fetchone()[0]
  pending=db.execute("SELECT count(*) FROM nexus_workers.research_jobs WHERE status IN ('QUEUED','RETRY_PENDING','CLAIMED','RUNNING','CANCEL_REQUESTED')").fetchone()[0]
  print(json.dumps(dict(purpose=purpose,authority_id=authority,version=version,writer_role=writer,
    database=name,role=role,unsafe=bool(unsafe or dangerous),financial_tables=financial,tables=tables,
    pending=pending,read_only=readonly=='on')))
'''


def validate_database(proof,authority=None):
    require(proof.get('purpose')=='research_shadow_metadata' and proof.get('version')==1 and
      proof.get('writer_role')==proof.get('role')=='nexus_staging_worker' and
      proof.get('database')=='nexus_staging_metadata' and proof.get('unsafe') is False and
      proof.get('read_only') is True and proof.get('financial_tables')==0 and proof.get('tables')==37 and
      isinstance(proof.get('authority_id'),str) and len(proof['authority_id'])==32,
      'ISOLATED_INSTALLED_METADATA_AND_RESTRICTED_ROLE_REQUIRED')
    require(proof.get('pending')==0,'RESEARCH_QUEUE_MUST_BE_IDLE')
    require(authority is None or authority==proof['authority_id'],'METADATA_AUTHORITY_CHANGED')


SNAPSHOT_CODE=r'''
import base64,hashlib,json,os,pathlib,sqlite3,tempfile,time
def capture(root):
 root=pathlib.Path(root);files=[];total=0;sqlite_count=0;started=time.monotonic()
 for path in sorted(root.rglob('*')):
  if path.is_symlink():raise ValueError('RESEARCH_STATE_SYMLINK')
  if path.is_dir():continue
  if not path.is_file():raise ValueError('RESEARCH_STATE_SPECIAL_FILE')
  if len(files)>=64 or path.stat().st_size>32*1024**2:raise ValueError('RESEARCH_STATE_SIZE_BOUND')
  if path.name.endswith(('-wal','-shm','-journal')):
   parent=path.with_name(path.name.rsplit('-',1)[0])
   with parent.open('rb') as source:header=source.read(16)
   if header!=b'SQLite format 3\x00':raise ValueError('UNRECOGNIZED_SQLITE_SIDECAR')
   continue
  with path.open('rb') as source:header=source.read(16)
  is_sqlite=header==b'SQLite format 3\x00'
  if path.suffix in ('.db','.sqlite','.sqlite3') and not is_sqlite:raise ValueError('INVALID_SQLITE_STATE')
  if is_sqlite:
   sqlite_count+=1
   with tempfile.TemporaryDirectory() as temporary:
    target=pathlib.Path(temporary)/'copy.sqlite'
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3) as source,sqlite3.connect(target) as dest:
     def progress(*_):
      if time.monotonic()-started>60:raise TimeoutError('RESEARCH_BACKUP_TIME_BOUND')
     source.backup(dest,pages=128,progress=progress,sleep=0.01)
     if dest.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or dest.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('SQLITE_INTEGRITY_FAILED')
    data=target.read_bytes()
  else:data=path.read_bytes()
  total+=len(data)
  if total>32*1024**2:raise ValueError('RESEARCH_STATE_TOTAL_SIZE_BOUND')
  files.append(dict(path=path.relative_to(root).as_posix(),sqlite=is_sqlite,bytes=len(data),
    sha256=hashlib.sha256(data).hexdigest(),data=base64.b64encode(data).decode()))
 if not sqlite_count:raise ValueError('EXISTING_OPERATIONAL_DATABASE_REQUIRED')
 return dict(files=files,sqlite_databases=sqlite_count,bytes=total)
if __name__=='__main__':print(json.dumps(capture('/observation')))
'''


def restore_snapshot(payload,directory):
    files=payload['files'];total=0;count=0;seen=set();verified=[]
    require(0<len(files)<=64,'RESEARCH_BACKUP_FILE_BOUND')
    for row in files:
        name=row['path'];path=PurePosixPath(name)
        require(bool(name) and not path.is_absolute() and '..' not in path.parts and
          str(path)==name and '\\' not in name and name not in seen,'UNSAFE_RESEARCH_BACKUP_PATH')
        seen.add(name);data=base64.b64decode(row['data'],validate=True);total+=len(data)
        require(total<=MAX_SNAPSHOT and len(data)==row['bytes'] and
          hashlib.sha256(data).hexdigest()==row['sha256'],'RESEARCH_BACKUP_HASH_OR_SIZE_MISMATCH')
        verified.append((row,data))
    for row,data in verified:
        target=directory/row['path'];target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with target.open('xb') as stream:stream.write(data)
        if row['sqlite']:
            count+=1
            with sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True) as db:
                require(db.execute('PRAGMA integrity_check').fetchall()==[('ok',)] and
                  not db.execute('PRAGMA foreign_key_check').fetchall(),'RESTORED_RESEARCH_SQLITE_INTEGRITY_FAILED')
    require(count==payload['sqlite_databases'] and count>0,'RESEARCH_BACKUP_INCOMPLETE')
    return {'files':len(files),'sqlite_databases':count,'bytes':total,
      'full_integrity_and_foreign_keys_passed':True,'scope':'RESEARCH_OPERATIONAL_STATE_ONLY'}


def apply_with_rollback(change,rollback):
    common={'task6_complete':False,'api_replaced':False,'new_paper_execution':False,'worker_update_attempted':True}
    try:return common|change()|{'status':'RESEARCH_WORKER_DEPLOYED_AND_RESTART_VERIFIED','worker_updated':True}
    except Exception as exc:
        result=common|{'status':'WORKER_UPDATE_FAILED_ROLLED_BACK',
          'reason':str(exc) if isinstance(exc,Blocked) else type(exc).__name__}
        try:result.update(rollback_status='PASSED',rollback=rollback(),worker_updated=False)
        except Exception as recovery:
            result.update(status='WORKER_UPDATE_AND_ROLLBACK_BLOCKED',rollback_status='BLOCKED',worker_updated='UNVERIFIED',
              rollback_reason=str(recovery) if isinstance(recovery,Blocked) else type(recovery).__name__)
        return result


def phase(name):print(json.dumps({'phase':name}),flush=True)


def wait_healthy(image_id,timeout=75):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        worker=inspect(WORKER)
        require(worker['Image']==image_id,'WORKER_IMAGE_MISMATCH')
        require(not worker['State']['OOMKilled'] and worker['RestartCount']==0,'WORKER_OOM_OR_AUTOMATIC_RESTART')
        if worker['State'].get('Health',{}).get('Status')=='healthy':return worker
        require(worker['State']['Status'] in ('running','created'),'WORKER_NOT_RUNNING')
        time.sleep(3)
    raise Blocked('WORKER_HEALTH_TIMEOUT')


def rollout(packet,preload,pg,directory):
    phase('VERIFY_APPROVED_PACKET_AND_EXISTING_BOUNDARIES')
    verified=preload.verify_packet(packet)
    before=preload.preflight(native)
    all_stage=preload.containers(STAGE,native)
    workers=[c for c in all_stage if (c['Config'].get('Labels') or {}).get('com.docker.compose.service')=='research-worker']
    require(len(workers)==1 and workers[0]['Name']=='/'+WORKER,'ONE_EXISTING_WORKER_REQUIRED')
    old=workers[0];require(old['Image']==OLD_IMAGE,'EXPECTED_ROLLBACK_WORKER_REQUIRED')
    require(old['State'].get('Health',{}).get('Status')=='healthy','HEALTHY_EXISTING_WORKER_REQUIRED')
    network=validate_worker(old)
    network_info=json.loads(native(['network','inspect',network]))[0]
    require(network_info.get('Internal') is True and network_info.get('Labels',{}).get('com.docker.compose.project')==STAGE,
      'EXISTING_INTERNAL_METADATA_NETWORK_REQUIRED')
    target,_=preload.inspect_loaded(verified['exports']['worker'],preload.POLICY,native)
    old_image=json.loads(native(['image','inspect',old['Image']]))[0]
    require(old_image['Id']==old['Image'],'EXACT_ROLLBACK_IMAGE_REQUIRED')
    require(NEW_IMAGE in target.get('RepoDigests',[]),'APPROVED_LOCAL_WORKER_REPO_DIGEST_REQUIRED')
    others=preload.fingerprint([c for c in all_stage if c['Id']!=old['Id']])
    def guard():
        after=preload.preflight(native)
        require(after['production']==before['production'],'PRODUCTION_CONTAINER_CHANGED')
        require(preload.fingerprint([c for c in preload.containers(STAGE,native) if c['Name']!='/'+WORKER])==others,
          'NON_WORKER_STAGING_CONTAINER_CHANGED')
        require(shutil.disk_usage(BASE).free>=preload.RESERVE,'EIGHT_GIB_DISK_RESERVE_REQUIRED')
        require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==value for p,value in hashes.items()),
          'EXISTING_COMPOSE_CONFIGURATION_CHANGED')
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [*CONFIGS,MEMORY,ENV_FILE]}
    def render(overlay=None):return json.loads(native(compose_args(overlay)+['config','--format','json']))
    baseline=render()
    # A rollback must reproduce the actual running configuration, not merely a
    # different configuration currently present in files on disk.
    validate_live_hash(old,native(compose_args()+['config','--hash','research-worker']))
    new_overlay=directory/'compose.worker.yaml';rollback_overlay=directory/'compose.rollback.yaml'
    for path,image in ((new_overlay,NEW_IMAGE),(rollback_overlay,old['Image'])):
        path.write_text(json.dumps({'services':{'research-worker':{'image':image,'pull_policy':'never'}}}))
        validate_render(baseline,render(path),image)
    phase('VERIFY_NEW_IMAGE_WITH_EXISTING_RESTRICTED_DATABASE_CREDENTIAL')
    dsn=envmap(old['Config'])['NEXUS_RESEARCH_WORKER_DSN']
    probe=json.loads(native(['run','--rm','-i','--pull=never','--network',network,'--read-only',
      '--user','100:100','--cpus','0.5','--memory','256m','--memory-swap','256m','--pids-limit','32',
      '--cap-drop','ALL','--security-opt','no-new-privileges:true','--entrypoint','python',target['Id'],'-B','-c',DB_PROBE],
      input_text=json.dumps({'dsn':dsn})))
    validate_database(probe);authority=probe['authority_id']
    phase('BACK_UP_AND_RESTORE_RESEARCH_OPERATIONAL_STATE')
    snapshot=json.loads(native(['exec','-i',WORKER,'python','-B','-c',SNAPSHOT_CODE],timeout=90))
    restored=directory/'research-state';restored.mkdir(mode=0o700)
    snapshot_proof=restore_snapshot(snapshot,restored)
    manifest=[{k:v for k,v in row.items() if k!='data'} for row in snapshot['files']]
    (directory/'research-backup-manifest.json').write_text(json.dumps(manifest,indent=2))
    del snapshot
    phase('BACK_UP_POSTGRES_AND_RESTORE_INTO_NEW_DETACHED_DATABASE')
    pg.docker=lambda args,operation,**kwargs:native(args,**kwargs)
    previous=sys.argv;sys.argv=[str(Path(__file__).parent/'vps-postgres-recovery.py')]
    output=io.StringIO()
    try:
        with redirect_stdout(output):code=pg.main()
    finally:sys.argv=previous
    pg_proof=json.loads(output.getvalue())
    require(code==0 and pg_proof.get('status')=='POSTGRES_BACKUP_AND_DETACHED_RESTORE_PASSED',
      'FRESH_POSTGRES_RECOVERY_FAILED:'+str(pg_proof.get('reason','UNKNOWN')))
    def current_db():
        value=json.loads(native(['exec','-i',WORKER,'python','-B','-c',DB_PROBE],input_text='{}'))
        validate_database(value,authority);return value
    current_db();guard()
    require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==value for p,value in hashes.items()),
      'EXISTING_COMPOSE_CONFIGURATION_CHANGED')
    validate_render(baseline,render(new_overlay),NEW_IMAGE)
    def verify(image):
        worker=wait_healthy(image['Id']);compare_runtime(old,worker,old_image,image)
        health=json.loads(native(['exec',WORKER,'python','-B','-c',
          "import json,pathlib;print(pathlib.Path('/observation/research-health.json').read_text())"]))
        require(health.get('readiness')=='READY' and health.get('database_status')=='AVAILABLE' and
          health.get('backend')=='postgresql' and health.get('concurrency')==1 and
          health.get('execution_runtime_loaded') is False and health.get('metadata_authority_id')==authority,
          'AUTHENTICATED_METADATA_WORKER_HEALTH_FAILED')
        current_db();guard();return worker
    def change():
        phase('UPDATE_ONLY_EXISTING_RESEARCH_WORKER')
        native(up_args(compose_args(new_overlay)))
        first=verify(target);phase('RESTART_ONLY_RESEARCH_WORKER_AND_VERIFY_PERSISTENCE')
        native(compose_args(new_overlay)+['restart','--no-deps','research-worker'])
        final=verify(target)
        require(first['Id']==final['Id'] and first['State']['StartedAt']!=final['State']['StartedAt'],
          'EXPLICIT_WORKER_RESTART_NOT_PROVEN')
        post=json.loads(native(['exec','-i',WORKER,'python','-B','-c',SNAPSHOT_CODE],timeout=90))
        post_dir=directory/'post-restart-research-state';post_dir.mkdir(mode=0o700)
        post_proof=restore_snapshot(post,post_dir)
        return {'worker_container_id':final['Id'],'worker_image_id':final['Image'],
          'source_commit':preload.POLICY['commit'],'source_sha256':preload.POLICY['source_sha256'],
          'image_config_id':verified['exports']['worker']['config_id'],'image_oci_manifest':verified['exports']['worker']['export_oci_manifest_digest'],
          'worker_health':'healthy','database':'AVAILABLE','concurrency':1,'execution_runtime_loaded':False,
          'restart_verified':True,'research_state_after_restart':post_proof,
          'production_container_identities_unchanged':True,'other_staging_container_identities_unchanged':True,
          'api_memory_limit_bytes':1610612736,'api_memory_swap_limit_bytes':1610612736}
    def rollback():
        phase('ROLL_BACK_ONLY_RESEARCH_WORKER_TO_ORIGINAL_IMAGE')
        native(up_args(compose_args(rollback_overlay)));verify(old_image)
        return {'image_id':old_image['Id'],'healthy':True,'other_containers_unchanged':True}
    result=apply_with_rollback(change,rollback)
    result.update(research_state_backup=snapshot_proof,postgres_backup=pg_proof,
      active_compose_overlay=str(new_overlay if result['status']=='RESEARCH_WORKER_DEPLOYED_AND_RESTART_VERIFIED' else rollback_overlay),
      receipt_directory=str(directory),security_scan_performed_on_vps=False,
      ci_security_reports=verified['scans'],remaining_blockers=['API_STARTUP_CAN_RESTORE_SAVED_PAPER_EXECUTION',
      'CURRENT_FINANCIAL_STATE_RECOVERY_NOT_PROVEN','AUTHENTICATED_PRD_ACCEPTANCE_PENDING'])
    return result


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deploy',action='store_true',required=True)
    parser.parse_args()
    require(BASE.is_dir() and not BASE.is_symlink(),'EXISTING_STAGE_BASE_REQUIRED')
    with (BASE/'.task6-worker-rollout.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Blocked('ANOTHER_WORKER_ROLLOUT_IS_RUNNING') from None
        directory=BASE/('task6-worker-rollout-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:12])
        directory.mkdir(mode=0o700)
        # Keep the deployment/rollback controller alive if the SSH terminal
        # disappears. Its private progress log remains usable on reconnection.
        signal.signal(signal.SIGHUP,signal.SIG_IGN)
        kinds=(Blocked,)
        with (directory/'rollout.log').open('x') as progress,redirect_stdout(DurableOutput(sys.stdout,progress)):
            phase('PRIVATE_RECEIPT_DIRECTORY:'+str(directory))
            try:
                preload=dependency('vps-image-preload.py');pg=dependency('vps-postgres-recovery.py')
                kinds=(Blocked,preload.Blocked,pg.Blocked)
                result=rollout(Path('/home/ubuntu/nexus-task6-approved-images.zip'),preload,pg,directory)
            except Exception as exc:
                result={'status':'BLOCKED_BEFORE_WORKER_UPDATE','reason':str(exc) if isinstance(exc,kinds) else type(exc).__name__,
                  'api_replaced':False,'task6_complete':False,'worker_updated':False,'receipt_directory':str(directory)}
            result['completed_at_utc']=datetime.now(timezone.utc).isoformat()
            (directory/'status.json').write_text(json.dumps(result,indent=2))
            print(json.dumps(result,indent=2),flush=True)
        return 0 if result['status']=='RESEARCH_WORKER_DEPLOYED_AND_RESTART_VERIFIED' else 1


class DurableOutput:
    def __init__(self,terminal,private):self.terminal=terminal;self.private=private
    def write(self,value):
        self.private.write(value);self.private.flush()
        if self.terminal is not None:
            try:self.terminal.write(value);self.terminal.flush()
            except OSError:self.terminal=None
        return len(value)
    def flush(self):
        self.private.flush()
        if self.terminal is not None:
            try:self.terminal.flush()
            except OSError:self.terminal=None


if __name__=='__main__':
    try:raise SystemExit(main())
    except Blocked as error:
        print(json.dumps({'status':'BLOCKED','reason':str(error),'task6_complete':False}),flush=True)
        raise SystemExit(1)
