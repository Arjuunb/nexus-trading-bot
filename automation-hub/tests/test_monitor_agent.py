"""AI Monitoring Agent.

Contract: it compares live behaviour against the strategy's OWN backtest, it
refuses to draw trade-based conclusions from a sample too small to support one,
it never modifies anything, and every finding carries the two numbers behind it.
"""
from services import monitor_agent as ma

BASE = {"total_trades": 120, "win_rate": 55.0, "profit_factor": 1.9,
        "net_r": 60.0, "expectancy_r": 0.5, "max_drawdown_r": 8.0,
        "span_days": 365}


def _live(**over):
    d = {"total_trades": 40, "win_rate": 54.0, "profit_factor": 1.8,
         "net_r": 18.0, "expectancy_r": 0.45, "max_drawdown_r": 7.0,
         "span_days": 120}
    d.update(over)
    return d


# ------------------------------------------------------------- live metrics

def test_live_metrics_match_the_backtest_metric_shape():
    m = ma.live_metrics([{"r": 2.0}, {"r": -1.0}, {"r": 1.0}, {"r": -1.0}])
    assert m["total_trades"] == 4
    assert m["win_rate"] == 50.0
    assert m["profit_factor"] == 1.5          # 3.0 won / 2.0 lost
    assert m["net_r"] == 1.0
    assert m["expectancy_r"] == 0.25


def test_live_metrics_read_paper_historys_rr_field():
    assert ma.live_metrics([{"rr": 1.5}, {"rr": -1.0}])["total_trades"] == 2


def test_trades_with_no_r_are_excluded_not_counted_as_break_even():
    """An unmeasurable trade is missing data — counting it as 0R would quietly
    drag expectancy toward zero and invent a result."""
    m = ma.live_metrics([{"r": 2.0}, {"symbol": "BTCUSDT"}, {"r": None}])
    assert m["total_trades"] == 1
    assert m["expectancy_r"] == 2.0


def test_drawdown_is_peak_to_trough_of_the_r_curve():
    #        +3      -> peak 3, then -4 -> trough -1, so the drawdown is 4
    assert ma.live_metrics([{"r": 3.0}, {"r": -4.0}])["max_drawdown_r"] == 4.0


def test_empty_history_is_zeroes_not_an_exception():
    assert ma.live_metrics([])["total_trades"] == 0


# ------------------------------------------------------------- gating

def test_no_baseline_says_so_rather_than_judging():
    out = ma.evaluate(baseline=None, live=_live())
    assert out["available"] is False and out["status"] == "no_baseline"
    assert "nothing to be compared against" in out["note"]


def test_small_live_sample_suppresses_performance_verdicts():
    """Four terrible trades are not evidence the strategy broke."""
    out = ma.evaluate(baseline=BASE, live=_live(total_trades=4, profit_factor=0.2,
                                                win_rate=10.0, expectancy_r=-0.8))
    assert out["status"] == "warming_up"
    assert out["findings"] == []
    assert f"{ma.MIN_LIVE_TRADES}" in out["note"]


def test_ambient_checks_still_run_while_warming_up():
    """Volatility and slippage are observable immediately — they do not need a
    trade sample, so a warming-up strategy still gets them."""
    out = ma.evaluate(baseline=BASE, live=_live(total_trades=2),
                      volatility_band={"p10": 0.5, "median": 1.0, "p90": 1.5},
                      current_atr_pct=3.0)
    assert out["status"] == "warming_up"
    assert [f["key"] for f in out["findings"]] == ["volatility-out-of-range"]


def test_ambient_checks_run_even_with_no_baseline():
    out = ma.evaluate(baseline=None, live=None,
                      volatility_band={"p10": 0.5, "median": 1.0, "p90": 1.5},
                      current_atr_pct=3.0)
    assert out["available"] is False
    assert [f["key"] for f in out["findings"]] == ["volatility-out-of-range"]


# ------------------------------------------------------------- deviation

def test_a_strategy_tracking_its_backtest_raises_nothing():
    out = ma.evaluate(baseline=BASE, live=_live())
    assert out["status"] == "in_line" and out["findings"] == []
    assert "consistent with the backtest" in out["note"]


def test_win_rate_slide_is_reported_with_both_numbers():
    out = ma.evaluate(baseline=BASE, live=_live(win_rate=30.0))
    f = next(f for f in out["findings"] if f["key"] == "win-rate-deviation")
    assert "55.0% -> 30.0%" in f["evidence"][0]["value"]


def test_profit_factor_collapse_below_one_is_critical():
    out = ma.evaluate(baseline=BASE, live=_live(profit_factor=0.7))
    f = next(f for f in out["findings"] if f["key"] == "profit-factor-deviation")
    assert f["severity"] == "critical"
    assert out["status"] == "critical"


def test_a_mild_profit_factor_dip_is_not_flagged():
    """1.9 -> 1.5 is a normal live/backtest gap, not a deviation."""
    out = ma.evaluate(baseline=BASE, live=_live(profit_factor=1.5))
    assert [f["key"] for f in out["findings"]] == []


def test_drawdown_beyond_the_tested_range_is_flagged():
    out = ma.evaluate(baseline=BASE, live=_live(max_drawdown_r=14.0))
    f = next(f for f in out["findings"] if f["key"] == "drawdown-exceeds-backtest")
    assert f["severity"] == "critical"
    assert "8.0 -> 14.0" in f["evidence"][0]["value"]


def test_drawdown_sign_convention_does_not_matter():
    """Some sources report drawdown negative; the verdict must be identical."""
    a = ma.evaluate(baseline=BASE, live=_live(max_drawdown_r=14.0))
    b = ma.evaluate(baseline=BASE, live=_live(max_drawdown_r=-14.0))
    assert [f["key"] for f in a["findings"]] == [f["key"] for f in b["findings"]]


def test_negative_expectancy_against_a_positive_backtest_is_critical():
    out = ma.evaluate(baseline=BASE, live=_live(expectancy_r=-0.2, profit_factor=1.7))
    assert any(f["key"] == "expectancy-negative" for f in out["findings"])


def test_trade_frequency_collapse_is_flagged_as_a_plumbing_problem():
    # backtest: 120 trades / 365d ~= 9.9 per 30d. live: 4 / 120d ~= 1.0
    out = ma.evaluate(baseline=BASE, live=_live(total_trades=20, span_days=600))
    f = next(f for f in out["findings"] if f["key"] == "trade-frequency-low")
    assert "decision log" in f["recommendation"]


def test_frequency_check_is_skipped_when_span_is_unknown():
    out = ma.evaluate(baseline=BASE, live=_live(span_days=0))
    assert not any(f["key"] == "trade-frequency-low" for f in out["findings"])


# ------------------------------------------------------------- volatility

def test_volatility_inside_the_tested_band_is_silent():
    out = ma.evaluate(baseline=BASE, live=_live(),
                      volatility_band={"p10": 0.5, "median": 1.0, "p90": 1.5},
                      current_atr_pct=1.1)
    assert not any(f["key"] == "volatility-out-of-range" for f in out["findings"])


def test_unusually_quiet_market_is_info_not_a_warning():
    out = ma.evaluate(baseline=BASE, live=_live(),
                      volatility_band={"p10": 0.5, "median": 1.0, "p90": 1.5},
                      current_atr_pct=0.2)
    f = next(f for f in out["findings"] if f["key"] == "volatility-out-of-range")
    assert f["severity"] == "info" and "below" in f["title"]


def test_volatility_check_needs_a_band_and_a_reading():
    assert ma.evaluate(baseline=BASE, live=_live(), current_atr_pct=9.0)["findings"] == []
    assert ma.evaluate(baseline=BASE, live=_live(),
                       volatility_band={"p10": 1, "p90": 2})["findings"] == []


def test_atr_band_needs_enough_samples_to_be_a_distribution():
    class B:
        def __init__(s, c): s.high, s.low, s.close, s.open = c + 1, c - 1, c, c
    assert ma.atr_pct_band([B(100) for _ in range(10)]) is None


def test_atr_band_returns_percentiles_from_real_bars():
    class B:
        def __init__(s, c, rng): s.high, s.low, s.close, s.open = c + rng, c - rng, c, c
    bars = [B(100 + i, 1 + (i % 5)) for i in range(120)]
    band = ma.atr_pct_band(bars)
    assert band is not None
    assert band["p10"] <= band["median"] <= band["p90"]
    assert band["samples"] > 20


# ------------------------------------------------------------- execution

def test_slippage_worse_than_the_model_is_flagged_as_optimistic_backtests():
    out = ma.evaluate(baseline=BASE, live=_live(), execution={
        "taker": {"fills": 40},
        "model_calibration": {"assumed_bps": 2.0, "measured_bps": 9.0}})
    f = next(f for f in out["findings"] if f["key"] == "slippage-increased")
    assert "optimistic" in f["detail"]
    assert "re-run the backtest" in f["recommendation"]


def test_slippage_within_tolerance_is_silent():
    out = ma.evaluate(baseline=BASE, live=_live(), execution={
        "model_calibration": {"assumed_bps": 2.0, "measured_bps": 2.5}})
    assert out["findings"] == []


def test_no_calibration_block_means_no_slippage_verdict():
    assert ma.evaluate(baseline=BASE, live=_live(),
                       execution={"overall": {"fills": 0}})["findings"] == []


# ------------------------------------------------------------- guarantees

def test_the_agent_never_modifies_and_says_so():
    for out in (ma.evaluate(baseline=None, live=None),
                ma.evaluate(baseline=BASE, live=_live(profit_factor=0.1))):
        assert out["auto_modify"] is False
    assert not hasattr(ma, "apply") and not hasattr(ma, "fix")


def test_every_finding_carries_evidence_and_a_human_action():
    out = ma.evaluate(baseline=BASE, live=_live(win_rate=20.0, profit_factor=0.5,
                                                expectancy_r=-0.4, max_drawdown_r=20.0),
                      volatility_band={"p10": 0.5, "median": 1.0, "p90": 1.5},
                      current_atr_pct=4.0,
                      execution={"model_calibration": {"assumed_bps": 2.0, "measured_bps": 20.0}})
    assert len(out["findings"]) >= 5
    for f in out["findings"]:
        assert f["evidence"] and all(e["stat"] and e["value"] for e in f["evidence"])
        assert f["recommendation"] and f["severity"] in ("critical", "warning", "info")


def test_findings_are_ordered_worst_first():
    out = ma.evaluate(baseline=BASE, live=_live(win_rate=20.0, expectancy_r=-0.4),
                      volatility_band={"p10": 0.5, "median": 1.0, "p90": 1.5},
                      current_atr_pct=0.1)
    sev = [f["severity"] for f in out["findings"]]
    assert sev == sorted(sev, key=lambda s: ma._SEV_RANK[s])
    assert sev[0] == "critical"


def test_baseline_from_results_carries_engine_numbers_unchanged():
    b = ma.baseline_from_results({"total_trades": 50, "win_rate": 44.0,
                                  "profit_factor": 1.3, "junk": 1})
    assert b == {"total_trades": 50, "win_rate": 44.0, "profit_factor": 1.3}
    assert ma.baseline_from_results({"total_trades": 0}) is None
    assert ma.baseline_from_results(None) is None


# ------------------------------------------------------------- span

def test_span_days_measures_the_trade_record():
    t = [{"opened_at": "2025-01-01T00:00:00"}, {"opened_at": "2025-01-31T00:00:00"}]
    assert ma.span_days(t) == 30.0


def test_span_days_tolerates_mixed_naive_and_aware_stamps():
    """The ledger writes naive stamps; other sources write Z-suffixed ones.
    Subtracting the two must not explode."""
    t = [{"closed_at": "2025-01-01T00:00:00Z"}, {"closed_at": "2025-01-11T00:00:00"}]
    assert ma.span_days(t) == 10.0


def test_span_days_is_zero_when_stamps_are_missing_or_unparseable():
    assert ma.span_days([{"symbol": "BTCUSDT"}]) == 0.0
    assert ma.span_days([{"opened_at": "not-a-date"}, {"opened_at": "also-not"}]) == 0.0
    assert ma.span_days([{"opened_at": "2025-01-01T00:00:00"}]) == 0.0


# ------------------------------------------------------------- endpoint

import pytest


@pytest.fixture()
def client():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    app = FastAPI()
    app.include_router(webhook_api.router)
    return TestClient(app)


SPEC = {
    "name": "EMA", "symbol": "BTCUSDT", "timeframe": "4h", "side": "long",
    "entry": {"op": "AND", "rules": [
        {"type": "ema_cross", "fast": 20, "slow": 50, "dir": "above"}]},
    "stop": {"type": "atr", "mult": 1.5, "period": 14},
    "target": {"type": "rr", "rr": 2}, "risk_per_trade_pct": 0.01,
}


def test_monitor_endpoint_runs_a_real_baseline(client):
    r = client.post("/strategy/monitor", json={"spec": SPEC, "range": "1Y"})
    assert r.status_code == 200
    j = r.json()
    assert j["auto_modify"] is False
    assert j["symbol"] == "BTCUSDT" and j["timeframe"] == "4h"
    assert "volatility" in j
    if j["available"]:
        assert j["baseline"]["total_trades"] > 0


def test_monitor_endpoint_requires_entry_rules(client):
    assert client.post("/strategy/monitor", json={"spec": {}}).status_code == 400


# ───────────── "current volatility" has to actually be current ─────────────
#
# Every branch of the volatility check is a claim about NOW, and the candles
# behind it come from a cache measured 15.8 hours behind the venue. Yesterday's
# ATR asserted as today's would tell the operator to resize stops for a regime
# that may already have ended.

BAND = {"p10": 0.5, "median": 1.0, "p90": 1.5}
FRESH = {"status": "FRESH", "age_seconds": 25.0, "last_close": "2026-09-19T12:00:00+00:00"}
STALE = {"status": "STALE", "age_seconds": 56880.0, "last_close": "2026-09-18T20:00:00+00:00"}
GONE = {"status": "MISSING", "age_seconds": None, "last_close": None}


def _keys(out):
    return {f["key"] for f in out["findings"]}


def test_a_current_reading_is_still_judged_normally():
    """The guard must not have disabled the check it protects."""
    out = ma.evaluate(baseline=BASE, live=_live(), volatility_band=BAND,
                      current_atr_pct=9.0, volatility_freshness=FRESH)
    assert "volatility-out-of-range" in _keys(out)
    assert "volatility-not-current" not in _keys(out)


def test_a_stale_reading_is_not_reported_as_current_volatility():
    out = ma.evaluate(baseline=BASE, live=_live(), volatility_band=BAND,
                      current_atr_pct=9.0, volatility_freshness=STALE)
    assert "volatility-out-of-range" not in _keys(out)
    assert "volatility-not-current" in _keys(out)


def test_the_stale_reading_says_how_old_and_that_it_is_not_an_all_clear():
    """An absent finding reads as 'volatility is in range'. It has to say
    that the check could not run."""
    out = ma.evaluate(baseline=BASE, live=_live(), volatility_band=BAND,
                      current_atr_pct=9.0, volatility_freshness=STALE)
    f = next(x for x in out["findings"] if x["key"] == "volatility-not-current")
    assert f["severity"] == "warning"
    assert "15.8 hours old" in f["detail"]
    assert "not a reading that volatility is in range" in f["detail"]
    assert "/data/sync" in f["recommendation"]


def test_no_candles_at_all_is_treated_the_same_as_stale_ones():
    """BNBUSDT held none. Absent must not read as in-range either."""
    out = ma.evaluate(baseline=BASE, live=_live(), volatility_band=BAND,
                      current_atr_pct=9.0, volatility_freshness=GONE)
    assert "volatility-not-current" in _keys(out)
    f = next(x for x in out["findings"] if x["key"] == "volatility-not-current")
    assert "unknown age" in f["detail"] and "none held" in str(f["evidence"])


def test_a_caller_that_supplies_no_freshness_behaves_as_before():
    """Backward compatible: an unjudged reading is judged on its value alone
    rather than silently suppressed."""
    out = ma.evaluate(baseline=BASE, live=_live(), volatility_band=BAND,
                      current_atr_pct=9.0)
    assert "volatility-out-of-range" in _keys(out)
    assert "volatility-not-current" not in _keys(out)
