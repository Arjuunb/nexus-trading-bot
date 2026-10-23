"""Authoritative-shaped, isolated fixtures; no reported production returns."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import pytest

from services.paper_reconciliation import ReconciliationPolicy,reconcile_paper

T=datetime(2026,10,12,8,tzinfo=timezone.utc)


@pytest.fixture
def complete():
    from services.strategy_identity import configuration_fingerprint
    frozen={'strategy_id':'adaptive_trend_pullback','strategy_version':'1.0.0','effective_parameters':{'min_quality':75},'fixture_only':True}
    scope={'owner_id':'alice','account_id':'paper-one','strategy_id':'adaptive_trend_pullback','strategy_version':'1.0.0',
        'config_fingerprint':configuration_fingerprint(frozen),'simulation_session_id':'session-one','execution_mode':'paper','symbol':'XRPUSDT'}
    fills=[]
    for i,(effect,qty,price,gross) in enumerate([('ENTRY','1','100','0'),('EXIT','.4','105','2'),('EXIT','.6','103','1.8')]):
        fills.append({**scope,'fill_id':'fill-'+str(i),'order_id':'order-'+str(i),'episode_id':'episode-one',
            'effect':effect,'side':'BUY' if effect=='ENTRY' else 'SELL','quantity':qty,'price':price,'fee':'.1','gross_pnl':gross,
            'timestamp':(T+timedelta(seconds=60*i)).isoformat(),'source_reference':'booked-fill-'+str(i),
            'initial_risk':'10' if i==0 else None,'cost_basis':'ORIGINAL_EMBEDDED_PRICE',
            'spread':'0','slippage':'0','reference_price':price})
    orders=[{**scope,'order_id':f['order_id'],'intent_reference':'intent-'+f['order_id'],
        'filled_quantity':f['quantity'],'source_reference':'original-order-'+f['order_id']} for f in fills]
    funding=[{**scope,'funding_id':'funding-one','episode_id':'episode-one','amount':'.2','quantity':'1',
        'mark_price':'100','rate':'.002','timestamp':(T+timedelta(seconds=30)).isoformat(),
        'source_reference':'original-rate-and-exposure','coverage':'OBSERVED'}]
    journal=[{'journal_id':'cash-'+f['fill_id'],'reference_id':f['fill_id'],'account_id':'paper-one',
        'cash_delta':delta} for f,delta in zip(fills,['-.1','1.9','1.7'])]
    journal.append({'journal_id':'cash-funding','reference_id':'funding-one','account_id':'paper-one','cash_delta':'-.2'})
    return {'schema_version':'paper_financial_snapshot.v1','authority_status':'COMPLETE','scope':scope,
        'configuration_evidence':{'snapshot':frozen,'source_reference':'original-immutable-configuration','status':'VERIFIED'},
        'source_reference':'original-isolated-account-snapshot','records_complete':True,'journal_complete':True,
        'account':{'account_id':'paper-one','owner_id':'alice','account_type':'PAPER','starting_balance':'1000',
            'balance':'1003.3','fees_paid':'.3','funding_paid':'.2'},'orders':orders,'fills':fills,
        'episodes':[{'episode_id':'episode-one','direction':'LONG','status':'closed','fill_ids':[f['fill_id'] for f in fills],
            'opened_at':T.isoformat(),'closed_at':(T+timedelta(minutes=2)).isoformat(),'funding_coverage':'COMPLETE',
            'expected_funding_ids':['funding-one'],'funding_exposure_reference':'original-schedule-and-exposure'}],
        'funding':funding,'journal':journal,'equity_series':None,
        'operational_evidence':{'monitoring_status':'COMPLETE','evidence_references':['original-monitoring-events'],
            'worker_availability':1,'feed_availability':1,'commit_success_rate':1,'scope':scope,
            'start':T.isoformat(),'end':(T+timedelta(minutes=5)).isoformat()}}


def run(bundle):return reconcile_paper(bundle,owner_id='alice',start=T,end=T+timedelta(minutes=5))


def test_complete_partial_exit_episode_reconciles_decimal_money(complete):
    original=deepcopy(complete);report=run(complete)
    assert report['status']=='VERIFIED'
    assert report['performance_type']=='RECONCILED_PAPER_PERFORMANCE'
    assert report['metrics']['completed_episodes']==1
    assert report['metrics']['net_pnl']=='3.3'
    assert report['metrics']['fees']=='0.3'
    assert report['metrics']['funding']=='0.2'
    assert report['metrics']['average_realized_r']=='0.33'
    assert report['metrics']['maximum_drawdown'] is None
    assert complete==original
    assert report['execution_authorization'] is False


def test_missing_funding_does_not_become_zero(complete):
    complete['funding']=[];complete['episodes'][0]['funding_coverage']='UNKNOWN'
    report=run(complete)
    assert report['performance_eligible'] is False
    assert report['cost_coverage']['funding']=='UNKNOWN'
    assert report['metrics']['net_pnl'] is None


def test_verified_zero_requires_original_exemption_evidence(complete):
    complete['funding']=[];episode=complete['episodes'][0];episode['expected_funding_ids']=[]
    episode['funding_coverage']='ZERO_COST_VERIFIED';episode['funding_exposure_reference']='original-no-scheduled-funding-exposure'
    complete['account']['funding_paid']='0';complete['account']['balance']='1003.5';complete['journal']=complete['journal'][:-1]
    assert run(complete)['metrics']['funding']=='0'
    episode['funding_exposure_reference']=None
    assert run(complete)['performance_eligible'] is False


@pytest.mark.parametrize('field',['quantity','fee','gross_pnl'])
def test_inconsistent_financial_source_fails_without_repair(complete,field):
    complete['fills'][-1][field]='123'
    report=run(complete)
    assert report['status']=='FAILED'
    assert report['performance_eligible'] is False
    assert report['metrics']['net_pnl'] is None


def test_duplicate_and_missing_fills_fail_closed(complete):
    missing=deepcopy(complete);missing['fills'].pop()
    assert run(missing)['performance_eligible'] is False
    complete['fills'].append(deepcopy(complete['fills'][0]))
    assert 'duplicate_fills' in run(complete)['failed_checks']


def test_cross_account_and_strategy_configuration_do_not_mix(complete):
    complete['fills'][0]['account_id']='another-account'
    assert 'exact_cohort' in run(complete)['failed_checks']
    complete['fills'][0]['account_id']='paper-one';complete['fills'][0]['config_fingerprint']='b'*64
    assert 'exact_cohort' in run(complete)['failed_checks']


def test_missing_risk_and_equity_do_not_create_r_or_drawdown(complete):
    complete['fills'][0]['initial_risk']=None
    report=run(complete)
    assert report['status']=='VERIFIED'
    assert report['metrics']['average_realized_r'] is None
    assert report['metrics']['maximum_drawdown'] is None


def test_spread_slippage_embedded_in_fill_are_not_charged_twice(complete):
    complete['fills'][0].update(spread='.1',slippage='.2',reference_price='99.7')
    report=run(complete)
    assert report['metrics']['net_pnl']=='3.3'
    assert report['cost_coverage']['slippage']=='ORIGINAL_EMBEDDED_PRICE'


def test_journal_or_operational_gap_blocks_verified_performance(complete):
    gap=deepcopy(complete);gap['journal_complete']=False
    assert run(gap)['performance_eligible'] is False
    complete['operational_evidence']['monitoring_status']='UNKNOWN'
    assert run(complete)['performance_eligible'] is False


def test_policy_identity_versioning_and_no_financial_float_math():
    policy=ReconciliationPolicy()
    assert policy.fingerprint==ReconciliationPolicy().fingerprint
    with pytest.raises(ValueError):ReconciliationPolicy(money_tolerance='NaN')


def test_funding_quantity_must_match_actual_exposure_not_just_arithmetic(complete):
    complete['funding'][0].update(quantity='2',rate='.001')
    report=run(complete)
    assert 'funding_exposure:funding-one' in report['failed_checks']


def test_duplicate_orders_funding_and_cash_ids_are_not_accepted(complete):
    complete['orders'].append(deepcopy(complete['orders'][0]))
    complete['funding'].append(deepcopy(complete['funding'][0]))
    complete['journal'].append(deepcopy(complete['journal'][0]))
    report=run(complete)
    assert all(name in report['failed_checks'] for name in ('duplicate_orders','duplicate_funding','duplicate_journal'))


def test_embedded_costs_require_matching_original_reference_prices(complete):
    complete['fills'][0]['reference_price']='1'
    assert 'execution_price_costs' in run(complete)['failed_checks']


def test_unassigned_or_double_counted_episode_fills_block_performance(complete):
    complete['episodes'][0]['fill_ids'].append('fill-0')
    assert 'episode_fill_coverage' in run(complete)['failed_checks']


def test_drawdown_requires_original_complete_period_matched_equity(complete):
    complete['equity_series']={'status':'COMPLETE','basis':'EQUITY_INCLUDING_UNREALIZED','source_reference':'original-equity-journal',
        'points':[{'timestamp':(T+timedelta(minutes=i)).isoformat(),'equity':value,'source_reference':'equity-'+str(i)}
            for i,value in ((0,'1000'),(1,'900'),(5,'1003.3'))]}
    report=run(complete)
    assert report['metrics']['maximum_drawdown']=={'amount':'100','fraction':'0.1','basis':'ORIGINAL_PERIOD_MATCHED_EQUITY_INCLUDING_UNREALIZED'}
    complete['equity_series']['status']='PARTIAL'
    assert run(complete)['metrics']['maximum_drawdown'] is None


def test_cost_sensitivity_and_subgroups_remain_explicitly_modelled(complete):
    complete['episodes'][0]['session']='LONDON'
    report=run(complete)
    assert report['metrics']['session_performance']['LONDON']['completed_episodes']==1
    assert report['metrics']['strategy_performance']['strategy_version']=='1.0.0'
    assert report['metrics']['cost_sensitivity']['evidence_kind']=='MODELLED_COST_STRESS'
    assert report['metrics']['cost_sensitivity']['scenarios'][0]['net_pnl']=='3.175'


def test_operational_evidence_from_another_account_or_period_cannot_verify_paper(complete):
    complete['operational_evidence']['scope']={**complete['scope'],'account_id':'other-paper'}
    assert 'operational_evidence_scope' in run(complete)['failed_checks']
    complete['operational_evidence']['scope']=complete['scope']
    complete['operational_evidence']['start']=(T+timedelta(minutes=1)).isoformat()
    assert run(complete)['performance_eligible'] is False


def test_configuration_presence_without_original_snapshot_cannot_verify_performance(complete):
    complete.pop('configuration_evidence')
    assert 'configuration_identity' in run(complete)['unknown_checks']
    assert run(complete)['metrics']['net_pnl'] is None


def test_conflicting_original_configuration_snapshot_fails_closed(complete):
    complete['configuration_evidence']['snapshot']['effective_parameters']['min_quality']=76
    assert 'configuration_identity' in run(complete)['failed_checks']
