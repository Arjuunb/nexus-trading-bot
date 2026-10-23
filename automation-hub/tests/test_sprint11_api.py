from datetime import timedelta
from test_integrity_operations_api import client
from test_shadow_router import setup
from test_paper_reconciliation import complete,T

from services.local_readiness import LocalReadinessStore


def test_readiness_api_is_owned_read_only_and_does_not_authorize_execution(client):
    http,router,_=client;store=LocalReadinessStore(router.store)
    report=store.commit(owner_id='alice',environment_id='local:isolated-test',software_revision='fixture',
        database_schema_version='workers.2',worker_backend='sqlite',start=T,end=T+timedelta(minutes=1),
        reviewer='test-reviewer',gates={})
    base='/api/v2/strategy-intelligence/local-readiness'
    assert http.get(base).status_code==401
    rows=http.get(base,headers={'x-test-owner':'alice'}).json()['rows']
    assert rows==[report]
    assert http.get(base+'/'+report['assessment_id'],headers={'x-test-owner':'bob'}).status_code==404
    assert http.get(base,headers={'x-test-owner':'bob'}).json()['rows']==[]
    assert http.post(base,json={'status':'LOCAL_VERIFIED','activate':True},headers={'x-test-owner':'alice'}).status_code==405
    assert http.get(base+'?limit=201',headers={'x-test-owner':'alice'}).status_code==422


def test_native_lineage_and_resilience_are_read_only_and_fail_closed(client):
    http,_,bundle=client;base='/api/v2/strategy-intelligence';headers={'x-test-owner':'alice'}
    args={'start':T.isoformat(),'end':(T+timedelta(minutes=5)).isoformat()}
    result=http.get(base+'/paper-reconciliation/sources/owned-paper/lineage',params=args,headers=headers)
    assert result.status_code==200
    assert result.json()['total_episodes']==1
    assert result.json()['configuration_evidence']==bundle['configuration_evidence']
    assert result.json()['execution_authorization'] is False
    assert http.get(base+'/paper-reconciliation/sources/owned-paper/lineage',params=args,headers={'x-test-owner':'bob'}).status_code==404
    health=http.get(base+'/operational-resilience/health',headers=headers)
    assert health.status_code==200 and health.json()['spool']['status']=='UNKNOWN'
    assert health.json()['feed_soak']['status']=='BLOCKED'
    assert http.get(base+'/operational-resilience/health').status_code==401


def test_paper_review_is_explicit_owned_idempotent_and_not_activation(client):
    http,_,_=client;root='/api/v2/strategy-intelligence/paper-reconciliation';headers={'x-test-owner':'alice'}
    report=http.post(root+'/reports',headers=headers,json={'source_id':'owned-paper','start':T.isoformat(),
        'end':(T+timedelta(minutes=5)).isoformat(),'idempotency_key':'report-for-review'}).json()
    url=root+'/reports/'+report['report_id']+'/review'
    body={'decision':'REQUEST_MORE_EVIDENCE','reason':'Need longer original observation period','idempotency_key':'human-review-one'}
    assert http.post(url,json=body).status_code==401
    reviewed=http.post(url,json=body,headers=headers)
    assert reviewed.status_code==200,reviewed.text
    assert reviewed.json()['reviewer_id']=='alice' and reviewed.json()['execution_authorization'] is False
    assert http.post(url,json=body,headers=headers).json()==reviewed.json()
    assert http.post(url,json=body,headers={'x-test-owner':'bob'}).status_code==404
    assert http.post(url,json={**body,'activate':True},headers=headers).status_code==422
