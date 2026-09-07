"""Strategy presets + the control-center simulation/compare logic.

Maps the human strategy names shown in the top control bar to runnable specs,
applies the brain-tuning panel settings, and reruns REAL simulations so the user
can quickly switch strategy / symbol / timeframe / brain settings and see how
performance changes. Rule-based presets run through the brain-aware custom
simulator (full diagnosis + blocked log + quality score); class-based presets
run through the built-in strategy simulator with the same quality gate.
"""
from __future__ import annotations

from typing import Optional
from strategies.builtin_versions import builtin_strategy_version

# The strategy options the control bar offers.
PRESETS: dict = {
    "Decision Brain": {"kind": "builtin", "key": "brain"},
    "Trend Following": {"kind": "builtin", "key": "supertrend"},
    "Supply/Demand": {"kind": "builtin", "key": "smc"},
    "Breakout Retest": {"kind": "custom", "side": "long",
                        "rules": [{"type": "breakout", "lookback": 20, "dir": "up"},
                                  {"type": "pullback", "period": 20, "dir": "up"}]},
    "Support/Resistance Rejection": {"kind": "custom", "side": "long",
                                     "rules": [{"type": "support_bounce", "lookback": 30,
                                                "dir": "support", "tolerance_pct": 0.5}]},
    "EMA 8/30": {"kind": "custom", "side": "long",
                 "rules": [{"type": "ema_cross", "fast": 8, "slow": 30, "dir": "above"}]},
    "EMA 20/50": {"kind": "custom", "side": "long",
                  "rules": [{"type": "ema_cross", "fast": 20, "slow": 50, "dir": "above"}]},
    "Liquidity Sweep": {"kind": "builtin", "key": "liquidity_sweep"},
    "Adaptive MTF Trend Pullback": {"kind": "builtin", "key": "adaptive_trend_pullback"},
    "Custom Strategy": {"kind": "custom_user"},
}
STRATEGY_OPTIONS = list(PRESETS)

# Real strategy registry the selector pulls from (id / version / metadata).
REGISTRY = [
    {"id": "decision_brain", "name": "Decision Brain", "version": builtin_strategy_version("brain"), "kind": "builtin",
     "timeframes": ["1h", "4h"], "description": "Multi-factor trend + regime + momentum"},
    {"id": "trend_following", "name": "Trend Following", "version": builtin_strategy_version("supertrend"), "kind": "builtin",
     "timeframes": ["4h", "1d"], "description": "Supertrend ATR trend-following"},
    {"id": "supply_demand", "name": "Supply/Demand", "version": "1.0", "kind": "builtin",
     "timeframes": ["15m", "4h"], "description": "SMC: liquidity sweep + CHoCH/BOS + FVG"},
    {"id": "breakout_retest", "name": "Breakout Retest", "version": "1.0", "kind": "custom",
     "timeframes": ["15m", "1h"], "description": "N-bar breakout then EMA pullback retest"},
    {"id": "sr_rejection", "name": "Support/Resistance Rejection", "version": "1.0", "kind": "custom",
     "timeframes": ["15m", "1h"], "description": "Reaction at a recent support/resistance level"},
    {"id": "ema_8_30", "name": "EMA 8/30", "version": "1.0", "kind": "custom",
     "timeframes": ["5m", "15m"], "description": "Fast EMA 8 over EMA 30 cross"},
    {"id": "ema_20_50", "name": "EMA 20/50", "version": "1.0", "kind": "custom",
     "timeframes": ["1h", "4h"], "description": "EMA 20 over EMA 50 cross"},
    {"id": "liquidity_sweep", "name": "Liquidity Sweep", "version": "1.0", "kind": "builtin",
     "timeframes": ["5m", "15m", "1h", "4h"], "description": "Stop-hunt wick + confirmed reclaim + ATR invalidation"},
    {"id": "adaptive_trend_pullback", "name": "Adaptive MTF Trend Pullback",
     "version": builtin_strategy_version("adaptive_trend_pullback"), "kind": "builtin",
     "timeframes": ["5m"],
     "description": "5m entry + native 1h regime gate + native 4h bias + 15m pullback context"},
    {"id": "custom", "name": "Custom Strategy", "version": "1.0", "kind": "custom_user",
     "timeframes": [], "description": "User-built rule strategy"},
]
_NAME_TO_ID = {r["name"]: r["id"] for r in REGISTRY}
# Crypto pairs have REAL data (live ccxt + backfill); they power the live
# engine and the control bar. Stocks appear only in the simulation pickers,
# clearly labeled — the AAPL sample is real daily data, the rest simulate.
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
           "BNBUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT"]
TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d", "1w"]
MODES = ["Simulation", "Paper Trading", "Live Trading (locked)"]

# default brain-tuning panel
DEFAULT_TUNING = {
    "min_score": 60, "rr": 2.0,
    "trend_filter": True, "volume_filter": False, "regime_filter": True,
    "session_filter": False, "max_trades_per_day": 0, "cooldown_after_loss": 0,
    "max_consecutive_losses": 0,
}


def _risk_kwargs(tuning: dict) -> dict:
    """The risk-manager limits the simulator enforces (priority: risk manager)."""
    t = {**DEFAULT_TUNING, **(tuning or {})}
    return {"max_trades_per_day": int(t["max_trades_per_day"]),
            "cooldown_after_loss": int(t["cooldown_after_loss"]),
            "max_consecutive_losses": int(t["max_consecutive_losses"])}


def _apply_tuning(spec: dict, tuning: dict) -> dict:
    """Translate the brain-tuning panel into concrete spec settings."""
    t = {**DEFAULT_TUNING, **(tuning or {})}
    spec["min_score"] = int(t["min_score"])
    spec["target"] = {"type": "rr", "rr": float(t["rr"])}
    # trend + regime live inside the brain quality filter
    spec["quality_filter"] = bool(t["trend_filter"] or t["regime_filter"])
    rules = list((spec.get("entry") or {}).get("rules") or [])
    if t["volume_filter"] and not any(r.get("type") == "volume" for r in rules):
        rules.append({"type": "volume", "period": 20, "op": "above"})
    spec.setdefault("entry", {"op": "AND"})["rules"] = rules
    if t["session_filter"]:
        spec["session"] = {"start": 7, "end": 21}      # London+NY window (UTC)
    else:
        spec.pop("session", None)
    spec["max_trades_per_day"] = int(t["max_trades_per_day"])
    spec["cooldown_after_loss"] = int(t["cooldown_after_loss"])
    spec["max_consecutive_losses"] = int(t["max_consecutive_losses"])
    return spec


def resolve(strategy: str, symbol: str, timeframe: str, tuning: dict,
            custom_spec: Optional[dict] = None) -> dict:
    """Return a runnable descriptor: {kind, key|spec, label}."""
    preset = PRESETS.get(strategy)
    if preset is None:
        return {"error": f"unknown strategy {strategy}"}
    if preset["kind"] == "builtin":
        return {"kind": "builtin", "key": preset["key"], "label": strategy}
    if preset["kind"] == "custom_user":
        if not custom_spec:
            return {"error": "Custom Strategy selected but no custom spec provided."}
        spec = {**custom_spec, "symbol": symbol, "timeframe": timeframe,
                "name": custom_spec.get("name", "Custom")}
    else:
        spec = {"name": strategy, "symbol": symbol, "timeframe": timeframe,
                "side": preset.get("side", "long"),
                "entry": {"op": "AND", "rules": [dict(r) for r in preset["rules"]]},
                "stop": {"type": "atr", "mult": 1.5, "period": 14},
                "risk_per_trade_pct": 0.01, "mtf_filter": True}
    return {"kind": "custom", "spec": _apply_tuning(spec, tuning), "label": strategy}


def _run_on(strategy: str, symbol: str, timeframe: str, tuning: dict, custom_spec: dict,
            rows, macro: str = None, confirmation: str = None, fill_cost: float = 0.0) -> dict:
    """Run a resolved configuration over a given bar list (no fetch). Returns the
    raw results dict (with diagnosis, _mtf_gate, _spec) or {'error': ...}.

    ``fill_cost`` (per-side, as a fraction of price) adds the realistic-fill
    spread+slippage+latency on top of the base cost — the same execution model
    the paper engine uses, so backtests stop assuming perfect fills."""
    from strategies.custom import simulate, simulate_strategy
    from strategies.brain import TradeBrain
    from strategies.diagnosis import diagnose
    from services.mtf_engine import make_trend_lookup

    desc = resolve(strategy, symbol, timeframe, tuning or {}, custom_spec)
    if "error" in desc:
        return desc

    mtf_lookup = mtf_tfs = None
    requested = [tf for tf in (confirmation, macro) if tf and tf != timeframe]
    if requested:
        mtf_lookup = make_trend_lookup(rows, timeframe, requested)
        mtf_tfs = mtf_lookup.timeframes

    slippage = 0.0002 + max(0.0, float(fill_cost))
    min_score = int((tuning or {}).get("min_score", DEFAULT_TUNING["min_score"]))
    brain = TradeBrain()
    if desc["kind"] == "builtin":
        from services.strategy_factory import make_builtin_strategy
        strat = make_builtin_strategy(desc["key"], symbol)
        results = simulate_strategy(strat, rows, brain=brain, min_score=min_score, slippage=slippage,
                                    mtf_lookup=mtf_lookup, mtf_tfs=mtf_tfs,
                                    **_risk_kwargs(tuning))
    else:
        spec = desc["spec"]
        use_brain = spec.get("quality_filter", True)
        results = simulate(spec, rows, brain=brain if use_brain else None, slippage=slippage,
                           min_score=min_score if use_brain else 0,
                           mtf_lookup=mtf_lookup, mtf_tfs=mtf_tfs)
    results["diagnosis"] = diagnose(results, results.get("blocked"))
    results["_mtf_gate"] = list(mtf_tfs) if mtf_tfs else []
    results["_spec"] = desc.get("spec")
    return results


_METRIC_KEYS = ("total_trades", "win_rate", "profit_factor", "net_r",
                "max_drawdown_pct", "expectancy_r")


def _metrics(results: dict) -> dict:
    return {"trades": results.get("total_trades", 0),
            **{k: results.get(k, 0) for k in _METRIC_KEYS if k != "total_trades"}}


def make_replay_strategy(strategy: str, symbol: str, timeframe: str, custom_spec: dict = None):
    """Return a strategy OBJECT (with .on_bar) for the named strategy, plus its
    registry id — so the replay engine uses the SELECTED strategy's real entry
    logic, not a hardcoded one. Internal quality/MTF gates are off here; the
    replay engine applies scoring + the multi-timeframe gate itself."""
    desc = resolve(strategy, symbol, timeframe, {}, custom_spec)
    if "error" in desc:
        return None, desc["error"], None
    sid = _NAME_TO_ID.get(strategy, strategy)
    if desc["kind"] == "builtin":
        from services.strategy_factory import make_builtin_strategy
        strat = make_builtin_strategy(desc["key"], symbol)
        # Characterization replay preserves old research fixtures explicitly;
        # the REAL_PAPER factory never enables these legacy clocks.
        if hasattr(strat, "allow_legacy_htf_resample"):
            strat.allow_legacy_htf_resample = True
    else:
        from strategies.custom_adapter import CustomStrategyAdapter
        spec = {**desc["spec"], "quality_filter": False, "mtf_filter": False}
        strat = CustomStrategyAdapter(symbol, spec)
    strat.strategy_id = sid
    return strat, None, sid


def run_simulation(strategy: str, symbol: str, timeframe: str, *, tuning: dict = None,
                   custom_spec: dict = None, bars: int = 4000,
                   macro: str = None, confirmation: str = None, realistic: bool = False) -> dict:
    """Run a REAL simulation for the chosen control-bar configuration.

    ``macro`` / ``confirmation`` are the higher timeframes the multi-timeframe
    gate checks (chosen in the control bar); a trade against either is blocked.
    ``realistic`` charges the paper engine's fill friction (spread + slippage +
    latency) so the backtest matches realistic execution, not perfect fills.
    """
    from data.market_data import get_bars
    rows, source = get_bars(symbol, n=max(600, min(int(bars), 10000)), timeframe=timeframe,
                            require_real=True)
    if not rows:
        return {"error": "Historical data not available. Please load Binance data first "
                         "(run /data/sync).", "data_source": source, "available": False}
    fill_cost = _realistic_fill_cost() if realistic else 0.0
    results = _run_on(strategy, symbol, timeframe, tuning or {}, custom_spec, rows, macro, confirmation,
                      fill_cost=fill_cost)
    if "error" in results:
        return results
    gate = results.pop("_mtf_gate", [])
    spec = results.pop("_spec", None)
    return {
        "strategy": strategy, "symbol": symbol, "timeframe": timeframe, "mtf_gate": gate,
        "data_source": source, "available": True, "results": results,
        "warning": underperforming(results), "spec": spec, "fills": "realistic" if realistic else "ideal",
    }


def _realistic_fill_cost() -> float:
    """Per-side fill friction shared with the live paper engine (spread/2 +
    slippage + latency), so backtests and the engine use one execution model."""
    from services.fill_model import RealisticFill
    return RealisticFill().cost_pct


def auto_tune(strategy: str, symbol: str, timeframe: str, *, macro: str = None,
              confirmation: str = None, custom_spec: dict = None, bars: int = 4000,
              split: float = 0.7) -> dict:
    """Search the brain-tuning space on real data with a train/test split and an
    overfit verdict — the bot's 'tune this losing strategy' helper. Optimises on
    the train slice, validates on the unseen test slice."""
    from data.market_data import get_bars
    rows, source = get_bars(symbol, n=max(800, min(int(bars), 10000)), timeframe=timeframe,
                            require_real=True)
    if not rows:
        return {"available": False,
                "error": "Historical data not available. Please load Binance data first."}
    cut = int(len(rows) * split)
    train, test = rows[:cut], rows[cut:]

    def metric_on(t, segment):
        r = _run_on(strategy, symbol, timeframe, t, custom_spec, segment, macro, confirmation)
        return _metrics(r) if "error" not in r else {"trades": 0, "net_r": -1e9, "profit_factor": 0}

    base = {**DEFAULT_TUNING}
    base_train, base_test = metric_on(base, train), metric_on(base, test)

    # small grid (keep it tight to limit overfitting)
    best = None
    trials = []
    for ms in (55, 65, 75):
        for rr in (1.5, 2.0, 2.5):
            tuning = {**DEFAULT_TUNING, "min_score": ms, "rr": rr}
            m = metric_on(tuning, train)
            trials.append({"min_score": ms, "rr": rr, **m})
            if m["trades"] >= 10 and (best is None or m["net_r"] > best["m"]["net_r"]):
                best = {"tuning": tuning, "m": m}
    if best is None:                                   # nothing traded enough -> keep baseline
        best = {"tuning": base, "m": base_train}

    val = metric_on(best["tuning"], test)
    improved = (val["net_r"] > base_test["net_r"] and val["profit_factor"] >= 1
                and val["trades"] >= 10)
    overfit = best["m"]["net_r"] > base_train["net_r"] and val["net_r"] <= base_test["net_r"]
    if improved:
        verdict, note = "improvement", ("Tuned settings improve out-of-sample. Validate in paper "
                                        "trading before any live use.")
    elif overfit:
        verdict, note = "overfit", "Tuned settings help on training data but NOT on unseen data — don't adopt."
    else:
        verdict, note = "no_improvement", "No tuning in the grid beat the baseline out-of-sample."

    return {
        "available": True, "strategy": strategy, "symbol": symbol, "timeframe": timeframe,
        "data_source": source, "best_tuning": best["tuning"], "train": best["m"],
        "validation": val, "baseline_train": base_train, "baseline_test": base_test,
        "verdict": verdict, "note": note,
        "trials": sorted(trials, key=lambda t: t["net_r"], reverse=True)[:9],
    }


def underperforming(results: dict) -> Optional[dict]:
    """Return a warning when a strategy is losing / weak / too drawdown-heavy."""
    n = results.get("total_trades", 0)
    if n < 10:
        return None                       # not enough trades to judge
    pf = results.get("profit_factor", 0)
    wr = results.get("win_rate", 0)
    dd = results.get("max_drawdown_pct", 0)
    reasons = []
    if pf < 1.0:
        reasons.append(f"profit factor {pf} (< 1.0)")
    if wr < 40:
        reasons.append(f"win rate {wr}% (low)")
    if dd > 30:
        reasons.append(f"max drawdown {dd}% (high)")
    if not reasons:
        return None
    return {"level": "warning",
            "message": ("This strategy is underperforming (" + ", ".join(reasons) + "). "
                        "Test another strategy, timeframe, or adjust the brain filters.")}


def compare(a: dict, b: dict, *, bars: int = 4000) -> dict:
    """Compare two control configurations on real data and pick a winner."""
    ra = run_simulation(a["strategy"], a["symbol"], a["timeframe"],
                        tuning=a.get("tuning"), custom_spec=a.get("custom_spec"), bars=bars,
                        macro=a.get("macro"), confirmation=a.get("confirmation"))
    rb = run_simulation(b["strategy"], b["symbol"], b["timeframe"],
                        tuning=b.get("tuning"), custom_spec=b.get("custom_spec"), bars=bars,
                        macro=b.get("macro"), confirmation=b.get("confirmation"))
    if not ra.get("available") or not rb.get("available"):
        return {"error": "Historical data not available for one or both configurations.",
                "a": ra, "b": rb}

    def score(r):
        s = r["results"]
        return (s.get("net_r", -1e9), s.get("profit_factor", 0))
    winner = "A" if score(ra) >= score(rb) else "B"
    return {"a": ra, "b": rb, "winner": winner,
            "summary": f"{(ra if winner == 'A' else rb)['strategy']} "
                       f"({(ra if winner == 'A' else rb)['timeframe']}) wins on net R."}
