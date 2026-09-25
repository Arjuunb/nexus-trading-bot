"""Advanced Backtesting lab.

Robustness tools that go beyond a single backtest, all on REAL Binance history
(no synthetic fallback):

  * walk_forward()       — rolling optimise-then-validate folds; reports the
                           out-of-sample edge and whether it is robust.
  * monte_carlo()        — bootstrap the trade sequence to a distribution of
                           outcomes (net R / drawdown percentiles, P(profit)).
  * out_of_sample()      — train/test split with an honest overfit verdict.
  * sliced_performance() — regime / session / symbol-conditional results.

Reuses the real simulator (strategy_presets._run_on) so the numbers match the
rest of the app; pure aside from the real-data load.
"""
from __future__ import annotations

import random
from typing import Optional


def _fetch(symbol: str, timeframe: str, bars: int):
    from data.market_data import get_bars
    return get_bars(symbol, n=max(600, min(int(bars), 10000)), timeframe=timeframe, require_real=True)


def _metrics_on(strategy, symbol, timeframe, tuning, rows, custom_spec=None):
    """(_metrics, raw results) for a strategy over ``rows``; None on error/empty."""
    from services.strategy_presets import _run_on, _metrics
    if not rows:
        return None
    r = _run_on(strategy, symbol, timeframe, tuning or {}, custom_spec, rows)
    if "error" in r:
        return None
    return _metrics(r), r


def _gate(quality_gate: str) -> dict:
    """Tuning that selects the gate-off rule, or nothing for the default gate."""
    if str(quality_gate).lower() not in ("on", "off"):
        raise ValueError("quality_gate must be 'on' or 'off'")
    return {"quality_gate": "off"} if str(quality_gate).lower() == "off" else {}


def _trade_rs(results) -> list:
    return [t["r"] for t in (results.get("trades") or []) if t.get("r") is not None]


# ─────────────────────────────────── walk-forward ───────────────────────────
def walk_forward(strategy: str, symbol: str, timeframe: str = "4h", *, bars: int = 4000,
                 folds: int = 4, custom_spec: Optional[dict] = None,
                 quality_gate: str = "on") -> dict:
    """Rolling walk-forward: optimise the min-score on each train block, validate
    on the next (unseen) block, and aggregate the out-of-sample result.

    ``quality_gate="off"`` runs the gate-off rule a Trading Instance can be
    switched to (services/quality_gate.py). There is no score to optimise
    then, so each fold is a plain train/test pair."""
    gate = _gate(quality_gate)
    rows, src = _fetch(symbol, timeframe, bars)
    if not rows:
        return {"available": False, "error": "Historical data not available. Load Binance data first."}
    n = len(rows)
    folds = max(2, min(folds, n // 150))
    block = n // (folds + 1)
    grid = [50, 60, 70, 80] if not gate else [0]
    out = []
    for i in range(folds):
        train = rows[i * block:(i + 1) * block]
        test = rows[(i + 1) * block:(i + 2) * block]
        if len(train) < 80 or len(test) < 40:
            continue
        best = None
        for ms in grid:
            m = _metrics_on(strategy, symbol, timeframe, {"min_score": ms, **gate}, train, custom_spec)
            if m and m[0]["trades"] >= 3 and (best is None or m[0]["net_r"] > best[1]["net_r"]):
                best = (ms, m[0])
        if best is None:
            continue
        tm = _metrics_on(strategy, symbol, timeframe, {"min_score": best[0], **gate}, test, custom_spec)
        tmet = tm[0] if tm else {"net_r": 0.0, "trades": 0, "profit_factor": 0.0}
        out.append({
            "fold": i + 1, "best_min_score": best[0],
            "train_net_r": best[1]["net_r"], "test_net_r": tmet["net_r"],
            "test_trades": tmet["trades"], "test_pf": tmet.get("profit_factor", 0.0),
        })
    oos = round(sum(f["test_net_r"] for f in out), 2)
    pos = sum(1 for f in out if f["test_net_r"] > 0)
    total = len(out)
    if total and pos >= max(1, round(total * 0.6)) and oos > 0:
        verdict, note = "robust", "Edge persists out-of-sample across folds. Validate in paper before sizing up."
    elif oos <= 0:
        verdict, note = "fragile", "No out-of-sample edge — the in-sample result does not generalise."
    else:
        verdict, note = "mixed", "Some folds hold up, others don't — treat the edge as marginal."
    return {"available": True, "data_source": src, "symbol": symbol, "timeframe": timeframe,
            "quality_gate": "off" if gate else "on",
            "folds": out, "total_folds": total, "positive_folds": pos,
            "oos_net_r": oos, "verdict": verdict, "note": note}


# ─────────────────────────────────── Monte Carlo ────────────────────────────
def monte_carlo(strategy: str, symbol: str, timeframe: str = "4h", *, bars: int = 4000,
                runs: int = 1000, seed: int = 1, ruin_r: float = 20.0,
                custom_spec: Optional[dict] = None, quality_gate: str = "on") -> dict:
    """Bootstrap-resample the realised trade sequence into a distribution of net
    R and max drawdown, plus probability of ruin / survival / recovery — how much
    of the result is luck vs edge, and how survivable it is. ``ruin_r`` is the
    drawdown (in R) that counts as account ruin."""
    rows, src = _fetch(symbol, timeframe, bars)
    if not rows:
        return {"available": False, "error": "Historical data not available. Load Binance data first."}
    m = _metrics_on(strategy, symbol, timeframe, _gate(quality_gate), rows, custom_spec)
    rs = _trade_rs(m[1]) if m else []
    if len(rs) < 10:
        return {"available": True, "data_source": src, "trades": len(rs),
                "error": "Not enough trades for a Monte Carlo distribution (need ≥ 10)."}
    rnd = random.Random(seed)
    runs = max(100, min(int(runs), 5000))
    nets, dds = [], []
    ruined = recovered = 0
    for _ in range(runs):
        eq = peak = dd = 0.0
        hit_ruin = False
        for _ in range(len(rs)):
            eq += rs[rnd.randrange(len(rs))]
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
            if peak - eq >= ruin_r:
                hit_ruin = True
        nets.append(eq)
        dds.append(dd)
        if hit_ruin:
            ruined += 1
        if dd >= ruin_r * 0.5 and eq >= peak - 1e-9:    # had a deep dip but ended at a new high
            recovered += 1
    nets.sort(); dds.sort()

    def pctl(a, p):
        return round(a[min(len(a) - 1, int(p * len(a)))], 2)

    # equity-path fan: bootstrap a bounded set of full equity curves, then take
    # per-step percentile bands (p5/p25/median/p75/p95) plus a few raw sample
    # paths for texture — this is what the frontend renders as a fan chart.
    steps = len(rs)
    path_runs = min(runs, 400)
    curves = []
    for _ in range(path_runs):
        eq = 0.0
        c = [0.0]
        for _ in range(steps):
            eq += rs[rnd.randrange(len(rs))]
            c.append(round(eq, 3))
        curves.append(c)
    bands = {"p5": [], "p25": [], "median": [], "p75": [], "p95": []}
    for t in range(steps + 1):
        col = sorted(c[t] for c in curves)
        bands["p5"].append(pctl(col, 0.05)); bands["p25"].append(pctl(col, 0.25))
        bands["median"].append(pctl(col, 0.5)); bands["p75"].append(pctl(col, 0.75))
        bands["p95"].append(pctl(col, 0.95))
    samples = [[round(v, 2) for v in c] for c in curves[:24]]

    deep = sum(1 for d in dds if d >= ruin_r * 0.5) or 1
    return {
        "available": True, "data_source": src, "runs": runs, "trades": len(rs), "ruin_r": ruin_r,
        "net_r": {"p5": pctl(nets, 0.05), "median": pctl(nets, 0.5),
                  "p95": pctl(nets, 0.95), "mean": round(sum(nets) / len(nets), 2)},
        "max_drawdown_r": {"median": pctl(dds, 0.5), "p95": pctl(dds, 0.95), "worst": round(dds[-1], 2)},
        "prob_profit_pct": round(sum(1 for x in nets if x > 0) / len(nets) * 100, 1),
        "expected_return_r": round(sum(nets) / len(nets), 2),
        "probability_of_ruin_pct": round(ruined / runs * 100, 1),
        "survival_probability_pct": round((runs - ruined) / runs * 100, 1),
        "recovery_probability_pct": round(recovered / deep * 100, 1),
        "paths": {"steps": steps, "bands": bands, "samples": samples},
    }



# ─────────────────────────────────── out-of-sample ──────────────────────────
def out_of_sample(strategy: str, symbol: str, timeframe: str = "4h", *, bars: int = 4000,
                  split: float = 0.7, tuning: Optional[dict] = None,
                  custom_spec: Optional[dict] = None, quality_gate: str = "on") -> dict:
    tuning = {**(tuning or {}), **_gate(quality_gate)}
    rows, src = _fetch(symbol, timeframe, bars)
    if not rows:
        return {"available": False, "error": "Historical data not available. Load Binance data first."}
    cut = int(len(rows) * split)
    tr = _metrics_on(strategy, symbol, timeframe, tuning, rows[:cut], custom_spec)
    te = _metrics_on(strategy, symbol, timeframe, tuning, rows[cut:], custom_spec)
    if not tr or not te:
        return {"available": False, "error": "Could not simulate one of the segments."}
    train, test = tr[0], te[0]
    if test["net_r"] > 0 and test.get("profit_factor", 0) >= 1:
        verdict, note = "holds", "The edge holds on unseen data."
    elif train["net_r"] > 0 and test["net_r"] <= 0:
        verdict, note = "overfit", "Profitable in-sample but not out-of-sample — do not trust it."
    else:
        verdict, note = "weak", "No clear out-of-sample edge."
    return {"available": True, "data_source": src, "split": split,
            "train": train, "test": test, "verdict": verdict, "note": note}


# ─────────────────────────────── sliced (regime/session/symbol) ──────────────
def sliced_performance(strategy: str, timeframe: str = "15m", *,
                       symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"),
                       limit: int = 800) -> dict:
    """Regime- / session- / symbol-conditional results for one strategy (#5:
    regime-based, session-based and symbol-based testing)."""
    from services.replay import build_replay
    from services.coach import _bucket
    from strategies.diagnosis import _hour, _session
    all_trades, by_symbol, sources = [], [], {}
    for sym in symbols:
        rep = build_replay(sym, timeframe, limit, strategy=strategy)
        sources[sym] = rep["meta"]["data_source"]
        cl = [t for t in rep["trades"] if t.get("rr") is not None]
        for t in cl:
            all_trades.append({**t, "symbol": sym})
        s = rep["stats"]
        by_symbol.append({"key": sym, "trades": s["trades"], "net_r": s["net_r"],
                          "win_rate": s["win_rate"], "avg_r": s.get("avg_rr", 0.0)})
    by_symbol.sort(key=lambda b: b["net_r"], reverse=True)
    return {
        "strategy": strategy, "timeframe": timeframe, "symbols": list(symbols),
        "by_regime": _bucket(all_trades, lambda t: t.get("regime", "—")),
        "by_session": _bucket(all_trades, lambda t: _session(_hour(t.get("entry_time", "")))),
        "by_symbol": by_symbol, "total_trades": len(all_trades), "data_sources": sources,
    }
