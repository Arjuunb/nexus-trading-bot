from datetime import datetime,timedelta,timezone
from copy import deepcopy
import sqlite3

import pytest

from services.operational_evidence import OperationalEvidenceStore,calculate_reliability
from services.shadow_research import ShadowResearchStore
from services.shadow_observation_store import ShadowObservationStore

T=datetime(2026,10,12,8,tzinfo=timezone.utc)


def event(kind,at,payload=None):
    return {'event_id':'fixture-'+kind+'-'+str(at),'event_type':kind,'occurred_at':at.isoformat(),
        'committed_at':at.isoformat(),'payload':payload or {},'evidence_kind':'OPERATIONAL_OBSERVATION'}


def monitored():
    events=[event('MONITORING_STARTED',T)]
    for i in range(6):
        events.append(event('HEALTH_PROBE',T+timedelta(seconds=60*i),{'worker_ready':True,'feed_connected':True}))
    events.append(event('MONITORING_STOPPED',T+timedelta(minutes=6)))
    return events


def test_availability_is_a_separate_explicit_probe_denominator():
    events=monitored();events[3]['payload']['feed_connected']=False
    result=calculate_reliability(events,start=T,end=T+timedelta(minutes=6),timeframe='5m')
    assert result['worker_availability']==1
    assert result['feed_availability']==5/6
    assert result['worker_denominator']==6
    assert result['finalized_candle_receipts']==0
    assert result['decision_evaluation_success_rate'] is None


def test_monitoring_cannot_verify_a_preceding_unobserved_period():
    result=calculate_reliability(monitored(),start=T-timedelta(days=1),end=T+timedelta(minutes=6),timeframe='5m')
    assert result['monitoring_status']=='UNKNOWN'
    assert result['worker_availability'] is None
    assert 'UNMONITORED_PERIOD' in result['warning_codes']


def test_missing_probes_do_not_become_worker_uptime():
    events=monitored();del events[3]
    result=calculate_reliability(events,start=T,end=T+timedelta(minutes=6),timeframe='5m')
    assert result['monitoring_status']=='PARTIAL'
    assert result['worker_availability'] is None
    assert result['missing_probe_opportunities']==1


def test_rest_repair_is_not_live_delivery_and_duplicate_does_not_inflate_coverage():
    events=monitored();close=T+timedelta(minutes=5)
    events += [event('CANDLE_RECEIVED',close,{'candle_close':close.isoformat(),'finalized':True,'origin':'LIVE_WEBSOCKET','candle_hash':'a'*64})]*2
    events += [event('CANDLE_RECEIVED',T,{'candle_close':T.isoformat(),'finalized':True,'origin':'REST_REPAIR','candle_hash':'b'*64})]
    result=calculate_reliability(events,start=T,end=T+timedelta(minutes=6),timeframe='5m')
    assert result['finalized_candle_receipts']==1
    assert result['duplicate_events']==1
    assert result['historical_repair_events']==1


def test_commit_failure_is_not_a_successful_evaluation():
    events=monitored()+[event('EVALUATION_COMPLETED',T,{'status':'NO_SIGNAL'}),event('COMMIT_FAILED',T,{'error_category':'DatabaseUnavailable'})]
    result=calculate_reliability(events,start=T,end=T+timedelta(minutes=6),timeframe='5m')
    assert result['evaluation_completions']==1
    assert result['commit_success_rate']==0
    assert result['evidence_commitment_failures']==1


def test_restarts_and_stale_higher_context_remain_original_events():
    events=monitored()+[event('WORKER_RESTART',T+timedelta(seconds=20)),event('STALE_HTF_CANDLE',T+timedelta(seconds=30))]
    result=calculate_reliability(events,start=T,end=T+timedelta(minutes=6),timeframe='5m')
    assert result['worker_restarts']==1
    assert result['stale_htf_events']==1
    assert result['recovery_duration_seconds'] is None


def test_store_commitment_immutable_idempotent_and_tenant_scoped(tmp_path):
    raw=ShadowResearchStore(tmp_path/'shadow.sqlite');store=OperationalEvidenceStore(ShadowObservationStore(raw),clock=lambda:T)
    store.register(owner_id='alice',source_id='shadow-one',scope={'symbol':'XRPUSDT','timeframe':'5m','market_type':'usdm_futures'})
    body={'source_id':'shadow-one','owner_id':'alice','event_type':'HEALTH_PROBE','occurred_at':T.isoformat(),
        'worker_id':'worker-A','generation':'generation-A','payload':{'worker_ready':True,'feed_connected':False}}
    first=store.emit(body,key='heartbeat-original')
    assert store.emit(body,key='heartbeat-original')==first
    assert store.events('shadow-one',owner_id='bob',start=T,end=T+timedelta(minutes=1))==[]
    with pytest.raises(ValueError,match='CONFLICT'):store.emit({**body,'payload':{'worker_ready':False}},key='heartbeat-original')
    with pytest.raises(sqlite3.IntegrityError):raw._db.execute("UPDATE operational_events SET payload_json='{}'")


def test_event_and_byte_quotas_fail_closed(tmp_path):
    raw=ShadowResearchStore(tmp_path/'shadow.sqlite');store=OperationalEvidenceStore(ShadowObservationStore(raw),clock=lambda:T)
    store.max_events=1;store.register(owner_id='alice',source_id='shadow-one',scope={})
    body={'source_id':'shadow-one','owner_id':'alice','event_type':'WORKER_STARTED','occurred_at':T.isoformat(),'payload':{}}
    store.emit(body,key='first-event')
    with pytest.raises(ValueError,match='TELEMETRY_RETENTION_LIMIT'):store.emit(body,key='second-event')


def test_expired_evidence_never_attests_retained_coverage(tmp_path):
    raw=ShadowResearchStore(tmp_path/'shadow.sqlite');clock=[T];store=OperationalEvidenceStore(ShadowObservationStore(raw),clock=lambda:clock[0])
    store.retention_seconds=60;store.register(owner_id='alice',source_id='shadow-one',scope={})
    store.emit({'source_id':'shadow-one','owner_id':'alice','event_type':'MONITORING_STARTED','occurred_at':T.isoformat(),'payload':{}},key='start-monitor')
    clock[0]=T+timedelta(days=1)
    report=store.report('shadow-one',owner_id='alice',start=T,end=T+timedelta(minutes=1),timeframe='5m')
    assert report['monitoring_status']=='UNKNOWN'
    assert 'RETENTION_UNAVAILABLE' in report['warning_codes']


def test_stopped_monitor_cannot_attest_later_window():
    events=monitored()+[event('MONITORING_STOPPED',T-timedelta(seconds=1))]
    events[0]['occurred_at']=(T-timedelta(days=1)).isoformat()
    result=calculate_reliability(events,start=T,end=T+timedelta(minutes=6),timeframe='5m')
    assert result['monitoring_status']=='UNKNOWN'
    assert result['worker_availability'] is None


def test_long_running_source_uses_original_lifecycle_anchor_and_bounded_pages(tmp_path):
    raw=ShadowResearchStore(tmp_path/'shadow.sqlite');store=OperationalEvidenceStore(ShadowObservationStore(raw),clock=lambda:T+timedelta(days=10))
    store.register(owner_id='alice',source_id='one',scope={})
    def emit(kind,at,key,payload):
        return store.emit({'owner_id':'alice','source_id':'one','event_type':kind,'occurred_at':at.isoformat(),'payload':payload},key=key)
    emit('MONITORING_STARTED',T,'start',{'candle_capture_supported':True})
    start=T+timedelta(days=10)-timedelta(minutes=2)
    for i in range(2):emit('HEALTH_PROBE',start+timedelta(minutes=i),'probe-'+str(i),{'worker_ready':True,'feed_connected':True})
    report=store.report('one',owner_id='alice',start=start,end=start+timedelta(minutes=2),timeframe='5m')
    assert report['monitoring_status']=='COMPLETE'
    first=store.event_page('one',owner_id='alice',start=start,end=start+timedelta(minutes=2),limit=1)
    second=store.event_page('one',owner_id='alice',start=start,end=start+timedelta(minutes=2),limit=1,offset=1)
    assert first['has_more'] and not second['has_more']
    assert first['rows'][0]['event_id']!=second['rows'][0]['event_id']
    assert store.event_page('one',owner_id='bob',start=start,end=start+timedelta(minutes=2),limit=1)['rows']==[]


def test_evaluation_error_is_not_double_counted_as_a_completion(tmp_path):
    from services.operational_evidence import ShadowOperationalRecorder
    raw=ShadowResearchStore(tmp_path/'shadow.sqlite');store=OperationalEvidenceStore(ShadowObservationStore(raw),clock=lambda:T)
    recorder=ShadowOperationalRecorder(store,'worker',clock=lambda:T)
    reg={'registration_id':'one','owner_id':'alice','timeframe':'5m'}
    recorder.decision(reg,{'decision_id':'bad-eval','status':'EVALUATION_ERROR','evidence_commit_recorded_at':T.isoformat()})
    rows=store.events('one',owner_id='alice',start=T,end=T+timedelta(minutes=1))
    report=calculate_reliability(rows,start=T,end=T+timedelta(minutes=1),timeframe='5m')
    assert report['evaluation_errors']==1 and report['evaluation_completions']==0
    assert report['decision_evaluation_success_rate']==0


def test_pause_resume_monitoring_has_distinct_immutable_start_events(tmp_path):
    from services.operational_evidence import ShadowOperationalRecorder
    clock=[T];raw=ShadowResearchStore(tmp_path/'shadow.sqlite');store=OperationalEvidenceStore(ShadowObservationStore(raw),clock=lambda:clock[0])
    recorder=ShadowOperationalRecorder(store,'worker',clock=lambda:clock[0]);reg={'registration_id':'one','owner_id':'alice','timeframe':'5m'}
    assert recorder.record(reg,'WORKER_STARTED')
    assert recorder.record(reg,'MONITORING_STOPPED')
    recorder.started.discard('one');clock[0]+=timedelta(minutes=1)
    assert recorder.record(reg,'WORKER_STARTED')
    assert recorder.failures==0
    rows=store.events('one',owner_id='alice',start=T,end=T+timedelta(minutes=2))
    assert len([r for r in rows if r['event_type']=='MONITORING_STARTED'])==2


def test_feed_state_transitions_preserve_sampled_timing_and_missing_candle_reason(tmp_path):
    from services.operational_evidence import ShadowOperationalRecorder
    clock=[T];raw=ShadowResearchStore(tmp_path/'shadow.sqlite');store=OperationalEvidenceStore(ShadowObservationStore(raw),clock=lambda:clock[0])
    recorder=ShadowOperationalRecorder(store,'worker',clock=lambda:clock[0]);reg={'registration_id':'one','owner_id':'alice','timeframe':'5m'}
    recorder.probe(reg,{'transport_state':'CONNECTED','reliable':True});clock[0]+=timedelta(minutes=1)
    recorder.probe(reg,{'transport_state':'DISCONNECTED','reliable':False})
    recorder.decision(reg,{'decision_id':'missing','status':'BLOCKED_MISSING_CONTEXT','rejection_reasons':['MISSING_CANDLE'],
        'evaluation_started_at':T.isoformat(),'evaluation_completed_at':clock[0].isoformat(),'evidence_commit_recorded_at':clock[0].isoformat()})
    rows=store.events('one',owner_id='alice',start=T,end=T+timedelta(minutes=2))
    assert {'FEED_CONNECTED','FEED_DISCONNECTED','MISSING_CANDLE','EVALUATION_STARTED'}<={r['event_type'] for r in rows}
    disconnect=next(r for r in rows if r['event_type']=='FEED_DISCONNECTED')
    assert disconnect['payload']['timing_basis']=='OBSERVED_PROBE; ORIGINAL_DISCONNECT_TIME_UNKNOWN'
