from datetime import datetime, timedelta, timezone
import threading
import time

import pytest

from bot.types import Bar
from services.forward_paper_hub import ForwardPaperMarketDataHub, candle_id
from services.native_context_loader import NativeContextLoader
from services.price_action_lab import PriceActionLabRuntime, PriceActionPaperAccount
from services.smc_strategy_lab import SMCStrategyLabRuntime, SMCPaperAccount
from services.trading_instances import TradingInstance, TradingInstanceManager
from tests.test_isolated_forward_paper import FakePublicStream
from tests.test_lab_audit_runtime import RULES


NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
PRIMARY = Bar(NOW - timedelta(hours=1), 100, 101, 99, 100, 10)


def eventually(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(.01)
    raise AssertionError("background work did not complete")


class Market:
    def public_usdm_window(self, symbol, timeframe, *, limit):
        if timeframe == "4h":
            raise RuntimeError("4h offline")
        return [PRIMARY]

    def usdm_contract_rules(self, symbol):
        return RULES


@pytest.mark.parametrize("kind", ["pa", "smc"])
def test_four_hour_failure_still_evaluates_primary_context(tmp_path, monkeypatch, kind):
    if kind == "pa":
        from services.native_price_action import NativePriceActionEngine, PriceActionConfig
        account = PriceActionPaperAccount(tmp_path / "pa.db")
        runtime = PriceActionLabRuntime(Market(), account, autostart=False)
        runtime.identity = ("BTCUSDT", "5m")
        runtime.engine = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m"))
        bar = Bar(NOW, 100, 101, 99, 100, 10)
        monkeypatch.setattr(runtime.stream, "status", lambda: {"state": "CONNECTING", "reliable": False})
        monkeypatch.setattr(runtime.stream, "snapshot", lambda: {"quote": {}, "closed_bars": [bar]})
    else:
        account = SMCPaperAccount(tmp_path / "smc.db")
        runtime = SMCStrategyLabRuntime(Market(), account, autostart=False)
    try:
        eventually(lambda: bool(runtime._native_context("BTCUSDT", "5m")["1h"]))
        eventually(lambda: "4h" in runtime.native_loader.errors("BTCUSDT", "5m"))
        if kind == "pa":
            runtime._on_closed_bar(bar)
            assert account.state()["evaluations"], "closed candle must reach strategy evaluation"
            evidence = runtime.engine._mtf_evidence
        else:
            from services.smc_strategy_v1 import evaluate
            from tests.test_smc_strategy_ladder import seeded_engine
            evidence = runtime._native_mtf_evidence("BTCUSDT", "5m", NOW + timedelta(minutes=5))
            result = evaluate(seeded_engine(), mtf_evidence=evidence)
            assert result["mtf_evidence"]["primary"] is not None
        assert evidence["primary"] is not None
        assert evidence["secondary"] is None
        monkeypatch.setattr(runtime, "_native_context", lambda *_: {"1h": [], "4h": []})
        with pytest.raises(RuntimeError, match="HTF_PRIMARY_UNAVAILABLE"):
            if kind == "pa":
                runtime._on_closed_bar(Bar(NOW + timedelta(minutes=5), 100, 101, 99, 100, 10))
            else:
                runtime._native_mtf_evidence("BTCUSDT", "5m", NOW)
    finally:
        runtime.stop()


def test_subscriber_failure_does_not_starve_sibling_or_reconnect():
    hub = ForwardPaperMarketDataHub(lambda *_a, **_k: [], stream_factory=FakePublicStream)
    attempts, sibling = [], []
    fail = True
    def failing(bar):
        attempts.append(bar)
        if fail:
            raise RuntimeError("lab failed")
    pa = hub.subscription("PA", bar_sink=failing)
    smc = hub.subscription("SMC", bar_sink=sibling.append)
    pa.start("BTCUSDT", "5m")
    smc.start("BTCUSDT", "5m")
    channel = hub._for("PA")
    try:
        channel.stream.emit_bar(PRIMARY)
        assert sibling == [PRIMARY]
        assert pa.status()["subscriber_delivery"]["pending_candle_ids"] == [candle_id("BTCUSDT", "5m", PRIMARY)]
        assert smc.status()["reliable"]
        assert channel.stream.running
        fail = False
        hub.retry_failed()
        assert attempts == [PRIMARY, PRIMARY]
        assert sibling == [PRIMARY], "successful subscriber must not be replayed"
        assert pa.status()["subscriber_delivery"]["pending_candle_ids"] == []
    finally:
        hub.stop()


@pytest.mark.parametrize("kind", ["pa", "smc"])
def test_status_and_session_reads_return_while_four_hour_and_worker_lock_are_blocked(tmp_path, monkeypatch, kind):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api as wa
    from routers import price_action, native_smc
    release = threading.Event()
    entered = threading.Event()
    class SlowMarket(Market):
        def public_usdm_window(self, symbol, timeframe, *, limit):
            if timeframe == "4h":
                entered.set()
                release.wait(5)
                raise RuntimeError("4h timed out")
            return [PRIMARY]
    if kind == "pa":
        account = PriceActionPaperAccount(tmp_path / "pa.db")
        runtime = PriceActionLabRuntime(SlowMarket(), account, autostart=False)
        monkeypatch.setattr(wa, "price_action_paper", account)
        monkeypatch.setattr(wa, "price_action_runtime", runtime)
        router, prefix = price_action.router, "/research/price-action"
    else:
        account = SMCPaperAccount(tmp_path / "smc.db")
        runtime = SMCStrategyLabRuntime(SlowMarket(), account, autostart=False)
        monkeypatch.setattr(wa, "smc_paper", account)
        monkeypatch.setattr(wa, "smc_runtime", runtime)
        router, prefix = native_smc.router, "/research/smc"
    expected = account.session()
    runtime._native_context("BTCUSDT", "5m")
    assert entered.wait(1)
    eventually(lambda: bool(runtime._native_context("BTCUSDT", "5m")["1h"]))
    # This lock used to block PA state() and therefore bot-status indefinitely.
    locked = threading.Event()
    def hold():
        with account._lock:
            locked.set()
            release.wait(5)
    worker = threading.Thread(target=hold)
    worker.start()
    assert locked.wait(1)
    app = FastAPI()
    app.include_router(router)
    try:
        with TestClient(app) as client:
            start = time.monotonic()
            for endpoint in ("session", "paper", "bot-status", "live-chart"):
                response = client.get(prefix + "/" + endpoint)
                assert response.status_code == 200, response.text
                data = response.json()
                session_id = data.get("session_id") or data["session"]["id"]
                assert session_id == expected["id"]
                if endpoint == "bot-status":
                    assert data["mode"] == expected["operating_mode"]
                    assert data["mtf_policy"]["evidence"]["primary"]
                    assert data["mtf_policy"]["evidence"]["secondary"] is None
            assert time.monotonic() - start < 2, "hydration waited for provider/worker"
            assert not release.is_set()
    finally:
        release.set()
        worker.join(1)
        runtime.stop()


def test_htf_timeout_has_one_inflight_task_and_discards_late_result():
    release = threading.Event()
    calls = []
    def fetch(symbol, tf, limit):
        calls.append(tf)
        if tf == "4h":
            release.wait(2)
        return [PRIMARY]
    loader = NativeContextLoader(fetch, timeout=.05, refresh=30)
    try:
        loader.context("BTCUSDT", "5m")
        eventually(lambda: loader.context("BTCUSDT", "5m")["1h"])
        eventually(lambda: loader.errors("BTCUSDT", "5m").get("4h") == "HTF_LOAD_TIMEOUT"
                   or (loader.context("BTCUSDT", "5m") and False))
        for _ in range(10):
            assert not loader.context("BTCUSDT", "5m")["4h"]
        assert calls.count("4h") == 1
        release.set()
        eventually(lambda: not loader._loads[("BTCUSDT", "4h")].running)
        assert not loader.context("BTCUSDT", "5m")["4h"]
    finally:
        release.set()
        loader.stop()


def test_invalid_pending_ownership_is_named_and_quarantined_without_mutating_sibling():
    instance = TradingInstance(
        id="instance-sol", symbol="SOLUSDT", strategy_key="brain",
        strategy_label="Decision Brain", strategy_version="v1", timeframe="5m",
        risk_per_trade_pct=.005, capital_allocation=500,
        simulation_session_id="session-sol",
    )
    pending = {
        "strategy_limit_intents": {
            "ETHUSDT": {"payload": {"symbol": "ETHUSDT", "instance_id": "other"}},
            "SOLUSDT": {"payload": {"symbol": "SOLUSDT", "instance_id": instance.id}},
        },
        "forward_paper_intents": {},
    }

    TradingInstanceManager._quarantine_pending_ownership(instance, pending)

    assert "ETHUSDT" not in pending["strategy_limit_intents"]
    assert pending["strategy_limit_intents"]["SOLUSDT"]["payload"]["symbol"] == "SOLUSDT"
    quarantined = pending["quarantined_intents"]["strategy_limit_intents:ETHUSDT"]
    assert quarantined["blocker"] == "PENDING_ORDER_OWNERSHIP_INVALID"
    assert quarantined["original"]["payload"]["instance_id"] == "other"
