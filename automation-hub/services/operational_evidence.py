"""Durable operational facts; sampled readiness never becomes trading returns."""
from collections import Counter,defaultdict
from datetime import datetime,timedelta,timezone
import math
from pathlib import Path
import re

from bot.data.resample import TF_SECONDS
from services.shadow_router import utc
from services.strategy_identity import canonical_configuration_json as encoded,configuration_fingerprint as digest

VERSION='operational_reliability.v1'
EVENT_TYPES={'MONITORING_STARTED','MONITORING_STOPPED','HEALTH_PROBE','WORKER_STARTED','WORKER_STOPPED',
 'WORKER_RESTART','FEED_CONNECTED','FEED_DISCONNECTED','CANDLE_RECEIVED','MISSING_CANDLE','STALE_CANDLE',
 'STALE_HTF_CANDLE','EVALUATION_STARTED','EVALUATION_COMPLETED','EVALUATION_ERROR','COMMIT_SUCCEEDED',
 'COMMIT_FAILED','QUEUE_BACKLOG','DATABASE_UNAVAILABLE','RECOVERY_STARTED','RECOVERY_COMPLETED',
 'TELEMETRY_LOSS','INVALID_CANDLE'}


def calculate_reliability(events, *, start,end,timeframe,cadence_seconds=60):
    start,end=utc(start),utc(end);step=TF_SECONDS.get(timeframe)
    if not step or end<=start or end-start>timedelta(days=7) or not 5<=cadence_seconds<=300 or len(events)>100000:
        raise ValueError('OPERATIONAL_WINDOW_LIMIT')
    expected=math.ceil((end-start).total_seconds()/cadence_seconds)
    seen={};duplicates=conflicts=0;valid=[]
    for event in events:
        identity=event.get('event_id')
        if identity in seen:
            duplicates+=int(digest(seen[identity])==digest(event));conflicts+=int(digest(seen[identity])!=digest(event));continue
        seen[identity]=event
        try:at=utc(event['occurred_at'])
        except (KeyError,ValueError,TypeError):conflicts+=1;continue
        if event.get('evidence_kind')!='OPERATIONAL_OBSERVATION':conflicts+=1;continue
        valid.append((at,event))
    anchors=sorted(((at,e) for at,e in valid if e['event_type'] in {'MONITORING_STARTED','MONITORING_STOPPED'} and at<=start),key=lambda pair:(pair[0],pair[1].get('committed_at',''),pair[1]['event_type']=='MONITORING_STOPPED',pair[1]['event_id']))
    began=bool(anchors and anchors[-1][1]['event_type']=='MONITORING_STARTED')
    capture_known=bool(anchors and anchors[-1][1].get('payload',{}).get('candle_capture_supported') is True)
    stopped=any(e['event_type']=='MONITORING_STOPPED' and start<=at<end for at,e in valid)
    bins=defaultdict(list);counts=Counter();candles={};repairs=0;latencies=[]
    for at,event in valid:
        if not start<=at<end:continue
        kind=event['event_type'];payload=event.get('payload') or {};counts[kind]+=1
        if kind=='HEALTH_PROBE':bins[int((at-start).total_seconds()//cadence_seconds)].append(payload)
        if kind=='CANDLE_RECEIVED':
            if payload.get('origin')!='LIVE_WEBSOCKET':repairs+=1;continue
            try:close=utc(payload['candle_close'])
            except (KeyError,ValueError,TypeError):conflicts+=1;continue
            if payload.get('finalized') is not True or not start<=close<end or close.timestamp()%step or not re.fullmatch('[0-9a-f]{64}',str(payload.get('candle_hash') or '')):continue
            prior=candles.get(close.isoformat())
            if prior and prior!=payload['candle_hash']:conflicts+=1
            elif prior:duplicates+=1
            else:candles[close.isoformat()]=payload['candle_hash']
        latency=payload.get('latency_ms')
        if isinstance(latency,(int,float)) and not isinstance(latency,bool) and math.isfinite(latency) and latency>=0:latencies.append(latency)
    missing=expected-len(bins)
    status='UNKNOWN' if not began else 'PARTIAL' if stopped or missing or conflicts or counts['TELEMETRY_LOSS'] else 'COMPLETE'
    def availability(field):
        if status!='COMPLETE' or any(any(type(p.get(field)) is not bool for p in values) for values in bins.values()):return None
        return sum(all(p[field] for p in values) for values in bins.values())/expected
    native=math.ceil(end.timestamp()/step)-math.ceil(start.timestamp()/step)
    commits=counts['COMMIT_SUCCEEDED']+counts['COMMIT_FAILED'];evaluations=counts['EVALUATION_COMPLETED']+counts['EVALUATION_ERROR']
    return {'calculation_version':VERSION,'monitoring_status':status,'start':start.isoformat(),'end':end.isoformat(),
        'availability_basis':'OBSERVED_PROBE_OPPORTUNITIES; NOT CONTINUOUS_PROCESS_OR_TRANSPORT_CERTIFICATION',
        'worker_availability':availability('worker_ready'),'feed_availability':availability('feed_connected'),
        'worker_denominator':expected,'feed_denominator':expected,'probe_cadence_seconds':cadence_seconds,
        'missing_probe_opportunities':missing,'unmonitored_probe_duration_upper_bound_seconds':min((end-start).total_seconds(),missing*cadence_seconds),
        'expected_candle_opportunities':native,'finalized_candle_receipts':len(candles),
        'candle_receipt_coverage':len(candles)/native if native and (capture_known or candles) else None,
        'missing_candle_opportunities':max(0,native-len(candles)) if capture_known else None,'historical_repair_events':repairs,
        'telemetry_loss_events':counts['TELEMETRY_LOSS'],'invalid_candle_events':counts['INVALID_CANDLE'],
        'decision_evaluation_success_rate':counts['EVALUATION_COMPLETED']/evaluations if evaluations else None,
        'evaluation_completions':counts['EVALUATION_COMPLETED'],'evaluation_errors':counts['EVALUATION_ERROR'],
        'commit_success_rate':counts['COMMIT_SUCCEEDED']/commits if commits else None,
        'commit_denominator':commits,'evidence_commitment_failures':counts['COMMIT_FAILED'],
        'worker_restarts':counts['WORKER_RESTART'],'feed_disconnects':counts['FEED_DISCONNECTED'],
        'stale_candle_events':counts['STALE_CANDLE'],'stale_htf_events':counts['STALE_HTF_CANDLE'],
        'duplicate_events':duplicates,'conflicting_events':conflicts,'recovery_duration_seconds':None,
        'processing_latency_ms':{'samples':len(latencies),'mean':sum(latencies)/len(latencies) if latencies else None,'max':max(latencies) if latencies else None},
        'warning_codes':(['UNMONITORED_PERIOD'] if not began else [])+(['INCOMPLETE_PROBE_COVERAGE'] if missing else [])+(['CONFLICTING_OPERATIONAL_EVIDENCE'] if conflicts else []),
        'profitability':'NOT_ASSESSED','execution_authorization':False}


class OperationalEvidenceStore:
    max_events=500000
    max_owner_events=100000
    max_bytes=256*1024*1024
    max_owner_bytes=64*1024*1024
    retention_seconds=30*86400

    def __init__(self,shadow_store, *, clock=None):
        self.shadow=shadow_store;self.clock=clock or (lambda:datetime.now(timezone.utc))
        raw=shadow_store._store
        if getattr(raw,'backend',None)=='postgresql':raw.execute('SELECT owner_id FROM operational_sources LIMIT 0')
        else:
            with raw._lock,raw._db:
                tables={r[0] for r in raw._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if any(n.startswith('v2_') or n in {'paper_trades','paper_positions','trade_decision_journal','orders','positions','pa_sessions','smc_sessions'} for n in tables):raise ValueError('FINANCIAL_DATABASE_FORBIDDEN')
                raw._db.executescript((Path(__file__).parents[1]/'migrations/shadow/0006_operational_evidence.sql').read_text())

    def register(self, *, owner_id,source_id,scope):
        if not owner_id or not source_id or len(encoded(scope).encode())>8192:raise ValueError('INVALID_OPERATIONAL_SOURCE')
        with self.shadow.transaction() as db:
            if getattr(db,'backend',None)=='postgresql':db.execute('SELECT pg_advisory_xact_lock(1060006)')
            old=db.execute('SELECT scope_json FROM operational_sources WHERE owner_id=? AND source_id=?',(owner_id,source_id)).fetchone()
            if old:
                if old[0]!=encoded(scope):raise ValueError('OPERATIONAL_SOURCE_CONFLICT')
                return
            if db.execute('SELECT COUNT(*) FROM operational_sources').fetchone()[0]>=2000:raise ValueError('OPERATIONAL_SOURCE_LIMIT')
            db.execute('INSERT INTO operational_sources VALUES (?,?,?,?)',(owner_id,source_id,encoded(scope),self.clock().isoformat()))

    def emit(self,event, *, key):
        allowed={'source_id','owner_id','event_type','occurred_at','worker_id','generation','payload'}
        if set(event)-allowed or event.get('event_type') not in EVENT_TYPES or not 1<=len(key)<=200:raise ValueError('INVALID_OPERATIONAL_EVENT')
        event={**event,'occurred_at':utc(event['occurred_at']).isoformat()};material=encoded(event)
        if len(material.encode())>4096:raise ValueError('OPERATIONAL_EVENT_SIZE_LIMIT')
        with self.shadow.transaction() as db:
            if getattr(db,'backend',None)=='postgresql':db.execute('SELECT pg_advisory_xact_lock(1060006)')
            scope=(event['owner_id'],event['source_id'])
            if not db.execute('SELECT source_id FROM operational_sources WHERE owner_id=? AND source_id=?',scope).fetchone():raise KeyError(event['source_id'])
            old=db.execute('SELECT payload_json FROM operational_events WHERE owner_id=? AND source_id=? AND idempotency_key=?',(*scope,key)).fetchone()
            if old:
                import json
                old=json.loads(old[0])
                if encoded({k:old[k] for k in event})!=material:raise ValueError('OPERATIONAL_IDEMPOTENCY_CONFLICT')
                return old
            count=db.execute('SELECT COUNT(*),COALESCE(SUM(payload_bytes),0) FROM operational_events').fetchone()
            owner_count=db.execute('SELECT COUNT(*),COALESCE(SUM(payload_bytes),0) FROM operational_events WHERE owner_id=?',(event['owner_id'],)).fetchone()
            result={**event,'event_id':'operational:'+digest([*scope,key]),'committed_at':self.clock().isoformat(),
                'evidence_hash':digest(event),'evidence_kind':'OPERATIONAL_OBSERVATION','commitment_status':'COMMITTED'}
            payload=encoded(result);size=len(payload.encode())
            if count[0]>=self.max_events or owner_count[0]>=self.max_owner_events or count[1]+size>self.max_bytes or owner_count[1]+size>self.max_owner_bytes:raise ValueError('TELEMETRY_RETENTION_LIMIT')
            db.execute('INSERT INTO operational_events VALUES (?,?,?,?,?,?,?,?,?,?)',
                (result['event_id'],*scope,key,event['event_type'],event['occurred_at'],result['committed_at'],result['evidence_hash'],size,payload))
            return result

    def events(self,source_id, *, owner_id,start,end,limit=100000):
        import json
        if not 1<=limit<=100000:raise ValueError('OPERATIONAL_QUERY_LIMIT')
        raw=self.shadow._store
        with raw._lock:
            rows=raw._db.execute('SELECT payload_json FROM operational_events WHERE owner_id=? AND source_id=? AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at,event_id LIMIT ?',
                (owner_id,source_id,utc(start).isoformat(),utc(end).isoformat(),limit+1)).fetchall()
        if len(rows)>limit:raise ValueError('OPERATIONAL_QUERY_LIMIT')
        return [json.loads(r[0]) for r in rows]

    def event_page(self,source_id, *, owner_id,start,end,limit=100,offset=0):
        import json
        start,end=utc(start),utc(end)
        if not 1<=limit<=200 or not 0<=offset<=100000 or end<=start or end-start>timedelta(days=7):raise ValueError('OPERATIONAL_QUERY_LIMIT')
        raw=self.shadow._store
        with raw._lock:
            rows=raw._db.execute('SELECT payload_json FROM operational_events WHERE owner_id=? AND source_id=? AND occurred_at>=? AND occurred_at<? ORDER BY occurred_at,event_id LIMIT ? OFFSET ?',
                (owner_id,source_id,start.isoformat(),end.isoformat(),limit+1,offset)).fetchall()
        return {'rows':[json.loads(r[0]) for r in rows[:limit]],'has_more':len(rows)>limit,'offset':offset,'limit':limit}

    def sources(self, *, owner_id,limit=50):
        import json
        if not 1<=limit<=100:raise ValueError('OPERATIONAL_QUERY_LIMIT')
        raw=self.shadow._store
        with raw._lock:rows=raw._db.execute("SELECT s.*,(SELECT MIN(e.occurred_at) FROM operational_events e WHERE e.owner_id=s.owner_id AND e.source_id=s.source_id AND e.event_type='MONITORING_STARTED') AS monitoring_started_at FROM operational_sources s WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",(owner_id,limit)).fetchall()
        return [{'source_id':r['source_id'],'scope':json.loads(r['scope_json']),'source_registered_at':r['created_at'],'monitoring_started_at':r['monitoring_started_at']} for r in rows]

    def source(self,source_id, *, owner_id):
        raw=self.shadow._store
        with raw._lock:row=raw._db.execute('SELECT scope_json FROM operational_sources WHERE source_id=? AND owner_id=?',(source_id,owner_id)).fetchone()
        if not row:return None
        import json
        return json.loads(row[0])

    def report(self,source_id, *, owner_id,start,end,timeframe):
        start,end=utc(start),utc(end)
        if end>self.clock() or end<=start or end-start>timedelta(days=7):raise ValueError('OPERATIONAL_WINDOW_LIMIT')
        oldest=self.clock()-timedelta(seconds=self.retention_seconds)
        rows=self.events(source_id,owner_id=owner_id,start=max(start,oldest),end=end)
        # A ten-day-old start remains relevant to today's window. Retain the
        # latest preceding lifecycle record, including a stop, rather than
        # inferring monitoring from an arbitrary lookback or an empty table.
        raw=self.shadow._store
        with raw._lock:
            anchor=raw._db.execute("SELECT payload_json FROM operational_events WHERE owner_id=? AND source_id=? AND event_type IN ('MONITORING_STARTED','MONITORING_STOPPED') AND occurred_at<? ORDER BY occurred_at DESC,event_id DESC LIMIT 1",
                (owner_id,source_id,start.isoformat())).fetchone()
        if anchor:
            import json
            rows.insert(0,json.loads(anchor[0]))
        report=calculate_reliability(rows,start=start,end=end,timeframe=timeframe)
        if start<oldest:
            report.update(monitoring_status='UNKNOWN',worker_availability=None,feed_availability=None)
            report['warning_codes'].append('RETENTION_UNAVAILABLE')
        report.update(source_id=source_id,evidence_references=[r['event_id'] for r in rows],evidence_hash=digest(rows),
            retention_seconds=self.retention_seconds)
        return report


class ShadowOperationalRecorder:
    """Observe existing worker decisions/subscription probes, never execute.

    Missing delivery provenance is explicit: a polled REST-seeded frame cannot
    be promoted to a live WebSocket receipt merely because its candle is closed.
    """
    def __init__(self,store,worker_id, *, clock):
        self.store=store;self.worker_id=worker_id;self.clock=clock;self.started=set();self.last_probe={};self.failures=0;self.pending_failures=0;self.capture_supported={};self.start_count={};self.connection_state={}

    def record(self,reg,kind,payload=None,key=None,occurred_at=None):
        import logging
        source=reg['registration_id'];owner=reg['owner_id']
        scope={k:reg.get(k) for k in ('strategy_id','strategy_version','config_fingerprint','venue','market_type','symbol','timeframe')}
        scope['observation_kind']='RESEARCH_JOB_ATTEMPT' if reg.get('job_id') else 'SHADOW_REGISTRATION'
        if reg.get('worker_kind'):
            scope.update(observation_kind='OPERATOR_WORKER',worker_kind=reg['worker_kind'],worker_backend=reg.get('worker_backend'))
        try:
            self.store.register(owner_id=owner,source_id=source,scope=scope)
            if source not in self.started:
                self.start_count[source]=self.start_count.get(source,0)+1
                self.store.emit({'owner_id':owner,'source_id':source,'event_type':'MONITORING_STARTED',
                    'worker_id':self.worker_id,'generation':self.worker_id,'occurred_at':self.clock().isoformat(),'payload':{'candle_capture_supported':self.capture_supported.get(source)}},key='monitor-start:'+self.worker_id+':'+str(self.start_count[source]))
                self.started.add(source)
            if self.pending_failures:
                lost=self.pending_failures
                self.store.emit({'owner_id':owner,'source_id':source,'event_type':'TELEMETRY_LOSS','worker_id':self.worker_id,
                    'generation':self.worker_id,'occurred_at':self.clock().isoformat(),'payload':{'failed_commit_attempts':lost,'scope':'WORKER_PROCESS','not_reconstructed':True}},key='telemetry-loss:'+self.worker_id+':'+str(self.failures))
                self.pending_failures=0
            event={'owner_id':owner,'source_id':source,'event_type':kind,'worker_id':self.worker_id,
                'generation':self.worker_id,'occurred_at':utc(occurred_at).isoformat() if occurred_at else self.clock().isoformat(),'payload':payload or {}}
            return self.store.emit(event,key=key or digest(event))
        except Exception as exc:
            self.failures+=1;self.pending_failures+=1
            logging.getLogger(__name__).error('operational_evidence_commit_failed',extra={'error_category':type(exc).__name__})
            return None

    def probe(self,reg,connection):
        now=self.clock();sid=reg['registration_id']
        if sid in self.last_probe and (now-self.last_probe[sid]).total_seconds()<60:return
        old=self.last_probe.get(sid)
        state=connection.get('transport_state')
        if state in {'CONNECTED','DISCONNECTED','CONNECTING','RECOVERING','STALE'} and state!=self.connection_state.get(sid):
            kind={'CONNECTED':'FEED_CONNECTED','DISCONNECTED':'FEED_DISCONNECTED','STALE':'STALE_CANDLE','CONNECTING':'RECOVERY_STARTED','RECOVERING':'RECOVERY_STARTED'}[state]
            if self.record(reg,kind,{'transport_state':state,'timing_basis':'OBSERVED_PROBE; ORIGINAL_DISCONNECT_TIME_UNKNOWN'}):self.connection_state[sid]=state
        result=self.record(reg,'HEALTH_PROBE',{'worker_ready':True,
            'feed_connected':connection.get('transport_state')=='CONNECTED' if connection.get('transport_state') in {'CONNECTED','DISCONNECTED'} else None,
            'freshness':connection.get('reliable'),'previous_probe_known':old is not None})
        if result:self.last_probe[sid]=now

    def decision(self,reg,decision):
        did=decision['decision_id']
        if decision.get('evaluation_started_at'):self.record(reg,'EVALUATION_STARTED',{'decision_id':did},key=did+':start',occurred_at=decision['evaluation_started_at'])
        self.record(reg,'EVALUATION_ERROR' if decision['status']=='EVALUATION_ERROR' else 'EVALUATION_COMPLETED',{'decision_id':did,'status':decision['status'],
            'latency_ms':decision.get('evaluation_latency_ms')},key=did+':evaluation',occurred_at=decision.get('evaluation_completed_at'))
        if 'STALE_HTF_CANDLE' in decision.get('rejection_reasons',[]):self.record(reg,'STALE_HTF_CANDLE',{'decision_id':did},key=did+':htf-stale')
        if decision['status']=='BLOCKED_STALE_DATA':self.record(reg,'STALE_CANDLE',{'decision_id':did},key=did+':stale')
        if any(reason in {'MISSING_CANDLE','DATA_GAP'} for reason in decision.get('rejection_reasons',[])):self.record(reg,'MISSING_CANDLE',{'decision_id':did,'reasons':decision['rejection_reasons']},key=did+':missing')
        self.record(reg,'COMMIT_SUCCEEDED',{'decision_id':did,'decision_commit_timestamp':decision['evidence_commit_recorded_at']},key=did+':commit')
