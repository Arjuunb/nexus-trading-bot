from services.research_jobs import ResearchJobService
import threading
from datetime import datetime, timezone, timedelta


def _request(**overrides):
    request = {
        "idempotency_key": "research-test-001", "job_type": "HISTORICAL_BACKTEST",
        "strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
        "config_fingerprint": "a" * 64, "frozen_config": {}, "symbol": "XRPUSDT",
        "timeframe": "5m", "bars": 600,
    }
    request.update(overrides)
    return request


def test_research_job_duplicate_submission_is_idempotent_and_scoped(tmp_path):
    from test_strategy_evidence_runtime import runtime
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    service = ResearchJobService(store.context)
    first = service.create(_request(), owner_id="owner", scope={"owner_id": "owner"})
    second = service.create(_request(), owner_id="owner", scope={"owner_id": "owner"})
    assert first["job_id"] == second["job_id"]
    assert service.get(first["job_id"], owner_id="foreign") is None


def test_research_without_real_dataset_fails_closed_and_does_not_touch_ledger(tmp_path):
    from test_strategy_evidence_runtime import runtime
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    before = ledger.get_authoritative_evidence_snapshot()
    service = ResearchJobService(store.context)
    job = service.create(_request(idempotency_key="research-test-002"), owner_id="owner", scope={"owner_id": "owner"})
    finished = service.run(job["job_id"], owner_id="owner")
    assert finished["status"] in {"FAILED", "COMPLETED"}
    if finished["status"] == "FAILED":
        assert finished["error_class"] in {"RuntimeError", "ValueError"}
    assert ledger.get_authoritative_evidence_snapshot() == before


def test_research_job_routes_authenticate_and_preserve_idempotency(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.strategy_intelligence import create_router
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from test_strategy_evidence_runtime import runtime
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    intelligence = StrategyIntelligenceService(store)
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: intelligence,
                                     manager_provider=lambda: None,
                                     owner_resolver=lambda _request: "owner"))
    client = TestClient(app)
    response = client.post("/api/v2/strategy-intelligence/research-jobs", json=_request())
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert client.get(f"/api/v2/strategy-intelligence/research-jobs/{job_id}").status_code == 200
    assert client.post("/api/v2/strategy-intelligence/research-jobs", json=_request()).json()["job_id"] == job_id
    assert client.post(f"/api/v2/strategy-intelligence/research-jobs/{job_id}/cancel").json()["status"] == "CANCELLED"
    queued = client.post("/api/v2/strategy-intelligence/research-jobs", json=_request(idempotency_key="async-route"))
    async_job = queued.json()
    response = client.post(f"/api/v2/strategy-intelligence/research-jobs/{async_job['job_id']}/run", json={})
    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert response.json()["execution"] == "asynchronous"


def _journal_job(tmp_path, **overrides):
    from data.journal_store import JournalStore
    from services.research_jobs import ResearchJobService
    service = ResearchJobService(JournalStore(str(tmp_path / "research.sqlite")).context)
    return service, service.create(_request(**overrides), owner_id="owner", scope={"owner_id": "owner"})


def test_two_workers_only_one_can_claim_a_job(tmp_path):
    from services.research_jobs import ResearchJobWorker
    service, job = _journal_job(tmp_path, idempotency_key="race")
    workers = [ResearchJobWorker(service.context, worker_id=f"w{i}") for i in range(2)]
    claims = []
    lock = threading.Lock()
    def claim(worker):
        value = worker.context.claim_research_job(worker_id=worker.worker_id, lease_seconds=10)
        with lock:
            claims.append(value)
    threads = [threading.Thread(target=claim, args=(worker,)) for worker in workers]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sum(value is not None for value in claims) == 1
    assert service.get(job["job_id"], owner_id="owner")["attempt_number"] == 1


def test_expired_lease_is_recovered_and_stale_worker_is_fenced(tmp_path):
    from services.research_jobs import ResearchJobWorker
    service, job = _journal_job(tmp_path, idempotency_key="lease")
    first = ResearchJobWorker(service.context, worker_id="old")
    claim = service.context.claim_research_job(worker_id="old", lease_seconds=5)
    assert claim
    with service.context.journal.transaction():
        service.context.journal._c.execute("UPDATE research_jobs SET lease_expires_at=? WHERE job_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job["job_id"]))
    assert service.context.recover_expired_research_jobs() == 1
    second = service.context.claim_research_job(worker_id="new", lease_seconds=5)
    assert second and second["worker_id"] == "new"
    try:
        service.context.heartbeat_research_job(job["job_id"], worker_id="old", lease_token=claim["lease_token"])
        assert False, "stale worker heartbeat unexpectedly succeeded"
    except Exception:
        pass


def test_cancel_request_prevents_result_commit(tmp_path):
    service, job = _journal_job(tmp_path, idempotency_key="cancel-race")
    claim = service.context.claim_research_job(worker_id="worker", lease_seconds=5)
    service.context.transition_research_job(job["job_id"], to_status="RUNNING", worker_id="worker",
                                            lease_token=claim["lease_token"])
    service.cancel(job["job_id"], owner_id="owner")
    report = {"status": "COMPLETE", "method": "historical_backtest",
              "dataset": {"dataset_id": "ds", "content_hash": "hash", "period_start": None, "period_end": None}}
    try:
        service.context.commit_research_result(job["job_id"], worker_id="worker", lease_token=claim["lease_token"],
                                               report=report, validation=ResearchJobService.validation_record(job, report))
        assert False, "cancelled job accepted a result"
    except ValueError:
        pass
    assert service.context.validation_runs(filters={"run_id": "validation:" + job["job_id"]}) == []


def test_worker_commits_one_logical_result_and_reuses_it_on_restart(tmp_path):
    from services.research_jobs import ResearchJobWorker
    service, job = _journal_job(tmp_path, idempotency_key="commit")
    report = {"status": "COMPLETE", "method": "historical_backtest",
              "dataset": {"dataset_id": "ds", "content_hash": "hash", "period_start": None, "period_end": None}}
    service._execute = lambda _job, checkpoint=None: report
    worker = ResearchJobWorker(service.context, service=service, worker_id="worker")
    assert worker.process_once() is True
    finished = service.get(job["job_id"], owner_id="owner")
    assert finished["status"] == "COMPLETED"
    assert len(service.context.validation_runs(filters={"run_id": "validation:" + job["job_id"]})) == 1
    assert worker.process_once() is False
    assert service.get(job["job_id"], owner_id="owner")["status"] == "COMPLETED"


def _period_job(tmp_path, monkeypatch, *, warmup, cached_through_end=True, job_type="HISTORICAL_BACKTEST"):
    """A period job over a candle store that behaves like HistoricalStore.get_bars:
    the end bound is inclusive and only the newest n rows are kept."""
    from bot.types import Bar
    from data import market_data
    from services import strategy_presets
    from test_strategy_evidence_runtime import runtime
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    hour = timedelta(hours=1)
    cache = [Bar(start + hour * i, 100, 101, 99, 100, 10) for i in range(-warmup, 600 + (1 if cached_through_end else 0))]
    fetched, runs = [], []

    def get_bars(symbol, n=1500, timeframe="1h", seed=1, since_ms=None, end_ms=None, require_real=False):
        fetched.append(end_ms)
        rows = [b for b in cache if (since_ms is None or b.timestamp.timestamp() * 1000 >= since_ms)
                and (end_ms is None or b.timestamp.timestamp() * 1000 <= end_ms)]
        return rows[-n:], "isolated-test-store"

    def run_on(strategy, symbol, timeframe, tuning, custom_spec, rows, *, capture_identity=False, trade_start_at=None):
        runs.append({"rows": list(rows), "trade_start_at": trade_start_at})
        return {"trades": [], "_strategy_identity": None}

    monkeypatch.setattr(market_data, "get_bars", get_bars)
    monkeypatch.setattr(strategy_presets, "_run_on", run_on)
    service = ResearchJobService(store.context)
    job = service.create(_request(idempotency_key="period-%d-%s" % (warmup, job_type), job_type=job_type,
                                  timeframe="1h", bars=600, warmup=warmup,
                                  period_start=start.isoformat(), period_end=(start + hour * 600).isoformat()),
                         owner_id="owner", scope={"owner_id": "owner"})
    return service.run(job["job_id"], owner_id="owner"), start, fetched, runs


def test_period_fetch_excludes_the_candle_at_period_end(tmp_path, monkeypatch):
    """A cached candle exactly at period_end must not push out the first candle."""
    finished, start, fetched, runs = _period_job(tmp_path, monkeypatch, warmup=0)
    assert finished["status"] == "COMPLETED", finished
    assert fetched[0] == int((start + timedelta(hours=600)).timestamp() * 1000) - 1
    assert runs[0]["rows"][0].timestamp == start and len(runs[0]["rows"]) == 600


def test_requested_warmup_reaches_the_simulator_as_context_only(tmp_path, monkeypatch):
    finished, start, fetched, runs = _period_job(tmp_path, monkeypatch, warmup=50)
    assert finished["status"] == "COMPLETED", finished
    rows = runs[0]["rows"]
    assert len(rows) == 650
    assert rows[0].timestamp == start - timedelta(hours=50)
    assert runs[0]["trade_start_at"] == start          # no entry before the evaluation period


def test_without_warmup_the_simulator_gets_only_the_evaluation_period(tmp_path, monkeypatch):
    finished, start, fetched, runs = _period_job(tmp_path, monkeypatch, warmup=0, job_type="ANCHORED_WALK_FORWARD")
    assert finished["status"] == "COMPLETED", finished
    assert all(run["trade_start_at"] is None for run in runs)
    assert all(run["rows"][0].timestamp >= start for run in runs)


def test_run_on_feeds_warmup_to_builtins_and_drops_it_for_custom_specs(monkeypatch):
    from bot.types import Bar
    from strategies import custom
    from services.strategy_presets import _run_on
    seen = {}
    monkeypatch.setattr(custom, "simulate_strategy",
                        lambda strat, rows, **kw: seen.update(builtin=(len(rows), kw.get("trade_start_at"))) or {"trades": []})
    monkeypatch.setattr(custom, "simulate", lambda spec, rows, **kw: seen.update(custom=len(rows)) or {"trades": []})
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = [Bar(start + timedelta(hours=i), 100, 101, 99, 100, 10) for i in range(300)]
    cut = rows[100].timestamp
    _run_on("Decision Brain", "BTCUSDT", "1h", {}, None, rows, trade_start_at=cut)
    assert seen["builtin"] == (300, cut)
    _run_on("EMA 20/50", "BTCUSDT", "1h", {}, None, rows, trade_start_at=cut)
    assert seen["custom"] == 200
