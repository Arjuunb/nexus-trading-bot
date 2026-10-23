"""Local operational validation, not staging or production certification."""
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from data.journal_store import JournalStore
from services.shadow_worker_health import write_health, check_health


def test_health_file_is_atomic_bounded_and_checks_freshness(tmp_path):
    path=tmp_path/'worker-health.json';now=datetime.now(timezone.utc)
    value={'readiness':'READY','worker_id':'local-worker','execution_runtime_loaded':False,'candidate_store_status':'AVAILABLE'}
    write_health(path,value,now=now)
    assert check_health(path,now=now)['ready'] is True
    assert check_health(path,now=now+timedelta(seconds=30))['ready'] is False
    assert json.loads(path.read_text())['execution_runtime_loaded'] is False
    assert not list(tmp_path.glob('*.tmp.*'))


def test_health_requires_candidate_metadata_and_refuses_other_files(tmp_path):
    path=tmp_path/'health.json'
    write_health(path,{'readiness':'READY','candidate_store_status':'UNAVAILABLE','execution_runtime_loaded':False})
    assert check_health(path)['ready'] is False
    unrelated=tmp_path/'unrelated.json';unrelated.write_text('{"important":"preserve"}')
    with pytest.raises(ValueError,match='HEALTH_FILE_NAMESPACE'): write_health(unrelated,{'readiness':'READY'})
    assert unrelated.read_text()=='{"important":"preserve"}'
    with pytest.raises(ValueError): write_health(tmp_path/'journal.sqlite',{})


def test_health_rejects_reused_pid_generation(tmp_path):
    path=tmp_path/'health.json'
    write_health(path,{'readiness':'READY','candidate_store_status':'AVAILABLE','execution_runtime_loaded':False})
    state=json.loads(path.read_text());state['process_start_ticks']='0'
    path.write_text(json.dumps(state))
    assert check_health(path)['ready'] is False


def test_postgresql_worker_configuration_fails_closed_before_opening_storage(tmp_path,monkeypatch):
    from services.shadow_worker_main import build_worker
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError,match='UNSUPPORTED_POSTGRESQL_OBSERVATION_BACKEND'):
        build_worker(shadow_db='postgresql://example.invalid/research',journal_db=tmp_path/'journal.sqlite',market_data_dir=tmp_path/'market')
    assert not (tmp_path/'postgresql:').exists()


def test_real_standalone_process_writes_shutdown_health_without_execution(tmp_path):
    hub=Path(__file__).parents[1]
    journal=JournalStore(tmp_path/'journal.sqlite');journal._c.close()
    health=tmp_path/'worker-health.json'
    command=[sys.executable,'-m','services.shadow_worker_main','--shadow-db',str(tmp_path/'shadow.sqlite'),
        '--journal-db',str(tmp_path/'journal.sqlite'),'--market-data-dir',str(tmp_path/'market'),
        '--health-file',str(health)]
    process=subprocess.Popen(command,cwd=hub,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        until=time.monotonic()+15
        while time.monotonic()<until and not health.exists(): time.sleep(.1)
        assert health.exists(),'standalone health was not published'
        assert check_health(health)['ready'] is True
        assert json.loads(health.read_text())['execution_runtime_loaded'] is False
        process.terminate();_,err=process.communicate(timeout=15)
        assert process.returncode==0,err
        assert check_health(health)['ready'] is False
        assert json.loads(health.read_text())['readiness']=='STOPPED'
    finally:
        if process.poll() is None: process.kill();process.communicate(timeout=5)


@pytest.fixture
def supervised_container(tmp_path):
    if os.environ.get('NEXUS_SHADOW_CONTAINER_TEST')!='1':
        pytest.skip('opt-in disposable local Docker supervision/resource validation')
    repo=Path(__file__).parents[2]
    journal=JournalStore(tmp_path/'candidates.sqlite');journal._c.close()
    data=tmp_path/'observation';data.mkdir()
    project='nexus-sprint9-'+uuid.uuid4().hex[:12]
    env={**os.environ,'NEXUS_SHADOW_CANDIDATE_DB':str(tmp_path/'candidates.sqlite'),
         'NEXUS_SHADOW_LOCAL_DATA':str(data),'NEXUS_SHADOW_WORKER_IMAGE':'nexus-shadow-worker:sprint9-local',
         'NEXUS_SHADOW_UID':str(os.getuid()),'NEXUS_SHADOW_GID':str(os.getgid())}
    base=['docker','compose','-p',project,'-f',str(repo/'ops/shadow-worker.local.compose.yaml')]
    def run(*args,timeout=30):
        return subprocess.run([*base,*args],env=env,capture_output=True,text=True,timeout=timeout,check=True)
    try:
        run('up','-d','--no-build')
        cid=run('ps','-q','shadow-worker').stdout.strip()
        until=time.monotonic()+30
        state=None
        while time.monotonic()<until:
            state=json.loads(subprocess.run(['docker','inspect',cid],capture_output=True,text=True,check=True).stdout)[0]
            if state['State'].get('Health',{}).get('Status')=='healthy': break
            time.sleep(.2)
        if state['State'].get('Health',{}).get('Status')!='healthy':
            logs=subprocess.run(['docker','logs',cid],capture_output=True,text=True,check=True)
            pytest.fail('Local worker startup failed: '+logs.stderr[-3000:]+logs.stdout[-1500:])
        yield cid,state,data,run
    finally:
        run('down','--remove-orphans',timeout=30)


def test_local_supervision_restart_and_kernel_resource_controls(supervised_container):
    cid,state,data,run=supervised_container
    assert state['HostConfig']['Memory']==384*1024*1024
    assert state['HostConfig']['NanoCpus']==500000000
    assert state['HostConfig']['PidsLimit']==64
    assert state['HostConfig']['ReadonlyRootfs'] is True
    assert state['HostConfig']['Init'] is True
    assert state['HostConfig']['NetworkMode']=='none'
    assert state['HostConfig']['RestartPolicy']=={'Name':'on-failure','MaximumRetryCount':2}
    assert state['HostConfig']['CapDrop']==['ALL']
    names={v.split('=',1)[0] for v in state['Config']['Env']}
    assert not any('SECRET' in name or name in {'SUPABASE_KEY','BINANCE_API_KEY','BINANCE_API_SECRET','ANTHROPIC_API_KEY'} for name in names)
    probes=subprocess.run(['docker','exec',cid,'python','-c',
        'from pathlib import Path; print(Path("/sys/fs/cgroup/memory.max").read_text().strip()); print(Path("/sys/fs/cgroup/cpu.max").read_text().strip()); print(Path("/sys/fs/cgroup/pids.max").read_text().strip())'],capture_output=True,text=True,check=True)
    memory,cpu,pids=probes.stdout.splitlines()
    assert int(memory)==384*1024*1024 and cpu=='50000 100000' and pids=='64'
    health=json.loads((data/'health.json').read_text())
    assert health['execution_runtime_loaded'] is False
    before=health['worker_id']
    # Docker only enables its restart policy after a successful startup of at
    # least ten seconds. Observe the next genuine five-second health report.
    first=datetime.fromisoformat(health['recorded_at']);until=time.monotonic()+15
    while time.monotonic()<until:
        if datetime.fromisoformat(json.loads((data/'health.json').read_text())['recorded_at'])>=first+timedelta(seconds=5): break
        time.sleep(.1)
    assert datetime.fromisoformat(json.loads((data/'health.json').read_text())['recorded_at'])>=first+timedelta(seconds=5)
    crash=subprocess.run(['docker','exec',cid,'python','-c',
        'import json,os,signal; h=json.load(open("/observation/health.json")); os.kill(h["pid"],signal.SIGKILL)'],capture_output=True)
    assert crash.returncode in {0,137},crash.stderr.decode()
    until=time.monotonic()+30
    current=None
    while time.monotonic()<until:
        current=json.loads(subprocess.run(['docker','inspect',cid],capture_output=True,text=True,check=True).stdout)[0]
        if current['RestartCount']>=1 and current['State'].get('Health',{}).get('Status')=='healthy':break
        time.sleep(.2)
    assert current['RestartCount']==1
    assert current['State']['Health']['Status']=='healthy'
    assert json.loads((data/'health.json').read_text())['worker_id']!=before
    # No authorized registrations means no fabricated forward decisions after
    # restart. The real observer still uses its existing SQLite lease boundary.
    import sqlite3
    db=sqlite3.connect(data/'shadow.sqlite')
    assert db.execute('SELECT COUNT(*) FROM shadow_observations').fetchone()[0]==0
    db.close()
    run('stop','--timeout','10')
    assert json.loads((data/'health.json').read_text())['readiness']=='STOPPED'
