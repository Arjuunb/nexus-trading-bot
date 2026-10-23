"""Immutable advisory artifacts in the existing isolated shadow store."""
from pathlib import Path
import json

from services.strategy_identity import canonical_configuration_json as encoded
from services.strategy_identity import configuration_fingerprint as digest


class ForwardQualificationStore:
    max_window_bytes = 8 * 1024 * 1024
    max_total_window_bytes = 64 * 1024 * 1024
    max_owner_window_bytes = 32 * 1024 * 1024
    max_owner_reports = 200
    max_total_reports = 2000

    def __init__(self, shadow_store):
        self.shadow = shadow_store
        path = Path(__file__).parents[1] / 'migrations/shadow/0005_forward_qualification.sql'
        raw = shadow_store._store
        if getattr(raw,'backend',None)=='postgresql':
            raw.execute('SELECT policy_version FROM forward_qualification_policies LIMIT 0')
            return
        with raw._lock, raw._db:
            tables = {r[0] for r in raw._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if any(name.startswith('v2_') or name in {
                'trade_decision_journal', 'paper_accounts', 'paper_orders', 'paper_positions',
                'paper_fills', 'account_balances', 'financial_journal',
            } for name in tables):
                raise ValueError('FINANCIAL_DATABASE_FORBIDDEN')
            raw._db.executescript(path.read_text())

    def install_policy(self, policy):
        payload = encoded(policy.as_dict())
        with self.shadow.transaction() as db:
            if getattr(db,'backend',None)=='postgresql': db.execute('SELECT pg_advisory_xact_lock(1060005)')
            old = db.execute('SELECT payload_json FROM forward_qualification_policies WHERE policy_version=?',
                             (policy.policy_version,)).fetchone()
            if old and old[0] != payload: raise ValueError('POLICY_VERSION_CONFLICT')
            if not old:
                db.execute('INSERT INTO forward_qualification_policies VALUES (?,?,?)',
                           (policy.policy_version, policy.fingerprint, payload))

    def report(self, report_id, *, owner_id):
        raw = self.shadow._store
        with raw._lock:
            row = raw._db.execute('SELECT payload_json FROM forward_qualification_reports WHERE report_id=? AND owner_id=?',
                                  (report_id, owner_id)).fetchone()
        return json.loads(row[0]) if row else None

    def by_key(self, key, request, *, owner_id):
        raw = self.shadow._store
        with raw._lock:
            row = raw._db.execute('SELECT request_json,report_id FROM forward_qualification_requests WHERE owner_id=? AND idempotency_key=?',
                                  (owner_id, key)).fetchone()
            if not row: return None
            if row[0] != encoded(request): raise ValueError('FORWARD_IDEMPOTENCY_CONFLICT')
            return self.report(row[1], owner_id=owner_id)

    def window(self, window_id, *, owner_id):
        raw = self.shadow._store
        with raw._lock:
            row = raw._db.execute('SELECT payload_json FROM forward_observation_windows WHERE window_id=? AND owner_id=?',
                                  (window_id, owner_id)).fetchone()
        return json.loads(row[0]) if row else None

    def commit(self, window, report, *, request, key):
        owner = report['owner_id']
        payload = encoded(window); size = len(payload.encode())
        if size > self.max_window_bytes or len(encoded(report).encode()) > self.max_window_bytes:
            raise ValueError('FORWARD_RESULT_SIZE_LIMIT')
        with self.shadow.transaction() as db:
            if getattr(db,'backend',None)=='postgresql': db.execute('SELECT pg_advisory_xact_lock(1060005)')
            old = self.by_key(key, request, owner_id=owner)
            if old: return old
            counts = db.execute('SELECT COUNT(*),COUNT(CASE WHEN owner_id=? THEN 1 END) FROM forward_qualification_requests', (owner,)).fetchone()
            if counts[0] >= self.max_total_reports or counts[1] >= self.max_owner_reports:
                raise ValueError('FORWARD_REPORT_QUOTA')
            previous = self.window(window['window_id'], owner_id=owner)
            if previous is not None and encoded(previous) != payload:
                raise ValueError('FORWARD_WINDOW_CONFLICT')
            if previous is None:
                usage = db.execute('SELECT COALESCE(SUM(payload_bytes),0),COALESCE(SUM(CASE WHEN owner_id=? THEN payload_bytes ELSE 0 END),0) FROM forward_observation_windows', (owner,)).fetchone()
                if usage[0] + size > self.max_total_window_bytes or usage[1] + size > self.max_owner_window_bytes:
                    raise ValueError('FORWARD_STORAGE_QUOTA')
                db.execute('INSERT INTO forward_observation_windows VALUES (?,?,?,?,?,?,?,?,?)',
                           (window['window_id'], owner, window['source_id'], window['scope_key'],
                            window['start'], window['end'], window['evidence_hash'], size, payload))
            overlaps = db.execute('SELECT window_id FROM forward_observation_windows WHERE owner_id=? AND scope_key=? AND window_id<>? AND julianday(period_start)<julianday(?) AND julianday(period_end)>julianday(?) ORDER BY period_start LIMIT 201',
                                  (owner, window['scope_key'], window['window_id'], window['end'], window['start'])).fetchall()
            if len(overlaps) > 200: raise ValueError('FORWARD_HISTORY_LIMIT')
            # Discovery is atomic with commitment; no competing report can erase
            # an earlier overlap. This is as-of lineage, not independent samples.
            report = {**report, 'overlapping_window_ids': [r[0] for r in overlaps]}
            if overlaps:
                report['warning_codes'] = sorted(set([*report['warning_codes'], 'OVERLAPPING_OBSERVATION_WINDOWS']))
            old = self.report(report['report_id'], owner_id=owner)
            if old:
                # Original overlap/calculation clock is an immutable as-of fact.
                compared = {k:v for k,v in old.items() if k not in {'created_at','overlapping_window_ids'}}
                current = {k:v for k,v in report.items() if k not in {'created_at','overlapping_window_ids'}}
                compared['warning_codes']=[x for x in compared['warning_codes'] if x!='OVERLAPPING_OBSERVATION_WINDOWS']
                current['warning_codes']=[x for x in current['warning_codes'] if x!='OVERLAPPING_OBSERVATION_WINDOWS']
                if encoded(compared) != encoded(current): raise ValueError('FORWARD_REPORT_CONFLICT')
                report = old
            else:
                if report.get('supersedes_report_id'):
                    prior = self.report(report['supersedes_report_id'], owner_id=owner)
                    if not prior or prior['identity'] != report['identity']:
                        raise ValueError('INVALID_FORWARD_SUPERSESSION')
                db.execute('INSERT INTO forward_qualification_reports VALUES (?,?,?,?,?,?,?)',
                           (report['report_id'], owner, report['source_id'], window['window_id'],
                            report['policy']['policy_version'], report['created_at'], encoded(report)))
                for state in ('DRAFT','EVALUATING','COMPLETED'):
                    event = {'workflow_state':state,'scope':'BOUNDED_SYNCHRONOUS_EVIDENCE_READ; NO_RESEARCH_JOB'}
                    db.execute('INSERT INTO forward_qualification_events VALUES (?,?,?,?,?,?)',
                               ('forward-event:'+digest([report['report_id'],state]),owner,report['report_id'],state,report['created_at'],encoded(event)))
            db.execute('INSERT INTO forward_qualification_requests VALUES (?,?,?,?)',
                       (owner,key,encoded(request),report['report_id']))
        return json.loads(encoded(report))

    def reviews(self, report_id, *, owner_id):
        raw = self.shadow._store
        with raw._lock:
            return [json.loads(r[0]) for r in raw._db.execute('SELECT payload_json FROM forward_qualification_reviews WHERE report_id=? AND owner_id=? ORDER BY rowid', (report_id,owner_id))]

    def review(self, report_id, body, *, owner_id, now):
        if set(body) != {'idempotency_key','decision','reason'} or body['decision'] not in {'ACKNOWLEDGED','REJECTED','REQUEST_MORE_EVIDENCE','ARCHIVED'}:
            raise ValueError('INVALID_FORWARD_REVIEW')
        if not 8 <= len(body['idempotency_key']) <= 160 or not 3 <= len(body['reason'].strip()) <= 1000:
            raise ValueError('INVALID_FORWARD_REVIEW')
        event = {'report_id':report_id,'owner_id':owner_id,'reviewer_id':owner_id,
                 'decision':body['decision'],'reason':body['reason'].strip()}
        rid = 'forward-review:'+digest({**event,'idempotency_key':body['idempotency_key']})
        with self.shadow.transaction() as db:
            if getattr(db,'backend',None)=='postgresql': db.execute('SELECT pg_advisory_xact_lock(1060005)')
            if not self.report(report_id, owner_id=owner_id): raise KeyError(report_id)
            old = db.execute('SELECT payload_json FROM forward_qualification_reviews WHERE owner_id=? AND idempotency_key=?', (owner_id,body['idempotency_key'])).fetchone()
            if old:
                old = json.loads(old[0])
                if any(old[k] != v for k,v in event.items()): raise ValueError('FORWARD_REVIEW_IDEMPOTENCY_CONFLICT')
                return old
            if db.execute('SELECT COUNT(*) FROM forward_qualification_reviews WHERE owner_id=?', (owner_id,)).fetchone()[0] >= 1000:
                raise ValueError('FORWARD_REVIEW_QUOTA')
            prior = self.reviews(report_id,owner_id=owner_id)
            if prior and prior[-1]['decision']=='ARCHIVED': raise ValueError('FORWARD_REVIEW_ARCHIVED')
            event.update(review_id=rid,created_at=now,execution_authorization=False)
            db.execute('INSERT INTO forward_qualification_reviews VALUES (?,?,?,?,?,?,?,?,?)',
                       (rid,owner_id,body['idempotency_key'],report_id,owner_id,body['decision'],event['reason'],now,encoded(event)))
            db.execute('INSERT INTO forward_qualification_events VALUES (?,?,?,?,?,?)',
                       ('forward-event:'+digest(rid),owner_id,report_id,'ARCHIVED' if body['decision']=='ARCHIVED' else 'REVIEWED',now,encoded(event)))
        return event

    def history(self, *, owner_id, source_id=None, limit=50, offset=0):
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000: raise ValueError('FORWARD_HISTORY_LIMIT')
        where = 'owner_id=?'; args = [owner_id]
        if source_id: where += ' AND source_id=?'; args.append(source_id)
        raw = self.shadow._store
        with raw._lock:
            rows = raw._db.execute('SELECT payload_json FROM forward_qualification_reports WHERE '+where+' ORDER BY created_at DESC,report_id LIMIT ? OFFSET ?', (*args,limit,offset)).fetchall()
        return [json.loads(r[0]) for r in rows]
