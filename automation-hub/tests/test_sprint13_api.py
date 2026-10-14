import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers.strategy_intelligence import create_router


@pytest.fixture
def client():
    app=FastAPI();app.include_router(create_router(owner_resolver=lambda r:r.headers.get('x-test-owner'),
        operator_authorizer=lambda r:r.headers.get('x-test-role')=='operator'))
    return TestClient(app)


def test_operator_readiness_is_authenticated_read_only_and_no_store(client):
    url='/api/v2/strategy-intelligence/remote-staging-readiness'
    assert client.get(url).status_code==401
    assert client.get(url,headers={'x-test-owner':'alice'}).status_code==403
    h={'x-test-owner':'alice','x-test-role':'operator'};r=client.get(url,headers=h)
    assert r.status_code==200 and r.headers['cache-control']=='no-store'
    assert r.json()['remote_staging']=='NOT_READY'
    assert client.post(url,headers=h,json={'deploy':True}).status_code==405


def test_diagnostics_owned_by_other_user_are_not_exposed(client,tmp_path,monkeypatch):
    path=tmp_path/'diagnostics.json';path.write_text(json.dumps({'backup':{'owner_id':'alice','recorded_at':'2026-10-13T00:00:00+00:00','verified':True,'password':'PRIVATE_SENTINEL'}}))
    monkeypatch.setenv('NEXUS_STAGING_DIAGNOSTICS_FILE',str(path))
    r=client.get('/api/v2/strategy-intelligence/remote-staging-readiness',headers={'x-test-owner':'bob','x-test-role':'operator'})
    assert r.status_code==200 and 'alice' not in r.text and 'PRIVATE_SENTINEL' not in r.text and str(path) not in r.text
    assert r.json()['gates']['BACKUP_FRESHNESS']['status']=='UNKNOWN'

