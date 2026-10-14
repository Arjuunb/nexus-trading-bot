"""Append-only human review of an existing owned reconciliation report."""
from datetime import datetime,timezone
import json
from services.local_readiness import LocalReadinessStore
from services.strategy_identity import canonical_configuration_json as encoded,configuration_fingerprint as digest


class PaperEvidenceReview:
    def __init__(self,service):
        self.service=service;self.shadow=service.shadow if hasattr(service,'shadow') else service.store.shadow
        LocalReadinessStore(self.shadow)

    def review(self,report_id,body,*,owner_id):
        if set(body)-{'decision','reason','idempotency_key'}:raise ValueError('INVALID_PAPER_EVIDENCE_REVIEW')
        if body.get('decision') not in {'ACKNOWLEDGED','REJECTED','REQUEST_MORE_EVIDENCE','ARCHIVED'} or not 3<=len(body.get('reason',''))<=1000 or not 8<=len(body.get('idempotency_key',''))<=160:raise ValueError('INVALID_PAPER_EVIDENCE_REVIEW')
        report=self.service.get(report_id,owner_id=owner_id)
        if not report:raise KeyError(report_id)
        payload={'report_id':report_id,'owner_id':owner_id,'reviewer_id':owner_id,**body,
            'report_evidence_hash':report['evidence_hash'],'scope':'HUMAN_EVIDENCE_REVIEW_ONLY',
            'execution_authorization':False,'production_certification':False}
        identifier='paper-review:'+digest(payload);payload.update(review_id=identifier,created_at=datetime.now(timezone.utc).isoformat())
        with self.shadow.transaction() as db:
            if getattr(db,'backend',None)=='postgresql':db.execute('SELECT pg_advisory_xact_lock(1110008)')
            old=db.execute('SELECT payload_json FROM paper_evidence_reviews WHERE owner_id=? AND idempotency_key=?',(owner_id,body['idempotency_key'])).fetchone()
            if old:
                old=json.loads(old[0])
                if old['review_id']!=identifier:raise ValueError('PAPER_REVIEW_IDEMPOTENCY_CONFLICT')
                return old
            if db.execute('SELECT COUNT(*) FROM paper_evidence_reviews WHERE owner_id=?',(owner_id,)).fetchone()[0]>=1000:raise ValueError('PAPER_REVIEW_OWNER_QUOTA')
            db.execute('INSERT INTO paper_evidence_reviews VALUES (?,?,?,?,?,?)',(identifier,owner_id,report_id,body['idempotency_key'],payload['created_at'],encoded(payload)))
        return payload
