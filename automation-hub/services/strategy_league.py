"""Strategy League — which strategies earn, which lose, and how they relate.

Runs every built-in strategy over the SAME real candles (multiple symbols),
ranks them by the numbers that matter, and computes the pairwise correlation
of their daily return streams. Two lessons this table teaches at a glance:

  1. WIN RATE ALONE IS A TRAP. A 35% win rate at 3R-per-winner out-earns a
     60% win rate at 0.5R. Expectancy (average R per trade) is the ranking
     that pays, so that's the default sort — win rate is shown next to it.
  2. CORRELATION DECIDES WHAT TO RUN TOGETHER. Two profitable strategies
     that win at the SAME times are one bet sized twice (their drawdowns
     stack). A profitable pair with LOW correlation smooths the equity
     curve — that's the pair worth running side by side.

Real data only in production (honest no-data verdict otherwise).
"""
from __future__ import annotations

from math import sqrt

LEAGUE_STRATEGIES = ["Decision Brain", "Trend Following", "Supply/Demand",
                     "Breakout Retest", "EMA 8/30", "EMA 20/50", "Liquidity Sweep"]
MIN_TRADES = 10          # below this a strategy is "insufficient sample", not judged
REDUNDANT = 0.6          # daily-R correlation above this = same bet twice
DIVERSIFYING = 0.25      # below this = genuinely different return stream


def pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 5 or n != len(b):
        return None
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sqrt(sum((x - ma) ** 2 for x in a))
    vb = sqrt(sum((y - mb) ** 2 for y in b))
    if va == 0 or vb == 0:
        return None
    return round(cov / (va * vb), 3)


def _r(trade: dict) -> float:
    try:
        return float(trade.get("r") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _daily_r(trades: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in trades:
        day = (t.get("exit_time") or "")[:10]
        if day:
            out[day] = out.get(day, 0.0) + _r(t)
    return out


MIN_BARS = 400           # below this a symbol cannot be compared on, and is dropped


def _candles(symbol: str, timeframe: str, bars: int, venue: str,
             require_real: bool) -> tuple[list, str, dict]:
    """Live venue candles when they can be had, the local cache when they cannot.

    The league is a backtest, so it needs a series rather than a tick -- but a
    series that ends now is a different measurement from one that ends whenever
    the last /data/sync happened to run. On a host where that sync has not run
    for a day, every verdict in this table was computed on yesterday's market
    while claiming to rank strategies today.

    Live first, therefore, and the cache named as a fallback rather than
    silently substituted. Neither path is ever synthetic in production.
    """
    from services.live_candle_source import LiveCandlesUnavailable, judge, live_series

    attempts = []
    if require_real:
        try:
            rows = live_series(symbol, timeframe, venue, limit=bars)
        except LiveCandlesUnavailable as exc:
            attempts.append({"source": f"venue {venue}", "error": str(exc)})
        else:
            verdict = judge(symbol, timeframe, rows)
            attempts.append({"source": f"venue {venue}",
                             "freshness": verdict.to_dict()})
            return rows, f"venue {venue} (live)", {"attempts": attempts,
                                                   "freshness": verdict.to_dict(),
                                                   "live": True}

    from data.market_data import get_bars

    rows, src = get_bars(symbol, n=bars, timeframe=timeframe,
                         require_real=require_real)
    verdict = judge(symbol, timeframe, rows) if rows else None
    attempts.append({"source": "local candle cache",
                     "freshness": verdict.to_dict() if verdict else None,
                     "error": None if rows else src})
    return rows, src, {"attempts": attempts,
                       "freshness": verdict.to_dict() if verdict else None,
                       "live": False}


def league(symbols=("BTCUSDT", "ETHUSDT"), timeframe: str = "1h", bars: int = 2500,
           strategies=None, require_real: bool = True,
           venue: str = "binance_usdm") -> dict:
    from services.strategy_presets import _run_on

    strategies = list(strategies or LEAGUE_STRATEGIES)
    data = {}
    provenance: dict[str, dict] = {}
    dropped: list[dict] = []
    for sym in symbols:
        rows, src, meta = _candles(sym, timeframe, bars, venue, require_real)
        if rows and len(rows) >= MIN_BARS:
            data[sym] = (rows, src)
            provenance[sym] = {
                "source": src, "candles": len(rows),
                "first": rows[0].timestamp.isoformat(),
                "last": rows[-1].timestamp.isoformat(), **meta}
            continue
        # A symbol quietly disappearing from the comparison is how a league
        # labelled "BTCUSDT + ETHUSDT" ends up measuring one of them.
        dropped.append({"symbol": sym, "candles": len(rows or []),
                        "required": MIN_BARS, "source": src, **meta})
    if not data:
        return {"available": False, "verdict": "no-real-data",
                "dropped": dropped,
                "detail": "No real candles — the venue was unreachable and the "
                          "local cache is empty. Press 'Load real Binance data' "
                          "in the Bot Control Center, or check outbound access."}

    table = []
    daily: dict[str, dict[str, float]] = {}
    for strat in strategies:
        agg_trades: list[dict] = []
        wins = total = 0
        net = dd = 0.0
        for sym, (rows, _src) in data.items():
            res = _run_on(strat, sym, timeframe, {}, None, rows)
            if "error" in res:
                continue
            trades = res.get("trades", [])
            agg_trades.extend(trades)
            n = res.get("total_trades", 0)
            total += n
            wins += round(res.get("win_rate", 0) / 100 * n)
            net += res.get("net_r", 0.0)
            dd = max(dd, res.get("max_drawdown_pct", 0.0) or 0.0)
        # Profit factor comes from the trades themselves. It used to read
        # gross_profit_r / gross_loss_r off the simulator result, and the
        # simulator has never produced either key -- so the column rendered a
        # dash for every strategy in every league, which reads as "not
        # applicable" rather than "never computed". The per-trade R is already
        # aggregated here for the correlation stream; this is the same number.
        gp = sum(_r(t) for t in agg_trades if _r(t) > 0)
        gl = abs(sum(_r(t) for t in agg_trades if _r(t) < 0))
        expectancy = round(net / total, 3) if total else None
        if total < MIN_TRADES:
            verdict = "insufficient-sample"
        elif expectancy is not None and expectancy > 0.05:
            verdict = "earning"
        elif expectancy is not None and expectancy < -0.05:
            verdict = "losing"
        else:
            verdict = "breakeven"
        table.append({"strategy": strat, "trades": total,
                      "win_rate": round(100 * wins / total, 1) if total else None,
                      "expectancy_r": expectancy, "net_r": round(net, 2),
                      "profit_factor": round(gp / gl, 2) if gl else None,
                      "max_drawdown_pct": round(dd, 1), "verdict": verdict})
        daily[strat] = _daily_r(agg_trades)

    # rank by what pays: expectancy (with enough sample), then net R
    table.sort(key=lambda r: (r["verdict"] != "insufficient-sample",
                              r["expectancy_r"] if r["expectancy_r"] is not None else -9),
               reverse=True)

    # pairwise correlation of daily R streams (union of active days)
    judged = [r["strategy"] for r in table if r["verdict"] != "insufficient-sample"]
    correlations = []
    for i, a in enumerate(judged):
        for b in judged[i + 1:]:
            days = sorted(set(daily[a]) | set(daily[b]))
            corr = pearson([daily[a].get(d, 0.0) for d in days],
                           [daily[b].get(d, 0.0) for d in days])
            if corr is None:
                continue
            rel = ("redundant" if corr >= REDUNDANT else
                   "diversifying" if corr <= DIVERSIFYING else "related")
            correlations.append({"a": a, "b": b, "correlation": corr, "relation": rel})

    # the actionable combo: both profitable, lowest correlation
    earning = {r["strategy"] for r in table if r["verdict"] == "earning"}
    combos = [c for c in correlations if c["a"] in earning and c["b"] in earning]
    combos.sort(key=lambda c: c["correlation"])
    best_combo = combos[0] if combos else None

    guidance = ["Ranked by expectancy (avg R per trade) — win rate alone misleads: "
                "35% winners at 3R beat 60% winners at 0.5R.",
                "Correlation shows which strategies win at the same time: 'redundant' "
                "pairs stack drawdowns; 'diversifying' pairs smooth the curve."]
    if best_combo:
        guidance.append(f"Best pairing right now: {best_combo['a']} + {best_combo['b']} "
                        f"(corr {best_combo['correlation']}) — both earn and their "
                        f"return streams differ.")
    stale = [sym for sym, row in provenance.items()
             if (row.get("freshness") or {}).get("status") not in (None, "FRESH")]
    if stale:
        guidance.append(
            "Measured on candles that are not current for "
            f"{', '.join(stale)} — the ranking describes an older market than "
            "the one you are trading now.")
    if dropped:
        guidance.append(
            "Not measured: " + ", ".join(
                f"{row['symbol']} ({row['candles']} candles, needs {row['required']})"
                for row in dropped) + ".")
    return {"available": True, "timeframe": timeframe,
            "symbols": list(data.keys()),
            "data_source": next(iter(data.values()))[1],
            "live": all(row.get("live") for row in provenance.values()),
            "provenance": provenance, "dropped": dropped,
            "window": {"bars_requested": bars,
                       "first": min(row["first"] for row in provenance.values()),
                       "last": max(row["last"] for row in provenance.values())},
            "table": table, "correlations": correlations,
            "best_combo": best_combo, "guidance": guidance}
