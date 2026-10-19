"""Bounded read-only references to existing paper episode evidence.

This projection cannot prove external ledger reconciliation. It never invokes
accounting/recovery or interprets a paper decision as an executed fill.
"""
import json
import logging
import sqlite3

from services.active_decision_evidence import ActiveDecisionSources
from services.shadow_router import utc
from services.strategy_evidence import build_episode

log=logging.getLogger(__name__)


def read_paper_references(path, *, scope, identity, start, end):
    result={'evidence_kind':'FORWARD_PAPER_TRADE','account_id':scope['account_id'],
            'authority_status':'UNKNOWN','journal_status':'UNKNOWN','rows':[],
            'source_identity':identity,'source_scope':scope,
            'source_snapshot_semantics':'BOUNDED_READ_ONLY_JOURNAL; EXTERNAL_LEDGER_NOT_ASSESSED',
            'limitations':['EXTERNAL_LEDGER_AND_EXECUTION_COST_ASSUMPTIONS_UNVERIFIED']}
    fields={**scope,'strategy_id':identity.get('strategy_id'),'strategy_version':identity.get('strategy_version'),
            'strategy_config_hash':identity.get('config_fingerprint'),'symbol':identity.get('symbol'),
            'execution_mode':'paper','source_kind':'forward_paper','lab_id':None}
    if any(fields.get(k) is None for k in ('owner_id','account_id','instance_id','simulation_session_id','strategy_id','strategy_version','strategy_config_hash','symbol')):
        result['limitations'].append('PAPER_COHORT_IDENTITY_UNVERIFIED');return result
    try:
        with ActiveDecisionSources.connection(path) as db:
            where=' AND '.join(k+' IS ?' for k in fields)
            headers=db.execute('SELECT * FROM strategy_position_episodes WHERE '+where+' ORDER BY rowid LIMIT 201',tuple(fields.values())).fetchall()
            if len(headers)>200: raise ValueError('PAPER_EVIDENCE_QUERY_LIMIT')
            ids=[r['episode_id'] for r in headers]
            events=[]
            if ids:
                raw=db.execute('SELECT * FROM strategy_evidence_events WHERE owner_id=? AND kind=\'execution_fill\' AND episode_id IN ('+','.join('?' for _ in ids)+') ORDER BY rowid LIMIT 2001',(scope['owner_id'],*ids)).fetchall()
                if len(raw)>2000: raise ValueError('PAPER_EVIDENCE_QUERY_LIMIT')
                for original in raw:
                    event=dict(original);event['payload']=json.loads(event.pop('payload_json'));event.pop('envelope_json',None);events.append(event)
            for header in headers:
                episode=build_episode(dict(header),events=[e for e in events if e['episode_id']==header['episode_id']])
                if episode['status']=='closed' and episode.get('closed_at') and utc(start)<=utc(episode['closed_at'])<utc(end):
                    # Keep original amounts, coverage and lifecycle. No fill IDs,
                    # quantities, funding or execution assumptions are invented.
                    result['rows'].append(episode)
    except (sqlite3.Error,ValueError,KeyError,TypeError,ArithmeticError) as exc:
        log.warning('forward_paper_reference_unavailable',extra={'error_category':type(exc).__name__})
        result['rows']=[];result['limitations'].append('PAPER_SOURCE_UNAVAILABLE_OR_BOUNDED')
    return result


def read_holdout_provenance(context, source, identity, *, owner_id):
    refs=source.get('validation_run_ids') or []
    result={'status':'UNKNOWN','references':[],'reason':'No original independently verified holdout lineage'}
    if not context or len(refs)>50: return result
    for rid in refs:
        run=context.get_validation_run(rid)
        if not run or run.get('owner_id')!=owner_id or run.get('status')!='COMPLETED': continue
        if any(run.get(k)!=identity.get(k) for k in ('strategy_id','strategy_version','config_fingerprint')): continue
        if run.get('method')!='out_of_sample': continue
        result['references'].append(rid)
        report=run.get('report') or {}
        if not isinstance(report,dict): continue
        access=run.get('holdout_access_count')
        integrity=report.get('holdout_independence','UNKNOWN')
        if integrity=='CONTAMINATED': result['status']='CONTAMINATED'
        elif result['status']!='CONTAMINATED' and ((type(access) is int and access>1) or integrity=='REUSED'): result['status']='REUSED'
        # A first access is not proof that earlier search history is complete.
        elif result['status']=='UNKNOWN' and integrity=='VERIFIED_INDEPENDENT': result['status']='VERIFIED_INDEPENDENT'
    return result


def operational_diagnostics(router):
    worker=getattr(router,'worker',None)
    health=worker.health() if worker else None
    if health is not None:
        health={k:health.get(k) for k in ('readiness','last_successful_sweep','sweep_age_seconds',
            'error_category','concurrency','database_backend','resource_boundary')}
    def gate(status,reason):return {'status':status,'reason':reason}
    pg_selected=getattr(router.store,'backend',None)=='postgresql'
    return {'schema_version':'forward-operational-readiness.v1','production_ready':False,'execution_authorization':False,
        'current_worker_health':health,'gates':{
            'worker_supervision':gate('PARTIAL','Standalone entrypoint exists; installed independent supervisor not attested by this API'),
            'postgresql_worker_concurrency':gate('PARTIAL' if pg_selected else 'BLOCKED',
                'PostgreSQL metadata backend selected; deployed concurrency/fencing certification is not attached' if pg_selected else 'Current worker uses SQLite; PostgreSQL local implementation/tests do not attest this runtime'),
            'resource_sandboxing':gate('PARTIAL','Application limits are cooperative; local container controls do not attest the in-app worker'),
            'migration_safety':gate('PARTIAL','Additive local schema; authorized production-shaped migration validation is outstanding'),
            'backup_restore':gate('UNKNOWN','No independently verified operational backup/restore record is attached'),
            'dataset_retention':gate('PARTIAL','Bounded immutable journal snapshots; exact retention and provider reconstruction outside stored revisions are not guaranteed'),
            'market_data_reliability':gate('UNKNOWN','Current decision evidence is not continuous provider uptime or live-feed soak certification'),
            'observability':gate('PARTIAL','Structured health and decision/audit evidence; continuous downtime and persistence failure counters unavailable'),
            'tenant_isolation':gate('PARTIAL','Owner-scoped service gates; deployed tenant/RLS configuration has not been independently validated'),
            'incident_recovery':gate('PARTIAL','Local lease/reconciliation tests do not certify production restart drills'),
            'production_credential_separation':gate('UNKNOWN','No production service or environment accessed; local credentials-free harness does not attest deployment')},
        'scope':'RUNTIME_CAPABILITIES_AND_KNOWN_GAPS; NOT A CLAIM THAT STAGING_TESTS_RAN'}
