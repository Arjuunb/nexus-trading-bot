"""Display hydration cannot replay strategies, hide outages or mix identities."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.types import Bar
from services.smc_lab_display import SMCLabDisplay, current_health
from services.smc_agent_runtime import AgentSMCStrategyLabRuntime
from services.native_smc_live_visual import NativeSMCLiveDataUnavailable
from services.price_action_stream import PriceActionPublicStream

NOW = datetime(2026, 9, 22, 12, 2, tzinfo=timezone.utc)
SESSION = dict(id="s1", symbol="BTCUSDT", timeframe="5m", model_id="SMC_M1_SWEEP_REVERSAL", operating_mode="automatic")


def healthy():
    return dict(state="SYNCHRONIZED", reliable=True, new_entries_paused=False,
                failing_dependency=None, last_candle_update=NOW.isoformat(),
                candle_age_seconds=0.1, health_reason="fresh")


def test_current_receipts_replace_cached_stale_verdict_without_mutating_it():
    old = dict(state="STALE_CANDLES", reliable=False, failing_dependency="BINANCE_USDM_KLINE_STREAM", candle_age_seconds=32)
    result = current_health(old, healthy())
    assert result["reliable"] and result["candle_age_seconds"] == .1
    assert result["failing_dependency"] is None
    assert old["state"] == "STALE_CANDLES"


@pytest.mark.parametrize("dependency", ["COMPLETED_CANDLE_RECONCILIATION", "SMC_CLOSED_CANDLE_RUNTIME", "HTF_PRIMARY_UNAVAILABLE"])
def test_fresh_transport_cannot_clear_runtime_failure(dependency):
    result = current_health(dict(state="ERROR", reliable=False, failing_dependency=dependency), healthy())
    assert not result["reliable"] and result["new_entries_paused"]
    assert result["failing_dependency"] == dependency


def test_new_outage_cannot_be_hidden_by_cached_healthy_state():
    outage = dict(state="STALE_MARK", reliable=False, new_entries_paused=True, failing_dependency="BINANCE_USDM_MARK_PRICE_STREAM")
    assert current_health(healthy(), outage)["state"] == "STALE_MARK"


def runtime_with_snapshot():
    bar = Bar(NOW - timedelta(minutes=7), 100, 101, 99, 100, 1)
    visual = {"candles": [{"timestamp": bar.timestamp.isoformat(), "close": 100}],
              "data_provenance": {"last_closed_candle": bar.timestamp.isoformat()},
              "source_strategy": {"state": "WATCHING"}, "live_display": healthy()}
    display = SMCLabDisplay()
    display.publish(SESSION, visual)
    snapshot = dict(connection=healthy(), closed_bars=[bar], forming=None, quote={"last": 101})
    runtime = SimpleNamespace(_display=display, account=SimpleNamespace(session=lambda: SESSION),
                              stream=SimpleNamespace(snapshot=lambda: snapshot))
    return runtime, snapshot


def test_100_refreshes_read_worker_snapshot_without_strategy_evaluation(monkeypatch):
    from services import native_smc_live_visual
    monkeypatch.setattr(native_smc_live_visual, "live_visual_state", lambda *a, **k: pytest.fail("HTTP must not evaluate"))
    runtime, snapshot = runtime_with_snapshot()
    for _ in range(100):
        result = AgentSMCStrategyLabRuntime.live_state(runtime, "BTCUSDT", "5m")
        assert result["live_display"]["reliable"]
        result["candles"].clear()
    assert runtime._display.read(SESSION)["candles"]
    snapshot["connection"] = {**healthy(), "state": "STALE_CANDLES", "reliable": False, "new_entries_paused": True}
    assert not AgentSMCStrategyLabRuntime.live_state(runtime, "BTCUSDT", "5m")["live_display"]["reliable"]


def test_new_candle_blocks_display_until_worker_reconciles():
    runtime, snapshot = runtime_with_snapshot()
    snapshot["closed_bars"].append(Bar(NOW - timedelta(minutes=2), 100, 101, 99, 100, 1))
    result = AgentSMCStrategyLabRuntime.live_state(runtime, "BTCUSDT", "5m")
    assert result["live_display"]["failing_dependency"] == "COMPLETED_CANDLE_RECONCILIATION"
    assert not result["live_display"]["reliable"]


def test_market_switch_never_reuses_previous_session_chart():
    runtime, _ = runtime_with_snapshot()
    with pytest.raises(ValueError, match="saved SMC session"):
        AgentSMCStrategyLabRuntime.live_state(runtime, "BTCUSDT", "15m")
    runtime.account.session = lambda: {**SESSION, "timeframe": "15m"}
    with pytest.raises(NativeSMCLiveDataUnavailable, match="SMC_WORKER_WARMUP"):
        AgentSMCStrategyLabRuntime.live_state(runtime, "BTCUSDT", "15m")
    assert runtime._display.read({**SESSION, "id": "different"}) is None


def test_candle_only_htf_channel_needs_no_unused_quote_socket():
    stream = PriceActionPublicStream(lambda *a, **k: [], quotes_enabled=False, clock=lambda: NOW)
    stream.symbol, stream.timeframe = "BTCUSDT", "1h"
    stream.history_loaded = stream.reconciliation_complete = True
    stream.last_closed_update = NOW - timedelta(minutes=2)
    stream.last_candle_update = NOW
    stream._set_channel_state("market", "CONNECTED")
    assert stream.status()["reliable"]
    stale = stream.status(now=NOW + timedelta(seconds=16))
    assert stale["state"] == "STALE_CANDLES"
    assert stale["new_entries_paused"]
