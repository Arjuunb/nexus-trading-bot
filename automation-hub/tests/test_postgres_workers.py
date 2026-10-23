"""Actual disposable PostgreSQL, never a production DSN or SQLite substitute."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import os
import subprocess
import time
import uuid

import pytest

from services.postgres_workers import PgResearchContext, PgShadowObservationStore, migrate_worker_database
from services.strategy_identity import configuration_fingerprint as digest
from test_shadow_router import candidate,frame,T
from services.shadow_router import ShadowRouter,FrozenSignalAdapter


@pytest.fixture(scope='module')
def postgres_server():
    if os.environ.get('NEXUS_PG_WORKER_TEST')!='1': pytest.skip('requires opt-in disposable PostgreSQL worker integration')
    name='nexus-s10-worker-pg-'+uuid.uuid4().hex[:10]
    subprocess.run(['docker','run','-d','--rm','--name',name,'-p','127.0.0.1::5432',
        *(['--tmpfs','/var/lib/postgresql/data:rw,size=512m'] if os.environ.get('NEXUS_PG_TEST_TMPFS')=='1' else []),
        '-e','POSTGRES_HOST_AUTH_METHOD=trust','postgres:16-alpine'],check=True,capture_output=True)
    try:
        port=subprocess.run(['docker','port',name,'5432'],check=True,capture_output=True,text=True).stdout.strip().rsplit(':',1)[1]
        import psycopg
        admin='postgresql://postgres@127.0.0.1:'+port+'/postgres'
        deadline=time.monotonic()+30
        while True:
            try:
                with psycopg.connect(admin,autocommit=True) as db: db.execute('CREATE ROLE nexus_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE')
                break
            except psycopg.OperationalError:
                if time.monotonic()>deadline:raise
                time.sleep(.2)
        yield admin,port,name
    finally: subprocess.run(['docker','rm','-f','-v',name],check=True,capture_output=True)


@pytest.fixture
def pg(postgres_server):
    import psycopg
    from psycopg import sql
    admin,port,*_=postgres_server;name='nexus_workers_'+uuid.uuid4().hex[:12]
    with psycopg.connect(admin,autocommit=True) as db: db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    operator=admin.rsplit('/',1)[0]+'/'+name
    migrate_worker_database(operator,target='research_shadow_metadata',worker_role='nexus_worker')
    migrate_worker_database(operator,target='research_shadow_metadata',worker_role='nexus_worker')
    dsn='postgresql://nexus_worker@127.0.0.1:'+port+'/'+name
    yield dsn,operator
    with psycopg.connect(admin,autocommit=True) as db: db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def job(key='job-key-1',owner='alice'):
    frozen={'min_quality':75}
    return {'job_id':uuid.uuid4().hex,'idempotency_key':key,'owner_id':owner,'job_type':'HISTORICAL_BACKTEST',
        'strategy_id':'adaptive_trend_pullback','strategy_version':'1.0.0','config_fingerprint':digest(frozen),
        'frozen_config':frozen,'status':'QUEUED','request':{'symbol':'XRPUSDT','timeframe':'5m'},
        'requested_at':datetime.now(timezone.utc).isoformat()}


def claim(ctx,worker='A',active=2):
    result=ctx.claim_research_job(worker_id=worker,max_active=active,lease_seconds=5)
    if result:ctx.transition_research_job(result['job_id'],to_status='RUNNING',worker_id=worker,lease_token=result['lease_token'])
    return result


def result(j):
    report={'status':'COMPLETE','method':'historical_backtest','fixture':True}
    validation={'run_id':'validation:'+j['job_id'],'strategy_id':j['strategy_id'],'strategy_version':j['strategy_version'],
        'config_fingerprint':j['config_fingerprint'],'dataset_identity':'fixture-dataset','execution_mode':'backtest',
        'method':'historical_backtest','status':'COMPLETE','owner_id':j['owner_id'],'report':report}
    return report,validation


def expire(operator,job_id):
    import psycopg
    with psycopg.connect(operator,autocommit=True) as db:
        db.execute("UPDATE nexus_workers.research_jobs SET lease_expires_at=(clock_timestamp()-interval '1 second')::text WHERE job_id=%s",(job_id,))


def test_two_real_connections_claim_once(pg):
    first=PgResearchContext(pg[0]);second=PgResearchContext(pg[0]);j=first.create_research_job(job())
    with ThreadPoolExecutor(2) as threads:
        values=list(threads.map(lambda c:c.claim_research_job(worker_id=uuid.uuid4().hex,max_active=2),[first,second]))
    assert sum(v is not None for v in values)==1
    assert first.get_research_job(j['job_id'])['attempt_number']==1


def test_dead_connection_recovers_same_worker_handle_and_preserves_authority(pg):
    import psycopg
    ctx=PgResearchContext(pg[0]);j=ctx.create_research_job(job());authority=ctx.journal.authority_id
    pid=ctx.journal.execute('SELECT pg_backend_pid()').fetchone()[0]
    with psycopg.connect(pg[1],autocommit=True) as db:db.execute('SELECT pg_terminate_backend(%s)',(pid,))
    with pytest.raises(psycopg.Error):ctx.get_research_job(j['job_id'])
    assert ctx.get_research_job(j['job_id'])['job_id']==j['job_id']
    assert ctx.journal.authority_id==authority
    assert claim(ctx)['job_id']==j['job_id']


def test_real_postgres_backup_restore_preserves_result_and_authority(pg,postgres_server):
    import psycopg
    from psycopg import sql
    context=PgResearchContext(pg[0]);j=context.create_research_job(job());attempt=claim(context)
    report,validation=result(j);context.commit_research_result(j['job_id'],worker_id='A',lease_token=attempt['lease_token'],report=report,validation=validation)
    container=postgres_server[2];source_name=pg[1].rsplit('/',1)[1];restore_name='nexus_restore_'+uuid.uuid4().hex[:10]
    backup=subprocess.run(['docker','exec',container,'pg_dump','-U','postgres','--schema=nexus_workers',source_name],check=True,capture_output=True).stdout
    assert len(backup)>1000
    with psycopg.connect(postgres_server[0],autocommit=True) as db:db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(restore_name)))
    try:
        subprocess.run(['docker','exec','-i',container,'psql','-U','postgres','-v','ON_ERROR_STOP=1','-d',restore_name],input=backup,check=True,capture_output=True)
        restored=PgResearchContext(pg[0].rsplit('/',1)[0]+'/'+restore_name)
        assert restored.journal.authority_id==context.journal.authority_id
        assert restored.get_research_job(j['job_id'])==context.get_research_job(j['job_id'])
        assert restored.get_validation_run(validation['run_id'])==context.get_validation_run(validation['run_id'])
        migrate_worker_database(pg[1].rsplit('/',1)[0]+'/'+restore_name,target='research_shadow_metadata',worker_role='nexus_worker')
        assert len(restored.validation_runs(filters={'owner_id':'alice'}))==1
    finally:
        with psycopg.connect(postgres_server[0],autocommit=True) as db:db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(restore_name)))


def test_migration_failure_rolls_back_schema_and_clean_rerun_preserves_rows(pg,monkeypatch):
    import psycopg
    from pathlib import Path
    context=PgResearchContext(pg[0]);j=context.create_research_job(job());read=Path.read_text
    def damaged(path,*args,**kwargs):
        text=read(path,*args,**kwargs)
        return text+'\nSELECT nonexistent_partial_migration_function();' if path.name=='0001_postgresql_worker_metadata.sql' else text
    with monkeypatch.context() as patch:
        patch.setattr(Path,'read_text',damaged)
        with pytest.raises(psycopg.Error):migrate_worker_database(pg[1],target='research_shadow_metadata',worker_role='nexus_worker')
    assert context.get_research_job(j['job_id'])['job_id']==j['job_id']
    migrate_worker_database(pg[1],target='research_shadow_metadata',worker_role='nexus_worker')
    assert context.get_research_job(j['job_id'])['status']=='QUEUED'


def test_research_attempt_emits_durable_owner_scoped_operational_facts(pg):
    from services.research_jobs import ResearchJobWorker
    from services.operational_evidence import OperationalEvidenceStore
    ctx=PgResearchContext(pg[0]);j=ctx.create_research_job(job());claimed=ctx.claim_research_job(worker_id='telemetry-worker')
    class FixtureService:
        def _execute(self,job,checkpoint):checkpoint();return result(j)[0]
        def validation_record(self,job,report):return result(j)[1]
    worker=ResearchJobWorker(ctx,worker_id='telemetry-worker',service=FixtureService());worker.process_claimed(claimed)
    store=OperationalEvidenceStore(PgShadowObservationStore(pg[0]))
    start=datetime.now(timezone.utc)-timedelta(minutes=1);end=start+timedelta(minutes=2)
    rows=store.events(j['job_id'],owner_id='alice',start=start,end=end)
    assert {'WORKER_STARTED','HEALTH_PROBE','EVALUATION_COMPLETED','COMMIT_SUCCEEDED','MONITORING_STOPPED'}<={r['event_type'] for r in rows}
    assert store.events(j['job_id'],owner_id='bob',start=start,end=end)==[]


def test_lost_commit_acknowledgement_recovers_one_logical_completed_result(pg,monkeypatch):
    from services.research_jobs import ResearchJobWorker
    ctx=PgResearchContext(pg[0]);j=ctx.create_research_job(job());attempt=ctx.claim_research_job(worker_id='ack-worker')
    class FixtureService:
        def _execute(self,job,checkpoint):checkpoint();return result(j)[0]
        def validation_record(self,job,report):return result(j)[1]
    original=ctx.commit_research_result
    def interrupted_ack(*args,**kwargs):original(*args,**kwargs);raise ConnectionError('injected lost acknowledgement')
    monkeypatch.setattr(ctx,'commit_research_result',interrupted_ack)
    ResearchJobWorker(ctx,worker_id='ack-worker',service=FixtureService()).process_claimed(attempt)
    assert ctx.get_research_job(j['job_id'])['status']=='COMPLETED'
    assert len(ctx.validation_runs(filters={'owner_id':'alice'}))==1
    assert ctx.recover_expired_research_jobs()==0


def test_expired_worker_fenced_and_replacement_commits_once(pg):
    a=PgResearchContext(pg[0]);b=PgResearchContext(pg[0]);j=a.create_research_job(job());old=claim(a)
    expire(pg[1],j['job_id']);b.recover_expired_research_jobs();new=claim(b,'B')
    assert new['attempt_number']>old['attempt_number']
    report,validation=result(j)
    with pytest.raises(ValueError,match='FENC'): a.commit_research_result(j['job_id'],worker_id='A',lease_token=old['lease_token'],report=report,validation=validation)
    completed=b.commit_research_result(j['job_id'],worker_id='B',lease_token=new['lease_token'],report=report,validation=validation)
    assert completed['status']=='COMPLETED', (completed.get('error_class'),completed.get('error_message'))
    assert b.commit_research_result(j['job_id'],worker_id='B',lease_token=new['lease_token'],report=report,validation=validation)==completed
    assert len(b.validation_runs(filters={'owner_id':'alice'}))==1
    assert PgResearchContext(pg[0]).get_research_job(j['job_id'])['status']=='COMPLETED'


def test_cancellation_cannot_be_recovered_as_a_new_execution(pg):
    c=PgResearchContext(pg[0]);j=c.create_research_job(job());owned=claim(c)
    c.request_research_cancellation(j['job_id'],owner_id='alice');expire(pg[1],j['job_id'])
    c.recover_expired_research_jobs()
    assert c.get_research_job(j['job_id'])['status']=='CANCELLED'
    assert claim(c,'B') is None
    report,validation=result(j)
    with pytest.raises(ValueError):c.commit_research_result(j['job_id'],worker_id='A',lease_token=owned['lease_token'],report=report,validation=validation)
    assert c.validation_runs(filters={'owner_id':'alice'})==[]


def test_duplicate_submission_conflict_and_tenant_scope(pg):
    c=PgResearchContext(pg[0]);original=job();stored=c.create_research_job(original)
    assert c.create_research_job({**original,'job_id':uuid.uuid4().hex})['job_id']==stored['job_id']
    with pytest.raises(ValueError,match='IDEMPOTENCY'):c.create_research_job({**original,'strategy_version':'2.0.0'})
    assert c.get_research_job(stored['job_id'],owner_id='bob') is None
    assert c.research_jobs(filters={'owner_id':'bob'})==[]


def test_expiry_without_reclaim_blocks_renewal_and_commit(pg):
    c=PgResearchContext(pg[0]);j=c.create_research_job(job());owned=claim(c);expire(pg[1],j['job_id'])
    assert c.heartbeat_research_job(j['job_id'],worker_id='A',lease_token=owned['lease_token']) is False
    report,validation=result(j)
    with pytest.raises(ValueError):c.commit_research_result(j['job_id'],worker_id='A',lease_token=owned['lease_token'],report=report,validation=validation)


def test_real_concurrent_jobs_obey_global_quota(pg):
    contexts=[PgResearchContext(pg[0]) for _ in range(4)]
    for i in range(4):contexts[0].create_research_job(job('job-'+str(i)))
    with ThreadPoolExecutor(4) as workers:
        owned=list(workers.map(lambda c:c.claim_research_job(worker_id=uuid.uuid4().hex,max_active=2),contexts))
    assert len({r['job_id'] for r in owned if r})==2
    assert sum(r is not None for r in owned)==2


def test_rollback_and_conflicting_or_partial_result_cannot_add_validation(pg):
    c=PgResearchContext(pg[0]);j=c.create_research_job(job());owned=claim(c);report,validation=result(j)
    with pytest.raises(ValueError):c.commit_research_result(j['job_id'],worker_id='A',lease_token=owned['lease_token'],report={**report,'status':'PARTIAL'},validation=validation)
    assert c.validation_runs(filters={'owner_id':'alice'})==[]
    with pytest.raises(RuntimeError):
        with c.journal.transaction():
            c.record_validation_run(validation)
            raise RuntimeError('simulate commit failure')
    assert c.validation_runs(filters={'owner_id':'alice'})==[]
    c.commit_research_result(j['job_id'],worker_id='A',lease_token=owned['lease_token'],report=report,validation=validation)
    with pytest.raises(ValueError,match='CONFLICT'):c.commit_research_result(j['job_id'],worker_id='A',lease_token=owned['lease_token'],report={**report,'conflict':True},validation=validation)
    assert len(c.validation_runs(filters={'owner_id':'alice'}))==1


def test_heartbeat_cancellation_race_always_fences_completion(pg):
    c=PgResearchContext(pg[0]);other=PgResearchContext(pg[0]);j=c.create_research_job(job());owned=claim(c)
    with ThreadPoolExecutor(2) as workers:
        heart=workers.submit(c.heartbeat_research_job,j['job_id'],worker_id='A',lease_token=owned['lease_token'])
        cancel=workers.submit(other.request_research_cancellation,j['job_id'],owner_id='alice')
        heart.result();cancel.result()
    report,validation=result(j)
    with pytest.raises(ValueError):c.commit_research_result(j['job_id'],worker_id='A',lease_token=owned['lease_token'],report=report,validation=validation)


def test_runtime_role_is_not_an_operator_and_financial_target_rejected(pg):
    import psycopg
    with pytest.raises(ValueError,match='UNSAFE_WORKER_DATABASE_ROLE'):PgResearchContext(pg[1])
    with pytest.raises(ValueError,match='INVALID_MIGRATION_TARGET'):migrate_worker_database(pg[1],target='financial',worker_role='nexus_worker')
    with psycopg.connect(pg[1],autocommit=True) as db:db.execute('CREATE TABLE public.paper_trades(secret TEXT)')
    with pytest.raises(ValueError,match='FINANCIAL_DATABASE_FORBIDDEN'):migrate_worker_database(pg[1],target='research_shadow_metadata',worker_role='nexus_worker')
    with pytest.raises(ValueError,match='FINANCIAL_DATABASE_FORBIDDEN'):PgResearchContext(pg[0])


def test_existing_simulator_commits_using_retained_postgres_input(pg):
    from services.research_jobs import ResearchJobService,ResearchJobWorker,_manifest
    from services.research_dataset_retention import ResearchDatasetRetention
    c=PgResearchContext(pg[0]);service=ResearchJobService(c)
    frozen=candidate()['frozen_config']
    bars=frame()['context']['5m'];request={'job_type':'HISTORICAL_BACKTEST','strategy_id':'adaptive_trend_pullback',
        'strategy_version':'1.0.0','config_fingerprint':digest(frozen),'frozen_config':frozen,'symbol':'XRPUSDT','timeframe':'5m','idempotency_key':'retained-real-simulator'}
    j=service.create(request,owner_id='alice',scope={})
    manifest=_manifest(bars,symbol='XRPUSDT',timeframe='5m',request=j['request'],source='fixture-binance-usdm')
    manifest['coverage']={'status':'UNKNOWN','warmup_candles':0,'evaluation_candles':len(bars),'missing_candles':None,'duplicate_candles':None}
    ResearchDatasetRetention(c).retain(j,manifest,bars)
    completed=ResearchJobWorker(c).process_job(j['job_id'],owner_id='alice')
    assert completed['status']=='COMPLETED', (completed.get('error_class'),completed.get('error_message'))
    assert completed['result']['dataset']['content_hash']==manifest['content_hash']
    assert len(c.validation_runs(filters={'owner_id':'alice'}))==1


def test_shadow_concurrent_claim_pause_fencing_and_immutable_decision(pg):
    first=PgShadowObservationStore(pg[0]);second=PgShadowObservationStore(pg[0]);cand=candidate();clock=[T-timedelta(seconds=1)]
    router=ShadowRouter(first,candidate_reader=lambda _,owner:deepcopy(cand) if owner=='alice' else None,clock=lambda:clock[0])
    reg=router.register({'candidate_id':cand['candidate_id'],'idempotency_key':'pg-shadow-key','authorize':True,
        'authorization_reason':'Reviewed fixture observation','lookback_spec':FrozenSignalAdapter(cand).lookback},owner_id='alice')
    router.action(reg['registration_id'],'VALIDATE',owner_id='alice',reason='reviewed')
    router.action(reg['registration_id'],'START',owner_id='alice',reason='observe')
    clock[0]=T+timedelta(seconds=1)
    with ThreadPoolExecutor(2) as workers:
        claims=list(workers.map(lambda store:store.claim(reg['registration_id'],uuid.uuid4().hex,now=clock[0].timestamp()),(first,second)))
    assert sum(v is not None for v in claims)==1
    owned=next(v for v in claims if v)
    decision=router.evaluate(reg['registration_id'],frame(),owner_id='alice',lease_token=owned['lease_token'])
    assert decision['decision_id']
    assert len(first.decisions(reg['registration_id'],owner_id='alice'))==1
    router.action(reg['registration_id'],'PAUSE',owner_id='alice',reason='pause')
    with pytest.raises(ValueError):first.commit(decision,generation=owned['generation'],lease_token=owned['lease_token'],now=clock[0].timestamp())
    assert first.get(reg['registration_id'],owner_id='bob') is None


def test_disconnect_then_restart_recovers_without_duplicate_result(pg):
    import psycopg
    old=PgResearchContext(pg[0]);j=old.create_research_job(job());owned=claim(old)
    pid=old.journal._connection.info.backend_pid
    with psycopg.connect(pg[1],autocommit=True) as admin:admin.execute('SELECT pg_terminate_backend(%s)',(pid,))
    with pytest.raises(psycopg.OperationalError):old.heartbeat_research_job(j['job_id'],worker_id='A',lease_token=owned['lease_token'])
    expire(pg[1],j['job_id']);new=PgResearchContext(pg[0]);new.recover_expired_research_jobs();replacement=claim(new,'B')
    report,validation=result(j)
    new.commit_research_result(j['job_id'],worker_id='B',lease_token=replacement['lease_token'],report=report,validation=validation)
    restarted=PgResearchContext(pg[0])
    assert restarted.get_research_job(j['job_id'])['status']=='COMPLETED'
    assert len(restarted.validation_runs(filters={'owner_id':'alice'}))==1


def test_material_identity_and_committed_artifact_cannot_be_rewritten(pg):
    import psycopg
    c=PgResearchContext(pg[0]);j=c.create_research_job(job())
    with pytest.raises(psycopg.errors.RaiseException):
        c.journal.execute("UPDATE research_jobs SET strategy_version='9.9.9' WHERE job_id=?",(j['job_id'],))
    assert c.get_research_job(j['job_id'])['strategy_version']=='1.0.0'
    owned=claim(c);report,validation=result(j)
    c.commit_research_result(j['job_id'],worker_id='A',lease_token=owned['lease_token'],report=report,validation=validation)
    with pytest.raises(psycopg.errors.RaiseException):c.journal.execute("UPDATE strategy_validation_runs SET report_json='{}' WHERE run_id=?",(validation['run_id'],))


def test_worker_shutdown_keeps_incomplete_artifacts_out_of_qualification(pg):
    from services.research_jobs import ResearchJobService,ResearchJobWorker
    c=PgResearchContext(pg[0]);j=c.create_research_job(job());service=ResearchJobService(c);worker=ResearchJobWorker(c,service=service)
    def interrupted(job,checkpoint):
        worker._stop.set();checkpoint()
        raise AssertionError('shutdown checkpoint must interrupt')
    service._execute=interrupted
    owned=c.claim_research_job(worker_id=worker.worker_id)
    worker.process_claimed(owned)
    assert c.get_research_job(j['job_id'])['status']=='RETRY_PENDING'
    assert c.validation_runs(filters={'owner_id':'alice'})==[]


def test_real_independent_research_process_health_and_graceful_shutdown(pg,tmp_path):
    import json,sys
    from pathlib import Path
    from services.shadow_worker_health import check_health
    hub=Path(__file__).parents[1];health=tmp_path/'research-health.json'
    environment={k:v for k,v in os.environ.items() if k not in {'BINANCE_API_KEY','BINANCE_API_SECRET','SUPABASE_KEY','ALPACA_API_KEY','ALPACA_SECRET_KEY','BYBIT_API_KEY','BYBIT_API_SECRET','OANDA_API_KEY'}}
    environment['NEXUS_RESEARCH_WORKER_DSN']=pg[0]
    process=subprocess.Popen([sys.executable,'-m','services.research_worker_main','--backend','postgresql','--health-file',str(health)],cwd=hub,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline and not health.exists():time.sleep(.1)
        assert health.exists()
        assert check_health(health)['ready'] is True
        state=json.loads(health.read_text());assert state['backend']=='postgresql' and state['execution_runtime_loaded'] is False
        process.terminate();_,errors=process.communicate(timeout=15)
        assert process.returncode==0,errors
        assert json.loads(health.read_text())['readiness']=='STOPPED'
    finally:
        if process.poll() is None:process.kill();process.communicate(timeout=5)


def test_actual_shadow_lease_expiry_reclaims_and_fences_stale_commit(pg):
    import psycopg
    first=PgShadowObservationStore(pg[0]);second=PgShadowObservationStore(pg[0]);cand=candidate();clock=[T-timedelta(seconds=1)]
    router=ShadowRouter(first,candidate_reader=lambda *_:deepcopy(cand),clock=lambda:clock[0])
    reg=router.register({'candidate_id':cand['candidate_id'],'idempotency_key':'expire-shadow-key','authorize':True,
        'authorization_reason':'Reviewed isolated fixture','lookback_spec':FrozenSignalAdapter(cand).lookback},owner_id='alice')
    router.action(reg['registration_id'],'VALIDATE',owner_id='alice',reason='reviewed');router.action(reg['registration_id'],'START',owner_id='alice',reason='observe')
    clock[0]=T+timedelta(seconds=1);old=first.claim(reg['registration_id'],'A',now=clock[0].timestamp())
    with psycopg.connect(pg[1],autocommit=True) as db:db.execute('UPDATE nexus_workers.shadow_registrations SET lease_expiry=0 WHERE registration_id=%s',(reg['registration_id'],))
    assert first.heartbeat(reg['registration_id'],old['lease_token'],now=clock[0].timestamp()) is False
    new=second.claim(reg['registration_id'],'B',now=clock[0].timestamp())
    assert new['generation']>old['generation'] and new['lease_token']!=old['lease_token']
    decision=router.evaluate(reg['registration_id'],frame(),owner_id='alice',lease_token=new['lease_token'])
    with pytest.raises(ValueError,match='STALE_SHADOW_ATTEMPT'):first.commit(decision,generation=old['generation'],lease_token=old['lease_token'],now=clock[0].timestamp())
    assert len(second.decisions(reg['registration_id'],owner_id='alice'))==1


def test_real_pg_forward_and_paper_artifact_commitment(pg):
    from services.forward_qualification_store import ForwardQualificationStore
    from services.forward_qualification import ForwardQualificationService
    from test_forward_qualification import research_policy
    from services.paper_reconciliation_store import PaperReconciliationService
    from test_paper_reconciliation import complete as complete_fixture,T as PAPER_T
    store=PgShadowObservationStore(pg[0]);cand=candidate();clock=[T-timedelta(seconds=1)]
    router=ShadowRouter(store,candidate_reader=lambda *_:deepcopy(cand),clock=lambda:clock[0])
    reg=router.register({'candidate_id':cand['candidate_id'],'idempotency_key':'pg-forward-key','authorize':True,
        'authorization_reason':'Reviewed isolated fixture','lookback_spec':FrozenSignalAdapter(cand).lookback},owner_id='alice')
    router.action(reg['registration_id'],'VALIDATE',owner_id='alice',reason='reviewed');router.action(reg['registration_id'],'START',owner_id='alice',reason='observe')
    clock[0]=T+timedelta(seconds=1);lease=store.claim(reg['registration_id'],'observer',now=clock[0].timestamp())
    router.evaluate(reg['registration_id'],frame(),owner_id='alice',lease_token=lease['lease_token']);clock[0]=T+timedelta(minutes=5)
    service=ForwardQualificationService(router,ForwardQualificationStore(store),policies=[research_policy()])
    request={'source_id':reg['registration_id'],'source_kind':'FORWARD_SHADOW_DECISION','start':T.isoformat(),'end':clock[0].isoformat(),'policy_version':'fixture-forward.v1','idempotency_key':'pg-artifact-forward'}
    report=service.create(request,owner_id='alice')
    assert service.create(request,owner_id='alice')==report
    assert report['outcome']=='BEHAVIOUR_ONLY'
    bundle=complete_fixture.__wrapped__()
    class Reader:
        def read(self,*_,**kwargs):return deepcopy(bundle)
    recon=PaperReconciliationService(store,Reader(),clock=lambda:PAPER_T+timedelta(minutes=10))
    body={'source_id':'fixture-account','start':PAPER_T.isoformat(),'end':(PAPER_T+timedelta(minutes=5)).isoformat(),'idempotency_key':'pg-paper-artifact'}
    result=recon.assess(body,owner_id='alice',commit=True)
    assert result['status']=='VERIFIED'
    assert recon.assess(body,owner_id='alice',commit=True)==result
    assert recon.get(result['report_id'],owner_id='bob') is None


@pytest.mark.parametrize('kind',['research','shadow'])
def test_real_container_research_supervision_and_kernel_limits(pg,postgres_server,tmp_path,kind):
    if os.environ.get('NEXUS_PG_SUPERVISOR_TEST')!='1':pytest.skip('opt-in isolated PG worker container supervision')
    from pathlib import Path
    import json
    repo=Path(__file__).parents[2];project='nexus-s10-research-'+uuid.uuid4().hex[:10]
    network=project+'-private';pg_name=postgres_server[2];data=tmp_path/'health';data.mkdir()
    database=pg[0].rsplit('/',1)[1]
    research_job=None
    if kind=='research':
        from services.research_jobs import ResearchJobService,_manifest
        from services.research_dataset_retention import ResearchDatasetRetention
        context=PgResearchContext(pg[0]);frozen=candidate()['frozen_config'];bars=frame()['context']['5m']
        research_job=ResearchJobService(context).create({'job_type':'HISTORICAL_BACKTEST','strategy_id':'adaptive_trend_pullback',
            'strategy_version':'1.0.0','config_fingerprint':digest(frozen),'frozen_config':frozen,'symbol':'XRPUSDT','timeframe':'5m','idempotency_key':'container-real-research'},owner_id='alice',scope={})
        manifest=_manifest(bars,symbol='XRPUSDT',timeframe='5m',request=research_job['request'],source='fixture-binance-usdm')
        manifest['coverage']={'status':'UNKNOWN','warmup_candles':0,'evaluation_candles':len(bars),'missing_candles':None,'duplicate_candles':None}
        ResearchDatasetRetention(context).retain(research_job,manifest,bars)
    environment={**os.environ,'NEXUS_RESEARCH_LOCAL_DSN':'postgresql://nexus_worker@pg:5432/'+database,
        'NEXUS_RESEARCH_LOCAL_NETWORK':network,'NEXUS_RESEARCH_LOCAL_DATA':str(data),
        'NEXUS_RESEARCH_UID':str(os.getuid()),'NEXUS_RESEARCH_GID':str(os.getgid())}
    base=['docker','compose','--profile','shadow','-p',project,'-f',str(repo/'ops/research-workers.local.compose.yaml')]
    def compose(*args):return subprocess.run([*base,*args],env=environment,capture_output=True,text=True,check=True,timeout=30)
    subprocess.run(['docker','network','create','--internal',network],check=True,capture_output=True)
    subprocess.run(['docker','network','connect','--alias','pg',network,pg_name],check=True,capture_output=True)
    try:
        compose('up','-d','--no-build',kind+'-worker');cid=compose('ps','-q',kind+'-worker').stdout.strip();deadline=time.monotonic()+30
        state={}
        while time.monotonic()<deadline:
            state=json.loads(subprocess.run(['docker','inspect',cid],check=True,capture_output=True,text=True).stdout)[0]
            if state['State'].get('Health',{}).get('Status')=='healthy':break
            time.sleep(.2)
        assert state['State']['Health']['Status']=='healthy'
        if research_job:
            finished=context.get_research_job(research_job['job_id'])
            assert finished['status']=='COMPLETED',(finished['status'],finished.get('error_class'),finished.get('error_message'))
            assert finished['result']['dataset']['content_hash']==manifest['content_hash']
            assert len(context.validation_runs(filters={'owner_id':'alice'}))==1
        memory=(512 if kind=='research' else 384)*1024*1024
        assert state['HostConfig']['ReadonlyRootfs'] is True and state['HostConfig']['Memory']==memory
        assert state['HostConfig']['NanoCpus']==(1000000000 if kind=='research' else 500000000) and state['HostConfig']['PidsLimit']==64
        assert state['HostConfig']['CapDrop']==['ALL']
        probe=subprocess.run(['docker','exec',cid,'python','-c',
            'from pathlib import Path;print(Path("/sys/fs/cgroup/memory.max").read_text().strip());print(Path("/sys/fs/cgroup/cpu.max").read_text().strip());print(Path("/sys/fs/cgroup/pids.max").read_text().strip())'],check=True,capture_output=True,text=True)
        assert probe.stdout.splitlines()==[str(memory),'100000 100000' if kind=='research' else '50000 100000','64']
        health=data/(kind+'-health.json')
        original=json.loads(health.read_text());first=datetime.fromisoformat(original['recorded_at']);deadline=time.monotonic()+15
        while time.monotonic()<deadline and datetime.fromisoformat(json.loads(health.read_text())['recorded_at'])<first+timedelta(seconds=5):time.sleep(.1)
        assert datetime.fromisoformat(json.loads(health.read_text())['recorded_at'])>=first+timedelta(seconds=5)
        crash=subprocess.run(['docker','exec',cid,'python','-c','import json,os,signal;h=json.load(open("/observation/'+kind+'-health.json"));os.kill(h["pid"],signal.SIGKILL)'],capture_output=True)
        assert crash.returncode in {0,137}
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            state=json.loads(subprocess.run(['docker','inspect',cid],check=True,capture_output=True,text=True).stdout)[0]
            if state['RestartCount'] and state['State'].get('Health',{}).get('Status')=='healthy':break
            time.sleep(.2)
        assert state['RestartCount']==1 and state['State']['Health']['Status']=='healthy'
        assert json.loads(health.read_text())['worker_id']!=original['worker_id']
        assert json.loads(health.read_text())['execution_runtime_loaded'] is False
        names={item.split('=',1)[0] for item in state['Config']['Env']}
        assert not names & {'BINANCE_API_KEY','BINANCE_API_SECRET','SUPABASE_KEY'}
        compose('stop','--timeout','10',kind+'-worker')
        assert json.loads(health.read_text())['readiness']=='STOPPED'
    finally:
        compose('down','--remove-orphans')
        subprocess.run(['docker','network','disconnect',network,pg_name],check=True,capture_output=True)
        subprocess.run(['docker','network','rm',network],check=True,capture_output=True)
