"""Bounded public-candle transport, separate from every financial authority.

One writer journals notices from the existing public hub; readers mount this
database read-only and persist their own cursor in a separate namespace. No
strategy, account, broker, quote/order client or exchange credential is accepted.
REST repair supplies context, never a new forward observation. No raw pruning.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import queue
import re
import sqlite3
import uuid

from bot.types import Bar
from services.mtf_policy import TIMEFRAME_SECONDS, canonical_candle_id
from services.strategy_identity import configuration_fingerprint as digest

SCHEMA = 'finalized-public-candle-journal.v1'


def utc(value):
    value=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if not value.tzinfo: raise ValueError('UTC_TIMESTAMP_REQUIRED')
    return value.astimezone(timezone.utc)


def scope(symbol,timeframe):
    if not re.fullmatch(r'[A-Z0-9]{2,20}USDT',symbol) or timeframe not in TIMEFRAME_SECONDS:
        raise ValueError('PUBLIC_USDM_IDENTITY_REQUIRED')
    return symbol,timeframe


def _read(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1)


class CandleJournal:
    def __init__(self,path,*,revision,clock=None,max_candles=50000,max_bytes=256*1024*1024):
        self.path=Path(path);self.revision=revision
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',revision):raise ValueError('INVALID_FEED_REVISION')
        if self.path.is_symlink():raise ValueError('JOURNAL_NAMESPACE')
        self.max_candles=max_candles;self.max_bytes=max_bytes
        if not 1<=max_candles<=50000 or not 1024<=max_bytes<=256*1024*1024:raise ValueError('CAPACITY_LIMIT')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and ('feed_identity' not in tables or tables-{'feed_identity','feed_candles','feed_transport','sqlite_sequence'}):raise ValueError('JOURNAL_NAMESPACE')
            db.executescript('''
                PRAGMA journal_mode=DELETE;
                CREATE TABLE IF NOT EXISTS feed_identity(id INTEGER PRIMARY KEY CHECK(id=1),schema_version TEXT NOT NULL,revision TEXT NOT NULL,authority TEXT NOT NULL,fault TEXT,duplicates INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS feed_candles(sequence INTEGER PRIMARY KEY AUTOINCREMENT,candle_id TEXT UNIQUE NOT NULL,symbol TEXT NOT NULL,timeframe TEXT NOT NULL,open_timestamp TEXT NOT NULL,close_timestamp TEXT NOT NULL,candle_hash TEXT NOT NULL,payload_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS feed_candle_scope ON feed_candles(symbol,timeframe,close_timestamp);
                CREATE TABLE IF NOT EXISTS feed_transport(symbol TEXT NOT NULL,timeframe TEXT NOT NULL,observed_at TEXT NOT NULL,connected INTEGER NOT NULL,reliable INTEGER NOT NULL,PRIMARY KEY(symbol,timeframe));
                CREATE TRIGGER IF NOT EXISTS feed_candles_immutable_update BEFORE UPDATE ON feed_candles BEGIN SELECT RAISE(ABORT,'immutable candle');END;
                CREATE TRIGGER IF NOT EXISTS feed_candles_immutable_delete BEFORE DELETE ON feed_candles BEGIN SELECT RAISE(ABORT,'immutable candle');END;
            ''')
            old=db.execute('SELECT schema_version,revision FROM feed_identity WHERE id=1').fetchone()
            if old and old!=(SCHEMA,revision):raise ValueError('FEED_REVISION_MISMATCH')
            db.execute('INSERT OR IGNORE INTO feed_identity(id,schema_version,revision,authority) VALUES(1,?,?,?)',(SCHEMA,revision,'public-feed:'+uuid.uuid4().hex));db.commit()

    def fault(self,code):
        if code not in {'CONFLICTING_CANDLE','CAPACITY_EXCEEDED','NOTICE_BACKPRESSURE','PERSISTENCE_FAILED','INVALID_RECEIPT'}:
            raise ValueError('FAULT_CLASSIFICATION')
        with closing(sqlite3.connect(self.path,timeout=1)) as db:
            db.execute('UPDATE feed_identity SET fault=? WHERE id=1 AND fault IS NULL',(code,));db.commit()

    def append(self,notice):
        if notice.get('finalized') is not True:raise ValueError('FINALIZED_CANDLE_REQUIRED')
        if (notice.get('venue'),notice.get('market_type'),notice.get('source'))!=('binance_usdm','perpetual','BINANCE_USDM_PUBLIC'):
            raise ValueError('PUBLIC_USDM_IDENTITY_REQUIRED')
        symbol,tf=scope(notice['symbol'],notice['timeframe']);bar=notice['bar']
        opened=utc(bar.timestamp);closed=opened+timedelta(seconds=TIMEFRAME_SECONDS[tf]);received=utc(notice['delivery_observed_at']);now=utc(self.clock())
        if opened.timestamp()%TIMEFRAME_SECONDS[tf]:raise ValueError('CANDLE_ALIGNMENT_INVALID')
        if closed>received or received>now+timedelta(seconds=2):raise ValueError('FUTURE_CANDLE_OR_RECEIPT')
        values=[bar.open,bar.high,bar.low,bar.close,bar.volume]
        if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in values) or min(values[:4])<=0 or values[4]<0 or bar.high<max(bar.open,bar.low,bar.close) or bar.low>min(bar.open,bar.high,bar.close):
            raise ValueError('INVALID_OHLCV')
        origin=notice.get('origin')
        if origin not in {'LIVE_WEBSOCKET','REST_REPAIR','REST_BOOTSTRAP','DETERMINISTIC_FIXTURE'}:raise ValueError('ORIGIN_REQUIRED')
        material={'open_timestamp':opened.isoformat(),'ohlcv':values}
        candle_hash=digest(material);cid=canonical_candle_id(symbol,tf,bar)
        payload={'schema_version':SCHEMA,'venue':'binance_usdm','market_type':'perpetual','source':'BINANCE_USDM_PUBLIC',
            'symbol':symbol,'timeframe':tf,'material':material,'candle_id':cid,'candle_hash':candle_hash,
            'close_timestamp':closed.isoformat(),'received_at':received.isoformat(),'committed_at':now.isoformat(),
            'origin':origin,'finalized':True,'revision':self.revision,'availability_basis':'LOCAL_RECEIPT; EXCHANGE_AVAILABILITY_UNKNOWN'}
        failure=None
        with closing(sqlite3.connect(self.path,timeout=1)) as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT sequence,candle_hash,payload_json FROM feed_candles WHERE candle_id=?',(cid,)).fetchone()
            if old:
                if old[1]!=candle_hash:
                    db.execute("UPDATE feed_identity SET fault='CONFLICTING_CANDLE' WHERE id=1 AND fault IS NULL");failure='CONFLICTING_CANDLE'
                else:
                    db.execute('UPDATE feed_identity SET duplicates=duplicates+1 WHERE id=1')
                    payload=json.loads(old[2]);payload['sequence']=old[0]
            elif db.execute('SELECT COUNT(*) FROM feed_candles').fetchone()[0]>=self.max_candles or self.path.stat().st_size>=self.max_bytes-4096:
                db.execute("UPDATE feed_identity SET fault='CAPACITY_EXCEEDED' WHERE id=1 AND fault IS NULL");failure='CAPACITY_EXCEEDED'
            else:
                cur=db.execute('INSERT INTO feed_candles(candle_id,symbol,timeframe,open_timestamp,close_timestamp,candle_hash,payload_json) VALUES(?,?,?,?,?,?,?)',
                    (cid,symbol,tf,opened.isoformat(),closed.isoformat(),candle_hash,json.dumps(payload,sort_keys=True,separators=(',',':'))))
                payload['sequence']=cur.lastrowid
            db.commit()
        if failure:raise ValueError(failure)
        return payload

    def transport(self,symbol,timeframe,*,connected,reliable):
        scope(symbol,timeframe)
        with closing(sqlite3.connect(self.path,timeout=1)) as db:
            db.execute('INSERT INTO feed_transport VALUES(?,?,?,?,?) ON CONFLICT(symbol,timeframe) DO UPDATE SET observed_at=excluded.observed_at,connected=excluded.connected,reliable=excluded.reliable',
                (symbol,timeframe,utc(self.clock()).isoformat(),int(connected),int(reliable)));db.commit()

    def health(self):
        with closing(_read(self.path)) as db:
            row=db.execute('SELECT revision,fault,duplicates FROM feed_identity WHERE id=1').fetchone()
            return {'revision':row[0],'fault':row[1],'duplicates':row[2],
                'candle_count':db.execute('SELECT COUNT(*) FROM feed_candles').fetchone()[0],
                'bytes':self.path.stat().st_size,'retention':'BOUNDED_FAIL_CLOSED; NO_AUTOMATIC_DELETION'}


class DistributedMarketDataHub:
    def __init__(self,journal_path,cursor_path,*,revision,clock=None,allow_fixture=False):
        self.path=Path(journal_path).resolve();self.cursor_path=Path(cursor_path).resolve();self.revision=revision
        self.clock=clock or (lambda:datetime.now(timezone.utc));self.allow_fixture=allow_fixture
        if self.path==self.cursor_path or Path(journal_path).is_symlink() or Path(cursor_path).is_symlink():raise ValueError('CURSOR_NAMESPACE')
        with closing(_read(self.path)) as db:
            identity=db.execute('SELECT schema_version,revision,authority FROM feed_identity WHERE id=1').fetchone()
        if identity[:2]!=(SCHEMA,revision):raise ValueError('FEED_REVISION_MISMATCH')
        self.authority=identity[2]
        self.cursor_path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.cursor_path)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables!={'feed_cursor','feed_binding'}:raise ValueError('CURSOR_NAMESPACE')
            db.execute('CREATE TABLE IF NOT EXISTS feed_cursor(revision TEXT PRIMARY KEY,sequence INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS feed_binding(id INTEGER PRIMARY KEY CHECK(id=1),authority TEXT NOT NULL)')
            old=db.execute('SELECT authority FROM feed_binding WHERE id=1').fetchone()
            if old and old[0]!=self.authority:raise ValueError('FEED_AUTHORITY_MISMATCH')
            db.execute('INSERT OR IGNORE INTO feed_binding VALUES(1,?)',(self.authority,));db.commit()

    def cursor(self,symbol=None,timeframe=None):
        with closing(_read(self.cursor_path)) as db:
            row=(db.execute('SELECT sequence FROM feed_cursor WHERE revision=?',(self.revision+'|'+symbol+'|'+timeframe,)).fetchone()
                 if symbol else db.execute('SELECT MAX(sequence) FROM feed_cursor').fetchone())
            return (row[0] or 0) if row else 0

    def subscription(self,consumer_id,**sinks):return _Subscription(self,consumer_id,**sinks)
    def stop(self):pass


class _Subscription:
    def __init__(self,hub,consumer_id,**sinks):
        self.hub=hub;self.consumer_id=consumer_id;self.running=False
        self.symbol=None;self.timeframe=None
        # Existing observer notices are supported without financial sinks.
        self.consumer=self;self.observation_notice=None;self._notice_cursor=0
    def start(self,symbol,timeframe,*,quotes=False):
        if quotes:raise ValueError('READ_ONLY_CANDLE_TRANSPORT_NO_QUOTES')
        self.symbol,self.timeframe=scope(symbol,timeframe);self._notice_cursor=self.hub.cursor(symbol,timeframe);self.running=True;return True
    def stop(self):self.running=False
    def status(self):return self.snapshot()['connection']
    def snapshot(self):
        now=utc(self.hub.clock());rows=[];bars=[];reason='DATA_UNAVAILABLE';transport=None;fault=None
        try:
            with closing(_read(self.hub.path)) as db:
                identity=db.execute('SELECT schema_version,revision,authority,fault FROM feed_identity WHERE id=1').fetchone()
                if identity[:3]!=(SCHEMA,self.hub.revision,self.hub.authority):raise ValueError('FEED_REVISION_OR_AUTHORITY_MISMATCH')
                fault=identity[3]
                raw=db.execute('SELECT sequence,payload_json FROM feed_candles WHERE symbol=? AND timeframe=? AND close_timestamp<=? ORDER BY open_timestamp DESC LIMIT 1500',
                    (self.symbol,self.timeframe,now.isoformat())).fetchall()
                transport=db.execute('SELECT observed_at,connected,reliable FROM feed_transport WHERE symbol=? AND timeframe=?',(self.symbol,self.timeframe)).fetchone()
                for sequence,text in reversed(raw):
                    payload=json.loads(text);material=payload['material']
                    if payload['revision']!=self.hub.revision or payload['candle_hash']!=digest(material):raise ValueError('CANDLE_HASH_MISMATCH')
                    b=Bar(utc(material['open_timestamp']),*material['ohlcv'])
                    if (payload['venue'],payload['market_type'],payload['source'],payload['symbol'],payload['timeframe'],payload['finalized'])!=('binance_usdm','perpetual','BINANCE_USDM_PUBLIC',self.symbol,self.timeframe,True):raise ValueError('CANDLE_IDENTITY_MISMATCH')
                    closed=b.timestamp+timedelta(seconds=TIMEFRAME_SECONDS[self.timeframe])
                    if canonical_candle_id(self.symbol,self.timeframe,b)!=payload['candle_id'] or closed!=utc(payload['close_timestamp']) or closed>now:raise ValueError('CANDLE_TIME_IDENTITY_MISMATCH')
                    bars.append(b);rows.append((sequence,payload,b))
            missing=sum(max(0,int((b.timestamp-a.timestamp).total_seconds()/TIMEFRAME_SECONDS[self.timeframe])-1) for a,b in zip(bars,bars[1:]))
            latest=rows[-1][1] if rows else None
            if fault:reason=fault
            elif not self.running:reason='FEED_DISCONNECTED'
            elif not latest:reason='DATA_UNAVAILABLE'
            elif missing:reason='DATA_GAP'
            elif (now-utc(latest['received_at'])).total_seconds()<0:reason='FUTURE_RECEIPT'
            elif (now-utc(latest['received_at'])).total_seconds()>TIMEFRAME_SECONDS[self.timeframe]+60 or (now-utc(latest['close_timestamp'])).total_seconds()>TIMEFRAME_SECONDS[self.timeframe]+60:reason='STALE_CANDLES'
            elif latest['origin']=='DETERMINISTIC_FIXTURE' and self.hub.allow_fixture:reason='FIXTURE_ONLY'
            elif latest['origin']!='LIVE_WEBSOCKET':reason='NON_FORWARD_ORIGIN'
            elif not transport or not transport[1] or not transport[2]:reason='FEED_DISCONNECTED'
            elif not 0<=(now-utc(transport[0])).total_seconds()<=20:reason='STALE_TRANSPORT_HEARTBEAT'
            else:reason='SYNCHRONIZED'
            if rows:
                high=max(r[0] for r in rows)
                if self.hub.cursor(self.symbol,self.timeframe)>high:reason='REPLAY_GAP'
                # Callback only ORIGINAL receipts; restart never invents receipt time.
                for seq,payload,b in rows:
                    if seq>self._notice_cursor and self.observation_notice:
                        self.observation_notice({'bar':b,'origin':payload['origin'],'finalized':True,'timeframe':self.timeframe,
                            'symbol':self.symbol,'delivery_observed_at':payload['received_at'],'consumer_received_at':now.isoformat()})
                self._notice_cursor=max(self._notice_cursor,high)
                with closing(sqlite3.connect(self.hub.cursor_path,timeout=1)) as db:
                    db.execute('INSERT INTO feed_cursor VALUES(?,?) ON CONFLICT(revision) DO UPDATE SET sequence=MAX(sequence,excluded.sequence)',(self.hub.revision+'|'+self.symbol+'|'+self.timeframe,high));db.commit()
            connection={'transport_state':'CONNECTED' if reason in {'SYNCHRONIZED','FIXTURE_ONLY'} else reason,
                'reliable':reason in {'SYNCHRONIZED','FIXTURE_ONLY'},'reason':reason,'missing_candles':missing,
                'last_candle_update':latest['received_at'] if latest else None,'candle_hash':latest['candle_hash'] if latest else None,
                'candle_id':latest['candle_id'] if latest else None,'origin':latest['origin'] if latest else None,
                'revision':self.hub.revision,'source':'BINANCE_USDM_PUBLIC_JOURNAL',
                'evidence_kind':'DETERMINISTIC_FIXTURE' if self.hub.allow_fixture else 'PUBLIC_CANDLE_OBSERVATION',
                'cross_process':'READ_ONLY_JOURNAL; OWNED_PERSISTED_CURSOR'}
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
            connection={'transport_state':'DATA_UNAVAILABLE','reliable':False,'reason':'JOURNAL_OR_CURSOR_UNAVAILABLE','last_candle_update':None}
        return {'closed_bars':bars,'forming':None,'quote':{},'connection':connection}


class JournalFeedPublisher:
    def __init__(self,journal,*,max_pending=256):
        if not 1<=max_pending<=256:raise ValueError('QUEUE_LIMIT')
        self.journal=journal;self.pending=queue.Queue(maxsize=max_pending);self.overflow=0;self.failures=0;self.subscriptions=[]
    def notice(self,metadata):
        # Existing feed thread performs no SQL/network or wait.
        try:self.pending.put_nowait(dict(metadata))
        except queue.Full:self.overflow+=1
    def attach(self,hub,*,symbol,timeframe,authorized_public=False):
        if not authorized_public:raise PermissionError('EXPLICIT_PUBLIC_FEED_AUTHORIZATION_REQUIRED')
        sub=hub.subscription('read-only-distribution:'+symbol+':'+timeframe)
        # This adapter is specific to the inspected Binance USD-M hub. Other
        # providers must not be routed through it as equivalent market data.
        from services.forward_paper_hub import ForwardPaperMarketDataHub
        if not isinstance(hub,ForwardPaperMarketDataHub):raise ValueError('EXISTING_USDM_HUB_REQUIRED')
        sub.consumer.observation_notice=lambda value:self.notice({**value,'venue':'binance_usdm','market_type':'perpetual','source':'BINANCE_USDM_PUBLIC'})
        if not sub.start(symbol,timeframe,quotes=False):raise ValueError('PUBLIC_FEED_START_FAILED')
        self.subscriptions.append((sub,symbol,timeframe));return sub
    def drain(self):
        if self.overflow:self.journal.fault('NOTICE_BACKPRESSURE')
        for _ in range(256):
            try:notice=self.pending.get_nowait()
            except queue.Empty:break
            try:self.journal.append(notice)
            except (ValueError,OSError,sqlite3.Error):
                self.failures+=1;self.journal.fault('PERSISTENCE_FAILED')
        for sub,symbol,tf in self.subscriptions:
            status=sub.status()
            self.journal.transport(symbol,tf,
                connected=status.get('transport_state')=='CONNECTED',reliable=status.get('reliable') is True)
    def health(self):return {'queue_depth':self.pending.qsize(),'queue_overflow':self.overflow,'persistence_failures':self.failures}
    def stop(self):
        for sub,_,_ in self.subscriptions:sub.stop()
        self.drain()
