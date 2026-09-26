#!/usr/bin/env python3
"""Backtest one strategy on real Binance candles, with the robustness checks.

By default: the owner's 3-Candle Rejection · EMA 9/33 on BTCUSDT and
ETHUSDT, 15m and 1h, in three modes:

* ``on``  -- the Decision Brain quality gate, what an instance does by default;
* ``off`` -- the per-instance gate-off switch (size/account blocks only);
* ``raw`` -- research only: the strategy's own signals with no Decision Brain
  at all. No instance runs this; it measures the strategy by itself.

For each market it

1. syncs real Binance candles into the local cache first -- the same thing
   POST /data/sync does, so it only adds candles and never removes any
   (skip with --no-sync);
2. runs the whole period once, then the lab's out-of-sample split (70/30),
   walk-forward (4 folds) and Monte Carlo (1,000 reshuffles of the trades),
   exactly as the Backtesting Lab does (services/backtest_lab.py);
3. prints one table and a plain verdict, and writes everything to JSON.

It also counts the signals TradeBrain's losing-streak pause refused: after 5
losses in a row a symbol sits out 24 hours from its latest loss (the same rule
the live engine applies, services/quality_gate.py). Those signals were not
tested, so a run with many of them says less about the strategy.

Costs are the simulator's: 0.04% fee and 0.02% slippage on each side.
Nothing here trades, places an order or changes an instance.

On the server:

    cd /opt/nexus-trading-bot
    docker compose exec -T app python scripts/backtest_strategy.py

    # other markets, a longer history, one gate mode:
    docker compose exec -T app python scripts/backtest_strategy.py \\
        --symbols BTCUSDT,ETHUSDT,SOLUSDT --timeframes 15m,1h,4h --bars 10000 --gate both
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Run as `python scripts/backtest_strategy.py`: sys.path[0] is then scripts/,
# not the app root, so services/ and data/ would not import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_STRATEGY = "3-Candle Rejection · EMA 9/33"
#: Below this many trades a result is not evidence of anything.
MIN_TRADES = 30


def _sync(symbol: str, timeframe: str, bars: int) -> str:
    from config import settings
    from data.historical import HistoricalStore, sync
    try:
        res = sync(HistoricalStore(settings.market_db), symbol, timeframe, target_candles=bars)
    except Exception as exc:  # noqa: BLE001 -- report and carry on with what is cached
        return f"sync failed ({type(exc).__name__}: {exc}); using the cached candles"
    if "error" in res:
        return f"sync: {res['error']}; using the cached candles"
    return f"synced {res.get('fetched', 0)} candles from Binance"


def _when(bar) -> str:
    return bar.timestamp.strftime("%Y-%m-%d")


def run_market(strategy: str, symbol: str, timeframe: str, bars: int, gates: list[str],
               *, do_sync: bool, runs: int) -> dict:
    from services import backtest_lab as lab

    out: dict = {"symbol": symbol, "timeframe": timeframe}
    if do_sync:
        out["sync"] = _sync(symbol, timeframe, bars)
    rows, source = lab._fetch(symbol, timeframe, bars)
    if not rows:
        out["error"] = f"no real candles ({source})"
        return out
    out.update({"source": source, "candles": len(rows), "from": _when(rows[0]), "to": _when(rows[-1])})
    for gate in gates:
        tuning = lab._gate(gate)
        whole = lab._metrics_on(strategy, symbol, timeframe, tuning, rows)
        out[gate] = {
            "whole_period": whole[0] if whole else None,
            "streak_pauses": streak_pauses(whole[1]) if whole else None,
            "out_of_sample": lab.out_of_sample(strategy, symbol, timeframe, bars=bars, quality_gate=gate),
            "walk_forward": lab.walk_forward(strategy, symbol, timeframe, bars=bars, quality_gate=gate),
            "monte_carlo": lab.monte_carlo(strategy, symbol, timeframe, bars=bars, runs=runs, quality_gate=gate),
        }
        # The fan-chart paths are for the Lab's chart, not a terminal report.
        out[gate]["monte_carlo"].pop("paths", None)
    return out


def streak_pauses(results: dict) -> dict | None:
    """How many signals the 24-hour losing-streak pause refused in this run."""
    refused = [b for b in (results.get("blocked") or [])
               if str(b.get("reason", "")).startswith("losing-streak cooldown")]
    if not refused:
        return None
    return {"first": str(refused[0].get("time", ""))[:10], "signals_refused": len(refused)}


def _fmt(value, spec: str = ".2f") -> str:
    return "—" if value is None else format(value, spec)


def summary_rows(report: dict) -> list[str]:
    lines = [f"{'market':<14}{'gate':<5}{'trades':>7}{'win%':>7}{'PF':>6}{'net R':>8}{'maxDD%':>8}"
             f"  {'out-of-sample':<22}{'walk-forward':<22}{'Monte Carlo P(profit)':<20}"]
    for market in report["markets"]:
        name = f"{market['symbol']} {market['timeframe']}"
        if market.get("error"):
            lines.append(f"{name:<14}{market['error']}")
            continue
        for gate in report["gates"]:
            part = market.get(gate) or {}
            whole = part.get("whole_period") or {}
            oos = part.get("out_of_sample") or {}
            wf = part.get("walk_forward") or {}
            mc = part.get("monte_carlo") or {}
            if not oos.get("available") or not oos.get("test"):
                oos_text = "unavailable"
            elif not oos["test"].get("trades"):
                oos_text = "no trades in test"
            else:
                oos_text = f"{oos['verdict']} ({oos['test']['net_r']:+.1f}R/{oos['test']['trades']}t)"
            if not wf.get("available"):
                wf_text = "unavailable"
            elif not wf.get("total_folds"):
                wf_text = "too few trades"
            else:
                wf_text = f"{wf['verdict']} ({wf['positive_folds']}/{wf['total_folds']} folds)"
            mc_text = (f"{mc['prob_profit_pct']}% (ruin {mc['probability_of_ruin_pct']}%)"
                       if "prob_profit_pct" in mc else "needs ≥10 trades")
            lines.append(
                f"{name:<14}{gate:<5}{whole.get('trades', 0):>7}{_fmt(whole.get('win_rate'), '.1f'):>7}"
                f"{_fmt(whole.get('profit_factor')):>6}{_fmt(whole.get('net_r'), '+.1f'):>8}"
                f"{_fmt(whole.get('max_drawdown_pct'), '.1f'):>8}  {oos_text:<22}{wf_text:<22}{mc_text:<20}")
            name = ""
    for market in report["markets"]:
        for gate in report["gates"]:
            pauses = (market.get(gate) or {}).get("streak_pauses")
            if pauses:
                lines.append(f"  ! {market['symbol']} {market['timeframe']} gate {gate}: "
                             f"{pauses['signals_refused']} signal(s) refused by the 24-hour pause after "
                             f"5 losses in a row (first on {pauses['first']}); those were not tested.")
    return lines


def verdict(report: dict) -> list[str]:
    """Plain words, from the lab's own verdicts. Never calls anything profitable."""
    lines = []
    for gate in report["gates"]:
        markets = [m for m in report["markets"] if not m.get("error") and m.get(gate)]
        trades = sum((m[gate]["whole_period"] or {}).get("trades", 0) for m in markets)
        holds = [f"{m['symbol']} {m['timeframe']}" for m in markets
                 if (m[gate]["out_of_sample"] or {}).get("verdict") == "holds"
                 and ((m[gate]["out_of_sample"] or {}).get("test") or {}).get("trades", 0) >= 10
                 and (m[gate]["walk_forward"] or {}).get("verdict") == "robust"
                 and (m[gate]["walk_forward"] or {}).get("total_folds", 0) >= 2]
        label = {"on": "gate on ", "off": "gate off", "raw": "raw     "}[gate]
        if trades < MIN_TRADES:
            lines.append(f"{label}: {trades} trades in total — too few to say anything.")
        elif holds:
            lines.append(f"{label}: holds out-of-sample AND robust in walk-forward on {', '.join(holds)}. "
                         "That is a candidate for forward paper, not a proven edge.")
        else:
            lines.append(f"{label}: no market held up both out-of-sample and in walk-forward. "
                         "No evidence of an edge in this test.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY, help="Backtesting Lab preset name")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--timeframes", default="15m,1h")
    parser.add_argument("--bars", type=int, default=10000, help="candles per market (max 10000)")
    parser.add_argument("--gate", choices=("on", "off", "raw", "both", "all"), default="all",
                        help="on (instance default), off (the switch), raw (no Decision Brain; research "
                             "only), both (on and off) or all")
    parser.add_argument("--runs", type=int, default=1000, help="Monte Carlo reshuffles")
    parser.add_argument("--no-sync", action="store_true", help="use only candles already cached")
    parser.add_argument("--json", default="", help="where to write the full report (default: beside the market cache)")
    args = parser.parse_args(argv)

    from services.strategy_presets import PRESETS
    if args.strategy not in PRESETS:
        print(f"Unknown strategy {args.strategy!r}. Presets: {', '.join(PRESETS)}", file=sys.stderr)
        return 2
    gates = {"both": ["on", "off"], "all": ["on", "off", "raw"]}.get(args.gate, [args.gate])
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    bars = max(600, min(int(args.bars), 10000))

    report = {"strategy": args.strategy, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "bars": bars, "gates": gates, "costs": "0.04% fee + 0.02% slippage per side",
              "markets": []}
    for symbol in symbols:
        for timeframe in timeframes:
            print(f"… {symbol} {timeframe}", file=sys.stderr, flush=True)
            market = run_market(args.strategy, symbol, timeframe, bars, gates,
                                do_sync=not args.no_sync, runs=args.runs)
            report["markets"].append(market)
            detail = market.get("error") or f"{market['candles']} candles {market['from']} → {market['to']} ({market['source']})"
            print(f"  {market.get('sync', 'no sync')} · {detail}", file=sys.stderr, flush=True)

    sources = sorted({m["source"] for m in report["markets"] if m.get("source")}) or ["none"]
    print(f"\n{args.strategy} · data: {', '.join(sources)} · {report['costs']}\n")
    print("\n".join(summary_rows(report)))
    print("\n" + "\n".join(verdict(report)))
    if "raw" in gates:
        print("raw = the strategy with no Decision Brain. No instance runs that way; it shows the "
              "strategy's own rules.")
    print("\nR = the trade's initial risk. A backtest is not a forward result; any candidate still "
          "has to prove itself on paper with live data.")

    path = Path(args.json) if args.json else None
    if path is None:
        from config import settings
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = Path(settings.market_db).resolve().parent / "reports" / f"backtest-{stamp}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nFull report: {path}")
    except OSError as exc:
        print(f"\nCould not write the report ({exc}); the table above is complete.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
