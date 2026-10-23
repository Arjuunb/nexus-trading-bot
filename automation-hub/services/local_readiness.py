"""Immutable LOCAL assessment of retained validation evidence, never deployment."""
from datetime import datetime,timezone
import json
import re
from pathlib import Path
from xml.etree import ElementTree

from services.strategy_identity import canonical_configuration_json as encoded,configuration_fingerprint as digest

GATES={'postgresql','supervision','resource_limits','migration','backup_restore','spool_recovery',
       'financial_reconciliation','feed_soak','credential_separation','regression'}


def junit_evidence(path,*,suite,environment_id):
    """Trusted local test-run output. No API-provided paths or success flags."""
    data=Path(path).read_bytes()
    if len(data)>8*1024*1024:raise ValueError('TEST_EVIDENCE_SIZE_LIMIT')
    root=ElementTree.fromstring(data)
    cases=list(root.iter('testcase'))
    if not cases:raise ValueError('TEST_EVIDENCE_EMPTY')
    failed=sum(any(c.find(name) is not None for name in ('failure','error')) for c in cases)
    skipped=sum(c.find('skipped') is not None for c in cases)
    return {'kind':'ORIGINAL_LOCAL_JUNIT','suite':suite,'environment_id':environment_id,
        'passed':len(cases)-failed-skipped,'failed':failed,'skipped':skipped,
        'sha256':__import__('hashlib').sha256(data).hexdigest(),'result_reference':Path(path).name,
        'status':'LOCAL_VERIFIED' if failed==0 and skipped==0 else 'BLOCKED' if failed else 'PARTIAL'}


class LocalReadinessStore:
    def __init__(self,shadow_store,*,clock=None):
        self.shadow=shadow_store;self.clock=clock or (lambda:datetime.now(timezone.utc));raw=shadow_store._store
        if getattr(raw,'backend',None)=='postgresql':raw.execute('SELECT assessment_id FROM local_readiness_assessments LIMIT 0')
        else:
            with raw._lock,raw._db:
                names={r[0] for r in raw._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if any(n.startswith('v2_') or n in {'paper_trades','positions','orders','financial_journal'} for n in names):raise ValueError('FINANCIAL_DATABASE_FORBIDDEN')
                raw._db.executescript((Path(__file__).parents[1]/'migrations/shadow/0008_operational_resilience.sql').read_text())

    def commit(self,*,owner_id,environment_id,software_revision,database_schema_version,worker_backend,start,end,reviewer,gates):
        """Operator-only method; no authenticated HTTP write route exists."""
        from services.shadow_router import utc
        if not owner_id or not reviewer or not environment_id.startswith(('local:','disposable:')) or worker_backend not in {'sqlite','postgresql'} or utc(end)<=utc(start):raise ValueError('INVALID_LOCAL_READINESS_SCOPE')
        if set(gates)-GATES:raise ValueError('UNKNOWN_READINESS_GATE')
        normalized={}
        for name in sorted(GATES):
            gate=gates.get(name) or {'status':'UNKNOWN','reason':'NO_LOCAL_VALIDATION_EVIDENCE'}
            if gate.get('status') not in {'LOCAL_VERIFIED','PARTIAL','BLOCKED','UNKNOWN'}:raise ValueError('INVALID_READINESS_STATUS')
            if gate['status']=='LOCAL_VERIFIED' and (not gate.get('evidence_reference') or not re.fullmatch('[0-9a-f]{64}',str(gate.get('evidence_hash') or ''))):raise ValueError('READINESS_EVIDENCE_REQUIRED')
            normalized[name]=gate
        statuses={g['status'] for g in normalized.values()}
        status='BLOCKED' if 'BLOCKED' in statuses else 'LOCAL_VERIFIED' if statuses=={'LOCAL_VERIFIED'} else 'PARTIAL' if 'LOCAL_VERIFIED' in statuses else 'UNKNOWN'
        body={'schema_version':'local_readiness.v1','owner_id':owner_id,'environment_id':environment_id,
            'software_revision':software_revision,'database_schema_version':database_schema_version,
            'worker_backend':worker_backend,'start':utc(start).isoformat(),'end':utc(end).isoformat(),
            'reviewer':reviewer,'gates':normalized,'status':status,'production_certification':False,
            'staging_certification':False,'execution_authorization':False}
        fingerprint=digest(body);body.update(assessment_id='local-readiness:'+fingerprint,evidence_hash=fingerprint,created_at=self.clock().isoformat())
        payload=encoded(body)
        if len(payload.encode())>256*1024:raise ValueError('READINESS_SIZE_LIMIT')
        with self.shadow.transaction() as db:
            if getattr(db,'backend',None)=='postgresql':db.execute('SELECT pg_advisory_xact_lock(1110008)')
            old=db.execute('SELECT payload_json FROM local_readiness_assessments WHERE assessment_id=? AND owner_id=?',(body['assessment_id'],owner_id)).fetchone()
            if old:return json.loads(old[0])
            if db.execute('SELECT COUNT(*) FROM local_readiness_assessments WHERE owner_id=?',(owner_id,)).fetchone()[0]>=100:raise ValueError('READINESS_OWNER_QUOTA')
            if db.execute('SELECT COUNT(*) FROM local_readiness_assessments').fetchone()[0]>=1000:raise ValueError('READINESS_GLOBAL_QUOTA')
            db.execute('INSERT INTO local_readiness_assessments VALUES (?,?,?,?,?,?)',
                (body['assessment_id'],owner_id,body['created_at'],fingerprint,len(payload.encode()),payload))
        return body

    def get(self,assessment_id,*,owner_id):
        raw=self.shadow._store
        with raw._lock:row=raw._db.execute('SELECT payload_json FROM local_readiness_assessments WHERE assessment_id=? AND owner_id=?',(assessment_id,owner_id)).fetchone()
        return json.loads(row[0]) if row else None

    def history(self,*,owner_id,limit=50,offset=0):
        if not 1<=limit<=100 or not 0<=offset<=10000:raise ValueError('READINESS_QUERY_LIMIT')
        raw=self.shadow._store
        with raw._lock:rows=raw._db.execute('SELECT payload_json FROM local_readiness_assessments WHERE owner_id=? ORDER BY created_at DESC,assessment_id LIMIT ? OFFSET ?',(owner_id,limit,offset)).fetchall()
        return [json.loads(r[0]) for r in rows]
