"""Controlled evidence fixtures, never claims about actual trading returns."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import sqlite3

import pytest

from services.forward_qualification import (
    EVIDENCE_TAXONOMY, ForwardQualificationPolicy, ForwardQualificationService,
    calculate_observation_window, evaluate_forward_window, assess_paper_evidence, decision_facts,
)
from services.forward_qualification_store import ForwardQualificationStore
from services.shadow_observation_store import ShadowObservationStore
from services.shadow_research import ShadowResearchStore
from test_shadow_router import setup, T, frame
from test_active_evidence_capture import captured, financial


@pytest.fixture
def decision(setup):
    router,_,_,reg,_,_=setup
    return router.evaluate(reg['registration_id'],frame(),owner_id='alice')


def window(decision, *, count=4, rows=None, events=None):
    values=[]
    for i in range(count):
        r=deepcopy(decision);at=T+timedelta(minutes=5*i)
        r.update(decision_id=f'fixture-{i}',candle_close_timestamp=at.isoformat(),
                 decision_timestamp=(at+timedelta(seconds=1)).isoformat(),
                 evidence_commit_recorded_at=(at+timedelta(seconds=2)).isoformat())
        r['canonical_candle']['open_timestamp']=(at-timedelta(minutes=5)).isoformat()
        values.append(r)
    return calculate_observation_window(values if rows is None else rows,owner_id='alice',
        evidence_kind='FORWARD_SHADOW_DECISION',timeframe='5m',start=T,
        end=T+timedelta(minutes=5*count),events=events or [])


def research_policy():
    return replace(ForwardQualificationPolicy(),policy_version='fixture-forward.v1',
                   minimum_duration_seconds=300,minimum_opportunities=1)


def test_taxonomy_does_not_relabel_shadow_decisions_as_trades():
    assert len(EVIDENCE_TAXONOMY)==8
    assert EVIDENCE_TAXONOMY['FORWARD_SHADOW_DECISION']['profitability_eligible'] is False
    assert EVIDENCE_TAXONOMY['ACTIVE_DECISION_OBSERVATION']['profitability_eligible'] is False
    assert EVIDENCE_TAXONOMY['FORWARD_PAPER_TRADE']['execution_basis']=='ISOLATED_EXECUTED_PAPER_EPISODES'


def test_policy_identity_is_deterministic_and_versioned():
    p=ForwardQualificationPolicy()
    assert p.fingerprint==ForwardQualificationPolicy().fingerprint
    assert p.fingerprint!=replace(p,minimum_observation_coverage=.99).fingerprint


def test_short_forward_window_is_insufficient_not_profitable(decision):
    report=evaluate_forward_window(window(decision,count=1),policy=ForwardQualificationPolicy())
    assert report['outcome']=='FORWARD_EVIDENCE_INSUFFICIENT'
    assert report['profitability']=='NOT_ASSESSED'
    assert 'LOW_SIGNAL_SAMPLE_SIZE' in report['warning_codes']


def test_complete_decisions_without_trade_outcomes_are_behaviour_only(decision):
    report=evaluate_forward_window(window(decision),policy=research_policy())
    assert report['outcome']=='BEHAVIOUR_ONLY'
    assert report['profitability']=='NOT_ASSESSED'
    assert report['paper_trial_readiness']!='READY_FOR_HUMAN_REVIEW'
    assert 'holdout_provenance' in report['unknown_gates']


def test_missing_opportunities_do_not_become_neutral(decision):
    w=window(decision,rows=[decision])
    assert w['coverage']['expected_opportunities']==4
    assert w['coverage']['missing_decisions']==3
    assert w['coverage']['decision_evaluation_coverage']==.25
    assert w['behaviour']['signal_count']==1
    assert w['reliability']['worker_availability'] is None
    assert evaluate_forward_window(w,policy=research_policy())['outcome']=='FORWARD_EVIDENCE_UNRELIABLE'


@pytest.mark.parametrize('status', ['BLOCKED_STALE_DATA','EVALUATION_ERROR','ACTIVE_PAUSED'])
def test_blocks_errors_and_inactivity_are_separate(decision,status):
    record={**decision,'status':status,'evidence_completeness':'PARTIAL'}
    w=window(decision,count=1,rows=[record])
    assert w['coverage']['eligible_decisions']==0
    assert w['coverage']['blocked_decisions']==1
    assert w['behaviour']['signal_count']==0
    assert w['reliability']['evaluation_error_rate']==(1 if status=='EVALUATION_ERROR' else 0)
    assert w['reliability']['stale_data_rate']==(1 if status=='BLOCKED_STALE_DATA' else 0)
    assert w['coverage']['inactive_opportunities']==(1 if status=='ACTIVE_PAUSED' else 0)


def test_all_neutral_is_not_a_signal_sample(decision):
    neutral={**decision,'status':'NO_SIGNAL','signal_direction':None}
    w=window(neutral)
    assert w['behaviour']['signal_frequency']==0
    assert w['behaviour']['long_share'] is None
    assert evaluate_forward_window(w,policy=research_policy())['profitability']=='NOT_ASSESSED'


def test_conflicting_decisions_and_categories_fail_closed(decision):
    conflicting={**decision,'status':'SIGNAL_SHORT'}
    w=window(decision,count=1,rows=[decision,conflicting])
    assert w['coverage']['conflicting_opportunities']==1
    assert evaluate_forward_window(w,policy=research_policy())['outcome']=='FORWARD_EVIDENCE_UNRELIABLE'
    w=window(decision,count=1,rows=[{**decision,'evidence_kind':'HISTORICAL_BACKTEST'}])
    assert w['coverage']['eligible_decisions']==0
    assert 'SHADOW_EVIDENCE_CATEGORY_MISMATCH' in w['exclusion_reasons']


def test_foreign_tenant_is_never_eligible(decision):
    w=window(decision,count=1,rows=[{**decision,'tenant_id':'bob'}])
    assert w['coverage']['eligible_decisions']==0
    assert 'TENANT_MISMATCH' in w['exclusion_reasons']


def test_incomplete_or_uncommitted_decision_is_excluded(decision):
    bad={**decision,'evidence_commit_recorded_at':None}
    assert window(decision,count=1,rows=[bad])['coverage']['eligible_decisions']==0


def test_required_candle_context_gate_cannot_pass_without_context_identity(decision):
    report=evaluate_forward_window(window({**decision,'context_hash':None}),policy=research_policy())
    checks={item['name']:item['status'] for item in report['checks']}
    assert checks['candle_context']=='FAIL'


def test_calculation_snapshot_is_bounded_without_changing_classification(decision):
    from services.strategy_identity import canonical_configuration_json
    facts=decision_facts(decision)
    assert len(canonical_configuration_json(facts).encode())*2016 < ForwardQualificationStore.max_window_bytes
    original=window(decision,count=1,rows=[decision])
    projected=window(decision,count=1,rows=[facts])
    assert original['coverage']==projected['coverage']
    assert original['behaviour']==projected['behaviour']
    assert facts['context_hash']==decision['context_hash']


def test_unknown_funding_blocks_paper_outcome_qualification():
    source={'evidence_kind':'FORWARD_PAPER_TRADE','account_id':'one','authority_status':'COMPLETE',
            'journal_status':'COMPLETE','rows':[{'episode_id':'e','net_pnl':'1.23','fees':'0.10',
            'fees_coverage':'OBSERVED','funding':None,'funding_coverage':'UNKNOWN'}]}
    result=assess_paper_evidence(source)
    assert result['cost_coverage']['funding']=='UNKNOWN'
    assert result['qualification_eligible'] is False
    assert result['profitability']=='NOT_ASSESSED'


def test_paper_evidence_cannot_mix_accounts_or_categories():
    assert assess_paper_evidence({'evidence_kind':'HYPOTHETICAL_SHADOW_OUTCOME','rows':[]})['qualification_eligible'] is False
    evidence={'evidence_kind':'FORWARD_PAPER_TRADE','account_id':'one','rows':[{'account_id':'two','episode_id':'e'}]}
    assert 'PAPER_ACCOUNT_MISMATCH' in assess_paper_evidence(evidence)['blocking_reasons']


@pytest.fixture
def forward(setup):
    router,store,raw,reg,clock,_=setup
    router.evaluate(reg['registration_id'],frame(),owner_id='alice')
    clock[0]=T+timedelta(minutes=5)
    journal=ForwardQualificationStore(store)
    service=ForwardQualificationService(router,journal,policies=[research_policy()])
    request={'source_kind':'FORWARD_SHADOW_DECISION','source_id':reg['registration_id'],
             'start':T.isoformat(),'end':clock[0].isoformat(),
             'policy_version':'fixture-forward.v1','idempotency_key':'forward-fixture-1'}
    return service,journal,request,setup


def test_report_retry_immutability_and_human_review(forward):
    svc,store,request,setup=forward
    original=svc.create(request,owner_id='alice')
    assert svc.create(request,owner_id='alice')==original
    assert svc.get(original['report_id'],owner_id='bob') is None
    review=svc.review(original['report_id'],{'idempotency_key':'review-fixture-1',
        'decision':'ACKNOWLEDGED','reason':'Behaviour evidence only; obtain missing outcomes'},owner_id='alice')
    assert review['reviewer_id']=='alice'
    assert svc.get(original['report_id'],owner_id='alice')['workflow_state']=='REVIEWED'
    assert store.report(original['report_id'],owner_id='alice')==original
    with pytest.raises(sqlite3.IntegrityError):
        setup[2]._db.execute("UPDATE forward_qualification_reports SET payload_json='{}'")


def test_policy_version_cannot_silently_change(forward):
    svc,store,request,_=forward
    svc.create(request,owner_id='alice')
    with pytest.raises(ValueError,match='POLICY_VERSION_CONFLICT'):
        ForwardQualificationService(svc.router,store,policies=[replace(research_policy(),minimum_opportunities=50)])


def test_changed_request_under_same_key_is_a_conflict(forward):
    svc,_,request,_=forward
    svc.create(request,owner_id='alice')
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):
        svc.create({**request,'end':(T+timedelta(minutes=10)).isoformat()},owner_id='alice')


def test_restart_preserves_report_and_review_without_financial_mutation(forward,captured):
    svc,store,request,setup=forward
    account=captured[0]
    before=financial(account)
    original=svc.create(request,owner_id='alice')
    restarted=ForwardQualificationService(svc.router,ForwardQualificationStore(
        ShadowObservationStore(ShadowResearchStore(setup[2].path))),policies=[research_policy()])
    assert restarted.create(request,owner_id='alice')==original
    restarted.review(original['report_id'],{'idempotency_key':'restart-review-1','decision':'ARCHIVED','reason':'Preserve original evidence'},owner_id='alice')
    assert financial(account)==before
    assert setup[2].table_counts()['shadow_orders']==0
    assert setup[2].table_counts()['shadow_fills']==0


def test_overlapping_windows_preserve_identity_and_do_not_sum_samples(forward):
    svc,_,request,setup=forward
    first=svc.create(request,owner_id='alice')
    setup[4][0]=T+timedelta(minutes=10)
    second=svc.create({**request,'end':setup[4][0].isoformat(),'idempotency_key':'overlap-request-2'},owner_id='alice')
    assert second['window_id']!=first['window_id']
    assert first['window_id'] in second['overlapping_window_ids']
    history=svc.history(owner_id='alice',source_id=request['source_id'])
    assert history['independent_samples_claimed'] is False
    assert 'OVERLAPPING_OBSERVATION_WINDOWS' in history['warning_codes']
    assert 'aggregate_signal_count' not in history


def test_active_source_can_be_qualified_without_rerunning_strategy(captured,forward):
    svc,_,_,_=forward
    svc.router.active_sources=captured[-2]
    report=svc.create({'source_kind':'ACTIVE_DECISION_OBSERVATION','source_id':captured[-1],
        'start':T.isoformat(),'end':(T+timedelta(minutes=5)).isoformat(),
        'policy_version':'fixture-forward.v1','idempotency_key':'native-active-report'},owner_id='alice')
    assert report['identity']['strategy_version']=='1.1.0'
    assert report['outcome']=='BEHAVIOUR_ONLY'
    assert report['coverage']['active_decision_evidence_coverage']==1


def test_source_authorization_and_forbidden_actions(forward):
    svc,_,request,_=forward
    with pytest.raises(KeyError): svc.create(request,owner_id='bob')
    for field in ('leverage','enable_live','place_order','promote_champion'):
        with pytest.raises(ValueError,match='INVALID_FORWARD_REQUEST'):
            svc.create({**request,field:True},owner_id='alice')


def test_matching_requires_the_same_market_candle(forward,captured):
    svc,_,request,_=forward
    svc.router.active_sources=captured[-2]
    result=svc.create({**request,'active_source_id':captured[-1]},owner_id='alice')
    assert result['outcome']=='NOT_COMPARABLE'
    assert result['matched_comparison']['metrics']['matched_opportunities']==0
    assert any(x.startswith('SCOPE_MISMATCH') for x in result['matched_comparison']['exclusion_reasons'])


def test_window_keeps_configuration_versions_separate(decision):
    rows=[deepcopy(decision),deepcopy(decision)]
    rows[1].update(candle_close_timestamp=(T+timedelta(minutes=5)).isoformat(),
                   decision_timestamp=(T+timedelta(minutes=5,seconds=1)).isoformat(),
                   strategy_version='another-version')
    rows[1]['canonical_candle']['open_timestamp']=T.isoformat()
    w=window(decision,count=2,rows=rows)
    assert evaluate_forward_window(w,policy=research_policy())['outcome']=='FORWARD_EVIDENCE_UNRELIABLE'


def test_window_backup_restore_preserves_policy_report_and_original_decisions(forward,tmp_path):
    svc,store,request,setup=forward
    original=svc.create(request,owner_id='alice')
    path=tmp_path/'restored-shadow.db'
    db=sqlite3.connect(path);setup[2]._db.backup(db);db.close()
    restored=ForwardQualificationStore(ShadowObservationStore(ShadowResearchStore(path)))
    assert restored.report(original['report_id'],owner_id='alice')==original
    assert restored.window(original['window_id'],owner_id='alice')['evidence_hash']==original['evidence_hash']
    assert restored.shadow._store._db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert restored.shadow._store._db.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert restored.shadow.decision_at(request['source_id'],T.isoformat(),owner_id='alice') is not None


def test_missing_active_record_is_not_a_neutral_pair(forward):
    svc,_,request,_=forward
    class EmptySource:
        def read(self,*args,**kwargs):
            return {'coverage_class':'PARTIAL_DECISION_EVIDENCE'},[]
    svc.router.active_sources=EmptySource()
    result=svc.create({**request,'active_source_id':'no-active'},owner_id='alice')
    assert result['matched_comparison']['timeline'][0]['active_status']=='NO_DECISION_RECORDED'
    assert result['matched_comparison']['metrics']['both_no_signal']==0


def test_changed_record_cannot_claim_frozen_registration_identity(forward,monkeypatch):
    svc,_,request,_=forward
    original=svc.router.store.comparison_inputs
    def changed(*args,**kwargs):
        rows,events=original(*args,**kwargs)
        rows[0]['config_fingerprint']='f'*64
        return rows,events
    monkeypatch.setattr(svc.router.store,'comparison_inputs',changed)
    result=svc.create(request,owner_id='alice')
    assert result['outcome']=='FORWARD_EVIDENCE_UNRELIABLE'
    assert 'SOURCE_IDENTITY_MISMATCH' in result['window_exclusion_reasons']


def test_review_is_not_trading_authorization_and_is_owner_scoped(forward):
    svc,_,request,_=forward
    report=svc.create(request,owner_id='alice')
    body={'idempotency_key':'human-review-1','decision':'ACKNOWLEDGED','reason':'Review only'}
    with pytest.raises(KeyError): svc.review(report['report_id'],body,owner_id='bob')
    assert svc.review(report['report_id'],body,owner_id='alice')['execution_authorization'] is False
    assert svc.review(report['report_id'],body,owner_id='alice')['reviewer_id']=='alice'
    with pytest.raises(ValueError,match='INVALID_FORWARD_REVIEW'):
        svc.review(report['report_id'],{**body,'decision':'ENABLE_PAPER'},owner_id='alice')


def test_cancellation_or_unresolved_revision_cannot_supply_matched_evidence(forward):
    svc,_,request,_=forward
    svc.router.store.event(request['source_id'],owner_id='alice',action='CANDLE_REVISION_CONFLICT',
        payload={'original_decision_id':svc.router.store.decisions(request['source_id'],owner_id='alice')[0]['decision_id']})
    result=svc.create(request,owner_id='alice')
    assert result['outcome']=='FORWARD_EVIDENCE_UNRELIABLE'


def test_gap_events_outside_window_are_not_window_reliability(decision):
    outside={'action':'OBSERVATION_GAP','timestamp':(T+timedelta(days=5)).isoformat(),
        'payload':{'after':(T+timedelta(days=5)).isoformat(),'until':(T+timedelta(days=6)).isoformat()}}
    assert window(decision,events=[outside])['reliability']['recorded_gap_events']==0


def test_policy_reassessment_is_not_an_additional_observation_sample(forward):
    svc,store,request,_=forward
    first=svc.create(request,owner_id='alice')
    second_policy=replace(research_policy(),policy_version='fixture-forward.v2',minimum_signal_sample=50)
    second=ForwardQualificationService(svc.router,store,policies=[second_policy]).create(
        {**request,'policy_version':second_policy.policy_version,'idempotency_key':'policy-comparison-2'},owner_id='alice')
    assert first['window_id']==second['window_id']
    assert first['report_id']!=second['report_id']
    assert 'REASSESSED_OBSERVATION_WINDOW' in svc.history(owner_id='alice')['warning_codes']


def test_committed_window_can_reproduce_its_original_calculation(forward):
    svc,store,request,_=forward
    original=svc.create(request,owner_id='alice')
    w=store.window(original['window_id'],owner_id='alice')
    inputs=w['inputs']
    repeated=calculate_observation_window(inputs['decisions'],owner_id='alice',
        evidence_kind=original['source_kind'],timeframe=w['timeframe'],start=w['start'],end=w['end'],
        events=inputs['audit_events'],expected_identity=w['identity'],source_errors=inputs['source_errors'])
    result=evaluate_forward_window(repeated,policy=research_policy(),paper=inputs['paper'],holdout=inputs['holdout'])
    assert result['outcome']==original['outcome']
    assert result['coverage']==original['coverage']
    assert result['checks']==original['checks']
    assert original['software_revision']['component_hashes']


def test_conflicting_report_under_same_logical_result_is_rejected(forward):
    svc,store,request,_=forward
    original=svc.create(request,owner_id='alice')
    w=store.window(original['window_id'],owner_id='alice')
    with pytest.raises(ValueError,match='FORWARD_REPORT_CONFLICT'):
        store.commit(w,{**original,'warning_codes':['FABRICATED_VERIFICATION']},request={'different':'binding'},key='conflicting-result-key')


def test_financial_database_is_not_a_qualification_storage_target(tmp_path):
    from data.journal_store import JournalStore
    journal=JournalStore(tmp_path/'financial-journal.sqlite')
    tables=journal._c.execute('SELECT name FROM sqlite_master').fetchall()
    class Raw:
        _db=journal._c
        _lock=journal._lock
    class Facade:
        _store=Raw()
    with pytest.raises(ValueError,match='FINANCIAL_DATABASE_FORBIDDEN'): ForwardQualificationStore(Facade())
    assert journal._c.execute('SELECT name FROM sqlite_master').fetchall()==tables


def test_a_conflicting_decision_does_not_erase_known_candle_receipt(decision):
    w=window(decision,count=1,rows=[decision,{**decision,'status':'SIGNAL_SHORT'}])
    assert w['coverage']['received_finalized_candles']==1
    assert w['coverage']['eligible_decisions']==0


def test_report_commit_failure_rolls_back_window_and_replays_once(forward):
    svc,store,request,setup=forward
    raw=setup[2]._db
    def deny(action,name,*_):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and name=='forward_qualification_requests' else sqlite3.SQLITE_OK
    raw.set_authorizer(deny)
    try:
        with pytest.raises(sqlite3.DatabaseError): svc.create(request,owner_id='alice')
    finally: raw.set_authorizer(None)
    assert raw.execute('SELECT COUNT(*) FROM forward_qualification_reports').fetchone()[0]==0
    assert raw.execute('SELECT COUNT(*) FROM forward_observation_windows').fetchone()[0]==0
    first=svc.create(request,owner_id='alice')
    assert svc.create(request,owner_id='alice')==first


def test_report_computation_concurrency_is_bounded(forward,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    svc,_,request,_=forward
    original=svc.router.store.comparison_inputs
    entered=threading.Event();release=threading.Event()
    def delayed(*args,**kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(svc.router.store,'comparison_inputs',delayed)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first=executor.submit(svc.preview,request,owner_id='alice')
        try:
            assert entered.wait(2)
            with pytest.raises(ValueError,match='FORWARD_ASSESSMENT_BUSY'):
                svc.preview(request,owner_id='alice')
        finally: release.set()
        assert first.result(timeout=5)['commitment_status']=='NOT_COMMITTED'


def test_partial_additive_migration_recovers_without_erasing_existing_evidence(tmp_path):
    raw=ShadowResearchStore(tmp_path/'upgrade.sqlite')
    shadow=ShadowObservationStore(raw)
    before=raw.table_counts()
    def interrupt(action,name,*_):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_TABLE and name=='forward_qualification_reports' else sqlite3.SQLITE_OK
    raw._db.set_authorizer(interrupt)
    try:
        with pytest.raises(sqlite3.DatabaseError): ForwardQualificationStore(shadow)
    finally: raw._db.set_authorizer(None)
    assert raw._db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='forward_observation_windows'").fetchone()[0]==1
    ForwardQualificationStore(shadow)
    ForwardQualificationStore(shadow)
    assert raw.table_counts()==before
    assert raw._db.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert raw._db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'forward_%'").fetchone()[0]==12


def test_independent_connections_commit_one_logical_report(forward):
    from concurrent.futures import ThreadPoolExecutor
    svc,store,request,context=forward
    report=svc.create(request,owner_id='alice')
    window_record=store.window(report['window_id'],owner_id='alice')
    second=ForwardQualificationStore(ShadowObservationStore(ShadowResearchStore(context[2].path)))
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(lambda target:target.commit(window_record,report,
            request={'matching':'binding'},key='concurrent-new-binding'),(store,second)))
    assert results==[report,report]
    db=context[2]._db
    assert db.execute('SELECT COUNT(*) FROM forward_qualification_reports').fetchone()[0]==1
    assert db.execute('SELECT COUNT(*) FROM forward_qualification_requests').fetchone()[0]==2
    assert db.execute('SELECT COUNT(*) FROM forward_qualification_events').fetchone()[0]==3
