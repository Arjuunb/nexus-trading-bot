"""Bounded local durable outbox for original operational observations.

At-least-once replay into the existing idempotent metadata store. This cannot
observe a dead process or certify uptime. Paths are trusted operator settings;
neither API clients nor research models can choose them.
"""
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
import json
import os
from pathlib import Path
import sqlite3
import threading

from services.operational_evidence import EVENT_TYPES
from services.shadow_router import utc
from services.strategy_identity import canonical_configuration_json as encoded,configuration_fingerprint as digest


class DurableOperationalSpool:
    def __init__(self,path,*,max_events=10000,max_bytes=32*1024*1024,max_owner_bytes=8*1024*1024,clock=None):
        self.path=str(path);self.clock=clock or (lambda:datetime.now(timezone.utc))
        if not 1<=max_events<=10000 or not 1024<=max_bytes<=32*1024*1024 or not 1024<=max_owner_bytes<=8*1024*1024:raise ValueError('INVALID_SPOOL_LIMIT')
        self.max_events=max_events;self.max_bytes=max_bytes;self.max_owner_bytes=max_owner_bytes;self._lock=threading.RLock()
        path=Path(path)
        if path.is_symlink():raise ValueError('INVALID_SPOOL_PATH')
        path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names-{'spool_sources','spool_events','spool_failures','sqlite_sequence'}:raise ValueError('FINANCIAL_OR_UNRELATED_DATABASE_FORBIDDEN')
            db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA max_page_count=16384')  # 64 MiB at the checked 4 KiB page size.
            if db.execute('PRAGMA page_size').fetchone()[0]!=4096:raise ValueError('UNSUPPORTED_SPOOL_PAGE_SIZE')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS spool_sources(owner_id TEXT NOT NULL,source_id TEXT NOT NULL,scope_json TEXT NOT NULL,PRIMARY KEY(owner_id,source_id));
                CREATE TABLE IF NOT EXISTS spool_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,owner_id TEXT NOT NULL,source_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,payload_json TEXT NOT NULL,payload_hash TEXT NOT NULL,payload_bytes INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,delivered_at TEXT,error_category TEXT,UNIQUE(owner_id,source_id,idempotency_key));
                CREATE INDEX IF NOT EXISTS spool_replay ON spool_events(status,sequence);
                CREATE TABLE IF NOT EXISTS spool_failures(owner_id TEXT NOT NULL,category TEXT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(owner_id,category));
            ''')
        os.chmod(path,0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=2,isolation_level=None);db.row_factory=sqlite3.Row
        db.execute('PRAGMA synchronous=FULL');db.execute('PRAGMA busy_timeout=2000')
        db.execute('PRAGMA max_page_count=16384')
        try:yield db
        finally:db.close()

    @contextmanager
    def transaction(self):
        with self._lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:yield db;db.commit()
            except BaseException:db.rollback();raise

    def register(self,*,owner_id,source_id,scope):
        text=encoded(scope)
        if not owner_id or not source_id or len(text.encode())>8192:raise ValueError('INVALID_OPERATIONAL_SOURCE')
        with self.transaction() as db:
            old=db.execute('SELECT scope_json FROM spool_sources WHERE owner_id=? AND source_id=?',(owner_id,source_id)).fetchone()
            if old:
                if old[0]!=text:raise ValueError('SPOOL_SOURCE_CONFLICT')
                return
            if db.execute('SELECT COUNT(*) FROM spool_sources').fetchone()[0]>=2000:raise ValueError('SPOOL_SOURCE_CAPACITY')
            db.execute('INSERT INTO spool_sources VALUES (?,?,?)',(owner_id,source_id,text))

    def enqueue(self,event,*,key):
        if set(event)-{'source_id','owner_id','event_type','occurred_at','worker_id','generation','payload'} or event.get('event_type') not in EVENT_TYPES or not 1<=len(key)<=200:raise ValueError('INVALID_OPERATIONAL_EVENT')
        event={**event,'occurred_at':utc(event['occurred_at']).isoformat()};text=encoded(event);size=len(text.encode())
        if size>4096:raise ValueError('INVALID_OPERATIONAL_EVENT_SIZE')
        owner,source=event['owner_id'],event['source_id'];event_id='operational:'+digest([owner,source,key]);overflow=False
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM spool_sources WHERE owner_id=? AND source_id=?',(owner,source)).fetchone():raise KeyError(source)
            old=db.execute('SELECT payload_hash FROM spool_events WHERE event_id=?',(event_id,)).fetchone()
            if old:
                if old[0]!=digest(event):raise ValueError('SPOOL_EVENT_CONFLICT')
                return event_id
            total=db.execute('SELECT COUNT(*),COALESCE(SUM(payload_bytes),0) FROM spool_events').fetchone()
            used=db.execute('SELECT COALESCE(SUM(payload_bytes),0) FROM spool_events WHERE owner_id=?',(owner,)).fetchone()[0]
            if total[0]>=self.max_events or total[1]+size>self.max_bytes or used+size>self.max_owner_bytes:
                db.execute("INSERT INTO spool_failures VALUES (?,'OVERFLOW',1) ON CONFLICT(owner_id,category) DO UPDATE SET count=count+1",(owner,));overflow=True
            else:db.execute('INSERT INTO spool_events(event_id,owner_id,source_id,idempotency_key,payload_json,payload_hash,payload_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                (event_id,owner,source,key,text,digest(event),size,'PENDING',self.clock().isoformat()))
        if overflow:raise ValueError('SPOOL_CAPACITY_EXHAUSTED')
        return event_id

    def replay(self,store,*,limit=100,event_id=None):
        if not 1<=limit<=200:raise ValueError('SPOOL_REPLAY_LIMIT')
        with self.connect() as db:
            rows=db.execute("SELECT * FROM spool_events WHERE status='PENDING'"+(' AND event_id=?' if event_id else '')+' ORDER BY sequence LIMIT ?',
                (event_id,limit) if event_id else (limit,)).fetchall()
        result={'delivered':0,'quarantined':0,'pending':0,'committed_events':{}}
        for row in rows:
            category=None
            try:
                event=json.loads(row['payload_json'])
                if digest(event)!=row['payload_hash'] or 'operational:'+digest([row['owner_id'],row['source_id'],row['idempotency_key']])!=row['event_id']:raise ValueError('SPOOL_CORRUPT')
                with self.connect() as db:scope=db.execute('SELECT scope_json FROM spool_sources WHERE owner_id=? AND source_id=?',(row['owner_id'],row['source_id'])).fetchone()
                store.register(owner_id=row['owner_id'],source_id=row['source_id'],scope=json.loads(scope[0]))
                accepted=store.emit(event,key=row['idempotency_key'])
                if accepted.get('event_id')!=row['event_id'] or accepted.get('commitment_status')!='COMMITTED':raise ValueError('SPOOL_COMMIT_CONFLICT')
                with self.transaction() as db:db.execute("UPDATE spool_events SET status='DELIVERED',delivered_at=?,error_category=NULL WHERE event_id=? AND status='PENDING'",(self.clock().isoformat(),row['event_id']))
                result['delivered']+=1
                result['committed_events'][row['event_id']]=accepted
            except (ValueError,KeyError,TypeError) as exc:
                category='CORRUPT' if str(exc)=='SPOOL_CORRUPT' else 'CONFLICT' if 'CONFLICT' in str(exc) else None
                if category:
                    with self.transaction() as db:db.execute('UPDATE spool_events SET status=?,error_category=? WHERE event_id=?',(category,category,row['event_id']))
                    result['quarantined']+=1
                else:result['pending']+=1;break
            except Exception:
                result['pending']+=1;break  # No error text or original event contents in logs.
        return result

    def expire_delivered(self,*,before):
        # This is only the transport copy, not source evidence or report storage.
        cutoff=utc(before)
        if cutoff>self.clock()-timedelta(days=1):raise ValueError('SPOOL_RETENTION_TOO_SHORT')
        with self.transaction() as db:
            removed=db.execute("DELETE FROM spool_events WHERE status='DELIVERED' AND delivered_at<?",(cutoff.isoformat(),)).rowcount
        return {'removed_transport_copies':removed,'original_metadata_evidence_deleted':False}

    def health(self,*,owner_id):
        with self.connect() as db:
            counts={r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM spool_events WHERE owner_id=? GROUP BY status',(owner_id,))}
            overflow=db.execute("SELECT count FROM spool_failures WHERE owner_id=? AND category='OVERFLOW'",(owner_id,)).fetchone()
            used=db.execute('SELECT COALESCE(SUM(payload_bytes),0) FROM spool_events WHERE owner_id=?',(owner_id,)).fetchone()[0]
        return {'status':'BLOCKED' if overflow or counts.get('CORRUPT') or counts.get('CONFLICT') else 'RECOVERING' if counts.get('PENDING') else 'LOCAL_VERIFIED',
            'pending':counts.get('PENDING',0),'delivered_transport_copies':counts.get('DELIVERED',0),
            'corrupt':counts.get('CORRUPT',0),'conflicting':counts.get('CONFLICT',0),'overflow_count':overflow[0] if overflow else 0,
            'owner_payload_bytes':used,'owner_payload_limit':self.max_owner_bytes,'global_payload_limit':self.max_bytes,
            'durability':'SQLITE_SYNCHRONOUS_FULL; LOCAL_DISK_NOT_OFF_HOST_BACKUP',
            'availability_claim':'BUFFERED_EVENTS_ONLY; UNOBSERVED_DOWNTIME_UNKNOWN','execution_authorization':False}


class BufferedOperationalStore:
    def __init__(self,store,spool):self.store=store;self.spool=spool
    def __getattr__(self,key):return getattr(self.store,key)
    def register(self,**kwargs):
        self.spool.register(**kwargs)
        try:self.store.register(**kwargs)
        except Exception:pass  # Durable source registration remains queued with each event.
    def emit(self,event,*,key):
        event_id=self.spool.enqueue(event,key=key)
        result=self.spool.replay(self.store,limit=1,event_id=event_id)
        if result['quarantined']:raise ValueError('SPOOL_EVENT_CONFLICT')
        if result['delivered']:
            return result['committed_events'][event_id]
        # A previously delivered transport copy remains idempotent without
        # claiming a database is currently reachable.
        with self.spool.connect() as db:
            old=db.execute('SELECT status FROM spool_events WHERE event_id=?',(event_id,)).fetchone()
        if old and old[0]=='DELIVERED':
            try:return self.store.emit(event,key=key)
            except Exception:pass
        return {**event,'event_id':event_id,'commitment_status':'BUFFERED'}


def worker_operational_store(store,*,path=None):
    selected=path or os.environ.get('NEXUS_OPERATIONAL_SPOOL_PATH')
    if not selected:
        raw=store.shadow._store
        if getattr(raw,'backend',None)=='postgresql':return store  # Operator must supply durable mounted disk.
        selected=str(Path(raw.path).with_suffix('.operational-spool.db'))
    return BufferedOperationalStore(store,DurableOperationalSpool(selected))
