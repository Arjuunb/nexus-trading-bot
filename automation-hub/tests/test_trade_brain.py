"""TradeBrain quality scoring, improved exits, diagnosis, and the brain-aware
simulator path. Uses deterministic synthetic bars so assertions are stable."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from strategies.brain import TradeBrain, aggregate_htf, htf_bias, detect_reversal, BrainConfig
from strategies.custom import simulate
from strategies.diagnosis import diagnose


def _bars(closes, *, vol=1000.0, spread=0.004):
    """Build bars from a close series; high/low straddle close by `spread`."""
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    out = []
    prev = closes[0]
    for k, c in enumerate(closes):
        hi = max(c, prev) * (1 + spread)
        lo = min(c, prev) * (1 - spread)
        out.append(Bar(timestamp=t0 + timedelta(hours=k), open=prev, high=hi, low=lo, close=c, volume=vol))
        prev = c
    return out


def _uptrend(n=320, start=100.0, step=0.5):
    return _bars([start + k * step for k in range(n)])


def _downtrend(n=320, start=260.0, step=0.5):
    return _bars([start - k * step for k in range(n)])


# --------------------------------------------------------------- HTF helpers
def test_aggregate_htf_groups_aligned_to_end():
    bars = _uptrend(n=40)
    htf = aggregate_htf(bars, 4)
    assert len(htf) == 10
    # last HTF candle closes on the last base bar
    assert htf[-1].close == bars[-1].close
    # HTF high is the max of its group
    assert htf[-1].high == max(b.high for b in bars[-4:])


def test_htf_bias_detects_direction():
    cfg = BrainConfig()
    up, s_up = htf_bias(_uptrend(), cfg, allow_legacy_resample=True)
    dn, s_dn = htf_bias(_downtrend(), cfg, allow_legacy_resample=True)
    assert up == "bullish" and dn == "bearish"
    assert 0.0 <= s_up <= 1.0 and s_dn > 0.3  # clean trend => strong


# --------------------------------------------------------------- scoring
def test_long_in_uptrend_allowed_and_scored():
    bars = _uptrend()
    i = len(bars) - 1
    entry = bars[i].close
    v = TradeBrain().evaluate(bars, i, side="long", entry=entry,
                              stop=entry * 0.985, target=entry * 1.03)
    assert v.allowed
    assert 0 <= v.score <= 100
    assert v.htf_bias == "bullish"
    assert set(("htf_alignment", "regime_fit", "rr_quality", "momentum")).issubset(v.components)


def test_short_against_strong_uptrend_blocked():
    bars = _uptrend()
    i = len(bars) - 1
    entry = bars[i].close
    v = TradeBrain().evaluate(bars, i, side="short", entry=entry,
                              stop=entry * 1.015, target=entry * 0.97)
    assert not v.allowed
    assert any("higher-timeframe" in b for b in v.blocks)


def test_block_reward_risk_below_one():
    bars = _uptrend()
    i = len(bars) - 1
    entry = bars[i].close
    # target closer than stop => RR < 1
    v = TradeBrain().evaluate(bars, i, side="long", entry=entry,
                              stop=entry * 0.97, target=entry * 1.01)
    assert not v.allowed
    assert any("reward:risk" in b for b in v.blocks)


def test_block_stop_too_tight():
    bars = _uptrend()
    i = len(bars) - 1
    entry = bars[i].close
    v = TradeBrain().evaluate(bars, i, side="long", entry=entry,
                              stop=entry * 0.9999, target=entry * 1.03)
    assert not v.allowed
    assert any("too tight" in b for b in v.blocks)


def test_losing_streak_blocks():
    bars = _uptrend()
    i = len(bars) - 1
    entry = bars[i].close
    v = TradeBrain().evaluate(bars, i, side="long", entry=entry,
                              stop=entry * 0.985, target=entry * 1.03, recent_losses=5)
    assert not v.allowed
    assert any("streak" in b for b in v.blocks)


def test_detect_reversal():
    assert detect_reversal({"entry": {"rules": [{"type": "liquidity_sweep"}]}})
    assert detect_reversal({"entry": {"rules": [{"type": "rsi", "op": "below", "value": 30}]}})
    assert detect_reversal({"entry": {"rules": [{"type": "bollinger", "zone": "below_lower"}]}})
    assert not detect_reversal({"entry": {"rules": [{"type": "ema_cross"}]}})


# --------------------------------------------------------------- simulator path
SPEC = {
    "symbol": "BTCUSDT", "timeframe": "1h", "side": "long",
    "entry": {"op": "AND", "rules": [{"type": "ema_cross", "fast": 20, "slow": 50, "dir": "above"}]},
    "stop": {"type": "atr", "mult": 1.5, "period": 14},
    "target": {"type": "rr", "rr": 2.0}, "risk_per_trade_pct": 0.01,
}


def test_simulate_without_brain_has_no_blocked():
    res = simulate(SPEC, _uptrend(n=400))
    assert res["blocked_count"] == 0
    assert res["blocked"] == []
    # new metric keys present
    for k in ("expectancy_r", "sharpe", "recovery_factor", "avg_hold_bars",
              "long_net_r", "short_net_r"):
        assert k in res


def test_simulate_with_brain_records_blocks_and_tags():
    bars = _downtrend(n=400)  # longs into a downtrend -> brain blocks (against HTF)
    spec = {**SPEC, "entry": {"op": "AND", "rules": [{"type": "rsi", "op": "below", "value": 100}]}}
    res = simulate(spec, bars, brain=TradeBrain(), min_score=60)
    assert res["blocked_count"] >= 1
    b = res["blocked"][0]
    assert "reason" in b and "score" in b and "regime" in b
    # any taken trade carries the brain tag
    for t in res["trades"]:
        assert "score" in t and "regime" in t and "setup_type" in t


def test_improved_exit_breakeven_changes_exit_reason():
    spec = {**SPEC, "exit": {"breakeven_at_r": 1.0}}
    res = simulate(spec, _uptrend(n=400))
    reasons = {t.get("exit_reason") for t in res["trades"]}
    # exit_reason is always populated now
    assert reasons and all(r in ("stop", "target", "breakeven", "time") for r in reasons)


# --------------------------------------------------------------- diagnosis
def test_diagnose_required_fields():
    res = simulate(SPEC, _uptrend(n=400), brain=TradeBrain(), min_score=55)
    d = diagnose(res, res.get("blocked"))
    for k in ("summary", "loss_reasons", "blocked_reasons", "worst_regime",
              "overtrading", "choppy_markets", "recommendations"):
        assert k in d
    assert isinstance(d["recommendations"], list) and d["recommendations"]


def test_diagnose_handles_no_trades():
    d = diagnose({"trades": [], "total_trades": 0}, [])
    assert d["loss_reasons"] == {}
    assert "No trades" in d["summary"]


# --------------------------------------------------------------- optimisation
def test_walk_forward_honest_verdict():
    from strategies.optimize import walk_forward
    bars = _uptrend(n=700) + _downtrend(n=300, start=450.0)
    rep = walk_forward(SPEC, bars)
    assert rep["verdict"] in ("reliable", "marginal", "overfit")
    assert "validation" in rep and "baseline_validation" in rep
    assert set(rep["best_params"]) == {"min_score", "rr", "stop_mult"}
    # never claims reliable unless out-of-sample profit factor >= 1
    if rep["verdict"] == "reliable":
        assert rep["validation"]["profit_factor"] >= 1


# --------------------------------------------------------------- paper adapter
def test_adapter_brain_blocks_and_logs():
    from strategies.custom_adapter import CustomStrategyAdapter
    spec = {"name": "X", "symbol": "BTCUSDT", "timeframe": "1h", "side": "long",
            "entry": {"op": "AND", "rules": [{"type": "rsi", "op": "below", "value": 100}]},
            "stop": {"type": "atr", "mult": 1.5, "period": 14},
            "target": {"type": "rr", "rr": 2.0}, "min_score": 60}
    blocks = []
    ad = CustomStrategyAdapter("BTCUSDT", spec, on_block=blocks.append)
    bars = _downtrend(n=400)  # longs into a downtrend -> brain should block
    sig = None
    for b in bars:
        sig = ad.on_bar(b) or sig
    assert blocks, "expected at least one blocked setup logged"
    assert {"symbol", "side", "score", "regime", "reason"}.issubset(blocks[0])


def test_adapter_quality_scales_confidence():
    from strategies.custom_adapter import CustomStrategyAdapter
    from services.mtf_policy import evidence_at
    spec = {"name": "X", "symbol": "BTCUSDT", "timeframe": "1h", "side": "long",
            "entry": {"op": "AND", "rules": [{"type": "ema_cross", "fast": 20, "slow": 50, "dir": "above"}]},
            "stop": {"type": "atr", "mult": 1.5, "period": 14},
            "target": {"type": "rr", "rr": 2.0}, "min_score": 40,
            "mtf_filter": False}
    ad = CustomStrategyAdapter("BTCUSDT", spec)
    native = {"4h": _uptrend(n=400)}
    got = None
    for b in _uptrend(n=400):
        decision = b.timestamp + timedelta(hours=1)
        ad.set_native_mtf_context(native, evidence_at("BTCUSDT", "1h", native, decision))
        s = ad.on_bar(b)
        if s is not None:
            got = s
            break
    assert got is not None
    assert 0.0 <= got.confidence <= 1.0
    assert "score" in got.reason


def test_adapter_filter_can_be_disabled():
    from strategies.custom_adapter import CustomStrategyAdapter
    # disable BOTH gates (brain + multi-timeframe) to exercise the raw path
    spec = {"name": "X", "symbol": "BTCUSDT", "timeframe": "1h", "side": "long",
            "entry": {"op": "AND", "rules": [{"type": "rsi", "op": "below", "value": 100}]},
            "stop": {"type": "atr", "mult": 1.5, "period": 14},
            "target": {"type": "rr", "rr": 2.0}, "quality_filter": False, "mtf_filter": False}
    ad = CustomStrategyAdapter("BTCUSDT", spec)
    assert ad.brain is None
    sig = None
    for b in _downtrend(n=400):
        sig = ad.on_bar(b) or sig
    assert sig is not None  # with both gates off, longs into a downtrend are taken


def test_strategy_health_endpoint():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    app = FastAPI(); app.include_router(webhook_api.router)
    body = TestClient(app).get("/strategy/health").json()
    assert "health" in body and "brain" in body and "breakdown" in body
    for k in ("blocked", "taken", "total", "block_rate", "top_reasons"):
        assert k in body["brain"]
    assert body["health"]["status"] in ("Healthy", "Degrading", "Unhealthy")
    assert "by_symbol" in body["breakdown"] and "by_session" in body["breakdown"]


def test_health_breakdown_aggregates():
    from webhook_api import _health_breakdown
    from collections import Counter
    hist = [
        {"symbol": "BTCUSDT", "pnl": 10.0, "opened_at": "2024-01-01T03:00:00"},   # Asia win
        {"symbol": "BTCUSDT", "pnl": -4.0, "opened_at": "2024-01-01T10:00:00"},   # London loss
        {"symbol": "ETHUSDT", "pnl": -6.0, "opened_at": "2024-01-01T18:00:00"},   # NY loss
    ]
    bd = _health_breakdown(hist, Counter({"SOLUSDT": 3}))
    syms = {r["name"]: r for r in bd["by_symbol"]}
    assert syms["BTCUSDT"]["trades"] == 2 and syms["BTCUSDT"]["net_pnl"] == 6.0
    assert syms["SOLUSDT"]["blocked"] == 3 and syms["SOLUSDT"]["trades"] == 0  # blocked-only symbol
    sessions = {r["name"] for r in bd["by_session"]}
    assert {"Asia", "London", "New York"} == sessions
