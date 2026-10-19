from copy import deepcopy
from datetime import timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routers.strategy_intelligence import create_router
from test_paper_reconciliation import complete,T
from test_shadow_router import setup
from services.operational_evidence import OperationalEvidenceStore
from services.paper_reconciliation_store import PaperReconciliationService


@pytest.fixture
def client(setup,complete):
    router=setup[0];router.clock=lambda:T+timedelta(minutes=10)
    class Reader:
        def sources(self, *, owner_id,**_):return [{'source_id':'owned-paper','kind':'pa-lab'}] if owner_id=='alice' else []
        def availability(self,source_id, *, owner_id):
            self.read(source_id,owner_id=owner_id)
            return {'fields':{'funding':{'status':'VERIFIED','reason':'Original fixture rates/exposure'}}}
        def read(self,source_id, *, owner_id,**_):
            if source_id!='owned-paper' or owner_id!='alice':raise KeyError(source_id)
            return deepcopy(complete)
    router.paper_reconciliation_service=PaperReconciliationService(router.store,Reader(),clock=router.clock)
    op=OperationalEvidenceStore(router.store,clock=router.clock)
    op.register(owner_id='alice',source_id='owned-shadow',scope={'timeframe':'5m','symbol':'XRPUSDT'})
    op.emit({'owner_id':'alice','source_id':'owned-shadow','event_type':'MONITORING_STARTED','occurred_at':T.isoformat(),'payload':{}},key='original-monitoring-start')
    router.operational_evidence_store=op
    app=FastAPI();app.include_router(create_router(shadow_provider=lambda:router,
        owner_resolver=lambda request:request.headers.get('x-test-owner')))
    return TestClient(app),router,complete


def test_real_reconciliation_calculation_and_immutable_api_report(client):
    http,router,bundle=client;before=deepcopy(bundle);headers={'x-test-owner':'alice'}
    root='/api/v2/strategy-intelligence/paper-reconciliation'
    body={'source_id':'owned-paper','start':T.isoformat(),'end':(T+timedelta(minutes=5)).isoformat(),'idempotency_key':'api-financial-report'}
    assert http.post(root+'/reports',json=body).status_code==401
    preview=http.post(root+'/preview',json=body,headers=headers)
    assert preview.status_code==200 and preview.json()['metrics']['net_pnl']=='3.3'
    assert preview.json()['commitment_status']=='NOT_COMMITTED'
    created=http.post(root+'/reports',json=body,headers=headers)
    assert created.status_code==201
    report=created.json();assert report['execution_authorization'] is False
    assert http.post(root+'/reports',json=body,headers=headers).json()==report
    assert http.get(root+'/reports/'+report['report_id'],headers=headers).json()['original_snapshot']==before
    assert http.get(root+'/reports/'+report['report_id'],headers={'x-test-owner':'bob'}).status_code==404
    assert http.get(root+'/reports',headers={'x-test-owner':'bob'}).json()['rows']==[]
    assert http.post(root+'/reports',json={**body,'funding':0},headers=headers).status_code==422
    assert http.post(root+'/reports',json={**body,'activate_paper':True},headers=headers).status_code==422
    assert http.get(root+'/reports?limit=500',headers=headers).status_code==422
    assert bundle==before


def test_operational_api_unknown_monitoring_and_cross_tenant_access(client):
    http,_,_=client;root='/api/v2/strategy-intelligence/operational-reliability';headers={'x-test-owner':'alice'}
    assert http.get(root+'/sources').status_code==401
    assert http.get(root+'/sources',headers=headers).json()['rows'][0]['source_id']=='owned-shadow'
    query={'start':T.isoformat(),'end':(T+timedelta(minutes=5)).isoformat()}
    report=http.get(root+'/sources/owned-shadow',params=query,headers=headers)
    assert report.status_code==200 and report.json()['worker_availability'] is None
    assert report.json()['monitoring_status']=='PARTIAL'
    assert http.get(root+'/sources/owned-shadow',params=query,headers={'x-test-owner':'bob'}).status_code==404
    assert http.get(root+'/sources',headers={'x-test-owner':'bob'}).json()['rows']==[]
