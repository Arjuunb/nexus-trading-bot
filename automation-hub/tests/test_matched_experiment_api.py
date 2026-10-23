from fastapi import FastAPI
from fastapi.testclient import TestClient


def _candidate(fingerprint):
    return {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
            "config_fingerprint": fingerprint * 64, "frozen_config": {"ema": 20},
            "dataset_identity": "xrp:5m:dataset", "data_revision": "revision-1", "symbol": "XRPUSDT",
            "venue": "binance", "market_type": "spot", "timeframe": "5m", "execution_mode": "backtest",
            "evidence_quality": "UNKNOWN", "scope": {"venue": "binance", "market_type": "spot", "symbol": "XRPUSDT",
            "timeframe": "5m", "execution_mode": "backtest", "dataset_identity": "xrp:5m:dataset", "data_revision": "revision-1",
            "funding_coverage": "COMPLETE", "slippage_model": "observed", "period_start": "a", "period_end": "b"}}


def test_matched_experiment_api_is_owner_scoped_and_idempotent(tmp_path):
    from data.journal_store import JournalStore
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from routers.strategy_intelligence import create_router
    intelligence = StrategyIntelligenceService(JournalStore(str(tmp_path / "journal.sqlite")))
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: intelligence, manager_provider=lambda: None,
                                     owner_resolver=lambda _request: "owner"))
    client = TestClient(app)
    first = client.post("/api/v2/strategy-intelligence/research-candidates", json=_candidate("a")).json()
    second = client.post("/api/v2/strategy-intelligence/research-candidates", json=_candidate("b")).json()
    body = {"idempotency_key": "matched-api-1", "champion_candidate_id": first["candidate_id"],
            "challenger_candidate_ids": [second["candidate_id"]], "method": "OUT_OF_SAMPLE_VALIDATION"}
    created = client.post("/api/v2/strategy-intelligence/matched-experiments", json=body)
    assert created.status_code == 201
    experiment_id = created.json()["experiment_id"]
    assert client.get(f"/api/v2/strategy-intelligence/matched-experiments/{experiment_id}/lineage").status_code == 200
    submitted = client.post(f"/api/v2/strategy-intelligence/matched-experiments/{experiment_id}/submit")
    assert submitted.status_code == 202
    assert len(submitted.json()["candidate_job_ids"]) == 2
    assert client.get(f"/api/v2/strategy-intelligence/matched-experiments/{experiment_id}").status_code == 200
