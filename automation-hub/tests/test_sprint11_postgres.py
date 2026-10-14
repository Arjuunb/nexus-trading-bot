"""Real disposable PostgreSQL cutover, outbox recovery and artifact persistence."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import sqlite3

import pytest

from test_postgres_workers import pg,postgres_server,job,result
from data.journal_store import JournalStore
from services.metadata_cutover import frozen_export,import_rehearsal
from services.postgres_workers import PgResearchContext,PgShadowObservationStore
from services.operational_evidence import OperationalEvidenceStore
from services.operational_spool import DurableOperationalSpool,BufferedOperationalStore
from services.local_readiness import LocalReadinessStore


def test_local_cutover_preserves_original_jobs_attempts_results_and_reruns(pg,tmp_path):
    source=JournalStore(tmp_path/'journal.sqlite');ctx=source.context
    j=ctx.create_research_job(job());claimed=ctx.claim_research_job(worker_id='original-worker',lease_seconds=60,max_active=1)
    ctx.transition_research_job(j['job_id'],to_status='RUNNING',worker_id='original-worker',lease_token=claimed['lease_token'])
    report,validation=result(j)
    ctx.commit_research_result(j['job_id'],worker_id='original-worker',lease_token=claimed['lease_token'],report=report,validation=validation)
    original=ctx.get_research_job(j['job_id']);source._c.commit()
    manifest=frozen_export([source.path],tmp_path/'frozen-backup')
    target=PgResearchContext(pg[0])
    rehearsal=import_rehearsal(manifest,pg[1],dry_run=True)
    assert rehearsal['status']=='LOCAL_VERIFIED'
    assert target.get_research_job(j['job_id']) is None
    imported=import_rehearsal(manifest,pg[1],dry_run=False)
    assert imported['backend_authority_changed'] is False
    restored=target.get_research_job(j['job_id'])
    for field in ('job_id','owner_id','config_fingerprint','frozen_config','result_reference','request','status','attempt_number'):
        assert restored[field]==original[field]
    assert target.get_validation_run('validation:'+j['job_id'])['report']==report
    assert import_rehearsal(manifest,pg[1],dry_run=False)['imported_rows']==0


def test_cutover_conflict_rolls_back_entire_import(pg,tmp_path):
    source=tmp_path/'legacy.sqlite'
    with sqlite3.connect(source) as db:
        db.executescript('CREATE TABLE operational_sources(owner_id TEXT,source_id TEXT,scope_json TEXT,created_at TEXT,PRIMARY KEY(owner_id,source_id));')
        db.execute('INSERT INTO operational_sources VALUES (?,?,?,?)',('alice','first','{}','2026-10-12T08:00:00+00:00'))
        db.execute('INSERT INTO operational_sources VALUES (?,?,?,?)',('alice','conflict','{}','2026-10-12T08:00:00+00:00'))
    manifest=frozen_export([source],tmp_path/'backup')
    op=OperationalEvidenceStore(PgShadowObservationStore(pg[0]));op.register(owner_id='alice',source_id='conflict',scope={'original':True})
    with pytest.raises(ValueError,match='CONFLICT'):import_rehearsal(manifest,pg[1],dry_run=False)
    assert op.source('first',owner_id='alice') is None


def test_real_database_disconnect_spool_restart_replays_once(pg,tmp_path):
    import psycopg
    store=OperationalEvidenceStore(PgShadowObservationStore(pg[0]));spool=DurableOperationalSpool(tmp_path/'spool.db')
    proxy=BufferedOperationalStore(store,spool);proxy.register(owner_id='alice',source_id='outage-worker',scope={'timeframe':'5m'})
    raw=store.shadow._store;pid=raw._connection.info.backend_pid
    with psycopg.connect(pg[1],autocommit=True) as db:db.execute('SELECT pg_terminate_backend(%s)',(pid,))
    now=datetime.now(timezone.utc)
    event={'owner_id':'alice','source_id':'outage-worker','event_type':'HEALTH_PROBE','occurred_at':now.isoformat(),'payload':{'worker_ready':True,'feed_connected':None}}
    # First dead-connection operation is not automatically replayed.
    assert proxy.emit(event,key='original-event')['commitment_status']=='BUFFERED'
    recovered=DurableOperationalSpool(spool.path);replacement=OperationalEvidenceStore(PgShadowObservationStore(pg[0]))
    assert recovered.replay(replacement)['delivered']==1
    assert recovered.replay(replacement)['delivered']==0
    assert len(replacement.events('outage-worker',owner_id='alice',start=now,end=now+timedelta(seconds=1)))==1


def test_real_pg_local_readiness_immutable_and_owner_scoped(pg):
    store=LocalReadinessStore(PgShadowObservationStore(pg[0]));now=datetime.now(timezone.utc)
    report=store.commit(owner_id='alice',environment_id='disposable:postgres-fixture',software_revision='fixture-only',
        database_schema_version='workers.2',worker_backend='postgresql',start=now-timedelta(minutes=1),end=now,
        reviewer='local-test',gates={'postgresql':{'status':'LOCAL_VERIFIED','evidence_reference':'real-pg-test','evidence_hash':'a'*64}})
    assert store.get(report['assessment_id'],owner_id='bob') is None
    assert store.get(report['assessment_id'],owner_id='alice')==report
    assert report['status']=='PARTIAL' and report['production_certification'] is False
    with pytest.raises(Exception,match='immutable'):
        with store.shadow.transaction() as db:db.execute("UPDATE local_readiness_assessments SET evidence_hash='changed'")


def test_total_metadata_outage_original_events_survive_local_restart(pg,postgres_server,tmp_path):
    import subprocess
    import time
    name=postgres_server[2]
    store=OperationalEvidenceStore(PgShadowObservationStore(pg[0]));store.shadow._store._connection.close()
    spool=DurableOperationalSpool(tmp_path/'outage.db');proxy=BufferedOperationalStore(store,spool)
    start=datetime.now(timezone.utc)
    subprocess.run(['docker','pause',name],check=True,capture_output=True)
    try:
        proxy.register(owner_id='alice',source_id='whole-outage',scope={'timeframe':'5m'})
        originals=[]
        for i in range(2):
            event={'owner_id':'alice','source_id':'whole-outage','event_type':'DATABASE_UNAVAILABLE',
                'occurred_at':datetime.now(timezone.utc).isoformat(),'payload':{'probe_index':i,'worker_ready':True,'feed_connected':None}}
            original=proxy.emit(event,key='outage-'+str(i));originals.append(original['event_id'])
            assert original['commitment_status']=='BUFFERED'
        assert DurableOperationalSpool(spool.path).health(owner_id='alice')['pending']==2
    finally:subprocess.run(['docker','unpause',name],check=True,capture_output=True)
    resumed=OperationalEvidenceStore(PgShadowObservationStore(pg[0]));restored=DurableOperationalSpool(spool.path)
    assert restored.replay(resumed)['delivered']==2
    assert restored.replay(resumed)['delivered']==0
    rows=resumed.events('whole-outage',owner_id='alice',start=start,end=datetime.now(timezone.utc))
    assert {r['event_id'] for r in rows}==set(originals)
    report=resumed.report('whole-outage',owner_id='alice',start=start,end=datetime.now(timezone.utc),timeframe='5m')
    assert report['monitoring_status']=='UNKNOWN' and report['worker_availability'] is None


def test_two_store_cutover_preserves_campaign_candidates_and_shadow_lineage(pg,tmp_path):
    from services.strategy_comparison import StrategyComparisonService
    from services.matched_experiments import MatchedExperimentService
    from services.shadow_research import ShadowResearchStore
    from services.shadow_observation_store import ShadowObservationStore
    from services.shadow_router import ShadowRouter,FrozenSignalAdapter
    from test_shadow_router import candidate
    journal=JournalStore(tmp_path/'journal.sqlite');context=journal.context
    comparison=StrategyComparisonService(context);matching=MatchedExperimentService(context)
    scope={'period_start':'2026-01-01','period_end':'2026-02-01','venue':'binance_usdm','market_type':'perpetual',
        'symbol':'XRPUSDT','timeframe':'5m','execution_mode':'backtest','cost_model_version':'fixture-cost',
        'funding_coverage':'UNKNOWN','slippage_model':'fixture-model','method':'out_of_sample',
        'oos_status':'UNKNOWN','classifier_version':'fixture-classifier','dataset_identity':'fixture-dataset','data_revision':'fixture-revision'}
    candidates=[comparison.create_candidate({**candidate(q),'dataset_identity':'fixture-dataset','data_revision':'fixture-revision',
        'execution_mode':'backtest','cost_model_version':'fixture-cost','scope':scope,'evidence_quality':'UNKNOWN'},owner_id='alice') for q in (75,76)]
    campaign=matching.create_campaign({'candidate_ids':[c['candidate_id'] for c in candidates],'idempotency_key':'local-campaign'},owner_id='alice')
    experiment=matching.create_experiment({'idempotency_key':'local-experiment','campaign_id':campaign['campaign_id'],
        'champion_candidate_id':candidates[0]['candidate_id'],'challenger_candidate_ids':[candidates[1]['candidate_id']],
        'scope':scope,'method':'OUT_OF_SAMPLE_VALIDATION'},owner_id='alice')
    context.record_holdout_exposure({'exposure_id':'original-exposure','owner_id':'alice','campaign_id':campaign['campaign_id'],
        'experiment_id':experiment['experiment_id'],'dataset_identity':'fixture-dataset','period_start':'2026-01-20','period_end':'2026-02-01',
        'exposure_kind':'MATCHED_RESEARCH','selection_influence':'UNKNOWN'})
    shadow=ShadowObservationStore(ShadowResearchStore(tmp_path/'shadow.sqlite'))
    router=ShadowRouter(shadow,candidate_reader=lambda cid,owner:context.get_research_candidate(cid,owner_id=owner))
    registration=router.register({'candidate_id':candidates[0]['candidate_id'],'authorize':True,'idempotency_key':'local-registration',
        'authorization_reason':'Isolated migration fixture only','lookback_spec':FrozenSignalAdapter(candidates[0]).lookback},owner_id='alice')
    manifest=frozen_export([journal.path,shadow._store.path],tmp_path/'backups')
    assert manifest['status']=='PREFLIGHT_VALID',manifest['blockers']
    assert import_rehearsal(manifest,pg[1],dry_run=False)['status']=='LOCAL_VERIFIED'
    restored=PgResearchContext(pg[0]);target_shadow=PgShadowObservationStore(pg[0])
    assert restored.get_research_campaign(campaign['campaign_id'],owner_id='alice')['candidate_count']==2
    assert restored.get_matched_experiment(experiment['experiment_id'],owner_id='alice')['champion_candidate_id']==candidates[0]['candidate_id']
    assert restored.get_research_candidate(candidates[0]['candidate_id'],owner_id='bob') is None
    assert target_shadow.get(registration['registration_id'],owner_id='alice')['config_fingerprint']==registration['config_fingerprint']
    assert target_shadow.get(registration['registration_id'],owner_id='bob') is None
