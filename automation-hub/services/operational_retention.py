"""Bounded content-addressed copies and aggregates of original telemetry.

Raw metadata deletion is deliberately not automatic: immutable reports pin
original evidence. Exhaustion is explicit. This archive does not fill gaps or
turn sampled probes into continuous uptime.
"""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime,timezone
import fcntl
import gzip
import json
import os
from pathlib import Path
import tempfile

from services.local_readiness import LocalReadinessStore
from services.shadow_router import utc
from services.strategy_identity import canonical_configuration_json as encoded,configuration_fingerprint as digest


class OperationalRetention:
    def __init__(self,store,root,*,max_bytes=128*1024*1024,max_owner_bytes=32*1024*1024):
        if not 1024<=max_bytes<=128*1024*1024 or not 1024<=max_owner_bytes<=32*1024*1024:raise ValueError('RETENTION_LIMIT_INVALID')
        self.store=store;self.root=Path(root);self.max_bytes=max_bytes;self.max_owner_bytes=max_owner_bytes
        if self.root.is_symlink():raise ValueError('INVALID_RETENTION_ROOT')
        self.root.mkdir(parents=True,exist_ok=True)
        LocalReadinessStore(store.shadow)

    @contextmanager
    def locked(self):
        with (self.root/'.archive.lock').open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(lock,fcntl.LOCK_UN)

    def archive(self,source_id,*,owner_id,start,end):
        start,end=utc(start),utc(end)
        if end<=start or (end-start).total_seconds()>7*86400:raise ValueError('RETENTION_WINDOW_LIMIT')
        if self.store.source(source_id,owner_id=owner_id) is None:raise KeyError(source_id)
        events=self.store.events(source_id,owner_id=owner_id,start=start,end=end,limit=10000)
        events=[e for e in events if utc(e['occurred_at'])<end]
        if not events:raise ValueError('RETENTION_NO_ORIGINAL_EVENTS')
        content=encoded(events).encode();content_hash=digest(events);compressed=gzip.compress(content,mtime=0)
        if len(content)>16*1024*1024 or len(compressed)>8*1024*1024:raise ValueError('RETENTION_SEGMENT_LIMIT')
        name=digest(owner_id)+'-'+content_hash+'.json.gz';path=self.root/name
        body={'schema_version':'operational-retention.v1','segment_id':'operational-segment:'+digest([owner_id,source_id,start.isoformat(),end.isoformat(),content_hash]),
            'owner_id':owner_id,'source_id':source_id,'start':start.isoformat(),'end':end.isoformat(),
            'content_hash':content_hash,'event_count':len(events),'compressed_bytes':len(compressed),
            'event_ids':[e['event_id'] for e in events],'event_hashes':[e['evidence_hash'] for e in events],
            'aggregate_version':'operational-counts.v1','event_type_counts':dict(Counter(e['event_type'] for e in events)),
            'coverage_claim':'ORIGINAL_EVENTS_ONLY; MISSING_OBSERVATIONS_UNKNOWN',
            'retention_status':'RETAINED; ORIGINAL_METADATA_NOT_DELETED','snapshot_file':name,
            'created_at':datetime.now(timezone.utc).isoformat(),'execution_authorization':False}
        with self.locked():
            files=list(self.root.glob('*.json.gz'))
            if len(files)>5000:raise ValueError('RETENTION_CAPACITY_EXHAUSTED')
            total=sum(p.stat().st_size for p in files);owner_used=sum(p.stat().st_size for p in files if p.name.startswith(digest(owner_id)+'-'))
            if not path.exists():
                if total+len(compressed)>self.max_bytes or owner_used+len(compressed)>self.max_owner_bytes:raise ValueError('RETENTION_CAPACITY_EXHAUSTED')
                fd,temporary=tempfile.mkstemp(dir=self.root,prefix='.pending-')
                try:
                    with os.fdopen(fd,'wb') as stream:stream.write(compressed);stream.flush();os.fsync(stream.fileno())
                    os.replace(temporary,path)
                    directory=os.open(self.root,os.O_RDONLY)
                    try:os.fsync(directory)
                    finally:os.close(directory)
                finally:
                    if Path(temporary).exists():Path(temporary).unlink()
            with self.store.shadow.transaction() as db:
                if getattr(db,'backend',None)=='postgresql':db.execute('SELECT pg_advisory_xact_lock(1110008)')
                old=db.execute('SELECT payload_json FROM operational_retention_segments WHERE segment_id=? AND owner_id=?',(body['segment_id'],owner_id)).fetchone()
                if old:return json.loads(old[0])
                db.execute('INSERT INTO operational_retention_segments VALUES (?,?,?,?,?,?,?,?,?)',
                    (body['segment_id'],owner_id,source_id,body['start'],body['end'],len(events),content_hash,body['created_at'],encoded(body)))
        return body

    def retrieve(self,segment_id,*,owner_id):
        raw=self.store.shadow._store
        with raw._lock:row=raw._db.execute('SELECT payload_json FROM operational_retention_segments WHERE segment_id=? AND owner_id=?',(segment_id,owner_id)).fetchone()
        if not row:raise KeyError(segment_id)
        manifest=json.loads(row[0]);name=digest(owner_id)+'-'+manifest['content_hash']+'.json.gz'
        path=self.root/name
        if not path.is_file() or path.is_symlink():raise ValueError('RETAINED_ORIGINAL_EVIDENCE_UNAVAILABLE')
        if path.stat().st_size>8*1024*1024:raise ValueError('RETENTION_SEGMENT_LIMIT')
        import zlib
        decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
        data=decoder.decompress(path.read_bytes(),16*1024*1024+1)
        if len(data)>16*1024*1024 or not decoder.eof:raise ValueError('RETENTION_INTEGRITY_FAILED')
        events=json.loads(data)
        if digest(events)!=manifest['content_hash'] or [e['event_id'] for e in events]!=manifest['event_ids'] or [e['evidence_hash'] for e in events]!=manifest['event_hashes']:raise ValueError('RETENTION_INTEGRITY_FAILED')
        return {'manifest':manifest,'events':events,'status':'LOCAL_VERIFIED','evidence_reconstruction':'EXACT_RETAINED_ORIGINALS'}

    def capacity(self,*,owner_id):
        with self.locked():
            files=list(self.root.glob(digest(owner_id)+'-*.json.gz'))
            used=sum(p.stat().st_size for p in files)
        return {'owner_bytes':used,'owner_limit':self.max_owner_bytes,'global_limit':self.max_bytes,
            'retained_files':len(files),'raw_metadata_reclamation':'BLOCKED_UNTIL_REVIEWED_REPORT_PIN_POLICY',
            'status':'PARTIAL','execution_authorization':False}
