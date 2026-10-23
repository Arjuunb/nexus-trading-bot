from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routers.strategy_intelligence import create_router
from test_active_evidence_capture import captured,T
from datetime import timedelta

BASE="/api/v2/strategy-intelligence/shadow/active-sources"


@pytest.fixture
def active_client(captured):
    class Shadow: pass
    router=Shadow();router.active_sources=captured[-2]
    app=FastAPI();app.include_router(create_router(shadow_provider=lambda:router,owner_resolver=lambda r:r.headers.get("x-test-owner")))
    return TestClient(app),captured[-1],{"start":T.isoformat(),"end":(T+timedelta(minutes=5)).isoformat()}


def test_health_api_authentication_tenant_and_real_fields(active_client):
    client,sid,query=active_client
    assert client.get(BASE+"/"+sid+"/health",params=query).status_code==401
    assert client.get(BASE+"/"+sid+"/health",params=query,headers={"x-test-owner":"bob"}).status_code==404
    response=client.get(BASE+"/"+sid+"/health",params=query,headers={"x-test-owner":"alice"})
    assert response.status_code==200
    assert response.json()["status"]=="VERIFIED"
    assert response.json()["field_matrix"]["configuration_reference"]=="PRESENT_VERIFIED"
    assert response.headers["cache-control"]=="no-store"


def test_active_timeline_is_bounded_and_owner_scoped(active_client):
    client,sid,query=active_client
    endpoint=BASE+"/"+sid+"/decisions"
    alice={"x-test-owner":"alice"}
    response=client.get(endpoint,params={**query,"limit":1},headers=alice)
    assert response.status_code==200
    row=response.json()["rows"][0]
    assert row["configuration_reference"].startswith("active-config:")
    assert row["persistence_status"]=="COMMITTED"
    assert client.get(endpoint,params=query,headers={"x-test-owner":"bob"}).status_code==404
    assert client.get(endpoint,params={**query,"limit":100000},headers=alice).status_code==422
    assert client.get(endpoint,params={**query,"start":"2030-01-01T00:00:00Z"},headers=alice).status_code==409
