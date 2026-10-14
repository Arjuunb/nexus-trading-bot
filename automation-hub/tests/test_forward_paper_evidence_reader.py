"""Actual journal interfaces with controlled evidence, not production trades."""
from datetime import timedelta
from copy import deepcopy
import pytest

from data.journal_store import JournalStore
from services.forward_paper_evidence_reader import read_paper_references, read_holdout_provenance
from services.forward_qualification import assess_paper_evidence
from test_shadow_router import T, candidate


@pytest.fixture
def paper_journal(tmp_path):
    journal=JournalStore(tmp_path/'journal.sqlite')
    registered=candidate()
    journal.save_strategy_identity({**registered,'configuration':registered['frozen_config']})
    scope={'owner_id':'alice','account_id':'instance:one:session','instance_id':'one','simulation_session_id':'session',
           'strategy_id':'adaptive_trend_pullback','strategy_version':'1.0.0','strategy_config_hash':registered['config_fingerprint'],
           'execution_mode':'paper','source_kind':'forward_paper','lab_id':None,'symbol':'XRPUSDT',
           'episode_id':'episode','trade_id':'trade','position_id':'position'}
    opening={'execution_id':'entry','action':'opened','initial_risk':'10','identity_status':'verified'}
    journal.record_execution_evidence('fill:entry',payload=opening,scope={**scope,'observed_at':(T-timedelta(minutes=5)).isoformat()},
        links=[{'trade_id':'trade','position_id':'position'}],create_episode=True)
    partial={'execution_id':'partial','action':'reduced','remainder_trade_id':'remainder',
             'net_pnl':'0.9','gross_pnl':'1','fees':'0.1','funding':None,'fees_coverage':'BOOKED','funding_coverage':'UNKNOWN'}
    journal.record_execution_evidence('fill:partial',payload=partial,scope={**scope,'observed_at':T.isoformat()},
        links=[{'trade_id':'remainder','position_id':'remainder-position','parent_trade_id':'trade'}],create_episode=False)
    closing={'execution_id':'final','action':'closed','net_pnl':'1.8','gross_pnl':'2','fees':'0.2',
             'funding':None,'fees_coverage':'BOOKED','funding_coverage':'UNKNOWN'}
    journal.record_execution_evidence('fill:final',payload=closing,
        scope={**scope,'trade_id':'remainder','position_id':'remainder-position','observed_at':(T+timedelta(minutes=1)).isoformat()},links=[],create_episode=False)
    selected={k:scope[k] for k in ('owner_id','account_id','instance_id','simulation_session_id')}
    identity={'strategy_id':scope['strategy_id'],'strategy_version':scope['strategy_version'],'config_fingerprint':scope['strategy_config_hash'],'symbol':scope['symbol']}
    return journal,selected,identity


def test_partial_exits_are_one_read_only_episode_with_unknown_funding(paper_journal):
    journal,scope,identity=paper_journal
    before=journal._c.execute('SELECT * FROM strategy_evidence_events').fetchall()
    result=read_paper_references(journal.path,scope=scope,identity=identity,start=T,end=T+timedelta(minutes=5))
    assert len(result['rows'])==1
    assert result['rows'][0]['realised_leg_count']==2
    assert result['rows'][0]['net_pnl']=='2.7'
    assert result['rows'][0]['fees']=='0.3'
    assert result['rows'][0]['funding'] is None
    assert assess_paper_evidence(result)['cost_coverage']['fees']=='COMPLETE'
    assert assess_paper_evidence(result)['cost_coverage']['funding']=='UNKNOWN'
    assert assess_paper_evidence(result)['qualification_eligible'] is False
    assert journal._c.execute('SELECT * FROM strategy_evidence_events').fetchall()==before


@pytest.mark.parametrize('changed', [{'owner_id':'bob'},{'account_id':'other'},{'simulation_session_id':'other'}])
def test_paper_account_tenant_and_session_isolation(paper_journal,changed):
    journal,scope,identity=paper_journal
    result=read_paper_references(journal.path,scope={**scope,**changed},identity=identity,start=T,end=T+timedelta(minutes=5))
    assert result['rows']==[]
    assert result['authority_status']=='UNKNOWN'


def test_paper_period_is_half_open_and_version_is_exact(paper_journal):
    journal,scope,identity=paper_journal
    assert read_paper_references(journal.path,scope=scope,identity=identity,start=T,end=T+timedelta(minutes=1))['rows']==[]
    assert read_paper_references(journal.path,scope=scope,identity={**identity,'strategy_version':'different'},start=T,end=T+timedelta(minutes=5))['rows']==[]
    assert read_paper_references(journal.path,scope=scope,identity={**identity,'config_fingerprint':'b'*64},start=T,end=T+timedelta(minutes=5))['rows']==[]


def test_missing_journal_is_not_created_or_made_complete(tmp_path,paper_journal):
    _,scope,identity=paper_journal
    path=tmp_path/'unavailable.sqlite'
    result=read_paper_references(path,scope=scope,identity=identity,start=T,end=T+timedelta(minutes=5))
    assert not path.exists()
    assert result['journal_status']=='UNKNOWN'


def test_first_holdout_access_does_not_prove_missing_search_history():
    identity={'strategy_id':'adaptive_trend_pullback','strategy_version':'1.0.0','config_fingerprint':'a'*64}
    record={**identity,'owner_id':'alice','status':'COMPLETED','method':'out_of_sample','holdout_access_count':1,'report':{}}
    class Context:
        def get_validation_run(self,rid):return deepcopy(record)
    context=Context();source={'validation_run_ids':['real-reference']}
    result=read_holdout_provenance(context,source,identity,owner_id='alice')
    assert result['status']=='UNKNOWN' and result['references']==['real-reference']
    assert read_holdout_provenance(context,source,identity,owner_id='bob')['references']==[]
    record['holdout_access_count']=2
    assert read_holdout_provenance(context,source,identity,owner_id='alice')['status']=='REUSED'


def test_malformed_historical_holdout_stays_unknown():
    identity={'strategy_id':'a','strategy_version':'v','config_fingerprint':'f'}
    class Context:
        def get_validation_run(self,rid):return {**identity,'owner_id':'alice','status':'COMPLETED','method':'out_of_sample','holdout_access_count':None,'report':None}
    assert read_holdout_provenance(Context(),{'validation_run_ids':['old']},identity,owner_id='alice')['status']=='UNKNOWN'
