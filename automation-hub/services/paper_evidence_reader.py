"""Read-only adapters for original financial sources, no accounting inference.

Native fill tables are an accounting authority, not automatically a complete
episode/cost/journal authority. Missing links are exposed, never manufactured.
"""
from copy import deepcopy
from datetime import datetime,timezone
import json
import sqlite3

from services.active_decision_evidence import ActiveDecisionSources
from services.strategy_identity import configuration_fingerprint as digest


class PaperEvidenceReader:
    def __init__(self,active_sources, *, instance_snapshot=None,operational_reader=None):
        self.active_sources=active_sources;self.instance_snapshot=instance_snapshot;self.operational_reader=operational_reader

    def sources(self, *, owner_id,limit=50):
        if not 1<=limit<=100:raise ValueError('PAPER_SOURCE_QUERY_LIMIT')
        return self.active_sources.sources(owner_id=owner_id,limit=limit)

    def _source(self,source_id,owner_id):
        candidates=self.sources(owner_id=owner_id,limit=100)
        source=next((r for r in candidates if r['source_id']==source_id),None)
        if source is None:raise KeyError(source_id)
        return source

    def availability(self,source_id, *, owner_id):
        source=self._source(source_id,owner_id)
        snapshot=self._snapshot(source,owner_id)
        def field(status,reason):return {'status':status,'reason':reason}
        account=snapshot.get('account') or {};fills=snapshot.get('fills') or [];orders=snapshot.get('orders') or []
        result={'source':source,'source_snapshot_hash':digest(snapshot),'fields':{
            'account_identity':field('VERIFIED' if account.get('account_id') else 'UNKNOWN','Original persisted account ID; no newly created account'),
            'strategy_version':field('PARTIAL' if any(r.get('strategy_version') for r in orders) else 'UNKNOWN','Per-order original version; no current-settings backfill'),
            'configuration_fingerprint':field('UNKNOWN','Financial fill-to-frozen-configuration linkage must be proved by original contemporaneous records'),
            'session_identity':field('PARTIAL','Original session/order metadata; protection/exit membership may be incomplete'),
            'order_intent':field('PARTIAL' if snapshot.get('order_metadata') else 'UNKNOWN','Original proposal metadata, not an invented acknowledgement'),
            'order_record':field('VERIFIED' if orders else 'UNKNOWN','Original persisted paper order rows'),
            'fill_quantity_price_fee':field('VERIFIED' if fills and all(r.get('quantity') is not None and r.get('price') is not None and r.get('fee') is not None for r in fills) else 'UNKNOWN','Original fill facts; not a completeness claim'),
            'funding':field('PARTIAL' if snapshot.get('funding') else 'UNKNOWN','Original funding events; fill funding=0 is a producer default, not zero-cost proof'),
            'slippage_spread':field('PARTIAL' if fills else 'UNKNOWN','Original row amounts require original reference-price/model evidence; embedded costs cannot be deducted twice'),
            'position_episode':field('UNKNOWN','No episode IDs may be invented from trade direction or current positions'),
            'financial_journal':field('PARTIAL' if snapshot.get('activity') else 'UNKNOWN','Audit events alone are not complete cash-journal evidence'),
            'account_balance':field('VERIFIED' if account.get('balance') is not None else 'UNKNOWN','Current original balance is not an equity history'),
            'equity_series':field('UNKNOWN','A current peak/max-drawdown scalar is not a period-matched authoritative equity series')},
            'comparison_scope':'ORIGINAL_FINANCIAL_RECORD_AVAILABILITY; NOT VERIFIED_PROFITABILITY','execution_authorization':False}
        if snapshot.get('source_events'):
            from services.native_paper_snapshot import capture_health
            result['native_capture']=capture_health(snapshot,owner_id)
            for field,key in (('configuration_fingerprint','configuration'),('position_episode','episodes'),('financial_journal','cash_journal'),('equity_series','equity'),('funding','funding')):
                result['fields'][field]={'status':result['native_capture'][key],
                    'reason':'Original transaction evidence; historical gaps and missing schedule/valuation remain explicit'}
        return result

    def _snapshot(self,source,owner_id):
        if source['kind']=='instance':
            if not self.instance_snapshot:return {'limitations':['AUTHORITATIVE_INSTANCE_SNAPSHOT_UNAVAILABLE']}
            original=self.instance_snapshot(source,owner_id)
            if original.get('schema_version')=='paper_financial_snapshot.v1':return original
            # The existing ledger outbox commits contemporaneous receipts and
            # configuration together. Expose those originals without treating
            # them as a complete paper-account equity or funding journal.
            fills=[]
            for event in original.get('outbox',[]):
                receipt=event.get('receipt') or {};context=event.get('context') or {}
                identity=context.get('journal_execution') or {};config=context.get('strategy_identity') or {}
                fills.append({'id':event.get('execution_id'),'order_id':context.get('alert_id'),
                    'account_id':identity.get('account_id'),'owner_id':identity.get('owner_id'),
                    'session_id':event.get('simulation_session_id'),'strategy':identity.get('strategy_id'),
                    'strategy_version':identity.get('strategy_version'),'config_fingerprint':identity.get('strategy_config_hash'),
                    'configuration_snapshot':config.get('effective_config'),'episode_id':context.get('episode_id'),
                    'symbol':receipt.get('symbol'),'side':receipt.get('side'),'quantity':receipt.get('size'),
                    'price':receipt.get('entry') if event.get('action')=='OPEN' else receipt.get('exit_price'),
                    'fee':receipt.get('booked_fees'),'realized_pnl':receipt.get('gross_pnl'),
                    'timestamp':event.get('observed_at') or event.get('created_at'),'risk_amount':receipt.get('initial_risk_amount'),
                    'execution_engine':'PAPER' if identity.get('execution_mode')=='paper' else None,
                    'source_reference':'paper_evidence_outbox:'+str(event.get('execution_id'))})
            return {**original,'fills':fills,'original_ledger_snapshot':original,'account':{},'orders':[],
                'limitations':['ORIGINAL_LEDGER_RECEIPTS_PRESENT; ACCOUNT_EQUITY_AND_FUNDING_AUTHORITY_UNAVAILABLE']}
        kind=source['kind'];path=self.active_sources.paths.get(kind)
        if not path:return {'limitations':['FINANCIAL_SOURCE_UNAVAILABLE']}
        prefix='pa' if kind=='pa-lab' else 'smc'
        try:
            with ActiveDecisionSources.connection(path) as db:
                names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                account=dict(db.execute('SELECT * FROM v2_account WHERE id=1').fetchone()) if 'v2_account' in names else {}
                result={'account':account,'fills':[],'orders':[],'funding':[],'order_metadata':[],'activity':[],'configuration_receipts':[],
                    'snapshot_semantics':'CONSISTENT_READ_ONLY_NATIVE_FINANCIAL_SNAPSHOT','source_kind':'FORWARD_PAPER_TRADE'}
                if 'v2_source_evidence' in names:
                    rows=db.execute('SELECT evidence_hash,payload_json FROM v2_source_evidence ORDER BY sequence LIMIT 30001').fetchall()
                    if len(rows)>30000:raise ValueError('PAPER_SOURCE_QUERY_LIMIT')
                    events=[json.loads(r['payload_json']) for r in rows]
                    if any(digest(event)!=r['evidence_hash'] for event,r in zip(events,rows)):raise ValueError('PAPER_SOURCE_EVIDENCE_CORRUPT')
                    result['source_events']=events
                for key,table in (('fills','v2_fills'),('orders','v2_orders'),('funding','v2_funding_events')):
                    if table in names:
                        rows=db.execute('SELECT * FROM '+table+' ORDER BY rowid LIMIT 10001').fetchall()
                        if len(rows)>10000:raise ValueError('PAPER_SOURCE_QUERY_LIMIT')
                        result[key]=[dict(r) for r in rows]
                for key,table in (('order_metadata',prefix+'_order_meta'),('activity',prefix+'_activity')):
                    if table in names:
                        rows=db.execute('SELECT * FROM '+table+' WHERE session_id=? ORDER BY rowid LIMIT 10001',(source['session_id'],)).fetchall()
                        if len(rows)>10000:raise ValueError('PAPER_SOURCE_QUERY_LIMIT')
                        result[key]=[dict(r) for r in rows]
                if 'active_decision_receipts' in names:
                    rows=db.execute('SELECT * FROM active_decision_receipts WHERE tenant_id=? AND source_kind=? AND session_id=? ORDER BY decision_id LIMIT 10001',
                        (owner_id,prefix,source['session_id'])).fetchall()
                    if len(rows)>10000:raise ValueError('PAPER_SOURCE_QUERY_LIMIT')
                    result['configuration_receipts']=[dict(r) for r in rows]
                return result
        except sqlite3.Error:return {'limitations':['FINANCIAL_SOURCE_UNAVAILABLE_OR_SCHEMA_UNSUPPORTED']}

    def read(self,source_id, *, owner_id,start,end):
        source=self._source(source_id,owner_id);original=self._snapshot(source,owner_id)
        if original.get('source_events'):
            from services.native_paper_snapshot import normalize_native
            return normalize_native(original,source=source,owner_id=owner_id,start=start,end=end,
                operational_evidence=self.operational_reader(source_id,owner_id,start,end) if self.operational_reader else None)
        # Only an original normalized producer snapshot can pass the full
        # contract. Raw sources remain partial until their missing links exist.
        if original.get('schema_version')=='paper_financial_snapshot.v1':return deepcopy(original)
        account=deepcopy(original.get('account') or {})
        scope={'owner_id':owner_id,'account_id':account.get('account_id'),'simulation_session_id':source.get('session_id'),
            'execution_mode':'paper','symbol':source.get('symbol'),'strategy_id':None,'strategy_version':None,'config_fingerprint':None}
        receipts={r['decision_id']:r for r in original.get('configuration_receipts',[])}
        metadata={r['order_id']:r for r in original.get('order_metadata',[])}
        orders=[];identity_by_order={};configuration_proofs={};prefix='pa' if source['kind']=='pa-lab' else 'smc'
        for row in original.get('orders',[]):
            meta=metadata.get(row.get('id'),{});proven={}
            try:
                config=json.loads(meta['config_json']);receipt=receipts[config['correlation_id']]
                frozen=json.loads(receipt['snapshot_json'])['active_evidence'];snapshot=frozen['configuration_snapshot']
                # An exact original decision-to-order join is necessary but not
                # sufficient: execution and evaluator configuration must agree.
                if frozen.get('configuration_status')=='VERIFIED' and digest(snapshot)==frozen['config_fingerprint'] and frozen.get('strategy_id')==row.get('strategy') and frozen.get('strategy_version')==row.get('strategy_version') and config.get('execution')==snapshot.get('execution_config') and config.get('strategy_configuration')==snapshot.get('native_config'):
                    proven={'config_fingerprint':frozen['config_fingerprint'],'configuration_reference':frozen['configuration_reference'],
                        'strategy_id':frozen['strategy_id'],'strategy_version':frozen['strategy_version'],'owner_id':frozen['tenant_id'],
                        'simulation_session_id':receipt['session_id']}
                    configuration_proofs[frozen['config_fingerprint']]={'snapshot':snapshot,'source_reference':'active_decision_receipts:'+receipt['decision_id'],'status':'VERIFIED'}
            except (KeyError,ValueError,TypeError):pass
            identity_by_order[row.get('id')]=proven
            orders.append({'order_id':row.get('id'),'account_id':row.get('account_id'),'owner_id':None,
                'strategy_id':row.get('strategy'),'strategy_version':row.get('strategy_version'),'config_fingerprint':None,
                'simulation_session_id':meta.get('session_id'),'execution_mode':'paper' if row.get('execution_engine')=='PAPER' else None,
                'symbol':row.get('symbol'),'filled_quantity':row.get('filled'),
                'intent_reference':prefix+':proposal:'+meta['proposal_id'] if meta.get('proposal_id') else None,
                'source_reference':'v2_orders:'+str(row.get('id')),**proven})
        fills=[]
        for row in original.get('fills') or []:
            # Do not assign unproven current session/configuration/episode to a
            # fill merely because the user selected this account/session.
            fills.append({'fill_id':row.get('id'),'order_id':row.get('order_id'),'account_id':row.get('account_id'),
                'strategy_id':row.get('strategy'),'strategy_version':row.get('strategy_version'),
                'config_fingerprint':row.get('config_fingerprint'),'simulation_session_id':row.get('session_id'),
                'owner_id':row.get('owner_id'),'execution_mode':'paper' if row.get('execution_engine')=='PAPER' else None,
                'symbol':row.get('symbol'),'episode_id':row.get('episode_id'),'effect':None,
                'side':str(row.get('side') or '').upper(),'quantity':row.get('quantity'),'price':row.get('price'),
                'fee':row.get('fee'),'gross_pnl':row.get('realized_pnl'),'timestamp':row.get('fill_timestamp') or row.get('timestamp'),
                'initial_risk':row.get('risk_amount'),'source_reference':row.get('source_reference') or 'v2_fills:'+str(row.get('id')),
                'reference_price':row.get('requested_price'),'spread':row.get('spread'),'slippage':row.get('slippage'),'cost_basis':'UNKNOWN',
                **identity_by_order.get(row.get('order_id'),{})})
        identities={(r.get('strategy_id'),r.get('strategy_version'),r.get('config_fingerprint')) for r in fills}
        if len(identities)==1:
            strategy,version,fingerprint=next(iter(identities))
            scope.update(strategy_id=strategy,strategy_version=version,config_fingerprint=fingerprint)
        funding=[{'funding_id':r.get('funding_key'),'account_id':r.get('account_id'),'position_id':r.get('position_id'),
            'symbol':r.get('symbol'),'timestamp':r.get('funding_timestamp'),'amount':r.get('amount'),
            'rate':r.get('rate'),'mark_price':r.get('mark_price'),'quantity':None,'episode_id':None,
            'source_reference':'v2_funding_events:'+str(r.get('funding_key'))} for r in original.get('funding',[])]
        return {'schema_version':'paper_financial_snapshot.v1','authority_status':'COMPLETE' if original.get('account') else 'UNKNOWN',
            'source_reference':'financial-snapshot:'+digest(original),'scope':scope,'account':account,
            'configuration_evidence':configuration_proofs.get(scope.get('config_fingerprint')),
            'records_complete':False,'journal_complete':False,'orders':orders,'fills':fills,'episodes':[],'funding':funding,
            'journal':[],'equity_series':None,'original_snapshot':original,
            'operational_evidence':self.operational_reader(source_id,owner_id,start,end) if self.operational_reader else None,
            'limitations':['UNPROVEN_FINANCIAL_CONFIGURATION_EPISODE_SESSION_LINKAGE',
                'FUNDING_EXPOSURE_SCHEDULE_AND_CASH_JOURNAL_COMPLETENESS_UNVERIFIED',
                'ORIGINAL_REFERENCE_PRICE_COST_MODEL_UNVERIFIED']}
