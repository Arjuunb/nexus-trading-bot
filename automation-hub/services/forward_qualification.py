"""Forward evidence readiness, deliberately independent of trading execution.

Only committed decision facts enter this module. Coverage and signal agreement
are descriptive; neither creates trade outcomes or authorizes paper/live use.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import hashlib
import math
from pathlib import Path
import platform
import threading

from bot.data.resample import TF_SECONDS
from services.shadow_router import utc
from services.shadow_comparison_service import ELIGIBLE, SIGNALS, _record_errors, canonical_candle, compare_decisions
from services.strategy_identity import configuration_fingerprint as digest

VERSION = 'forward_qualification.v1'
EVIDENCE_TAXONOMY = {
    'HISTORICAL_BACKTEST': {'execution_basis':'HISTORICAL_SIMULATION','profitability_eligible':False},
    'OUT_OF_SAMPLE_VALIDATION': {'execution_basis':'FROZEN_CHRONOLOGICAL_RESEARCH','profitability_eligible':False},
    'WALK_FORWARD_VALIDATION': {'execution_basis':'FROZEN_WINDOW_RESEARCH','profitability_eligible':False},
    'HYPOTHETICAL_SHADOW_OUTCOME': {'execution_basis':'EXPLICIT_VERSIONED_HYPOTHETICAL_ASSUMPTIONS','profitability_eligible':False},
    'FORWARD_SHADOW_DECISION': {'execution_basis':'OBSERVATION_WITHOUT_EXECUTION','profitability_eligible':False},
    'FORWARD_PAPER_TRADE': {'execution_basis':'ISOLATED_EXECUTED_PAPER_EPISODES','profitability_eligible':True},
    'LIVE_TRADE': {'execution_basis':'AUTHORITATIVE_LIVE_FILLS','profitability_eligible':True},
    'ACTIVE_DECISION_OBSERVATION': {'execution_basis':'PERSISTED_ACTIVE_DECISION_NOT_PROOF_OF_FILL','profitability_eligible':False},
}
for _kind,_contract in EVIDENCE_TAXONOMY.items():
    _contract.update(source_identity='ORIGINAL_OWNER_SCOPED_SOURCE_REFERENCE',
        configuration_identity='IMMUTABLE_EFFECTIVE_CONFIGURATION_FINGERPRINT',
        market_data_identity='EXACT_REVISION_OR_FINALIZED_CANONICAL_CANDLE',
        period_semantics='UTC_HALF_OPEN_INTERVAL',completeness='EXPLICIT_PASS_FAIL_UNKNOWN_GATES',
        cost_coverage='REQUIRED_FOR_OUTCOMES; UNKNOWN_IS_NOT_ZERO',
        reliability='ORIGINAL_COVERAGE_AND_FAILURE_EVIDENCE',
        qualification_eligibility='CONDITIONAL_EXECUTED_EPISODE_REVIEW' if _contract['profitability_eligible'] else 'CATEGORY_SPECIFIC_RESEARCH_OR_BEHAVIOUR_ONLY')

_ASSESSMENT_SLOT=threading.BoundedSemaphore(1)


@lru_cache(maxsize=1)
def software_revision():
    base=Path(__file__).parent
    names=('forward_qualification.py','forward_qualification_store.py','forward_paper_evidence_reader.py',
           'shadow_comparison_service.py','shadow_router.py','strategy_identity.py','operational_evidence.py')
    components={name:hashlib.sha256((base/name).read_bytes()).hexdigest() for name in names}
    components['bot/data/resample.py']=hashlib.sha256((base.parents[1]/'bot/data/resample.py').read_bytes()).hexdigest()
    return {'python_version':platform.python_version(),
            'component_hashes':components,
            'scope':'RETAIN_MATCHING_SOFTWARE_TO_RECOMPUTE; HASH_ALONE_DOES_NOT_RETAIN_CODE'}


@dataclass(frozen=True)
class ForwardQualificationPolicy:
    policy_version: str = 'forward-qualification.v1'
    minimum_duration_seconds: int = 7 * 86400
    minimum_opportunities: int = 1000
    minimum_observation_coverage: float = .95
    maximum_missing_data_rate: float = .05
    maximum_stale_data_rate: float = .01
    maximum_evaluation_error_rate: float = .01
    maximum_duplicate_conflict_rate: float = 0
    maximum_unexplained_worker_downtime_seconds: int = 300
    minimum_matched_opportunities: int = 100
    minimum_signal_sample: int = 30
    minimum_paper_episodes: int = 30
    require_configuration_identity: bool = True
    require_candle_context: bool = True
    require_lookback_verification: bool = True
    require_tenant_authorization: bool = True
    require_holdout_provenance: bool = True
    require_outcome_cost_coverage: bool = True

    def __post_init__(self):
        if not self.policy_version.strip() or len(self.policy_version)>80:
            raise ValueError('INVALID_FORWARD_POLICY')
        for name in ('minimum_duration_seconds','minimum_opportunities','minimum_matched_opportunities',
                     'minimum_signal_sample','minimum_paper_episodes'):
            if type(getattr(self,name)) is not int or getattr(self,name)<1: raise ValueError('INVALID_FORWARD_POLICY')
        if type(self.maximum_unexplained_worker_downtime_seconds) is not int or self.maximum_unexplained_worker_downtime_seconds<0:
            raise ValueError('INVALID_FORWARD_POLICY')
        for name in ('minimum_observation_coverage','maximum_missing_data_rate','maximum_stale_data_rate',
                     'maximum_evaluation_error_rate','maximum_duplicate_conflict_rate'):
            value=getattr(self,name)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1:
                raise ValueError('INVALID_FORWARD_POLICY')
        if self.minimum_observation_coverage<=0: raise ValueError('INVALID_FORWARD_POLICY')
        for name in ('require_configuration_identity','require_candle_context','require_lookback_verification',
                     'require_tenant_authorization','require_holdout_provenance','require_outcome_cost_coverage'):
            if type(getattr(self,name)) is not bool: raise ValueError('INVALID_FORWARD_POLICY')

    def as_dict(self): return asdict(self)

    @property
    def fingerprint(self): return digest(self.as_dict())


DEFAULT_POLICIES = (ForwardQualificationPolicy(), replace(ForwardQualificationPolicy(),
    policy_version='forward-qualification.strict.v1',minimum_observation_coverage=.99,
    maximum_missing_data_rate=.01,maximum_stale_data_rate=0,maximum_evaluation_error_rate=0))

DECISION_FIELDS = ('decision_id','owner_id','tenant_id','evidence_kind','source_evidence_kind',
    'source_id','registration_id','strategy_id','strategy_version','config_fingerprint',
    'configuration_reference','configuration_status','venue','market_type','symbol','timeframe',
    'candle_close_timestamp','decision_timestamp','canonical_candle','candle_hash','context_hash',
    'htf_context_identity','htf_alignment','lookback_status','observation_authorized','finalized',
    'source','data_freshness','status','signal_direction','evidence_completeness','persistence_status',
    'evidence_commit_recorded_at','market_context','rejection_reasons','missing_conditions',
    'evaluation_latency_ms','limitations')


def decision_facts(record):
    """Exact fields used by this calculation, not entire indicator histories."""
    facts=deepcopy({k:record.get(k) for k in DECISION_FIELDS if k!='market_context'})
    context=record.get('market_context') or {}
    facts['market_context']={k:deepcopy(context.get(k)) for k in
        ('session','trend_regime','volatility_regime','classifier_version')}
    return facts


def _gap_overlaps(event,start,end):
    if event.get('action')!='OBSERVATION_GAP': return False
    payload=event.get('payload') or {}
    try:
        if payload.get('after') and payload.get('until'):
            return utc(payload['after'])<end and utc(payload['until'])>start
        at=utc(payload.get('timestamp') or payload.get('at') or event['timestamp'])
        return start<=at<end
    except (KeyError,ValueError,TypeError): return None


def calculate_observation_window(decisions, *, owner_id, evidence_kind, timeframe, start, end, events=(), expected_identity=None, source_errors=()):
    start,end=utc(start),utc(end)
    step=TF_SECONDS.get(timeframe)
    if evidence_kind not in {'FORWARD_SHADOW_DECISION','ACTIVE_DECISION_OBSERVATION'}:
        raise ValueError('INVALID_FORWARD_EVIDENCE_CATEGORY')
    if not step or end<=start or end-start>timedelta(days=7) or start.timestamp()%step or end.timestamp()%step:
        raise ValueError('INVALID_OBSERVATION_WINDOW')
    n=int((end-start).total_seconds()/step)
    if n>2016 or len(decisions)>2016: raise ValueError('FORWARD_EVIDENCE_LIMIT')
    side='SHADOW' if evidence_kind=='FORWARD_SHADOW_DECISION' else 'ACTIVE'
    indexed=defaultdict(list);exclusions=Counter();malformed=0
    for record in decisions:
        try:
            stamp=utc(record['candle_close_timestamp'])
            if not start<=stamp<end or stamp.timestamp()%step:
                raise ValueError('OFF_CLOCK')
            indexed[stamp.isoformat()].append(record)
        except (KeyError,ValueError,TypeError): malformed+=1
    eligible=[];blocked=stale=errors=inactive=received=committed=conflicts=duplicates=0
    timestamps=[];identities=set();categories=Counter()
    for i in range(n):
        at=(start+timedelta(seconds=i*step)).isoformat();records=indexed.get(at,[])
        if not records: continue
        timestamps.append(at)
        unique={digest(r):r for r in records};duplicates+=len(records)-len(unique)
        candle_identities={digest(canonical_candle(r)) for r in unique.values()
                           if r.get('finalized') is True and canonical_candle(r) is not None}
        received+=int(len(candle_identities)==1)
        if len(unique)!=1:
            conflicts+=1;exclusions['CONFLICTING_DECISION_OPPORTUNITY']+=1;continue
        record=next(iter(unique.values()));status=record.get('status')
        identities.add(tuple(record.get(k) for k in ('strategy_id','strategy_version','config_fingerprint','venue','market_type','symbol','timeframe')))
        categories[status or 'UNKNOWN']+=1
        is_committed=bool(record.get('decision_id') and record.get('evidence_commit_recorded_at') and
            (side=='SHADOW' or record.get('persistence_status')=='COMMITTED'))
        committed+=int(is_committed)
        reasons=_record_errors(record,side,owner_id)
        reasons.extend(source_errors)
        if expected_identity is not None and any(record.get(k)!=v or v is None for k,v in expected_identity.items()):
            reasons.append('SOURCE_IDENTITY_MISMATCH')
        if record.get('timeframe')!=timeframe: reasons.append('TIMEFRAME_MISMATCH')
        if canonical_candle(record) is None: reasons.append('CANDLE_IDENTITY_UNVERIFIED')
        if not is_committed: reasons.append('DECISION_NOT_COMMITTED')
        stale+=int(status=='BLOCKED_STALE_DATA' or record.get('data_freshness') in {'STALE','STALE_CANDLES'})
        errors+=int(status=='EVALUATION_ERROR')
        inactive+=int(status in {'ACTIVE_PAUSED','ACTIVE_STOPPED'})
        if reasons:
            blocked+=1;exclusions.update(set(reasons))
        else: eligible.append(record)
    missing=n-len(indexed)
    if missing: exclusions['NO_DECISION_RECORDED']+=missing
    if malformed: exclusions['INVALID_OPPORTUNITY_TIMESTAMP']+=malformed
    if len(identities)>1: exclusions['CONFIGURATION_OR_SCOPE_CHANGED_WITHIN_WINDOW']+=1
    signals=[r for r in eligible if r['status'] in SIGNALS]
    longs=sum(r['status']=='SIGNAL_LONG' for r in signals)
    groups={key:Counter() for key in ('session','market_regime','volatility','rejection_reasons','missing_conditions')}
    for record in eligible:
        context=record.get('market_context') or {}
        for group,field in (('session','session'),('market_regime','trend_regime'),('volatility','volatility_regime')):
            groups[group][context.get(field) or 'UNKNOWN']+=1
    for records in indexed.values():
        for record in records:
            groups['rejection_reasons'].update(str(x) for x in record.get('rejection_reasons') or [])
            groups['missing_conditions'].update(str(x) for x in record.get('missing_conditions') or [])
    conflicts+=sum(e.get('action')=='CANDLE_REVISION_CONFLICT' and any(
        e.get('payload',{}).get('original_decision_id')==r.get('decision_id') for r in decisions) for e in events)
    coverage={'expected_opportunities':n,'received_finalized_candles':received,
        'recorded_decisions':len(indexed),'eligible_decisions':len(eligible),'missing_decisions':missing,
        'blocked_decisions':blocked,'inactive_opportunities':inactive,'conflicting_opportunities':conflicts,
        'duplicate_records':duplicates,'malformed_records':malformed,
        'market_data_coverage':received/n,'decision_evaluation_coverage':len(eligible)/n,
        'active_decision_evidence_coverage':len(eligible)/n if side=='ACTIVE' else None,
        'shadow_decision_evidence_coverage':len(eligible)/n if side=='SHADOW' else None,
        'evidence_commitment_completeness':committed/n,
        'denominator_basis':'REQUESTED_FINALIZED_CANDLE_CLOCK; NOT RAW_FEED_UPTIME'}
    return {'start':start.isoformat(),'end':end.isoformat(),'timeframe':timeframe,'evidence_kind':evidence_kind,
        'duration_seconds':(end-start).total_seconds(),'coverage':coverage,'exclusion_reasons':dict(exclusions),
        'evidence_completeness':'COMPLETE' if len(eligible)==n and not exclusions and not conflicts else 'PARTIAL' if decisions else 'UNKNOWN',
        'reliability':{'missing_data_rate':missing/n,'stale_data_rate':stale/n,'evaluation_error_rate':errors/n,
            'duplicate_conflict_rate':(conflicts+duplicates)/n,'data_freshness_compliance':sum(r.get('data_freshness')=='FRESH' for rs in indexed.values() for r in rs)/len(decisions) if decisions else None,
            'unobserved_opportunity_duration_seconds':missing*step,
            'worker_availability':None,'worker_downtime_seconds':None,'recovery_success_rate':None,
            'recorded_gap_events':sum(_gap_overlaps(e,start,end) is True for e in events),
            'unlocated_gap_events':sum(_gap_overlaps(e,start,end) is None for e in events),
            'gap_event_basis':'RECORDED_INTERVAL_OVERLAP_OR_HOST_EVENT_TIME; NOT DOWNTIME_DURATION',
            'worker_timing_basis':'UNKNOWN; MISSING DECISIONS DO NOT IDENTIFY DOWNTIME CAUSE'},
        'behaviour':{'signal_count':len(signals),'signal_frequency':len(signals)/len(eligible) if eligible else None,
            'long_count':longs,'short_count':len(signals)-longs,
            'long_share':longs/len(signals) if signals else None,'short_share':(len(signals)-longs)/len(signals) if signals else None,
            'no_signal_count':sum(r['status']=='NO_SIGNAL' for r in eligible),
            'decision_statuses':dict(categories),'breakdowns':{k:dict(v) for k,v in groups.items()}},
        'profitability':'NOT_ASSESSED'}


def _amount(value):
    try:
        number=Decimal(str(value))
        return number if not isinstance(value,bool) and number.is_finite() else None
    except (InvalidOperation,ValueError,TypeError): return None


def assess_paper_evidence(evidence=None):
    evidence=evidence or {};rows=evidence.get('rows') or [];reasons=[]
    if evidence.get('evidence_kind')!='FORWARD_PAPER_TRADE': reasons.append('FORWARD_PAPER_EVIDENCE_UNAVAILABLE')
    account=evidence.get('account_id')
    if not account: reasons.append('PAPER_ACCOUNT_UNVERIFIED')
    if any(r.get('account_id') not in {None,account} for r in rows): reasons.append('PAPER_ACCOUNT_MISMATCH')
    costs={}
    for name in ('fees','funding','slippage','spread'):
        costs[name]='COMPLETE' if rows and all(_amount(r.get(name)) is not None and r.get(name+'_coverage') in {'OBSERVED','BOOKED','VERIFIED_ZERO','EXEMPT'}
            and (r.get(name+'_coverage') not in {'VERIFIED_ZERO','EXEMPT'} or _amount(r.get(name))==0) for r in rows) else 'UNKNOWN'
        if costs[name]!='COMPLETE': reasons.append('PAPER_'+name.upper()+'_UNKNOWN')
    if evidence.get('authority_status')!='COMPLETE': reasons.append('AUTHORITATIVE_LEDGER_RECONCILIATION_UNVERIFIED')
    if evidence.get('journal_status')!='COMPLETE': reasons.append('PAPER_JOURNAL_COMPLETENESS_UNVERIFIED')
    if not rows or any(not all(r.get(k) is not None for k in ('episode_id','opened_at','closed_at','entry_fill_ids','exit_fill_ids','quantity','net_pnl')) for r in rows):
        reasons.append('PAPER_EPISODE_LINEAGE_INCOMPLETE')
    if len({r.get('episode_id') for r in rows})!=len(rows): reasons.append('DUPLICATE_PAPER_EPISODES')
    if evidence.get('limitations'): reasons.extend(evidence['limitations'])
    return {'evidence_kind':'FORWARD_PAPER_TRADE','account_id':account,'completed_episode_count':len(rows),
        'evidence_references':[r.get('episode_id') for r in rows if r.get('episode_id')],
        'cost_coverage':costs,'qualification_eligible':not reasons,'blocking_reasons':sorted(set(reasons)),
        'authority_status':evidence.get('authority_status','UNKNOWN'),'journal_status':evidence.get('journal_status','UNKNOWN'),
        'profitability':'NOT_ASSESSED'}


def evaluate_forward_window(window, *, policy=None, holdout=None, paper=None, matched=None):
    policy=policy or ForwardQualificationPolicy();c=window['coverage'];r=window['reliability'];n=c['expected_opportunities']
    checks=[]
    def check(name,condition,reason,purpose='BEHAVIOUR'):
        checks.append({'name':name,'status':'UNKNOWN' if condition is None else 'PASS' if condition else 'FAIL',
                       'reason':reason,'purpose':purpose})
    check('minimum_observation_duration',window['duration_seconds']>=policy.minimum_duration_seconds,
          f"requires {policy.minimum_duration_seconds}s; observed {window['duration_seconds']}s")
    check('minimum_opportunities',n>=policy.minimum_opportunities,f'requires {policy.minimum_opportunities}; observed {n}')
    check('observation_coverage',c['decision_evaluation_coverage']>=policy.minimum_observation_coverage,
          f"eligible/expected={c['eligible_decisions']}/{n}; requires {policy.minimum_observation_coverage}")
    for name,limit in (('missing_data_rate',policy.maximum_missing_data_rate),('stale_data_rate',policy.maximum_stale_data_rate),
                       ('evaluation_error_rate',policy.maximum_evaluation_error_rate),('duplicate_conflict_rate',policy.maximum_duplicate_conflict_rate)):
        check(name,r[name]<=limit,f'observed {r[name]}; maximum {limit}')
    reasons=window['exclusion_reasons']
    structural={'SOURCE_IDENTITY_MISMATCH','SOURCE_CONFIGURATION_UNVERIFIED','CONFIGURATION_OR_SCOPE_CHANGED_WITHIN_WINDOW','INVALID_OPPORTUNITY_TIMESTAMP','TIMEFRAME_MISMATCH','CANDLE_IDENTITY_UNVERIFIED','TENANT_MISMATCH',
                'CONFLICTING_DECISION_OPPORTUNITY','ACTIVE_EVIDENCE_CATEGORY_MISMATCH','SHADOW_EVIDENCE_CATEGORY_MISMATCH'}
    check('evidence_integrity',not any(k in reasons for k in structural) and not c['conflicting_opportunities'],
          'No unresolved conflicting identity, category, scope or opportunity timestamp is permitted')
    for name,required,needles in (('configuration_identity',policy.require_configuration_identity,('CONFIGURATION_UNVERIFIED',)),
        ('candle_context',policy.require_candle_context,('HTF_UNVERIFIED','CONTEXT_IDENTITY_UNVERIFIED','CANDLE_IDENTITY_UNVERIFIED','FINALITY_UNVERIFIED')),
        ('lookback_verification',policy.require_lookback_verification,('LOOKBACK_UNVERIFIED',)),
        ('tenant_authorization',policy.require_tenant_authorization,('AUTHORIZATION_UNVERIFIED','TENANT_MISMATCH'))):
        condition=True if not required else None if not c['recorded_decisions'] else not any(needle in k for k in reasons for needle in needles)
        check(name,condition,'Required original identity/context/authorization; current settings cannot repair missing history')
    check('worker_downtime',None if r['worker_downtime_seconds'] is None else r['worker_downtime_seconds']<=policy.maximum_unexplained_worker_downtime_seconds,
          'Continuous worker timing is unavailable; decision coverage is not uptime','OPERATIONAL')
    integrity=(holdout or {}).get('status','UNKNOWN')
    check('holdout_provenance',True if not policy.require_holdout_provenance else True if integrity=='VERIFIED_INDEPENDENT' else False if integrity in {'REUSED','CONTAMINATED'} else None,
          'Original committed holdout lineage required; observed '+integrity,'PAPER_REVIEW')
    p=assess_paper_evidence(paper)
    check('forward_paper_evidence',p['qualification_eligible'], 'Read-only authoritative episode lineage and journal reconciliation required','OUTCOMES')
    check('outcome_cost_coverage',True if not policy.require_outcome_cost_coverage else None if not p['completed_episode_count'] else all(v=='COMPLETE' for v in p['cost_coverage'].values()),
          'Fees, funding, slippage and spread require original observations or explicit proven exemption','OUTCOMES')
    check('minimum_paper_episodes',p['completed_episode_count']>=policy.minimum_paper_episodes,
          f"requires {policy.minimum_paper_episodes}; observed {p['completed_episode_count']}",'OUTCOMES')
    if matched is not None:
        check('matched_comparison',matched['eligibility']=='COMPARABLE' and matched['metrics']['matched_opportunities']>=policy.minimum_matched_opportunities,
              'Exact committed active/shadow observations and sufficient matched sample required','MATCHED')
    matched_status='UNKNOWN' if matched is None else 'BEHAVIOUR_COMPARABLE' if matched['eligibility']=='COMPARABLE' and matched['metrics']['matched_opportunities']>=policy.minimum_matched_opportunities else 'PARTIALLY_COMPARABLE' if matched['metrics']['matched_opportunities'] else 'NOT_COMPARABLE'
    behaviour=[x for x in checks if x['purpose']=='BEHAVIOUR']
    failed={x['name'] for x in behaviour if x['status']=='FAIL'}
    outcome='BEHAVIOUR_ONLY'
    if not c['recorded_decisions']: outcome='UNKNOWN'
    elif failed-{'minimum_observation_duration','minimum_opportunities'}: outcome='FORWARD_EVIDENCE_UNRELIABLE'
    elif failed: outcome='FORWARD_EVIDENCE_INSUFFICIENT'
    elif any(x['status']=='UNKNOWN' for x in behaviour): outcome='UNKNOWN'
    elif matched is not None and matched['eligibility']=='NOT_COMPARABLE': outcome='NOT_COMPARABLE'
    elif all(x['status']=='PASS' for x in checks): outcome='FORWARD_EVIDENCE_SUFFICIENT'
    warnings=['SIGNAL_BEHAVIOUR_IS_NOT_PROFITABILITY','TEMPORAL_DEPENDENCE_NOT_INDEPENDENT_SAMPLES','WORKER_UPTIME_UNKNOWN']
    if window['behaviour']['signal_count']<policy.minimum_signal_sample: warnings.append('LOW_SIGNAL_SAMPLE_SIZE')
    if integrity!='VERIFIED_INDEPENDENT': warnings.append('HOLDOUT_PROVENANCE_'+integrity)
    if not p['qualification_eligible']: warnings.append('PAPER_OUTCOMES_INCOMPLETE')
    if matched is not None and matched['metrics'].get('signal_relevant_denominator',0)<policy.minimum_signal_sample:
        warnings.append('LOW_MATCHED_SIGNAL_SAMPLE_SIZE')
    return {'calculation_version':VERSION,'policy':{**policy.as_dict(),'fingerprint':policy.fingerprint},
        'outcome':outcome,'checks':checks,'passed_gates':[x['name'] for x in checks if x['status']=='PASS'],
        'failed_gates':[x['name'] for x in checks if x['status']=='FAIL'],'unknown_gates':[x['name'] for x in checks if x['status']=='UNKNOWN'],
        'warning_codes':sorted(warnings),'blocking_reasons':[x['reason'] for x in checks if x['status']!='PASS'],
        'coverage':c,'reliability':r,'behaviour':window['behaviour'],'window_exclusion_reasons':window['exclusion_reasons'],'paper_evidence':p,'holdout_provenance':holdout or {'status':'UNKNOWN','references':[]},
        'matched_comparison':matched,'matched_qualification':matched_status,
        'evidence_completeness':window['evidence_completeness'],
        'profitability':'NOT_ASSESSED','profitability_verified':False,'execution_authorization':False,
        'paper_trial_readiness':'READY_FOR_HUMAN_REVIEW' if all(x['status']=='PASS' for x in checks if x['purpose'] in {'BEHAVIOUR','PAPER_REVIEW','OPERATIONAL','MATCHED'}) else 'BLOCKED',
        'explanation':f'{outcome}: readiness applies only to the recorded evidence and policy. No trading activation or profitability claim.'}


class ForwardQualificationService:
    def __init__(self, router, store, *, policies=None, paper_reader=None, holdout_reader=None,operational_reader=None):
        self.router=router;self.store=store
        self.policies={p.policy_version:p for p in (DEFAULT_POLICIES if policies is None else policies)}
        self.paper_reader=paper_reader;self.holdout_reader=holdout_reader
        self.operational_reader=operational_reader
        for p in self.policies.values(): store.install_policy(p)

    def create(self, request, *, owner_id):
        return self._assess(request,owner_id=owner_id,commit=True)

    def preview(self, request, *, owner_id):
        return self._assess(request,owner_id=owner_id,commit=False)

    def _assess(self,request,*,owner_id,commit):
        if not _ASSESSMENT_SLOT.acquire(blocking=False): raise ValueError('FORWARD_ASSESSMENT_BUSY')
        try: return self._calculate(request,owner_id=owner_id,commit=commit)
        finally: _ASSESSMENT_SLOT.release()

    def _calculate(self, request, *, owner_id, commit):
        allowed={'source_kind','source_id','start','end','policy_version','idempotency_key','active_source_id','paper_instance_id','paper_session_id','supersedes_report_id'}
        required={'source_kind','source_id','start','end','policy_version','idempotency_key'}
        if set(request)-allowed or not required<=set(request) or not owner_id or not 8<=len(str(request.get('idempotency_key') or ''))<=160:
            raise ValueError('INVALID_FORWARD_REQUEST')
        start,end=utc(request['start']),utc(request['end'])
        frozen={k:v for k,v in request.items() if k!='idempotency_key' and v is not None}
        frozen.update(start=start.isoformat(),end=end.isoformat(),calculation_version=VERSION)
        old=self.store.by_key(request['idempotency_key'],frozen,owner_id=owner_id) if commit else None
        if old: return old
        if end>self.router.clock() or end<=start: raise ValueError('INVALID_OBSERVATION_WINDOW')
        policy=self.policies.get(request['policy_version'])
        if policy is None: raise ValueError('UNKNOWN_FORWARD_POLICY')
        kind=request['source_kind'];sid=request['source_id'];events=[];matched=None;source_errors=[]
        if kind=='FORWARD_SHADOW_DECISION':
            source=self.router.store.get(sid,owner_id=owner_id)
            if source is None: raise KeyError(sid)
            rows,events=self.router.store.comparison_inputs(sid,owner_id=owner_id,start=start,end=end)
            identity={k:source.get(k) for k in ('strategy_id','strategy_version','config_fingerprint','venue','market_type','symbol','timeframe')}
            if digest(source.get('frozen_config'))!=source.get('config_fingerprint'):
                source_errors.append('SOURCE_CONFIGURATION_UNVERIFIED')
        elif kind=='ACTIVE_DECISION_OBSERVATION':
            if request.get('active_source_id'): raise ValueError('INVALID_FORWARD_REQUEST')
            sources=getattr(self.router,'active_sources',None)
            if sources is None: raise ValueError('ACTIVE_EVIDENCE_UNAVAILABLE')
            source,rows=sources.read(sid,owner_id=owner_id,start=start,end=end,limit=2016)
            identities={tuple(r.get(k) for k in ('strategy_id','strategy_version','config_fingerprint','venue','market_type','symbol','timeframe')) for r in rows}
            identity=dict(zip(('strategy_id','strategy_version','config_fingerprint','venue','market_type','symbol','timeframe'),next(iter(identities)))) if len(identities)==1 else {k:None for k in ('strategy_id','strategy_version','config_fingerprint','venue','market_type','symbol')}
            identity.setdefault('timeframe',source.get('timeframe'))
        else: raise ValueError('INVALID_FORWARD_EVIDENCE_CATEGORY')
        facts=[decision_facts(r) for r in rows]
        w=calculate_observation_window(facts,owner_id=owner_id,evidence_kind=kind,timeframe=identity['timeframe'],start=start,end=end,events=events,expected_identity=identity,source_errors=source_errors)
        active=[];active_source=None;configurations={}
        if kind=='FORWARD_SHADOW_DECISION' and identity.get('config_fingerprint'):
            configurations[identity['config_fingerprint']]=deepcopy(source.get('frozen_config'))
        else:
            configurations.update({r['config_fingerprint']:deepcopy(r['configuration_snapshot']) for r in rows if r.get('configuration_snapshot') and r.get('config_fingerprint')})
        if request.get('active_source_id'):
            active_source,active=self.router.active_sources.read(request['active_source_id'],owner_id=owner_id,start=start,end=end,limit=2016)
            configurations.update({r['config_fingerprint']:deepcopy(r['configuration_snapshot']) for r in active if r.get('configuration_snapshot') and r.get('config_fingerprint')})
            active=[decision_facts(r) for r in active]
            matched=compare_decisions(active,facts,owner_id=owner_id,start=start,end=end,timeframe=identity['timeframe'])
            if active_source.get('coverage_class')!='FULL_DECISION_EVIDENCE' and matched['eligibility']=='COMPARABLE':
                matched['eligibility']='PARTIALLY_COMPARABLE'
            if w['coverage']['conflicting_opportunities'] or any(k in w['exclusion_reasons'] for k in ('SOURCE_IDENTITY_MISMATCH','SOURCE_CONFIGURATION_UNVERIFIED')):
                matched.update(eligibility='NOT_COMPARABLE',metrics={},breakdowns={})
                # Preserve explicit sample fields without accepting conflicting pairs.
                matched['metrics']={'matched_opportunities':0,'signal_relevant_denominator':0,'all_eligible_agreement':None,'signal_relevant_agreement':None}
        paper=self.paper_reader(request,identity,owner_id,start,end) if request.get('paper_instance_id') and self.paper_reader else None
        holdout=self.holdout_reader(source,identity,owner_id) if self.holdout_reader else None
        operational=self.operational_reader(sid,owner_id,start,end,identity['timeframe']) if self.operational_reader else None
        inputs={'decisions':facts,'active':active,'audit_events':events,'paper':paper,'holdout':holdout,
                'source_identity':identity,'active_source':active_source,'source_errors':source_errors,
                'configuration_snapshots':configurations,'operational':operational}
        evidence_hash=digest(inputs)
        scope_key=digest({'owner_id':owner_id,'identity':identity,'evidence_kind':kind})
        revision=software_revision()
        wid='forward-window:'+digest({'owner_id':owner_id,'source_id':sid,'start':w['start'],'end':w['end'],'evidence_hash':evidence_hash,'software_revision':revision})
        w.update(window_id=wid,owner_id=owner_id,source_id=sid,scope_key=scope_key,evidence_hash=evidence_hash,inputs=inputs,identity=identity,
                 software_revision=revision,calculation_version=VERSION,
                 source_snapshot_semantics='SEPARATE_BOUNDED_READ_SNAPSHOTS; NOT CROSS_STORE_ATOMIC')
        report=evaluate_forward_window(w,policy=policy,paper=paper,holdout=holdout,matched=matched)
        report['operational_evidence']=operational
        if operational:
            # Preserve original v1 downtime UNKNOWN: sampled probes do not prove
            # continuous uptime. Reassessment retains actual narrower metrics.
            report['reliability']={**report['reliability'],'observed_worker_probe_availability':operational.get('worker_availability'),
                'observed_feed_probe_availability':operational.get('feed_availability'),
                'observed_evidence_commit_success_rate':operational.get('commit_success_rate')}
        report.update(owner_id=owner_id,tenant_id=owner_id,source_id=sid,source_kind=kind,identity=identity,
                      window_id=wid,start=w['start'],end=w['end'],evidence_hash=evidence_hash,
                      software_revision=revision,supersedes_report_id=request.get('supersedes_report_id'),created_at=self.router.clock().isoformat(),
                      evidence_references=[r.get('decision_id') for r in facts if r.get('decision_id')],
                      workflow_state='COMPLETED' if commit else 'DRAFT',commitment_status='COMMITTED' if commit else 'NOT_COMMITTED')
        report['report_id']='forward-report:'+digest({'window_id':wid,'policy':report['policy'],'calculation_version':VERSION,'supersedes':report['supersedes_report_id']})
        return self.store.commit(w,report,request=frozen,key=request['idempotency_key']) if commit else report

    def get(self, report_id, *, owner_id):
        report=self.store.report(report_id,owner_id=owner_id)
        if report is None: return None
        reviews=self.store.reviews(report_id,owner_id=owner_id)
        return {'report':report,'reviews':reviews,'workflow_state':'ARCHIVED' if reviews and reviews[-1]['decision']=='ARCHIVED' else 'REVIEWED' if reviews else 'COMPLETED'}

    def review(self, report_id, body, *, owner_id):
        return self.store.review(report_id,body,owner_id=owner_id,now=self.router.clock().isoformat())

    def history(self, *, owner_id, source_id=None, limit=50, offset=0):
        rows=self.store.history(owner_id=owner_id,source_id=source_id,limit=limit,offset=offset)
        overlap=any(r.get('overlapping_window_ids') for r in rows)
        reused=len({r['window_id'] for r in rows})<len(rows)
        trends=defaultdict(list)
        for r in sorted(rows,key=lambda r:(r['start'],r['report_id'])):
            cohort=digest({'identity':r['identity'],'source_kind':r['source_kind'],
                          'source_if_unknown':r['source_id'] if not r['identity'].get('config_fingerprint') else None})
            trends[cohort].append({k:r.get(k) for k in ('report_id','window_id','identity','policy','start','end','coverage','reliability','behaviour','outcome')})
        summaries=[{k:r.get(k) for k in ('report_id','window_id','source_id','source_kind','identity','start','end','policy','outcome','coverage','reliability','behaviour','warning_codes','created_at','overlapping_window_ids')} for r in rows]
        # No aggregate count or statistical independence is inferred from a
        # bounded history page. Different fingerprints remain separate rows.
        return {'rows':summaries,'limit':limit,'offset':offset,'independent_samples_claimed':False,
                'warning_codes':(['OVERLAPPING_OBSERVATION_WINDOWS'] if overlap else [])+(['REASSESSED_OBSERVATION_WINDOW'] if reused else []),
                'longitudinal':dict(trends),
                'aggregation_basis':'INDIVIDUAL_IMMUTABLE_WINDOWS; NO CROSS_COHORT_OR_OVERLAP_SUM'}
