from datetime import datetime,timedelta,timezone

import pytest

from bot.types import Bar
from data.historical import canonical_candle_manifest
from data.journal_store import JournalStore
from services.research_dataset_retention import ResearchDatasetRetention
from services.research_jobs import ResearchJobService
from test_research_jobs import _request


def fixture(tmp_path):
    context=JournalStore(tmp_path/"research.sqlite").context
    job=ResearchJobService(context).create(_request(),owner_id="alice",scope={})
    start=datetime(2025,1,1,tzinfo=timezone.utc)
    bars=[Bar(start+timedelta(minutes=i*5),10,12,9,11,100) for i in range(10)]
    manifest=canonical_candle_manifest(bars,symbol="TESTUSDT",timeframe="5m",provider="synthetic-test-fixture",venue="binance_usdm",market_type="perpetual")
    return ResearchDatasetRetention(context),job,bars,manifest


def test_retained_exact_revision_reconstructs_and_is_owner_scoped(tmp_path):
    retention,job,bars,manifest=fixture(tmp_path)
    first=retention.retain(job,manifest,bars)
    assert first["status"]=="RETAINED"
    assert first==retention.retain(job,manifest,bars)
    recovered=retention.load(job["job_id"],owner_id="alice")
    assert recovered["bars"]==bars
    assert recovered["content_hash"]==first["content_hash"]
    assert retention.load(job["job_id"],owner_id="bob") is None
    assert recovered["reconstruction_status"]=="RECONSTRUCTABLE"


def test_changed_input_cannot_overwrite_original_revision(tmp_path):
    retention,job,bars,manifest=fixture(tmp_path)
    first=retention.retain(job,manifest,bars)
    changed=list(bars); changed[-1]=Bar(bars[-1].timestamp,10,12,9,11.1,100)
    with pytest.raises(ValueError,match="DATASET_RETENTION_CONFLICT"):
        retention.retain(job,manifest,changed)
    assert retention.load(job["job_id"],owner_id="alice")["content_hash"]==first["content_hash"]


def test_retention_limits_fail_closed_no_silent_expiry(tmp_path):
    retention,job,bars,manifest=fixture(tmp_path)
    retention.max_payload_bytes=100
    with pytest.raises(ValueError,match="DATASET_RETENTION_LIMIT"):
        retention.retain(job,manifest,bars)
    assert retention.load(job["job_id"],owner_id="alice") is None


def test_input_retention_is_not_complete_validation_evidence(tmp_path):
    retention,job,bars,manifest=fixture(tmp_path)
    retention.retain(job,manifest,bars)
    assert retention.context.get_research_job(job["job_id"],owner_id="alice")["status"]=="QUEUED"
    assert retention.context.validation_runs(filters={"owner_id":"alice"})==[]


def test_retention_used_by_real_research_bridge(tmp_path,monkeypatch):
    from services import strategy_presets
    from data import market_data
    retention,job,bars,manifest=fixture(tmp_path)
    monkeypatch.setattr(market_data,"get_bars",lambda *a,**kw:(bars,"synthetic-test-fixture"))
    monkeypatch.setattr(strategy_presets,"_run_on",lambda *a,**kw:{})
    service=ResearchJobService(retention.context)
    result=service.run(job["job_id"],owner_id="alice")
    assert result["status"]=="COMPLETED"
    assert result["result"]["dataset"]["retention"]["status"]=="RETAINED"
    assert retention.load(job["job_id"],owner_id="alice")["bars"]==bars
    monkeypatch.setattr(market_data,"get_bars",lambda *a,**kw:pytest.fail("Retained revision must not be replaced by a provider fetch"))
    replay=service._execute(service.get(job["job_id"],owner_id="alice"))
    assert replay["dataset"]["retention"]==result["result"]["dataset"]["retention"]
    assert replay["metrics"]==result["result"]["metrics"]


def test_dataset_metadata_api_auth_unknown_revision_and_integrity(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.strategy_intelligence import create_router
    from services.strategy_intelligence_service import StrategyIntelligenceService
    retention,job,bars,manifest=fixture(tmp_path)
    intelligence=StrategyIntelligenceService(retention.context.journal)
    app=FastAPI()
    app.include_router(create_router(service_provider=lambda:intelligence,owner_resolver=lambda r:r.headers.get("x-test-owner")))
    client=TestClient(app,raise_server_exceptions=False)
    path="/api/v2/strategy-intelligence/research-jobs/"+job["job_id"]+"/dataset"
    assert client.get(path).status_code==401
    assert client.get(path,headers={"x-test-owner":"bob"}).status_code==404
    headers={"x-test-owner":"alice"}
    assert client.get(path,headers=headers).json()["reconstruction_status"]=="UNKNOWN"
    retention.retain(job,manifest,bars)
    response=client.get(path,headers=headers)
    assert response.status_code==200
    assert response.json()["status"]=="HASH_VERIFIED"
    assert "bars" not in response.json()
    def corrupt(*args,**kw): raise ValueError("DATASET_REVISION_MISMATCH")
    monkeypatch.setattr(ResearchDatasetRetention,"load",corrupt)
    assert client.get(path,headers=headers).status_code==409
    assert client.get(path,headers=headers).json()["detail"]=="DATASET_REVISION_MISMATCH"
