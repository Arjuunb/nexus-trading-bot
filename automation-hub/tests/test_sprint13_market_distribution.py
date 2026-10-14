"""Deterministic fixtures are not real forward market observations."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import subprocess
import sys
from pathlib import Path

import pytest
from bot.types import Bar
from services.market_data_distribution import CandleJournal, DistributedMarketDataHub, JournalFeedPublisher

NOW = datetime(2026, 10, 13, 12, 5, 1, tzinfo=timezone.utc)


def receipt(at=None, **changes):
    return {"bar": Bar(at or NOW-timedelta(seconds=301), 10., 12., 9., 11., 100.),
            "symbol": "XRPUSDT", "timeframe": "5m", "venue": "binance_usdm",
            "market_type": "perpetual", "source": "BINANCE_USDM_PUBLIC",
            "origin": "DETERMINISTIC_FIXTURE", "finalized": True,
            "delivery_observed_at": NOW.isoformat(), **changes}


@pytest.fixture
def journal(tmp_path):
    return CandleJournal(tmp_path/"public.sqlite", revision="fixture-v1", clock=lambda: NOW)


def consumer(journal, tmp_path, *, name="observer", now=NOW, fixture=True):
    hub = DistributedMarketDataHub(journal.path, tmp_path/(name+".cursor.sqlite"),
        revision="fixture-v1", clock=lambda: now, allow_fixture=fixture)
    sub = hub.subscription(name)
    assert sub.start("XRPUSDT", "5m", quotes=False)
    return hub, sub


@pytest.mark.parametrize("change,reason", [
    ({"finalized": False}, "FINALIZED"), ({"venue": "binance_spot"}, "IDENTITY"),
    ({"market_type": "spot"}, "IDENTITY"), ({"source": "YAHOO"}, "IDENTITY"),
    ({"delivery_observed_at": (NOW-timedelta(seconds=5)).isoformat()}, "FUTURE"),
    ({"bar": Bar(NOW-timedelta(seconds=301), 10, 8, 9, 11, 100)}, "OHLCV"),
    ({"bar": Bar(NOW-timedelta(seconds=300), 10, 12, 9, 11, 100)}, "ALIGNMENT"),
])
def test_invalid_receipts_never_commit(journal, change, reason):
    with pytest.raises(ValueError, match=reason): journal.append(receipt(**change))
    assert journal.health()["candle_count"] == 0


def test_duplicates_have_one_identity_and_conflicts_block(journal, tmp_path):
    a=journal.append(receipt()); b=journal.append(receipt())
    assert a["sequence"]==b["sequence"]
    assert journal.health()["duplicates"]==1
    with pytest.raises(ValueError, match="CONFLICT"):
        journal.append(receipt(bar=replace(receipt()["bar"], close=10.5)))
    _, sub=consumer(journal,tmp_path)
    assert sub.status()["reliable"] is False
    assert sub.status()["reason"]=="CONFLICTING_CANDLE"


def test_gap_and_rest_repair_preserve_origin(journal,tmp_path):
    journal.append(receipt(NOW-timedelta(seconds=901)))
    journal.append(receipt())
    _,sub=consumer(journal,tmp_path)
    assert sub.status()["reason"]=="DATA_GAP"
    journal.append(receipt(NOW-timedelta(seconds=601),origin="REST_REPAIR"))
    snap=sub.snapshot()
    assert len(snap["closed_bars"])==3
    assert snap["connection"]["origin"]=="DETERMINISTIC_FIXTURE"
    assert snap["connection"]["missing_candles"]==0


def test_stale_receipt_and_htf_are_blocked(journal,tmp_path):
    journal.append(receipt())
    _,sub=consumer(journal,tmp_path,now=NOW+timedelta(minutes=8))
    assert sub.status()["reason"]=="STALE_CANDLES"
    journal.append(receipt(NOW-timedelta(hours=4,seconds=301),timeframe="4h"))
    hub=DistributedMarketDataHub(journal.path,tmp_path/'htf-cursor',revision='fixture-v1',clock=lambda:NOW+timedelta(hours=5),allow_fixture=True)
    htf=hub.subscription('htf');htf.start('XRPUSDT','4h',quotes=False)
    assert htf.status()['reason']=='STALE_CANDLES'


def test_future_candles_cannot_change_earlier_snapshot(journal,tmp_path):
    journal.append(receipt())
    _,sub=consumer(journal,tmp_path)
    before=sub.snapshot()["closed_bars"]
    future=NOW+timedelta(seconds=299)
    journal.clock=lambda: NOW+timedelta(minutes=11)
    journal.append(receipt(future, delivery_observed_at=(NOW+timedelta(minutes=11)).isoformat()))
    assert sub.snapshot()["closed_bars"]==before


def test_production_reader_never_accepts_fixture_as_live(journal,tmp_path):
    journal.append(receipt())
    _,sub=consumer(journal,tmp_path,fixture=False)
    assert sub.status()["reason"]=="NON_FORWARD_ORIGIN"
    assert sub.status()["reliable"] is False


def test_disconnect_heartbeat_and_reconnect(journal,tmp_path):
    journal.append(receipt(origin="LIVE_WEBSOCKET"))
    journal.transport("XRPUSDT","5m",connected=False,reliable=False)
    _,sub=consumer(journal,tmp_path,fixture=False)
    assert sub.status()["reason"]=="FEED_DISCONNECTED"
    journal.transport("XRPUSDT","5m",connected=True,reliable=True)
    assert sub.status()["reliable"] is True


def test_revision_mismatch_does_not_substitute_data(journal,tmp_path):
    with pytest.raises(ValueError,match="REVISION"):
        DistributedMarketDataHub(journal.path,tmp_path/'cursor',revision='different')


def test_durable_cursor_and_two_real_processes_receive_same_hash(journal,tmp_path):
    journal.append(receipt())
    code="""import json,sys; from datetime import datetime; from services.market_data_distribution import DistributedMarketDataHub
h=DistributedMarketDataHub(sys.argv[1],sys.argv[2],revision='fixture-v1',allow_fixture=True,clock=lambda:datetime.fromisoformat(sys.argv[3]))
s=h.subscription('consumer');s.start('XRPUSDT','5m',quotes=False);x=s.snapshot(); print(json.dumps({'hash':x['connection']['candle_hash'],'cursor':h.cursor(),'n':len(x['closed_bars'])}))"""
    outputs=[]
    for n in range(2):
        r=subprocess.run([sys.executable,'-c',code,str(journal.path),str(tmp_path/f'process{n}.db'),NOW.isoformat()],cwd=Path(__file__).parents[1],capture_output=True,text=True,check=True)
        outputs.append(json.loads(r.stdout))
    assert outputs[0]==outputs[1] and outputs[0]['n']==1
    hub,sub=consumer(journal,tmp_path,name='restart')
    sub.snapshot(); old=hub.cursor()
    replacement,_=consumer(journal,tmp_path,name='restart')
    assert replacement.cursor()==old
    assert journal.health()['candle_count']==1


def test_read_only_reader_and_cursor_namespace(journal,tmp_path):
    before=journal.path.read_bytes()
    _,sub=consumer(journal,tmp_path);sub.snapshot()
    assert journal.path.read_bytes()==before
    with pytest.raises(ValueError,match='NAMESPACE'):
        DistributedMarketDataHub(journal.path,journal.path,revision='fixture-v1')


def test_bounded_storage_fails_closed(journal):
    journal.max_candles=1
    journal.append(receipt())
    with pytest.raises(ValueError,match='CAPACITY'):
        journal.append(receipt(NOW-timedelta(seconds=601)))
    assert journal.health()['candle_count']==1
    assert journal.health()['fault']=='CAPACITY_EXCEEDED'


def test_bounded_notice_does_no_sql_and_overflow_is_explicit(journal):
    publisher=JournalFeedPublisher(journal,max_pending=1)
    publisher.notice(receipt());publisher.notice(receipt())
    assert journal.health()['candle_count']==0
    assert publisher.health()['queue_overflow']==1
    publisher.drain()
    assert journal.health()['fault']=='NOTICE_BACKPRESSURE'


def test_live_subscription_requires_specific_authorization(journal):
    publisher=JournalFeedPublisher(journal)
    with pytest.raises(PermissionError,match='AUTHORIZATION'):
        publisher.attach(None,symbol='XRPUSDT',timeframe='5m',authorized_public=False)


def test_replay_gap_and_corrupt_payload_never_look_healthy(journal,tmp_path):
    journal.append(receipt());hub,sub=consumer(journal,tmp_path);sub.snapshot()
    import sqlite3
    with sqlite3.connect(journal.path) as db:
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):db.execute('DELETE FROM feed_candles')
        # Corrupt restoration simulates operator/physical damage, not normal API.
        db.execute('DROP TRIGGER feed_candles_immutable_delete')
        db.execute('DELETE FROM feed_candles')
    assert sub.status()['reliable'] is False


def test_cursors_are_channel_specific_not_shared_high_water_marks(journal,tmp_path):
    journal.append(receipt(timeframe='1h',at=NOW-timedelta(seconds=3901)))
    journal.append(receipt())
    hub,entry=consumer(journal,tmp_path);entry.snapshot()
    context=hub.subscription('htf');context.start('XRPUSDT','1h',quotes=False)
    assert context.status()['reason']=='FIXTURE_ONLY'
    assert hub.cursor('XRPUSDT','1h')!=hub.cursor('XRPUSDT','5m')


def test_replaced_journal_same_revision_is_not_original_authority(journal,tmp_path):
    consumer(journal,tmp_path)
    journal.path.rename(tmp_path/'retained-original.sqlite')
    CandleJournal(journal.path,revision='fixture-v1')
    with pytest.raises(ValueError,match='AUTHORITY'):consumer(journal,tmp_path)


def test_live_public_soak_default_has_no_network(tmp_path):
    script=Path(__file__).parents[2]/'ops/public-feed-distribution-soak.py'
    r=subprocess.run([sys.executable,str(script),'--output',str(tmp_path/'not-created')],capture_output=True,text=True,check=True)
    assert json.loads(r.stdout)['status']=='NOT_RUN'
    assert not (tmp_path/'not-created').exists()


def test_opt_in_shadow_worker_does_not_construct_exchange_provider(journal,tmp_path,monkeypatch):
    from services.shadow_worker_main import build_worker
    import services.shadow_worker_main as main
    import sqlite3
    candidate_db=tmp_path/'candidates.sqlite'
    with sqlite3.connect(candidate_db) as db:db.execute('CREATE TABLE untouched(value TEXT)')
    def forbidden(*a,**k):raise AssertionError('must not construct exchange provider')
    monkeypatch.setattr(main,'MarketDataService',forbidden)
    worker=build_worker(shadow_db=tmp_path/'shadow.sqlite',journal_db=candidate_db,market_data_dir=tmp_path/'unused',
        feed_journal=journal.path,feed_revision='fixture-v1',feed_cursors=tmp_path/'shadow-cursors.sqlite')
    worker.tick();assert worker.health()['owned_registrations']==0
    assert not (tmp_path/'unused').exists()


def test_existing_hub_read_only_adapter_preserves_original_delivery(journal):
    from services.price_action_stream import PriceActionPublicStream
    from services.forward_paper_hub import ForwardPaperMarketDataHub
    import threading
    class FixtureStream(PriceActionPublicStream):
        def start(self,symbol,timeframe):
            self.symbol=symbol;self.timeframe=timeframe;self.state='CONNECTED';return True
        @property
        def running(self):return self.state=='CONNECTED'
    streams=[]
    def factory(loader,**kwargs):
        x=FixtureStream(loader,clock=lambda:NOW,**kwargs);streams.append(x);return x
    hub=ForwardPaperMarketDataHub(lambda *a,**k:[],stream_factory=factory)
    publisher=JournalFeedPublisher(journal)
    original=[];done=threading.Event()
    def existing_delivery(bar):original.append(bar);done.set()
    existing=hub.subscription('existing-consumer',bar_sink=existing_delivery)
    existing.start('XRPUSDT','5m',quotes=False)
    publisher.attach(hub,symbol='XRPUSDT',timeframe='5m',authorized_public=True)
    try:
        stream=streams[0]
        bar=receipt()['bar']
        result=stream.ingest_event({'e':'kline','s':'XRPUSDT','k':{'s':'XRPUSDT','i':'5m','t':int(bar.timestamp.timestamp()*1000),'x':True,'o':'10','h':'12','l':'9','c':'11','v':'100'}})
        assert result['accepted'] and done.wait(2) and original==[bar]
        publisher.drain()
        assert journal.health()['candle_count']==1
        assert hub.channel_report()[0]['consumer_count']==2
    finally:publisher.stop();existing.stop();hub.stop()


def test_corrupt_restored_payload_identity_cannot_be_trusted(journal,tmp_path):
    import sqlite3
    journal.append(receipt())
    with sqlite3.connect(journal.path) as db:
        payload=json.loads(db.execute('SELECT payload_json FROM feed_candles').fetchone()[0]);payload['venue']='spot'
        db.execute('DROP TRIGGER feed_candles_immutable_update')
        db.execute('UPDATE feed_candles SET payload_json=?',(json.dumps(payload),))
    _,sub=consumer(journal,tmp_path)
    assert sub.status()['reliable'] is False


def test_duplicate_cannot_change_original_receipt_origin_or_timestamp(journal):
    original=journal.append(receipt(origin='REST_REPAIR'))
    journal.clock=lambda:NOW+timedelta(seconds=5)
    replay=journal.append(receipt(origin='LIVE_WEBSOCKET',delivery_observed_at=(NOW+timedelta(seconds=5)).isoformat()))
    assert replay==original and replay['origin']=='REST_REPAIR'


