"""Versioned Decimal reconciliation of original paper facts; no ledger writes.

The normalized input is produced by a trusted read-only source adapter, never
accepted as an HTTP body. Missing originals are unknown, not financial repairs.
"""
from collections import defaultdict
from dataclasses import dataclass,asdict
from decimal import Decimal,InvalidOperation,localcontext
import re

from services.shadow_router import utc
from services.strategy_identity import configuration_fingerprint as digest

VERSION='paper_reconciliation.v1'


class FinancialAmountUnavailable(ValueError):
    """An original field is absent, distinct from a contradictory amount."""


def amount(value):
    if value is None:raise FinancialAmountUnavailable('ORIGINAL_FINANCIAL_AMOUNT_UNAVAILABLE')
    if isinstance(value,bool):raise ValueError('INVALID_FINANCIAL_AMOUNT')
    try:result=Decimal(str(value))
    except (InvalidOperation,ValueError):raise ValueError('MISSING_OR_INVALID_FINANCIAL_AMOUNT') from None
    if not result.is_finite():raise ValueError('MISSING_OR_INVALID_FINANCIAL_AMOUNT')
    return result


def money(value):return format(value.normalize(),'f')


@dataclass(frozen=True)
class ReconciliationPolicy:
    policy_version:str='paper-reconciliation.v1'
    money_tolerance:str='0.00000001'
    quantity_tolerance:str='0.000000000001'
    minimum_sample_warning:int=30
    require_operational_evidence:bool=True
    def __post_init__(self):
        if not self.policy_version or not Decimal('0')<=amount(self.money_tolerance)<=Decimal('.000001') or not Decimal('0')<=amount(self.quantity_tolerance)<=Decimal('.00000001'):
            raise ValueError('INVALID_RECONCILIATION_POLICY')
        if type(self.require_operational_evidence) is not bool or type(self.minimum_sample_warning) is not int or self.minimum_sample_warning<1:raise ValueError('INVALID_RECONCILIATION_POLICY')
    @property
    def fingerprint(self):return digest(asdict(self))


def reconcile_paper(bundle, *, owner_id,start,end,policy=None):
    with localcontext() as context:
        context.prec=50
        return _reconcile(bundle,owner_id=owner_id,start=start,end=end,policy=policy or ReconciliationPolicy())


def _reconcile(bundle, *, owner_id,start,end,policy):
    start,end=utc(start),utc(end)
    if end<=start:raise ValueError('INVALID_RECONCILIATION_PERIOD')
    checks=[]
    def check(name,value,reason):checks.append({'name':name,'status':'UNKNOWN' if value is None else 'PASS' if value else 'FAIL','reason':reason})
    scope=bundle.get('scope') or {};account=bundle.get('account') or {}
    fills=bundle.get('fills') or [];orders=bundle.get('orders') or [];episodes=bundle.get('episodes') or []
    funding=bundle.get('funding') or [];journal=bundle.get('journal') or []
    if max(len(fills),len(orders),len(journal))>10000 or len(episodes)>1000:raise ValueError('PAPER_RECONCILIATION_LIMIT')
    check('authoritative_snapshot',True if bundle.get('schema_version')=='paper_financial_snapshot.v1' and bundle.get('authority_status')=='COMPLETE' and bundle.get('source_reference') else None,'Original consistent authoritative snapshot is required')
    check('account_ownership',None if not account.get('owner_id') or not account.get('account_id') else account.get('owner_id')==owner_id and scope.get('owner_id')==owner_id and account.get('account_id')==scope.get('account_id'), 'Exact original account and owner identity')
    check('paper_mode',None if not account.get('account_type') else account.get('account_type')=='PAPER' and scope.get('execution_mode')=='paper','Only isolated paper accounting; never live or shadow outcomes')
    fields=('owner_id','account_id','strategy_id','strategy_version','config_fingerprint','simulation_session_id','execution_mode','symbol')
    known=all(scope.get(k) for k in fields) and re.fullmatch('[0-9a-f]{64}',str(scope.get('config_fingerprint') or ''))
    configuration=bundle.get('configuration_evidence') or {};snapshot=configuration.get('snapshot')
    check('configuration_identity',None if not snapshot or not configuration.get('source_reference') or configuration.get('status')!='VERIFIED' else
        digest(snapshot)==scope.get('config_fingerprint') and snapshot.get('strategy_id')==scope.get('strategy_id') and snapshot.get('strategy_version')==scope.get('strategy_version'),
        'Original immutable effective configuration and source reference must reproduce the recorded fingerprint; a hash-shaped string alone is not verification')
    mismatched=any(scope.get(k) is not None and row.get(k) is not None and row.get(k)!=scope.get(k) for row in [*fills,*orders,*funding] for k in fields)
    missing_identity=any(row.get(k) is None for row in [*fills,*orders,*funding] for k in fields)
    check('exact_cohort',False if mismatched else None if not known or missing_identity else True,'All financial rows share the original frozen cohort; absent history remains unknown')
    check('records_complete',True if bundle.get('records_complete') is True else None,'Uncapped complete source cohort, not a truncated query')
    check('duplicate_fills',len({r.get('fill_id') for r in fills})==len(fills) and all(r.get('fill_id') for r in fills),'One original financial fill identity per receipt')
    for name,rows,key in (('duplicate_orders',orders,'order_id'),('duplicate_funding',funding,'funding_id'),('duplicate_journal',journal,'journal_id')):
        check(name,len({r.get(key) for r in rows})==len(rows) and all(r.get(key) for r in rows),'Every original source receipt identity is unique')
    order_map={r.get('order_id'):r for r in orders};fill_map={r.get('fill_id'):r for r in fills}
    check('order_intent_lineage',True if fills and all(order_map.get(r.get('order_id'),{}).get('intent_reference') and r.get('source_reference') for r in fills) else None,'Original order intent, record and fill references; no synthetic acknowledgement')
    check('episode_lineage',True if episodes and all(e.get('episode_id') and e.get('fill_ids') and e.get('opened_at') and e.get('closed_at') for e in episodes) else None,'Original complete position episodes; partial exits are constituents')
    assigned=[fid for episode in episodes for fid in episode.get('fill_ids',[])]
    check('episode_fill_coverage',None if not episodes else len(assigned)==len(set(assigned)) and set(assigned)==set(fill_map) and len({e.get('episode_id') for e in episodes})==len(episodes),'Every source fill belongs to exactly one original episode; duplicate legs cannot inflate results')
    check('journal_complete',True if bundle.get('journal_complete') is True else None,'Original journal must be complete for every financial receipt')
    op=bundle.get('operational_evidence') or {}
    check('operational_evidence',True if not policy.require_operational_evidence else True if op.get('monitoring_status')=='COMPLETE' and op.get('evidence_references') and all(op.get(k)==1 for k in ('worker_availability','feed_availability','commit_success_rate')) else None,'Original monitoring period, independent feed/worker/commit evidence; no pre-monitoring inference')
    op_scope=op.get('scope') or {}
    op_identity=('owner_id','account_id','strategy_id','strategy_version','config_fingerprint','simulation_session_id','execution_mode','symbol')
    op_matches=all(op_scope.get(k) is not None and op_scope.get(k)==scope.get(k) for k in op_identity)
    check('operational_evidence_scope',True if not policy.require_operational_evidence else None if not op_scope else op_matches,
        'Monitoring belongs to this original account, strategy, configuration and session; shadow monitoring cannot certify paper execution')
    try:op_period=utc(op['start'])<=start and utc(op['end'])>=end
    except (KeyError,ValueError,TypeError):op_period=None
    check('operational_evidence_period',True if not policy.require_operational_evidence else op_period,'Original monitoring spans the complete requested paper-performance period')
    check('funding_episode_lineage',None if funding and not episodes else all(f.get('episode_id') in {e.get('episode_id') for e in episodes} for f in funding),'Every recorded funding receipt belongs to an original evaluated position episode')
    costs={'fees':'UNKNOWN','funding':'UNKNOWN','spread':'UNKNOWN','slippage':'UNKNOWN'}
    results=[];expected_cash={};tolerance=amount(policy.money_tolerance);qtolerance=amount(policy.quantity_tolerance)
    try:
        check('order_quantity',all(abs(sum((amount(f['quantity']) for f in fills if f.get('order_id')==oid),Decimal(0))-amount(o['filled_quantity']))<=qtolerance for oid,o in order_map.items()),'Constituent fills reconcile to actual filled order quantity')
        fee_total=sum((amount(f['fee']) for f in fills),Decimal(0))
        check('fee_attribution',abs(fee_total-amount(account.get('fees_paid')))<=tolerance,'Recorded fill fees reconcile with account fees; no double commission')
        costs['fees']='RECORDED_FEE' if all(f.get('source_reference') for f in fills) else 'UNKNOWN'
        embedded=bool(fills) and all(f.get('cost_basis')=='ORIGINAL_EMBEDDED_PRICE' and f.get('reference_price') is not None and f.get('spread') is not None and f.get('slippage') is not None for f in fills)
        explained=embedded and all(f.get('side') in {'BUY','SELL'} and abs(amount(f['price'])-amount(f['reference_price'])-(amount(f['spread'])+amount(f['slippage']))*(1 if f['side']=='BUY' else -1))<=tolerance for f in fills)
        if explained:costs.update(spread='ORIGINAL_EMBEDDED_PRICE',slippage='ORIGINAL_EMBEDDED_PRICE')
        check('execution_price_costs',explained if embedded else None,'Original signed recorded spread/slippage explains fill-reference difference; embedded costs are not deducted twice')
        for fill in fills:expected_cash[fill['fill_id']]=amount(fill['gross_pnl'])-amount(fill['fee'])
        fund_total=sum((amount(f['amount']) for f in funding),Decimal(0))
        check('funding_attribution',abs(fund_total-amount(account.get('funding_paid')))<=tolerance,'Recorded signed funding charges reconcile with account funding')
        for fund in funding:expected_cash[fund['funding_id']]=-amount(fund['amount'])
        funding_known=True
        for episode in episodes:
            legs=[fill_map[fid] for fid in episode['fill_ids'] if fid in fill_map]
            check('episode_fills:'+episode['episode_id'],len(legs)==len(episode['fill_ids']) and all(f.get('episode_id')==episode['episode_id'] for f in legs),'Every original episode fill is present exactly once')
            if len(legs)!=len(episode['fill_ids']):continue
            entries=[f for f in legs if f.get('effect')=='ENTRY'];exits=[f for f in legs if f.get('effect')=='EXIT']
            entry_qty=sum((amount(f['quantity']) for f in entries),Decimal(0));exit_qty=sum((amount(f['quantity']) for f in exits),Decimal(0))
            check('quantity_conservation:'+episode['episode_id'],episode.get('status')=='closed' and entry_qty>0 and abs(entry_qty-exit_qty)<=qtolerance,'A completed episode closes all actual entry quantity')
            running_qty=Decimal(0);average_price=Decimal(0);gross=Decimal(0)
            sign=Decimal(1) if episode.get('direction')=='LONG' else Decimal(-1) if episode.get('direction')=='SHORT' else None
            if sign is None:raise ValueError('UNKNOWN_EPISODE_DIRECTION')
            for fill in sorted(legs,key=lambda f:utc(f['timestamp'])):
                qty=amount(fill['quantity']);price=amount(fill['price'])
                if qty<=0 or price<=0:raise ValueError('INVALID_FILL_QUANTITY_OR_PRICE')
                expected_side='BUY' if (sign==1 and fill['effect']=='ENTRY') or (sign==-1 and fill['effect']=='EXIT') else 'SELL'
                check('fill_direction:'+fill['fill_id'],None if not fill.get('side') else fill['side']==expected_side,'Actual entry/exit side agrees with the original episode direction')
                if fill['effect']=='ENTRY':
                    average_price=(average_price*running_qty+price*qty)/(running_qty+qty);running_qty+=qty
                elif fill['effect']=='EXIT':
                    pnl=(price-average_price)*qty*sign
                    check('realized_pnl:'+fill['fill_id'],running_qty>=qty and abs(pnl-amount(fill['gross_pnl']))<=tolerance,'Original fill prices/quantity reconcile with booked gross P&L')
                    running_qty-=qty;gross+=amount(fill['gross_pnl'])
                else:raise ValueError('UNSUPPORTED_EPISODE_LEG')
            linked_funding=[f for f in funding if f.get('episode_id')==episode['episode_id']]
            expected_ids=episode.get('expected_funding_ids')
            original_proof=episode.get('funding_exposure_reference')
            coverage=episode.get('funding_coverage')
            if not original_proof or expected_ids is None or set(expected_ids)!={f['funding_id'] for f in linked_funding} or coverage not in {'COMPLETE','ZERO_COST_VERIFIED'}:funding_known=False
            for f in linked_funding:
                exposure=amount(f['quantity'])*amount(f['mark_price'])*amount(f['rate'])*sign
                quantity_at_funding=sum((amount(leg['quantity'])*(1 if leg['effect']=='ENTRY' else -1) for leg in legs if utc(leg['timestamp'])<utc(f['timestamp'])),Decimal(0))
                ambiguous=any(utc(leg['timestamp'])==utc(f['timestamp']) for leg in legs)
                check('funding_exposure:'+f['funding_id'],None if ambiguous else bool(f.get('source_reference')) and utc(episode['opened_at'])<=utc(f['timestamp'])<utc(episode['closed_at']) and abs(quantity_at_funding-amount(f['quantity']))<=qtolerance and abs(exposure-amount(f['amount']))<=tolerance,'Original rate, actual contemporaneous quantity, signed exposure and funding time support the booked charge; ambiguous equal timestamps stay unknown')
            fees=sum((amount(f['fee']) for f in legs),Decimal(0));funds=sum((amount(f['amount']) for f in linked_funding),Decimal(0));net=gross-fees-funds
            risk=sum((amount(f['initial_risk']) for f in entries),Decimal(0)) if entries and all(f.get('initial_risk') is not None for f in entries) else None
            if start<=utc(episode['closed_at'])<end:results.append({'episode_id':episode['episode_id'],'net':net,'gross':gross,'fees':fees,'funding':funds,'r':net/risk if risk is not None and risk>0 else None,'session':episode.get('session'),
                'adverse_cost':fees+sum((max(amount(f['amount']),Decimal(0)) for f in linked_funding),Decimal(0))+
                    sum(((abs(amount(f['spread']))+abs(amount(f['slippage'])))*amount(f['quantity']) for f in legs),Decimal(0)) if explained else None})
        if funding_known and episodes:costs['funding']='ZERO_COST_VERIFIED' if not funding and all(e.get('funding_coverage')=='ZERO_COST_VERIFIED' for e in episodes) else 'RECORDED_FUNDING'
        check('funding_coverage',True if costs['funding']!='UNKNOWN' else None,'Missing funding or original exposure/schedule proof is never zero')
        cash_by_ref=defaultdict(Decimal)
        for row in journal:
            if row.get('account_id')!=scope.get('account_id'):raise ValueError('JOURNAL_ACCOUNT_MISMATCH')
            cash_by_ref[row['reference_id']]+=amount(row['cash_delta'])
        check('journal_receipt_match',None if bundle.get('journal_complete') is not True else bool(expected_cash) and set(cash_by_ref)==set(expected_cash) and all(abs(cash_by_ref[k]-v)<=tolerance for k,v in expected_cash.items()),'Every original cash journal reference matches its fill or funding receipt; absent journal is unknown')
        check('account_balance',abs(amount(account['starting_balance'])+sum(expected_cash.values(),Decimal(0))-amount(account['balance']))<=tolerance,'Complete account cash movement reconciles; no ledger repair')
    except FinancialAmountUnavailable:
        check('financial_values',None,'An original financial amount is absent; no zero value is substituted')
    except KeyError:
        check('financial_values',None,'An original financial field or lineage reference is absent')
    except (ValueError,TypeError,InvalidOperation,ZeroDivisionError) as exc:
        check('financial_values',False,type(exc).__name__+': original financial amount/lineage unavailable or invalid')
    failed=[c['name'] for c in checks if c['status']=='FAIL'];unknown=[c['name'] for c in checks if c['status']=='UNKNOWN']
    eligible=not failed and not unknown and bool(results)
    status='FAILED' if failed else 'UNKNOWN' if bundle.get('authority_status')!='COMPLETE' else 'PARTIAL' if unknown or not results else 'VERIFIED'
    metrics={k:None for k in ('completed_episodes','win_rate','average_win','average_loss','net_expectancy','profit_factor','gross_pnl','fees','funding','net_pnl','average_realized_r','maximum_drawdown','return_concentration','session_performance','strategy_performance','cost_sensitivity')}
    if eligible:
        n=len(results);nets=[r['net'] for r in results];winners=[v for v in nets if v>0];losers=[v for v in nets if v<0];total=sum(nets,Decimal(0))
        metrics.update(completed_episodes=n,win_rate=money(Decimal(len(winners))/n),net_expectancy=money(total/n),
            average_win=money(sum(winners,Decimal(0))/len(winners)) if winners else None,
            average_loss=money(sum(losers,Decimal(0))/len(losers)) if losers else None,
            profit_factor=money(sum(winners,Decimal(0))/-sum(losers,Decimal(0))) if losers else None,
            gross_pnl=money(sum((r['gross'] for r in results),Decimal(0))),fees=money(sum((r['fees'] for r in results),Decimal(0))),
            funding=money(sum((r['funding'] for r in results),Decimal(0))),net_pnl=money(total),
            average_realized_r=money(sum((r['r'] for r in results),Decimal(0))/n) if all(r['r'] is not None for r in results) else None,
            return_concentration={'largest_winner_share':money(max(winners)/total) if winners and total>0 else None,
                'net_excluding_largest_winner':money(total-max(winners)) if winners else money(total)})
        sessions=defaultdict(list)
        for row in results:sessions[row['session'] or 'UNKNOWN'].append(row)
        metrics['session_performance']={session:{'completed_episodes':len(rows),'net_pnl':money(sum((r['net'] for r in rows),Decimal(0))),
            'sample_warning':'LOW_EPISODE_SAMPLE_SIZE' if len(rows)<policy.minimum_sample_warning else None} for session,rows in sessions.items()}
        metrics['strategy_performance']={k:scope[k] for k in ('strategy_id','strategy_version','config_fingerprint')}
        metrics['strategy_performance'].update(completed_episodes=n,net_pnl=money(total),cohort_scope='EXACT_ACCOUNT_SESSION_CONFIGURATION')
        stress_cost=sum((r['adverse_cost'] for r in results),Decimal(0))
        metrics['cost_sensitivity']={'evidence_kind':'MODELLED_COST_STRESS','methodology':'Additional 25% and 50% of recorded fees, positive funding charges and embedded spread/slippage notional; favourable funding unchanged; quantities frozen',
            'limitations':['No strategy re-execution or altered fill eligibility','No prediction of future costs'],
            'scenarios':[{'cost_multiplier':money(factor),'net_pnl':money(total-stress_cost*(factor-1))} for factor in (Decimal('1.25'),Decimal('1.5'))]}
        equity=bundle.get('equity_series') or {}
        try:
            points=equity['points'];times=[utc(p['timestamp']) for p in points]
            if equity.get('status')=='COMPLETE' and equity.get('basis')=='EQUITY_INCLUDING_UNREALIZED' and equity.get('source_reference') and 2<=len(points)<=10000 and all(p.get('source_reference') for p in points) and times==sorted(set(times)) and times[0]==start and times[-1]==end:
                peak=amount(points[0]['equity']);drawdown=Decimal(0);fraction=Decimal(0)
                for point in points:
                    value=amount(point['equity']);peak=max(peak,value)
                    drawdown=max(drawdown,peak-value)
                    if peak>0:fraction=max(fraction,(peak-value)/peak)
                metrics['maximum_drawdown']={'amount':money(drawdown),'fraction':money(fraction),'basis':'ORIGINAL_PERIOD_MATCHED_EQUITY_INCLUDING_UNREALIZED'}
        except (KeyError,ValueError,TypeError,InvalidOperation):pass  # absent/invalid original series is unavailable, never reconstructed from trades
    warnings=['PAPER_RETURNS_ARE_NOT_LIVE_RETURNS','NO_AUTOMATIC_EXECUTION_AUTHORIZATION']
    if len(results)<policy.minimum_sample_warning:warnings.append('LOW_EPISODE_SAMPLE_SIZE')
    if metrics['maximum_drawdown'] is None:warnings.append('AUTHORITATIVE_EQUITY_SERIES_UNAVAILABLE')
    if metrics['average_realized_r'] is None:warnings.append('ORIGINAL_RISK_UNAVAILABLE')
    return {'calculation_version':VERSION,'policy':{**asdict(policy),'fingerprint':policy.fingerprint},
        'status':status,'performance_eligible':eligible,'performance_type':'RECONCILED_PAPER_PERFORMANCE' if eligible else 'PARTIALLY_RECONCILED_PAPER_PERFORMANCE' if bundle.get('authority_status')=='COMPLETE' else 'UNVERIFIED_PAPER_PERFORMANCE',
        'scope':scope,'start':start.isoformat(),'end':end.isoformat(),'checks':checks,'failed_checks':failed,'unknown_checks':unknown,
        'passed_checks':[c['name'] for c in checks if c['status']=='PASS'],'cost_coverage':costs,'metrics':metrics,
        'observed_counts':{'orders':len(orders),'fills':len(fills),'completed_episodes':len(results)},
        'evidence_hash':digest(bundle),'original_references':{'orders':[r.get('source_reference') for r in orders],
            'fills':[r.get('source_reference') for r in fills],'journal':[r.get('journal_id') for r in journal]},
        'warning_codes':warnings,'execution_authorization':False}
