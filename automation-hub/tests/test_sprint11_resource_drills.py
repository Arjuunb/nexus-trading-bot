"""Opt-in disposable kernel resource drills, never production services."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import tarfile
import uuid

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('NEXUS_SPRINT11_RESOURCE_TEST')!='1',reason='requires opt-in disposable container resource drill')
IMAGE='nexus-workers:sprint11-local'


def test_actual_build_context_excludes_credentials_and_local_financial_state(tmp_path):
    context=tmp_path/'context';context.mkdir()
    shutil.copy2(Path(__file__).parents[2]/'.dockerignore',context/'.dockerignore')
    private=['.env','.env.production','automation-hub/.env','tradexa-landing/.env',
        'automation-hub/logs/providers.json','.venv/private-binding']
    for name in private:
        p=context/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('ISOLATED_SENTINEL_NOT_A_REAL_SECRET')
    (context/'code.py').write_text('print("source retained")')
    (context/'Dockerfile').write_text('FROM scratch\nCOPY . /verified-context/\n')
    image='nexus-context-verification:'+uuid.uuid4().hex[:12];container=None;built=False
    config=tmp_path/'docker-config';config.mkdir()
    env={name:os.environ[name] for name in ('PATH','DOCKER_HOST','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH') if name in os.environ}
    env['DOCKER_CONFIG']=str(config)
    try:
        subprocess.run(['docker','build','--network=none','-t',image,str(context)],env=env,capture_output=True,text=True,check=True,timeout=30)
        built=True
        container=subprocess.check_output(['docker','create',image,'/not-executed'],text=True).strip()
        archive=tmp_path/'context.tar'
        subprocess.run(['docker','export','-o',str(archive),container],capture_output=True,text=True,check=True)
        with tarfile.open(archive) as data:
            names={m.name.lstrip('./') for m in data.getmembers()}
            assert data.extractfile('verified-context/code.py').read()==b'print("source retained")'
        assert not {'verified-context/'+name for name in private} & names
    finally:
        if container:subprocess.run(['docker','rm',container],capture_output=True,check=True)
        if built:subprocess.run(['docker','image','rm',image],capture_output=True,check=True)


def run_container(code,*,tmp_path=None,memory='64m',cpu='.1',tmpfs='1m'):
    name='nexus-s11-drill-'+uuid.uuid4().hex[:10]
    args=['docker','run','--name',name,'--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
        '--pids-limit','32','--memory',memory,'--memory-swap',memory,'--cpus',cpu,'--tmpfs','/tmp:rw,noexec,nosuid,size='+tmpfs]
    if tmp_path:args+=['--user',str(os.getuid())+':'+str(os.getgid()),'-v',str(tmp_path)+':/observation']
    try:
        process=subprocess.run(args+['--entrypoint','python',IMAGE,'-c',code],capture_output=True,text=True,timeout=30)
        detail=json.loads(subprocess.run(['docker','inspect',name],capture_output=True,text=True,check=True).stdout)[0]
        return process,detail
    finally:subprocess.run(['docker','rm','-f',name],capture_output=True,check=True)


def test_real_cpu_throttling_is_measured_without_financial_runtime():
    code="import time,json; t=time.monotonic(); initial=open('/sys/fs/cgroup/cpu.stat').read();\nwhile time.monotonic()-t<2: sum(i*i for i in range(10000))\nprint(json.dumps({'initial':initial,'final':open('/sys/fs/cgroup/cpu.stat').read()}))"
    process,detail=run_container(code)
    assert process.returncode==0
    stats=json.loads(process.stdout)
    number=lambda s:int(next(line.split()[1] for line in s.splitlines() if line.startswith('nr_throttled ')))
    assert number(stats['final'])>number(stats['initial'])
    assert detail['HostConfig']['NanoCpus']==100000000


def test_actual_tmpfs_exhaustion_is_visible_and_does_not_claim_persistence():
    code="import errno;\ntry:\n with open('/tmp/bounded-file','wb') as f: f.write(b'x'*2000000)\nexcept OSError as e:\n assert e.errno==errno.ENOSPC\n print('DISK_FULL_DETECTED')\nelse: raise RuntimeError('tmpfs boundary not enforced')"
    process,_=run_container(code)
    assert process.returncode==0 and 'DISK_FULL_DETECTED' in process.stdout


def test_container_oom_restart_keeps_original_durable_spool(tmp_path):
    code="from services.operational_spool import DurableOperationalSpool; from datetime import datetime,timezone; s=DurableOperationalSpool('/observation/spool.db'); s.register(owner_id='alice',source_id='one',scope={}); s.enqueue({'owner_id':'alice','source_id':'one','event_type':'HEALTH_PROBE','occurred_at':datetime.now(timezone.utc).isoformat(),'payload':{'worker_ready':True}},key='before-oom'); chunks=[];\nwhile True: chunks.append(bytearray(8*1024*1024))"
    process,detail=run_container(code,tmp_path=tmp_path,cpu='1')
    assert process.returncode==137 and detail['State']['OOMKilled'] is True
    resumed,_=run_container("from services.operational_spool import DurableOperationalSpool; import json; print(json.dumps(DurableOperationalSpool('/observation/spool.db').health(owner_id='alice')))",tmp_path=tmp_path,cpu='1')
    assert resumed.returncode==0 and json.loads(resumed.stdout)['pending']==1
