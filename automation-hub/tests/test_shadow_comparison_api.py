from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.strategy_intelligence import create_router
from services.shadow_comparison_service import ShadowComparisonService
from test_shadow_router import setup, frame, T

BASE="/api/v2/strategy-intelligence/shadow"


@pytest.fixture
def comparison_client(setup):
    router,store,_,reg,clock,_=setup
    shadow=router.evaluate(reg["registration_id"],frame(),owner_id="alice")
    class Sources:
        def sources(self,*,owner_id,limit=100):
            return [{"source_id":"fixture","coverage_class":"FULL_DECISION_EVIDENCE"}] if owner_id=="alice" else []
        def read(self,source_id,*,owner_id,**kw):
            if owner_id!="alice" or source_id!="fixture": raise KeyError(source_id)
            return self.sources(owner_id=owner_id)[0],[{**deepcopy(shadow),"evidence_kind":"ACTIVE_DECISION_OBSERVATION"}]
    router.active_sources=Sources()
    router.comparison_service=ShadowComparisonService(store,source_reader=router.active_sources.read,clock=router.clock)
    app=FastAPI();app.include_router(create_router(shadow_provider=lambda:router,owner_resolver=lambda r:r.headers.get("x-test-owner")))
    body={"registration_id":reg["registration_id"],"active_source_id":"fixture","start":T.isoformat(),"end":clock[0].isoformat(),"idempotency_key":"api-comparison-key"}
    return TestClient(app),body


def test_comparison_api_real_artifact_and_authentication(comparison_client):
    client,body=comparison_client
    assert client.get(BASE+"/active-sources").status_code==401
    assert client.post(BASE+"/comparisons",json=body).status_code==401
    headers={"x-test-owner":"alice"}
    assert client.get(BASE+"/active-sources",headers=headers).json()["rows"][0]["source_id"]=="fixture"
    response=client.post(BASE+"/comparisons",json=body,headers=headers)
    assert response.status_code==201
    first=response.json(); assert first["metrics"]["matched_opportunities"]==1
    assert client.post(BASE+"/comparisons",json=body,headers=headers).json()==first
    cid=first["comparison_id"]
    assert client.get(BASE+"/comparisons/"+cid,headers=headers).json()==first
    assert len(client.get(BASE+"/comparisons",headers=headers).json()["rows"])==1
    assert client.get(BASE+"/comparisons/"+cid+"/timeline?limit=1",headers=headers).json()["rows"][0]["eligible"] is True


def test_comparison_api_tenant_and_unknown_fields(comparison_client):
    client,body=comparison_client
    alice={"x-test-owner":"alice"};bob={"x-test-owner":"bob"}
    result=client.post(BASE+"/comparisons",json=body,headers=alice).json()
    assert client.post(BASE+"/comparisons",json=body,headers=bob).status_code==404
    assert client.get(BASE+"/active-sources",headers=bob).json()["rows"]==[]
    assert client.get(BASE+"/comparisons/"+result["comparison_id"],headers=bob).status_code==404
    assert client.get(BASE+"/comparisons/"+result["comparison_id"]+"/timeline",headers=bob).status_code==404
    assert client.post(BASE+"/comparisons",json={**body,"leverage":100},headers=alice).status_code==422
    assert client.get(BASE+"/comparisons?limit=100000",headers=alice).status_code==422
    assert client.get(BASE+"/active-sources?limit=1&limit=2",headers=alice).status_code==422
    assert client.post(BASE+"/comparisons",json={**body,"end":"2030-01-01T00:00:00Z"},headers=alice).status_code==409


def test_active_source_coverage_is_record_coverage_not_profitability(comparison_client):
    client,body=comparison_client
    response=client.get(BASE+"/active-sources/fixture/coverage",params={"start":body["start"],"end":body["end"]},headers={"x-test-owner":"alice"})
    assert response.status_code==200
    assert response.json()["profitability"]=="NOT_ASSESSED"
    assert response.json()["record_count"]==1
