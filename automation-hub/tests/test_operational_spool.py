"""Durable worker telemetry, without observations during unmonitored time."""
from datetime import datetime,timedelta,timezone
import sqlite3
import pytest

from services.operational_spool import DurableOperationalSpool, BufferedOperationalStore
from services.operational_evidence import OperationalEvidenceStore
from services.shadow_observation_store import ShadowObservationStore
from services.shadow_research import ShadowResearchStore

T=datetime(2026,10,12,8,tzinfo=timezone.utc)

def metadata(path):return OperationalEvidenceStore(ShadowObservationStore(ShadowResearchStore(path)),clock=lambda:T+timedelta(minutes=5))


def event(i=0):
    return {'source_id':'source-one','owner_id':'alice','event_type':'HEALTH_PROBE',
        'occurred_at':(T+timedelta(minutes=i)).isoformat(),'worker_id':'worker-one','generation':'one',
        'payload':{'worker_ready':True,'feed_connected':None}}


class Outage:
    def register(self,**_):raise ConnectionError('fixture metadata outage')
    def emit(self,*_,**__):raise ConnectionError('fixture metadata outage')


def test_outage_restart_replay_keeps_identity_and_missing_time_unknown(tmp_path):
    path=tmp_path/'spool.db';spool=DurableOperationalSpool(path)
    proxy=BufferedOperationalStore(Outage(),spool)
    proxy.register(owner_id='alice',source_id='source-one',scope={'timeframe':'5m'})
    for i in (0,1,4):assert proxy.emit(event(i),key=str(i))['commitment_status']=='BUFFERED'
    assert spool.health(owner_id='alice')['pending']==3
    store=metadata(tmp_path/'metadata.db')
    recovered=DurableOperationalSpool(path)
    assert recovered.replay(store)['delivered']==3
    assert recovered.replay(store)['delivered']==0
    rows=store.events('source-one',owner_id='alice',start=T,end=T+timedelta(minutes=5))
    assert len(rows)==3
    assert {r['occurred_at'] for r in rows}=={event(i)['occurred_at'] for i in (0,1,4)}
    report=store.report('source-one',owner_id='alice',start=T,end=T+timedelta(minutes=5),timeframe='5m')
    assert report['worker_availability'] is None
    assert report['missing_probe_opportunities']==2


def test_lost_ack_and_duplicate_replay_have_one_logical_effect(tmp_path):
    spool=DurableOperationalSpool(tmp_path/'spool.db')
    real=metadata(tmp_path/'metadata.db')
    real.register(owner_id='alice',source_id='source-one',scope={'timeframe':'5m'})
    class LostAck:
        register=real.register
        def emit(self,ev,*,key):real.emit(ev,key=key);raise ConnectionError('lost ack')
    proxy=BufferedOperationalStore(LostAck(),spool)
    proxy.register(owner_id='alice',source_id='source-one',scope={'timeframe':'5m'})
    proxy.emit(event(),key='stable-key');spool.replay(real);spool.replay(real)
    assert len(real.events('source-one',owner_id='alice',start=T,end=T+timedelta(minutes=1)))==1
    with pytest.raises(ValueError,match='CONFLICT'):
        proxy.emit({**event(),'payload':{'worker_ready':False}},key='stable-key')


def test_corruption_is_quarantined_and_overflow_is_durable(tmp_path):
    spool=DurableOperationalSpool(tmp_path/'spool.db',max_events=1)
    proxy=BufferedOperationalStore(Outage(),spool)
    proxy.register(owner_id='alice',source_id='source-one',scope={})
    proxy.emit(event(),key='one')
    with pytest.raises(ValueError,match='CAPACITY'):proxy.emit(event(1),key='two')
    with sqlite3.connect(spool.path) as db:db.execute("UPDATE spool_events SET payload_json='{}'")
    real=metadata(tmp_path/'metadata.db')
    spool.replay(real)
    health=DurableOperationalSpool(spool.path).health(owner_id='alice')
    assert health['corrupt']==1 and health['overflow_count']==1
    assert health['status']=='BLOCKED'


def test_disk_write_failure_cannot_claim_buffered_success(tmp_path,monkeypatch):
    spool=DurableOperationalSpool(tmp_path/'spool.db');proxy=BufferedOperationalStore(Outage(),spool)
    proxy.register(owner_id='alice',source_id='source-one',scope={})
    def full(*_,**__):raise sqlite3.OperationalError('database or disk is full')
    monkeypatch.setattr(spool,'enqueue',full)
    with pytest.raises(sqlite3.OperationalError,match='full'):proxy.emit(event(),key='one')


def test_scope_tenant_and_financial_database_protection(tmp_path):
    spool=DurableOperationalSpool(tmp_path/'spool.db');proxy=BufferedOperationalStore(Outage(),spool)
    proxy.register(owner_id='alice',source_id='source-one',scope={})
    proxy.emit(event(),key='one')
    assert spool.health(owner_id='mallory')['pending']==0
    with pytest.raises(ValueError,match='SOURCE_CONFLICT'):
        proxy.register(owner_id='alice',source_id='source-one',scope={'changed':True})
    path=tmp_path/'financial.db'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE v2_account(balance REAL)')
    with pytest.raises(ValueError,match='FINANCIAL'):DurableOperationalSpool(path)


def test_invalid_tool_payload_is_rejected_before_spooling(tmp_path):
    spool=DurableOperationalSpool(tmp_path/'spool.db');proxy=BufferedOperationalStore(Outage(),spool)
    proxy.register(owner_id='alice',source_id='source-one',scope={})
    with pytest.raises(ValueError,match='INVALID_OPERATIONAL'):
        proxy.emit({**event(),'event_type':'PLACE_ORDER'},key='forbidden')
    assert spool.health(owner_id='alice')['pending']==0


def test_only_acknowledged_old_transport_copies_expire_and_originals_remain(tmp_path):
    clock=[T];spool=DurableOperationalSpool(tmp_path/'spool.db',clock=lambda:clock[0])
    real=metadata(tmp_path/'metadata.db');proxy=BufferedOperationalStore(real,spool)
    proxy.register(owner_id='alice',source_id='source-one',scope={})
    proxy.emit(event(),key='original')
    clock[0]=T+timedelta(days=8)
    assert spool.expire_delivered(before=T+timedelta(days=1))['removed_transport_copies']==1
    assert len(real.events('source-one',owner_id='alice',start=T,end=T+timedelta(minutes=1)))==1
    proxy.store=Outage();proxy.emit(event(1),key='pending')
    assert spool.expire_delivered(before=T+timedelta(days=2))['removed_transport_copies']==0
    assert spool.health(owner_id='alice')['pending']==1


def test_canonical_utc_survives_outage_without_timezone_boundary_drift(tmp_path):
    spool=DurableOperationalSpool(tmp_path/'spool.db');proxy=BufferedOperationalStore(Outage(),spool)
    proxy.register(owner_id='alice',source_id='source-one',scope={})
    proxy.emit({**event(),'occurred_at':'2026-10-12T09:00:00+01:00'},key='same-opportunity')
    # Equivalent UTC representation is the same immutable event.
    proxy.emit(event(),key='same-opportunity')
    target=metadata(tmp_path/'metadata.db');spool.replay(target)
    rows=target.events('source-one',owner_id='alice',start=T,end=T+timedelta(seconds=1))
    assert len(rows)==1 and rows[0]['occurred_at']==T.isoformat()
