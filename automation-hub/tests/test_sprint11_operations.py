from datetime import datetime,timedelta,timezone
from pathlib import Path
import sqlite3

import pytest

from services.local_readiness import LocalReadinessStore,junit_evidence
from services.metadata_cutover import frozen_export
from services.operational_evidence import OperationalEvidenceStore,ShadowOperationalRecorder
from services.operational_retention import OperationalRetention
from services.worker_operational_monitor import WorkerOperationalMonitor
from services.operational_spool import BufferedOperationalStore,DurableOperationalSpool
from services.shadow_observation_store import ShadowObservationStore
from services.shadow_research import ShadowResearchStore

T=datetime(2026,10,12,8,tzinfo=timezone.utc)


def store(tmp_path):return ShadowObservationStore(ShadowResearchStore(tmp_path/'metadata.db'))


@pytest.mark.parametrize('database_status',['UNAVAILABLE','UNKNOWN',None])
def test_shadow_health_rejects_unverified_observation_database(tmp_path,database_status):
    from services.shadow_worker_health import write_health,check_health
    path=tmp_path/'shadow-health.json'
    write_health(path,{'readiness':'READY','candidate_store_status':'AVAILABLE',
        'database_status':database_status,'execution_runtime_loaded':False})
    result=check_health(path)
    assert result['ready'] is False
    assert result['database_verified'] is False


def test_shadow_health_separates_verified_database_from_legacy_process_probe(tmp_path):
    from services.shadow_worker_health import write_health,check_health
    path=tmp_path/'shadow-health.json'
    value={'readiness':'READY','candidate_store_status':'AVAILABLE','execution_runtime_loaded':False}
    write_health(path,value)
    legacy=check_health(path)
    assert legacy['ready'] is True
    assert legacy['database_verified'] is False
    assert legacy['database_status']=='UNKNOWN'
    write_health(path,{**value,'database_status':'AVAILABLE'})
    current=check_health(path)
    assert current['ready'] is True and current['database_verified'] is True
    stale=check_health(path,now=datetime.now(timezone.utc)+timedelta(seconds=30))
    assert stale['ready'] is False and stale['database_verified'] is False
    assert stale['database_status']=='UNKNOWN'


@pytest.mark.parametrize('credential',['HUB_EXCHANGE_API_KEY','HUB_EXCHANGE_API_SECRET',
    'SUPABASE_SERVICE_ROLE_KEY','HUB_CONTROL_KEY','HUB_WEBHOOK_SECRET','HUB_PASSWORD',
    'HUB_SECRET','ANTHROPIC_API_KEY','TELEGRAM_BOT_TOKEN','HUB_API_KEY'])
@pytest.mark.parametrize('worker_kind',['research','shadow'])
def test_standalone_workers_reject_platform_credentials_before_storage(tmp_path,monkeypatch,credential,worker_kind):
    from services.research_worker_main import build_worker as research
    from services.shadow_worker_main import build_worker as shadow
    monkeypatch.setenv(credential,'isolated-fixture-credential')
    journal=tmp_path/'journal.sqlite';observations=tmp_path/'shadow.sqlite'
    with pytest.raises(ValueError,match='FORBIDDEN_EXECUTION_CREDENTIAL'):
        if worker_kind=='research':research(backend='sqlite',journal_db=journal)
        else:shadow(shadow_db=observations,journal_db=journal,market_data_dir=tmp_path/'market')
    assert not journal.exists() and not observations.exists()


def test_idle_worker_samples_do_not_claim_feed_or_unmonitored_uptime(tmp_path):
    clock=[T];op=OperationalEvidenceStore(store(tmp_path),clock=lambda:clock[0]);spool=DurableOperationalSpool(tmp_path/'spool.db')
    recorder=ShadowOperationalRecorder(BufferedOperationalStore(op,spool),'generation-one',clock=lambda:clock[0])
    monitor=WorkerOperationalMonitor(recorder,worker_kind='research',backend='sqlite',owner_id='alice')
    monitor.sample(database_available=True,ready=True)
    clock[0]+=timedelta(minutes=4);monitor.sample(database_available=True,ready=True)
    clock[0]+=timedelta(minutes=1)
    result=op.report('operator-worker:research',owner_id='alice',start=T,end=clock[0],timeframe='5m')
    assert result['worker_availability'] is None and result['missing_probe_opportunities']==3
    assert result['feed_availability'] is None and result['candle_receipt_coverage'] is None


def test_retained_originals_are_verified_and_source_is_not_deleted(tmp_path):
    op=OperationalEvidenceStore(store(tmp_path),clock=lambda:T+timedelta(minutes=5))
    op.register(owner_id='alice',source_id='one',scope={'timeframe':'5m'})
    op.emit({'owner_id':'alice','source_id':'one','event_type':'HEALTH_PROBE','occurred_at':T.isoformat(),'payload':{'worker_ready':True}},key='one')
    archive=OperationalRetention(op,tmp_path/'retained')
    segment=archive.archive('one',owner_id='alice',start=T,end=T+timedelta(minutes=5))
    assert archive.retrieve(segment['segment_id'],owner_id='alice')['events'][0]['event_id']==segment['event_ids'][0]
    assert len(op.events('one',owner_id='alice',start=T,end=T+timedelta(minutes=5)))==1
    with pytest.raises(KeyError):archive.retrieve(segment['segment_id'],owner_id='mallory')
    (tmp_path/'retained'/segment['snapshot_file']).write_bytes(b'corrupt')
    with pytest.raises((ValueError,__import__('zlib').error)):archive.retrieve(segment['segment_id'],owner_id='alice')


def test_archive_capacity_is_explicit_and_empty_time_is_not_evidence(tmp_path):
    op=OperationalEvidenceStore(store(tmp_path));op.register(owner_id='alice',source_id='one',scope={})
    archive=OperationalRetention(op,tmp_path/'retained',max_bytes=1024,max_owner_bytes=1024)
    with pytest.raises(ValueError,match='NO_ORIGINAL'):archive.archive('one',owner_id='alice',start=T,end=T+timedelta(minutes=1))
    for i in range(15):op.emit({'owner_id':'alice','source_id':'one','event_type':'HEALTH_PROBE','occurred_at':T.isoformat(),'payload':{'i':i,'value':__import__('hashlib').sha256(str(i).encode()).hexdigest()}},key=str(i))
    with pytest.raises(ValueError,match='CAPACITY'):archive.archive('one',owner_id='alice',start=T,end=T+timedelta(minutes=1))


def test_readiness_is_immutable_owner_scoped_and_never_production_certification(tmp_path):
    local=LocalReadinessStore(store(tmp_path));body={'owner_id':'alice','environment_id':'local:fixture','software_revision':'fixture',
        'database_schema_version':'worker-metadata.2','worker_backend':'sqlite','start':T,'end':T+timedelta(minutes=5),'reviewer':'local-test-reviewer',
        'gates':{'spool_recovery':{'status':'LOCAL_VERIFIED','evidence_reference':'test-outage','evidence_hash':'a'*64},'feed_soak':{'status':'BLOCKED','reason':'NO_LIVE_SOAK'}}}
    report=local.commit(**body)
    assert report['status']=='BLOCKED' and report['production_certification'] is False
    assert local.commit(**body)==report and local.get(report['assessment_id'],owner_id='mallory') is None
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        local.shadow._store._db.execute("UPDATE local_readiness_assessments SET evidence_hash='changed'")
    with pytest.raises(ValueError,match='SCOPE'):local.commit(**{**body,'environment_id':'production:actual'})
    with pytest.raises(ValueError,match='EVIDENCE_REQUIRED'):
        local.commit(**{**body,'gates':{'postgresql':{'status':'LOCAL_VERIFIED'}}})


def test_readiness_junit_records_actual_counts(tmp_path):
    path=tmp_path/'actual.xml';path.write_text('<testsuite><testcase name="pass"/><testcase name="fail"><failure/></testcase><testcase name="skip"><skipped/></testcase></testsuite>')
    result=junit_evidence(path,suite='fixture',environment_id='local:fixture')
    assert (result['passed'],result['failed'],result['skipped'])==(1,1,1)
    assert result['status']=='BLOCKED'


def test_cutover_backup_is_frozen_and_financial_rows_are_excluded(tmp_path):
    source=tmp_path/'legacy.sqlite'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE operational_sources(owner_id TEXT,source_id TEXT,scope_json TEXT,created_at TEXT,PRIMARY KEY(owner_id,source_id))')
        db.execute("INSERT INTO operational_sources VALUES ('alice','one','{}','2026-10-12T08:00:00+00:00')")
        db.execute('CREATE TABLE paper_trades(pnl REAL)');db.execute('INSERT INTO paper_trades VALUES (123)')
    before=source.read_bytes();manifest=frozen_export([source],tmp_path/'backup')
    assert source.read_bytes()==before
    assert set(manifest['tables'])=={'operational_sources'}
    assert manifest['status']=='PREFLIGHT_VALID' and manifest['financial_records_imported'] is False
    with sqlite3.connect(source) as db:db.execute("UPDATE operational_sources SET source_id='changed'")
    assert manifest['tables']['operational_sources'][0]['source_id']=='one'
    with sqlite3.connect(tmp_path/'backup/source-0.sqlite') as db:assert db.execute('SELECT pnl FROM paper_trades').fetchone()[0]==123


def test_cutover_rejects_unreviewed_schema_and_active_leases(tmp_path):
    source=tmp_path/'legacy.sqlite'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE research_jobs(status TEXT,request_json TEXT)')
        db.execute("INSERT INTO research_jobs VALUES ('RUNNING','{}')")
        db.execute('CREATE TABLE research_unknown(value TEXT)');db.execute("INSERT INTO research_unknown VALUES ('unsupported')")
    result=frozen_export([source],tmp_path/'backup')
    assert result['status']=='BLOCKED'
    assert {b['code'] for b in result['blockers']}=={'UNSUPPORTED_LEGACY_METADATA','ACTIVE_RESEARCH_LEASE'}


def test_feed_soak_only_attaches_existing_hub_and_labels_fixtures():
    from types import SimpleNamespace
    from services.public_feed_soak import observe_existing_feed
    import threading
    stop=threading.Event()
    class Subscription:
        consumer=SimpleNamespace(observation_notice=None)
        def start(self,*_,**kwargs):
            assert kwargs['quotes'] is False
            self.consumer.observation_notice({'origin':'LIVE_WEBSOCKET','finalized':True,'fixture_only':True})
            stop.set();return True
        def stop(self):self.stopped=True
    sub=Subscription()
    class Hub:
        def subscription(self,*_):return sub
        def channel_report(self):return []
    report=observe_existing_feed(Hub(),symbol='XRPUSDT',timeframe='5m',duration_seconds=1,origin='DETERMINISTIC_FIXTURE',stop=stop)
    assert report['source_kind']=='DETERMINISTIC_FIXTURE' and report['continuous_uptime_certified'] is False
    assert report['execution_authorization'] is False and sub.stopped is True
    assert len(report['observed_receipts'])==1
