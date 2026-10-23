from fastapi import FastAPI
from fastapi.testclient import TestClient


def _body(fingerprint="a"):
    return {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
            "config_fingerprint": fingerprint * 64, "frozen_config": {"ema": 20},
            "dataset_identity": "xrp:5m:dataset", "data_revision": "revision-1",
            "symbol": "XRPUSDT", "venue": "binance", "market_type": "spot",
            "timeframe": "5m", "execution_mode": "backtest", "evidence_quality": "UNKNOWN"}


def test_comparison_api_is_authenticated_and_tenant_scoped(tmp_path):
    from data.journal_store import JournalStore
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from routers.strategy_intelligence import create_router
    intelligence = StrategyIntelligenceService(JournalStore(str(tmp_path / "journal.sqlite")))
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: intelligence, manager_provider=lambda: None,
                                     owner_resolver=lambda _request: "owner"))
    client = TestClient(app)
    first = client.post("/api/v2/strategy-intelligence/research-candidates", json=_body())
    second = client.post("/api/v2/strategy-intelligence/research-candidates", json=_body("b"))
    assert first.status_code == 201 and second.status_code == 201
    assert client.get("/api/v2/strategy-intelligence/research-champions", params={"scope_key": "xrp:5m"}).json()["status"] == "NO_RESEARCH_CHAMPION_SELECTED"
    assert client.get("/api/v2/strategy-intelligence/research-candidates").status_code == 200


def test_designation_cannot_use_unknown_candidate(tmp_path):
    from data.journal_store import JournalStore
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from routers.strategy_intelligence import create_router
    intelligence = StrategyIntelligenceService(JournalStore(str(tmp_path / "journal.sqlite")))
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: intelligence, manager_provider=lambda: None,
                                     owner_resolver=lambda _request: "owner"))
    response = TestClient(app).post("/api/v2/strategy-intelligence/research-champions", json={
        "scope_key": "xrp:5m", "candidate_id": "missing", "reason": "review"})
    assert response.status_code == 422
