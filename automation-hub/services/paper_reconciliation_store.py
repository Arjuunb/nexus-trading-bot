"""Immutable read-only reconciliation snapshots, never financial repair."""
from dataclasses import asdict
from datetime import datetime,timezone
import json
import hashlib
from pathlib import Path

from services.paper_reconciliation import ReconciliationPolicy,reconcile_paper,VERSION
from services.strategy_identity import canonical_configuration_json as encoded,configuration_fingerprint as digest
from services.shadow_router import utc


class PaperReconciliationService:
    def __init__(self,shadow_store,source_reader, *, policy=None,clock=None):
        self.shadow=shadow_store;self.reader=source_reader;self.policy=policy or ReconciliationPolicy()
        self.clock=clock or (lambda:datetime.now(timezone.utc));raw=shadow_store._store
        if getattr(raw,'backend',None)=='postgresql':raw.execute('SELECT report_id FROM paper_reconciliation_reports LIMIT 0')
        else:
            with raw._lock,raw._db:
                tables={r[0] for r in raw._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if any(n.startswith('v2_') or n in {'paper_trades','paper_positions','trade_decision_journal','orders','positions','pa_sessions','smc_sessions'} for n in tables):raise ValueError('FINANCIAL_DATABASE_FORBIDDEN')
                raw._db.executescript((Path(__file__).parents[1]/'migrations/shadow/0007_paper_reconciliation.sql').read_text())
        with self.shadow.transaction() as db:
            self._lock(db)
            old=db.execute('SELECT payload_json FROM paper_reconciliation_policies WHERE policy_version=?',(self.policy.policy_version,)).fetchone()
            payload=encoded(asdict(self.policy))
            if old and old[0]!=payload:raise ValueError('RECONCILIATION_POLICY_CONFLICT')
            if not old:db.execute('INSERT INTO paper_reconciliation_policies VALUES (?,?)',(self.policy.policy_version,payload))

    @staticmethod
    def _lock(db):
        if getattr(db,'backend',None)=='postgresql':db.execute('SELECT pg_advisory_xact_lock(1060007)')

    def get(self,report_id, *, owner_id,include_snapshot=False):
        raw=self.shadow._store
        with raw._lock:
            row=raw._db.execute('SELECT * FROM paper_reconciliation_reports WHERE report_id=? AND owner_id=?',(report_id,owner_id)).fetchone()
        if not row:return None
        report=json.loads(row['payload_json'])
        if include_snapshot:report['original_snapshot']=json.loads(row['snapshot_json'])
        return report

    def history(self, *, owner_id,limit=50,offset=0):
        if not 1<=limit<=100 or not 0<=offset<=10000:raise ValueError('RECONCILIATION_QUERY_LIMIT')
        raw=self.shadow._store
        with raw._lock:
            rows=raw._db.execute('SELECT payload_json FROM paper_reconciliation_reports WHERE owner_id=? ORDER BY created_at DESC,report_id LIMIT ? OFFSET ?',(owner_id,limit,offset)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def assess(self,body, *, owner_id,commit=False):
        allowed={'source_id','start','end','idempotency_key','supersedes_report_id'}
        if set(body)-allowed or not {'source_id','start','end','idempotency_key'}<=set(body) or not 8<=len(body['idempotency_key'])<=160:raise ValueError('INVALID_RECONCILIATION_REQUEST')
        start,end=utc(body['start']),utc(body['end'])
        if end>self.clock() or end<=start:raise ValueError('INVALID_RECONCILIATION_PERIOD')
        request={k:v for k,v in body.items() if k!='idempotency_key'};request.update(start=start.isoformat(),end=end.isoformat(),policy_version=self.policy.policy_version)
        raw=self.shadow._store
        with raw._lock:
            previous=raw._db.execute('SELECT request_json,report_id FROM paper_reconciliation_requests WHERE owner_id=? AND idempotency_key=?',(owner_id,body['idempotency_key'])).fetchone()
        if previous and commit:
            if previous[0]!=encoded(request):raise ValueError('RECONCILIATION_IDEMPOTENCY_CONFLICT')
            return self.get(previous[1],owner_id=owner_id)
        bundle=self.reader.read(body['source_id'],owner_id=owner_id,start=start,end=end)
        report=reconcile_paper(bundle,owner_id=owner_id,start=start,end=end,policy=self.policy)
        revision={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ('paper_reconciliation.py','paper_reconciliation_store.py')}
        report.update(owner_id=owner_id,source_id=body['source_id'],created_at=self.clock().isoformat(),
            software_revision=revision,
            commitment_status='COMMITTED' if commit else 'NOT_COMMITTED',supersedes_report_id=body.get('supersedes_report_id'),
            report_id='paper-reconciliation:'+digest({'owner_id':owner_id,'source_id':body['source_id'],'evidence_hash':report['evidence_hash'],
                'period':[start.isoformat(),end.isoformat()],'policy':report['policy'],'calculation_version':VERSION,'software_revision':revision,'supersedes':body.get('supersedes_report_id')}))
        if not commit:return report
        snapshot=encoded(bundle);size=len(snapshot.encode())+len(encoded(report).encode())
        if size>8*1024*1024:raise ValueError('RECONCILIATION_SNAPSHOT_LIMIT')
        with self.shadow.transaction() as db:
            self._lock(db)
            previous=db.execute('SELECT request_json,report_id FROM paper_reconciliation_requests WHERE owner_id=? AND idempotency_key=?',(owner_id,body['idempotency_key'])).fetchone()
            if previous:
                if previous[0]!=encoded(request):raise ValueError('RECONCILIATION_IDEMPOTENCY_CONFLICT')
                return self.get(previous[1],owner_id=owner_id)
            original=self.get(report['report_id'],owner_id=owner_id)
            if original:
                material=lambda value:{k:v for k,v in value.items() if k!='created_at'}
                if encoded(material(original))!=encoded(material(report)):raise ValueError('RECONCILIATION_RESULT_CONFLICT')
                report=original
            else:
                quota=db.execute('SELECT COUNT(*),COALESCE(SUM(payload_bytes),0) FROM paper_reconciliation_reports WHERE owner_id=?',(owner_id,)).fetchone()
                global_usage=db.execute('SELECT COALESCE(SUM(payload_bytes),0) FROM paper_reconciliation_reports').fetchone()[0]
                if quota[0]>=100 or quota[1]+size>64*1024*1024 or global_usage+size>256*1024*1024:raise ValueError('RECONCILIATION_RETENTION_LIMIT')
                if body.get('supersedes_report_id'):
                    old=self.get(body['supersedes_report_id'],owner_id=owner_id)
                    if not old or old['scope']!=report['scope']:raise ValueError('INVALID_RECONCILIATION_SUPERSESSION')
                db.execute('INSERT INTO paper_reconciliation_reports VALUES (?,?,?,?,?,?,?,?)',
                    (report['report_id'],owner_id,body['source_id'],report['created_at'],report['evidence_hash'],size,snapshot,encoded(report)))
            requests=db.execute('SELECT COUNT(*) FROM paper_reconciliation_requests WHERE owner_id=?',(owner_id,)).fetchone()[0]
            total_requests=db.execute('SELECT COUNT(*) FROM paper_reconciliation_requests').fetchone()[0]
            if requests>=1000 or total_requests>=10000:raise ValueError('RECONCILIATION_REQUEST_LIMIT')
            db.execute('INSERT INTO paper_reconciliation_requests VALUES (?,?,?,?)',(owner_id,body['idempotency_key'],encoded(request),report['report_id']))
        return report
