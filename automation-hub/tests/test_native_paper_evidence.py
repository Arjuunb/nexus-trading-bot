"""Isolated existing broker transactions; no active instance or provider orders."""
import json
import sqlite3
from copy import deepcopy

import pytest

from execution.paper_broker_v2 import PaperBrokerV2
from services.strategy_identity import configuration_fingerprint


def broker(tmp_path, **options):
    return PaperBrokerV2(tmp_path / 'paper.db', starting_balance=1000,
        fee_rate=.001, spread_bps=2, slippage_bps=3, participation_rate=1,
        evidence_owner='alice', **options)


def context(value=70):
    frozen={'strategy_id':'fixture-strategy','strategy_version':'1.0.0','effective_parameters':{'threshold':value}}
    return {'tenant_id':'alice','strategy_id':'fixture-strategy','strategy_version':'1.0.0',
        'effective_config_fingerprint':configuration_fingerprint(frozen),'configuration_snapshot':frozen,
        'configuration_reference':'original-decision:one','configuration_status':'VERIFIED',
        'execution_session_id':'session-one','venue':'binance_usdm','market_type':'perpetual',
        'timeframe':'5m','execution_mode':'paper','decision_reference':'original-decision:one','order_intent_reference':'intent:one'}


def submit(b, side='buy', quantity=1, **kwargs):
    return b.submit(symbol='XRPUSDT',side=side,order_type='market',quantity=quantity,
        strategy='fixture-strategy',strategy_version='1.0.0',evidence_context=context(),**kwargs)


def advance(b, price=100, volume=10):
    return b.process_candle('XRPUSDT',{'open':price,'high':price,'low':price,'close':price,'volume':volume})


def events(b, kind=None):
    rows=b._c.execute('SELECT payload_json FROM v2_source_evidence ORDER BY sequence').fetchall()
    result=[json.loads(r[0]) for r in rows]
    return [r for r in result if not kind or r['event_type']==kind]


def test_original_order_configuration_survives_change_and_restart(tmp_path):
    b=broker(tmp_path);frozen=context()
    order=b.submit(symbol='XRPUSDT',side='buy',order_type='market',quantity=1,
        strategy='fixture-strategy',strategy_version='1.0.0',evidence_context=frozen)
    frozen['configuration_snapshot']['effective_parameters']['threshold']=99
    again=broker(tmp_path);advance(again)
    captured=events(again,'ORDER_ACCEPTED')[0]
    assert captured['effective_config_fingerprint']==context()['effective_config_fingerprint']
    assert captured['payload']['order']['id']==order['id']
    assert events(again,'FILL_COMMITTED')[0]['effective_config_fingerprint']==captured['effective_config_fingerprint']


def test_scale_in_partial_exits_and_cash_conservation(tmp_path):
    b=broker(tmp_path);submit(b,quantity=2);advance(b,volume=1);advance(b,volume=1)
    episode=b.positions()[0]['position_id']
    submit(b,quantity=1);advance(b,110)
    submit(b,'sell',1,reduce_only=True);advance(b,115)
    assert b.positions()[0]['size']==2
    submit(b,'sell',2,reduce_only=True);advance(b,120)
    fills=events(b,'FILL_COMMITTED')
    allocations=[leg for r in fills for leg in r['payload']['allocations']]
    assert {leg['episode_id'] for leg in allocations}=={episode}
    assert sum(leg['episode_closed'] for leg in allocations)==1
    movements=[r['payload']['cash_delta'] for r in fills]
    assert b.account(persist_metrics=False)['balance']==pytest.approx(1000+sum(movements))
    assert len(fills)==5


def test_reversal_allocates_one_physical_fill_to_original_closed_and_new_episode(tmp_path):
    b=broker(tmp_path);submit(b);advance(b);old=b.positions()[0]['position_id']
    submit(b,'sell',2);advance(b,110)
    legs=events(b,'FILL_COMMITTED')[-1]['payload']['allocations']
    assert [(r['effect'],r['quantity']) for r in legs]==[('EXIT',1),('ENTRY',1)]
    assert legs[0]['episode_id']==old and legs[0]['episode_closed'] is True
    assert legs[1]['episode_id']==b.positions()[0]['position_id']!=old
    assert sum(r['fee'] for r in legs)==pytest.approx(b.fills()[0]['fee'])


def test_evidence_failure_rolls_back_original_financial_transaction(tmp_path):
    b=broker(tmp_path);submit(b);before=b.export_state()
    b._c.execute("CREATE TRIGGER fail_evidence BEFORE INSERT ON v2_source_evidence WHEN NEW.event_type='FILL_COMMITTED' BEGIN SELECT RAISE(ABORT,'injected evidence outage'); END")
    with pytest.raises(sqlite3.IntegrityError,match='injected'):advance(b)
    assert b.export_state()==before
    b._c.execute('DROP TRIGGER fail_evidence');advance(b)
    assert len(events(b,'FILL_COMMITTED'))==1


def test_funding_preserves_original_quantity_basis_and_duplicate_identity(tmp_path):
    b=broker(tmp_path);submit(b);advance(b)
    b.apply_funding('XRPUSDT',.001,100,funding_timestamp='2026-10-12T08:00:00+00:00')
    b.apply_funding('XRPUSDT',.001,100,funding_timestamp='2026-10-12T08:00:00+00:00')
    evidence=events(b,'FUNDING_COMMITTED')
    assert len(evidence)==1
    assert evidence[0]['payload']['position']['size']==1
    assert evidence[0]['payload']['cash_delta']==-.1
    assert evidence[0]['payload']['funding_coverage']=='RECORDED_FUNDING'
    assert evidence[0]['payload']['schedule_status']=='UNKNOWN'


def test_historical_source_is_not_given_invented_ownership_or_opening_balance(tmp_path):
    path=tmp_path/'paper.db';old=PaperBrokerV2(path,starting_balance=1000)
    old.submit(symbol='XRPUSDT',side='buy',order_type='market',quantity=1)
    advance(old);old._c.close()
    # Simulate a pre-capture original database, preserving every financial row.
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE v2_source_evidence')
    b=broker(tmp_path)
    first=events(b)[0]
    assert first['event_type']=='CAPTURE_STARTED'
    assert first['payload']['historical_completeness']=='UNKNOWN'
    assert len(b.fills())==1


def test_owner_mismatch_does_not_mutate_financial_state(tmp_path):
    b=broker(tmp_path);before=b.export_state()
    with pytest.raises(ValueError,match='OWNER_CONFLICT'):
        PaperBrokerV2(b.path,evidence_owner='mallory')
    assert b.export_state()==before


def test_protective_exit_has_original_internal_instruction_and_episode_context(tmp_path):
    b=broker(tmp_path);submit(b);advance(b);episode=b.positions()[0]['position_id']
    b.set_protection('XRPUSDT',stop_loss=95);advance(b,90)
    event=events(b,'FILL_COMMITTED')[-1]
    assert event['payload']['order_origin']=='EXISTING_INTERNAL_PROTECTION'
    assert event['payload']['allocations'][0]['episode_id']==episode
    assert event['effective_config_fingerprint']==context()['effective_config_fingerprint']
    assert len(b.orders())==1  # no new financial order created by evidence


def test_evidence_is_immutable_and_costs_are_explicitly_embedded(tmp_path):
    b=broker(tmp_path);submit(b);advance(b)
    cost=events(b,'FILL_COMMITTED')[0]['payload']['cost_model']
    assert cost['spread_bps']==2 and cost['slippage_bps']==3
    assert cost['basis']=='EMBEDDED_IN_PRICE'
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        b._c.execute('UPDATE v2_source_evidence SET evidence_hash=?',('a'*64,))


def test_equity_observation_does_not_fabricate_missing_or_stale_marks(tmp_path):
    b=broker(tmp_path);submit(b);advance(b)
    from services.native_paper_evidence import observe_equity
    result=observe_equity(b,{},observed_at='2026-10-12T08:05:00+00:00')
    assert result['payload']['equity'] is None
    assert result['payload']['completeness']=='UNKNOWN'
    before=deepcopy(b.export_state())
    result=observe_equity(b,{'XRPUSDT':{'price':105,'timestamp':'2026-10-12T08:05:00+00:00','source':'fixture-public-mark'}},observed_at='2026-10-12T08:05:01+00:00')
    assert result['payload']['completeness']=='COMPLETE'
    assert b.export_state()==before


def native_snapshot(b):
    return {'account':dict(b._account_row()),'fills':b.fills(),'orders':b.orders(),'source_events':events(b)}


def test_original_native_receipts_reconcile_one_episode_and_proved_zero_funding(tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    import execution.paper_broker_v2 as module
    from services.native_paper_evidence import record_funding_schedule
    from services.native_paper_snapshot import normalize_native
    from services.paper_reconciliation import ReconciliationPolicy,reconcile_paper
    start=datetime(2026,10,12,8,tzinfo=timezone.utc);clock=[start]
    monkeypatch.setattr(module,'_now',lambda:clock[0].isoformat())
    b=broker(tmp_path);submit(b,quantity=2);advance(b)
    clock[0]+=timedelta(minutes=1);submit(b,'sell',.5,reduce_only=True);advance(b,110)
    clock[0]+=timedelta(minutes=1);submit(b,'sell',1.5,reduce_only=True);advance(b,115)
    end=clock[0]+timedelta(minutes=1)
    record_funding_schedule(b,symbol='XRPUSDT',start=start.isoformat(),end=end.isoformat(),
        settlements=[],source_reference='fixture-original-public-schedule-complete-range')
    source={'session_id':'session-one','symbol':'XRPUSDT'}
    before=deepcopy(b.export_state())
    snapshot=normalize_native(native_snapshot(b),source=source,owner_id='alice',start=start,end=end)
    report=reconcile_paper(snapshot,owner_id='alice',start=start,end=end,policy=ReconciliationPolicy(require_operational_evidence=False))
    assert report['status']=='VERIFIED',report['checks']
    assert report['metrics']['completed_episodes']==1
    assert report['cost_coverage']['funding']=='ZERO_COST_VERIFIED'
    assert float(report['metrics']['net_pnl'])==pytest.approx(b.account(persist_metrics=False)['balance']-1000)
    assert b.export_state()==before
    snapshot['episodes'][0]['funding_exposure_reference']=None
    assert reconcile_paper(snapshot,owner_id='alice',start=start,end=end)['performance_eligible'] is False


def test_duplicate_quote_does_not_duplicate_financial_receipt(tmp_path):
    b=broker(tmp_path);submit(b)
    from datetime import datetime,timedelta,timezone
    now=datetime.now(timezone.utc)+timedelta(seconds=1)
    quote={'bid':99,'ask':100,'mark':100,'received_at':now.isoformat(),'event_timestamp':now.isoformat(),'sequence':1}
    b.process_tick('XRPUSDT',quote);b.process_tick('XRPUSDT',quote)
    assert len(events(b,'FILL_COMMITTED'))==1


@pytest.mark.parametrize('kind',['pa','smc'])
def test_contemporaneous_lab_capture_only_and_smc_model_alias_preserved(tmp_path,kind):
    from services.native_paper_evidence import original_lab_context
    b=broker(tmp_path);cfg={'model_id':'fixture-model'};frozen=context()['configuration_snapshot']
    frozen['execution_config']=cfg
    capture={'configuration_snapshot':frozen,'configuration_status':'VERIFIED','tenant_id':'alice',
        'strategy_id':frozen['strategy_id'],'strategy_version':'1.0.0','config_fingerprint':configuration_fingerprint(frozen)}
    b._c.execute(f'CREATE TABLE {kind}_evaluations(correlation_id TEXT,session_id TEXT,payload_json TEXT)')
    b._c.execute(f'INSERT INTO {kind}_evaluations VALUES (?,?,?)',('decision','session-one',json.dumps({'active_evidence':capture})))
    session={'id':'session-one','mode':'LIVE_PAPER','timeframe':'5m'}
    used=original_lab_context(b._c,kind,correlation_id='decision',owner='alice',session=session,
        execution_config=cfg,strategy=frozen['strategy_id'],version='1.0.0')
    order=b.submit(symbol='XRPUSDT',side='buy',order_type='market',quantity=1,strategy='fixture-model',
        strategy_version='1.0.0',evidence_context=used)
    assert events(b,'ORDER_ACCEPTED')[-1]['effective_config_fingerprint']==capture['config_fingerprint']
    changed=original_lab_context(b._c,kind,correlation_id='decision',owner='alice',session=session,
        execution_config={'model_id':'changed'},strategy=frozen['strategy_id'],version='1.0.0')
    assert changed.get('effective_config_fingerprint') is None
    assert b.order(order['id'])['strategy']=='fixture-model'


def test_authorized_source_delta_pins_all_unchanged_methods_and_native_strategy_rules():
    import ast,hashlib
    from acceptance_freeze import successor_hash
    from pathlib import Path
    root=Path(__file__).parents[1];manifest=json.loads((root/'data/pr6_real_paper_freeze.json').read_text())
    delta=next(r for r in manifest['subsequent_approved_deltas'] if r.get('delta_id')=='sprint11-native-financial-evidence.v1')
    assert delta['alpha_rules_changed'] is False and delta['fill_timing_changed'] is False
    assert delta['changed_methods']['services/price_action_lab.py']==['PriceActionPaperAccount.__init__','PriceActionPaperAccount._place_proposal','PriceActionPaperAccount.apply_funding_once']
    assert delta['changed_methods']['services/smc_strategy_lab.py']==['SMCPaperAccount.__init__','SMCPaperAccount.submit_order','SMCPaperAccount.apply_funding_once']
    verified=0
    for relative,unchanged in delta['unchanged_method_sha256'].items():
        text=(root/relative).read_text();actual={}
        for parent in ast.parse(text).body:
            if isinstance(parent,ast.ClassDef):
                for node in parent.body:
                    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                        actual[parent.name+'.'+node.name]=hashlib.sha256(ast.get_source_segment(text,node).encode()).hexdigest()
            elif isinstance(parent,(ast.FunctionDef,ast.AsyncFunctionDef)):
                actual[parent.name]=hashlib.sha256(ast.get_source_segment(text,parent).encode()).hexdigest()
        assert set(actual)==set(unchanged)|set(delta['method_updates'][relative])
        assert all(actual[name]==successor_hash(root,relative,expected,name) for name,expected in unchanged.items())
        assert all(actual[name]==successor_hash(root,relative,update['sha256'],name) for name,update in delta['method_updates'][relative].items())
        verified+=len(unchanged)
    assert verified==166


@pytest.mark.parametrize('kind',['pa','smc'])
def test_original_lab_funding_receipt_commits_atomically_and_survives_restart(tmp_path,kind):
    from services.price_action_lab import PriceActionPaperAccount
    from services.smc_strategy_lab import SMCPaperAccount
    cls=PriceActionPaperAccount if kind=='pa' else SMCPaperAccount
    account=cls(tmp_path/(kind+'.db'),starting_balance=1000,evidence_owner='alice')
    b=account.broker;submit(b);advance(b,volume=100)
    reference={'lab':kind,'session_id':account.session()['id'],'provider_timestamp':'2026-10-12T08:00:00+00:00','source':'fixture-original-provider'}
    before=b.export_state()
    b._c.execute(f"CREATE TRIGGER fail_funding_receipt BEFORE INSERT ON {kind}_funding_events BEGIN SELECT RAISE(ABORT,'original funding receipt outage'); END")
    with pytest.raises(sqlite3.IntegrityError,match='receipt outage'):
        b.apply_funding('XRPUSDT',.001,100,funding_reference=reference)
    assert b.export_state()==before
    assert events(b,'FUNDING_COMMITTED')==[]
    b._c.execute('DROP TRIGGER fail_funding_receipt')
    b.apply_funding('XRPUSDT',.001,100,funding_reference=reference)
    # Crash boundary: broker commit happened; caller's metadata/audit ACK did not.
    restored=cls(b.path,starting_balance=1000,evidence_owner='alice')
    recovered=restored.apply_funding_once(symbol='XRPUSDT',funding_time=reference['provider_timestamp'],rate=.001,mark_price=100)
    assert recovered['applied'] is False and recovered['originally_applied'] is True
    assert restored.broker.account(persist_metrics=False)['funding_paid']==pytest.approx(.1)
    assert len(events(restored.broker,'FUNDING_COMMITTED'))==1


def test_live_quote_equity_requires_original_mark_timestamp_without_changing_fill(tmp_path):
    from datetime import datetime,timedelta,timezone
    b=broker(tmp_path);submit(b);now=datetime.now(timezone.utc)+timedelta(seconds=1)
    result=b.process_tick('XRPUSDT',{'bid':99,'ask':100,'mark':100,'received_at':now.isoformat(),
        'event_timestamp':now.isoformat(),'sequence':1,'source':'fixture-original-public-mark','mark_event_timestamp':now.isoformat()})
    assert len(result['events'])==1
    assert events(b,'EQUITY_OBSERVED')[-1]['payload']['completeness']=='COMPLETE'
    later=now+timedelta(seconds=60)
    b.process_tick('XRPUSDT',{'bid':99,'ask':100,'mark':100,'received_at':later.isoformat(),
        'event_timestamp':later.isoformat(),'sequence':2,'source':'fixture-original-public-mark','mark_event_timestamp':now.isoformat()})
    assert events(b,'EQUITY_OBSERVED')[-1]['payload']['completeness']=='UNKNOWN'


def test_evidence_instrumentation_preserves_original_fill_economics(tmp_path,monkeypatch):
    import services.native_paper_evidence as capture
    first=broker(tmp_path);submit(first,quantity=2);advance(first,100)
    submit(first,'sell',.5,reduce_only=True);advance(first,110)
    submit(first,'sell',1.5,reduce_only=True);advance(first,115)
    expected=[{k:r[k] for k in ('side','quantity','price','fee','realized_pnl')} for r in reversed(first.fills())]
    other=tmp_path/'without-observer';other.mkdir()
    second=broker(other)
    monkeypatch.setattr(capture,'capture_order',lambda *a,**k:None)
    monkeypatch.setattr(capture,'capture_fill',lambda *a,**k:None)
    submit(second,quantity=2);advance(second,100)
    submit(second,'sell',.5,reduce_only=True);advance(second,110)
    submit(second,'sell',1.5,reduce_only=True);advance(second,115)
    assert [{k:r[k] for k in ('side','quantity','price','fee','realized_pnl')} for r in reversed(second.fills())]==expected
    assert second.account(persist_metrics=False)['balance']==first.account(persist_metrics=False)['balance']


def test_reversal_risk_uses_only_actual_new_entry_quantity(tmp_path):
    b=broker(tmp_path);submit(b,protection_stop_loss=95);advance(b)
    submit(b,'sell',2,protection_stop_loss=105);advance(b)
    event=events(b,'FILL_COMMITTED')[-1];entry=event['payload']['allocations'][1]
    assert entry['quantity']==1
    assert entry['initial_risk']==pytest.approx(abs(event['payload']['fill']['price']-105))
    assert entry['initial_risk']==pytest.approx(event['payload']['fill']['risk_amount']/2)
    assert event['payload']['allocations'][0]['initial_risk'] is None
