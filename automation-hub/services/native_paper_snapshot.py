"""Read-only projection of original native transaction evidence, not accounting."""
from collections import defaultdict
from copy import deepcopy
from services.shadow_router import utc
from services.strategy_identity import configuration_fingerprint as digest


def capture_health(original, owner_id):
    events = original.get('source_events') or []
    kinds = defaultdict(list)
    for event in events:
        kinds[event['event_type']].append(event)
    fills = kinds['FILL_COMMITTED']; count = len(original.get('fills') or [])
    complete = len(fills) == count and bool(fills)
    configs = all(e['payload']['context'].get('configuration_status') == 'VERIFIED' for e in fills)
    opening = kinds['ACCOUNT_OPENED']
    return {'schema_version':'native-paper-health.v1', 'source_events':len(events),
        'original_fill_count':count, 'captured_fill_count':len(fills),
        'transaction_capture':'COMPLETE' if complete else 'PARTIAL' if fills else 'UNKNOWN',
        'configuration':'VERIFIED' if fills and configs else 'UNKNOWN',
        'ownership':'VERIFIED' if opening and opening[0].get('tenant_id') == owner_id else 'UNKNOWN',
        'episodes':'CAPTURED' if complete else 'UNKNOWN',
        'funding':'PARTIAL' if kinds['FUNDING_COMMITTED'] else 'UNKNOWN',
        'cash_journal':'COMPLETE' if complete and opening and not kinds['ACCOUNT_RESET'] and not kinds['ACCOUNT_RESTORED'] else 'PARTIAL',
        'equity':'PARTIAL' if kinds['EQUITY_OBSERVED'] else 'UNKNOWN',
        'execution_authorization':False}


def normalize_native(original, *, source, owner_id, start, end, operational_evidence=None):
    events = original.get('source_events') or []
    actual = {r['id']:r for r in original.get('fills') or []}
    scope = {'owner_id':owner_id,'account_id':(original.get('account') or {}).get('account_id'),
        'simulation_session_id':source.get('session_id'),'execution_mode':'paper','symbol':source.get('symbol'),
        'strategy_id':None,'strategy_version':None,'config_fingerprint':None}
    fills=[]; orders={}; episodes={}; funding=[]; journal=[]; configurations={}; mismatches=[]; schedules=[]; equity=[]
    opening=None; broken=False
    def cohort(event):
        ctx=event['payload'].get('context') or {}
        return {'owner_id':event.get('tenant_id'),'account_id':event['paper_account_id'],
            'strategy_id':ctx.get('strategy_id'),'strategy_version':ctx.get('strategy_version'),
            'config_fingerprint':event.get('effective_config_fingerprint'),
            'simulation_session_id':event.get('execution_session_id'),'execution_mode':ctx.get('execution_mode'),
            'symbol':event.get('symbol')}
    for event in events:
        if event.get('tenant_id') not in {None,owner_id}:raise ValueError('PAPER_SOURCE_TENANT_CONFLICT')
        kind=event['event_type'];p=event['payload'];ref='v2_source_evidence:'+event['event_id']
        if kind=='ACCOUNT_OPENED':opening=event
        if kind in {'ACCOUNT_RESET','ACCOUNT_RESTORED','CAPTURE_STARTED'}:broken=True
        if kind=='FUNDING_SCHEDULE_RECORDED':schedules.append(event)
        if kind=='EQUITY_OBSERVED':equity.append({'timestamp':event['event_timestamp_utc'],'equity':p.get('equity'),
            'source_reference':ref,'completeness':p['completeness']})
        if kind not in {'ORDER_ACCEPTED','FILL_COMMITTED','FUNDING_COMMITTED'}:continue
        identity=cohort(event);ctx=p.get('context') or {}
        if ctx.get('configuration_status')=='VERIFIED' and ctx.get('configuration_snapshot'):
            configurations[event['effective_config_fingerprint']]={'snapshot':ctx['configuration_snapshot'],'status':'VERIFIED','source_reference':ref}
        if kind=='ORDER_ACCEPTED':
            order=p['order'];orders[order['id']]={**identity,'order_id':order['id'],'filled_quantity':0,
                'intent_reference':p.get('intent_reference'),'source_reference':ref}
        elif kind=='FILL_COMMITTED':
            fill=p['fill'];original_fill=actual.get(fill['id'])
            if not original_fill or original_fill != fill:mismatches.append('ORIGINAL_FILL_CHANGED_OR_UNAVAILABLE:'+fill['id'])
            order=orders.setdefault(fill['order_id'],{**identity,'order_id':fill['order_id'],'filled_quantity':0,
                'intent_reference':ref+':original-internal-instruction' if p.get('order_origin')=='EXISTING_INTERNAL_PROTECTION' else None,'source_reference':ref})
            order['filled_quantity']+=fill['quantity']
            for index,leg in enumerate(p['allocations']):
                fid=fill['id'] if len(p['allocations'])==1 else fill['id']+':allocation:'+str(index)
                cost=p['cost_model']
                fills.append({**identity,'fill_id':fid,'physical_fill_id':fill['id'],'order_id':fill['order_id'],
                    'episode_id':leg['episode_id'],'effect':leg['effect'],'side':fill['side'].upper(),
                    'quantity':leg['quantity'],'price':fill['price'],'fee':leg['fee'],'gross_pnl':leg['gross_pnl'],
                    'timestamp':fill['timestamp'],'initial_risk':leg.get('initial_risk') if leg['effect']=='ENTRY' else None,
                    'source_reference':ref,'allocation_basis':'ORIGINAL_TRANSACTION_RECORDED_ALLOCATION',
                    'cost_basis':'ORIGINAL_EMBEDDED_PRICE','reference_price':cost['reference_price'],
                    'spread':cost['spread_price_component'],'slippage':cost['slippage_price_component']})
                episode=episodes.setdefault(leg['episode_id'],{'episode_id':leg['episode_id'],'direction':leg['direction'],
                    'status':'open','fill_ids':[],'opened_at':None,'closed_at':None,'funding_coverage':'UNKNOWN',
                    'expected_funding_ids':None,'funding_exposure_reference':None})
                episode['fill_ids'].append(fid)
                if leg['effect']=='ENTRY' and episode['opened_at'] is None:episode['opened_at']=fill['timestamp']
                if leg['episode_closed']:episode.update(status='closed',closed_at=fill['timestamp'])
                journal.append({'journal_id':event['event_id']+':allocation:'+str(index),'reference_id':fid,
                    'account_id':scope['account_id'],'cash_delta':leg['gross_pnl']-leg['fee'],
                    'source_reference':ref,'source_transaction_id':event['source_transaction_id']})
        else:
            fid=p['funding_key'];funding.append({**identity,'funding_id':fid,
                'episode_id':p['position']['position_id'],'quantity':p['position']['size'],'amount':p['amount'],
                'mark_price':p['mark_price'],'rate':p['rate'],'timestamp':p['funding_timestamp'],'source_reference':ref})
            journal.append({'journal_id':event['event_id'],'reference_id':fid,'account_id':scope['account_id'],
                'cash_delta':p['cash_delta'],'source_reference':ref,'source_transaction_id':event['source_transaction_id']})
    identities={(f['strategy_id'],f['strategy_version'],f['config_fingerprint']) for f in fills}
    if len(identities)==1:scope.update(zip(('strategy_id','strategy_version','config_fingerprint'),next(iter(identities))))
    for episode in episodes.values():
        if not episode['opened_at'] or not episode['closed_at']:continue
        lo,hi=utc(episode['opened_at']),utc(episode['closed_at'])
        matching=[e for e in schedules if e.get('symbol')==scope['symbol'] and utc(e['payload']['start'])<=lo and utc(e['payload']['end'])>=hi]
        if not matching:continue
        expected={utc(r['timestamp']) for e in matching for r in e['payload']['settlements'] if lo<=utc(r['timestamp'])<hi}
        linked=[f for f in funding if f['episode_id']==episode['episode_id']]
        actual_times={utc(f['timestamp']) for f in linked}
        if expected==actual_times:
            episode.update(funding_coverage='COMPLETE' if linked else 'ZERO_COST_VERIFIED',
                expected_funding_ids=[f['funding_id'] for f in linked],funding_exposure_reference=matching[0]['event_id'])
    account=deepcopy(original.get('account') or {})
    account.update(account_type='PAPER',owner_id=opening.get('tenant_id') if opening else None)
    health=capture_health(original,owner_id)
    captured_ids={f['physical_fill_id'] for f in fills}
    records_complete=not mismatches and captured_ids==set(actual) and health['cash_journal']=='COMPLETE'
    return {'schema_version':'paper_financial_snapshot.v1','authority_status':'COMPLETE' if original.get('account') else 'UNKNOWN',
        'source_reference':'original-native-snapshot:'+digest(original),'scope':scope,'account':account,
        'configuration_evidence':configurations.get(scope['config_fingerprint']),'records_complete':records_complete,
        'journal_complete':records_complete and not broken,'orders':list(orders.values()),'fills':fills,
        'episodes':list(episodes.values()),'funding':funding,'journal':journal,
        'equity_series':{'status':'PARTIAL','basis':'EQUITY_INCLUDING_UNREALIZED','points':equity,'source_reference':'v2_source_evidence:EQUITY_OBSERVED'} if equity else None,
        'operational_evidence':operational_evidence,'original_snapshot':original,'capture_health':health,
        'limitations':mismatches+(['PRE_CAPTURE_OR_RESET_HISTORY_UNKNOWN'] if broken else [])+
            ['EQUITY_OBSERVATION_SCHEDULE_NOT_CONTINUOUS','PAPER_OPERATIONAL_COVERAGE_REQUIRED']}
