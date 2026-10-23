from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace
import pytest

from routers.strategy_intelligence import create_router
from test_shadow_router import setup


@pytest.fixture
def readiness_client(setup):
    shadow=setup[0]
    app=FastAPI()
    app.include_router(create_router(shadow_provider=lambda:shadow,
        service_provider=lambda:None,
        owner_resolver=lambda r:r.headers.get('x-test-owner'),
        operator_authorizer=lambda r:r.headers.get('x-test-role')=='operator'))
    return TestClient(app),shadow


def test_readiness_authentication_rbac_and_no_control_endpoint(readiness_client):
    c,_=readiness_client;url='/api/v2/strategy-intelligence/deployment-readiness'
    assert c.get(url).status_code==401
    assert c.get(url,headers={'x-test-owner':'alice','x-test-role':'viewer'}).status_code==403
    h={'x-test-owner':'alice','x-test-role':'operator'}
    response=c.get(url,headers=h);assert response.status_code==200
    assert response.headers['cache-control']=='no-store'
    assert response.json()['checks']['POSTGRES_READY']['status']=='BLOCKED'
    assert response.json()['execution_authorization'] is False
    assert c.post(url,json={'activate':True},headers=h).status_code==405


def test_readiness_never_exposes_other_tenant_artifacts_or_secrets(readiness_client,monkeypatch):
    c,_=readiness_client
    monkeypatch.setenv('NEXUS_RESEARCH_WORKER_DSN','postgresql://secret:password@private/db')
    monkeypatch.setenv('HUB_EXCHANGE_API_KEY','PRIVATE_SENTINEL')
    h={'x-test-owner':'bob','x-test-role':'operator'}
    r=c.get('/api/v2/strategy-intelligence/deployment-readiness',headers=h)
    assert r.status_code==200 and 'PRIVATE_SENTINEL' not in r.text and 'password' not in r.text
    assert 'alice' not in r.text and 'source_id' not in r.text


def test_readiness_probe_does_not_write_decisions_or_financial_tables(readiness_client):
    c,shadow=readiness_client;db=shadow.store._store._db
    def snapshot():return list(db.iterdump())
    before=snapshot()
    c.get('/api/v2/strategy-intelligence/deployment-readiness',headers={'x-test-owner':'alice','x-test-role':'operator'})
    assert snapshot()==before
