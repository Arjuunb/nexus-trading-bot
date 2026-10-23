from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routers.strategy_intelligence import create_router
from test_forward_qualification import forward
from test_shadow_router import setup

BASE='/api/v2/strategy-intelligence/forward-qualification'


@pytest.fixture
def client(forward):
    service,store,body,context=forward
    service.router.forward_qualification_service=service
    app=FastAPI()
    app.include_router(create_router(shadow_provider=lambda:service.router,
        owner_resolver=lambda request:request.headers.get('x-test-owner')))
    return TestClient(app),service,body


def test_authentication_policy_preview_commit_and_review(client):
    http,service,body=client;headers={'x-test-owner':'alice'}
    assert http.get(BASE+'/policies').status_code==401
    assert http.post(BASE+'/reports',json=body).status_code==401
    policies=http.get(BASE+'/policies',headers=headers).json()
    assert policies['rows'][0]['policy_version']=='fixture-forward.v1'
    preview=http.post(BASE+'/eligibility',json=body,headers=headers)
    assert preview.status_code==200
    assert preview.json()['commitment_status']=='NOT_COMMITTED'
    assert service.history(owner_id='alice')['rows']==[]
    response=http.post(BASE+'/reports',json=body,headers=headers)
    assert response.status_code==201
    original=response.json();rid=original['report_id']
    assert original['outcome']=='BEHAVIOUR_ONLY'
    assert original['profitability']=='NOT_ASSESSED'
    assert http.post(BASE+'/reports',json=body,headers=headers).json()==original
    assert http.get(BASE+'/reports/'+rid,headers=headers).json()['report']==original
    review=http.post(BASE+'/reports/'+rid+'/review',headers=headers,json={
        'idempotency_key':'api-human-review','decision':'ACKNOWLEDGED','reason':'Further evidence needed'})
    assert review.status_code==201
    assert review.json()['execution_authorization'] is False
    assert http.get(BASE+'/reports/'+rid,headers=headers).json()['workflow_state']=='REVIEWED'
    assert http.get(BASE+'/reports',headers=headers).json()['rows'][0]['report_id']==rid
    assert http.get(BASE+'/windows/'+original['window_id'],headers=headers).json()['window_id']==original['window_id']


def test_tenant_scoping_and_no_evidence_assertions(client):
    http,service,body=client;alice={'x-test-owner':'alice'};bob={'x-test-owner':'bob'}
    report=http.post(BASE+'/reports',json=body,headers=alice).json();rid=report['report_id']
    assert http.post(BASE+'/reports',json=body,headers=bob).status_code==404
    assert http.get(BASE+'/reports/'+rid,headers=bob).status_code==404
    assert http.get(BASE+'/windows/'+report['window_id'],headers=bob).status_code==404
    assert http.get(BASE+'/reports',headers=bob).json()['rows']==[]
    assert http.get(BASE+'/sources',headers=bob).json()['rows']==[]
    assert http.post(BASE+'/reports',json={**body,'funding':0},headers=alice).status_code==422
    assert http.post(BASE+'/reports',json={**body,'verified':True},headers=alice).status_code==422
    assert http.post(BASE+'/reports/'+rid+'/review',headers=alice,json={
        'idempotency_key':'forbidden-review','decision':'ENABLE_LIVE','reason':'forbidden'}).status_code==422
    assert http.get(BASE+'/reports?limit=5000',headers=alice).status_code==422
    assert http.get(BASE+'/reports?limit=1&limit=2',headers=alice).status_code==422


def test_operational_diagnostics_do_not_certify_postgres_workers(client):
    http,_,_=client
    result=http.get(BASE+'/operations',headers={'x-test-owner':'alice'})
    assert result.status_code==200
    gates=result.json()['gates']
    assert gates['postgresql_worker_concurrency']['status']=='BLOCKED'
    assert result.json()['production_ready'] is False
    assert result.json()['execution_authorization'] is False
