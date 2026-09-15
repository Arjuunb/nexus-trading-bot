#!/usr/bin/env python3
"""Replay the Nexus PA rulebook v0.1 over closed candles and show every decision.

The point of this script is the thing the rulebook keeps insisting on: a bot
cannot trade an adjective. For each 5M decision boundary it prints what the
engine concluded and the measured reason -- which zone, which regime, which
mandatory measure failed and by how much -- so a setup that never fires can be
told apart from a setup that fires and is rejected on net RR.

Research and replay only. Chapter 2 permits exactly this use of historical data
and forbids the other one: "Historical REST data may warm indicators and
structure. It must not create retrospective forward orders." Nothing here
touches a broker, a session or a database; it prints.

    python scripts/pa_rulebook_replay.py --symbol BTCUSDT --bars 3000
    python scripts/pa_rulebook_replay.py --symbol ETHUSDT --strategy flip --verbose
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.types import Bar                                          # noqa: E402
from services.pa_rulebook_v01 import (                             # noqa: E402
    CONFIRM_TF, CONTEXT_TF, FLIP_RETEST_ID, RULEBOOK_VERSION, SETUP_TF,
    SR_REJECTION_ID, CostModel, PriceActionRulebookEngine, RulebookConfig,
    SetupState,
)

STRATEGY_CHOICES = {"rejection": SR_REJECTION_ID, "flip": FLIP_RETEST_ID,
                    "both": None}


def _load(symbol: str, timeframe: str, bars: int) -> list[Bar]:
    """Closed candles for one timeframe, real data only.

    ``require_real=True`` is not caution, it is the rulebook's rule: the
    bundled sample and the synthetic generator are legitimate for charts and
    fixtures and must never reach a strategy decision, even a replayed one --
    a research result measured on manufactured candles is worse than no result.
    """
    from data.market_data import get_bars

    rows, source = get_bars(symbol, n=bars, timeframe=timeframe, require_real=True)
    return list(rows), source


def _slice_upto(rows: list[Bar], boundary) -> list[Bar]:
    """Every candle that had closed at `boundary`. Causality, not convenience."""
    return [row for row in rows if row.timestamp < boundary]


def replay(symbol: str, bars: int, strategy: str, equity: float,
           verbose: bool, loader=_load) -> int:
    config = RulebookConfig(symbol=symbol)
    config.validate()
    chosen = STRATEGY_CHOICES[strategy]
    engine = PriceActionRulebookEngine(
        config, CostModel(), strategies=(chosen,) if chosen else None)

    context, ctx_source = loader(symbol, CONTEXT_TF, max(bars // 12, config.warmup_bars + 50))
    setups, setup_source = loader(symbol, SETUP_TF, max(bars // 3, config.warmup_bars + 50))
    confirms, confirm_source = loader(symbol, CONFIRM_TF, bars)

    # An empty frame must stop the run, not produce a tidy "0 accepted" report.
    # A negative result and no data at all look identical once summarised, and
    # the second one silently answers a question nobody asked.
    for timeframe, rows, source in ((CONTEXT_TF, context, ctx_source),
                                    (SETUP_TF, setups, setup_source),
                                    (CONFIRM_TF, confirms, confirm_source)):
        if not rows:
            raise SystemExit(f"no {timeframe} candles for {symbol} (source: {source})")

    print(f"Nexus PA rulebook v{RULEBOOK_VERSION} -- RESEARCH REPLAY, NO ORDERS")
    print(f"  symbol    {symbol}   strategies {engine.strategies}")
    print(f"  {CONTEXT_TF:>4} {len(context):>6} candles  {ctx_source}")
    print(f"  {SETUP_TF:>4} {len(setups):>6} candles  {setup_source}")
    print(f"  {CONFIRM_TF:>4} {len(confirms):>6} candles  {confirm_source}")
    print()

    blockers: Counter = Counter()
    regimes: Counter = Counter()
    raised = confirmed = accepted = 0
    last_context = last_setup = None

    for bar in confirms:
        boundary = bar.timestamp
        ctx_slice = _slice_upto(context, boundary)
        setup_slice = _slice_upto(setups, boundary)
        confirm_slice = _slice_upto(confirms, boundary) + [bar]
        if len(ctx_slice) < config.warmup_bars or len(setup_slice) < 2:
            continue

        if ctx_slice[-1].timestamp != last_context:
            last_context = ctx_slice[-1].timestamp
            engine.update_context(ctx_slice)
        if setup_slice[-1].timestamp != last_setup:
            last_setup = setup_slice[-1].timestamp
            setup_decision = engine.on_setup_close(setup_slice)
            if setup_decision.setup is not None and not setup_decision.setup.terminal:
                if setup_decision.setup.state in (SetupState.WAIT_CONFIRM,
                                                  SetupState.WAIT_RETEST):
                    raised += 1
                    if verbose:
                        print(f"  {boundary:%Y-%m-%d %H:%M}  SETUP   "
                              f"{setup_decision.setup.strategy_id} "
                              f"{setup_decision.setup.direction} "
                              f"{setup_decision.setup.state.value} "
                              f"zone={setup_decision.setup.zone.id}")
            elif setup_decision.blocker is not None:
                blockers[setup_decision.blocker.value] += 1

        decision = engine.on_confirm_close(confirm_slice, equity=equity)
        regimes[decision.regime.value] += 1
        if decision.blocker is not None:
            blockers[decision.blocker.value] += 1
        if decision.state is SetupState.CONFIRMED:
            confirmed += 1
            plan = decision.plan
            if plan is not None and plan.accepted:
                accepted += 1
                print(f"  {boundary:%Y-%m-%d %H:%M}  TRADE   {plan.strategy_id} "
                      f"{plan.direction}  entry {plan.entry_bound:.2f}  "
                      f"stop {plan.stop:.2f}  target {plan.target:.2f}  "
                      f"net {plan.net_rr:.2f}R  qty {plan.quantity}")
            elif plan is not None:
                print(f"  {boundary:%Y-%m-%d %H:%M}  REJECT  {plan.strategy_id} "
                      f"{plan.direction}  {plan.blocker.value}"
                      + (f"  net {plan.net_rr:.2f}R" if plan.net_rr is not None else ""))

    print(f"\n  regimes seen      {dict(regimes)}")
    print(f"  setups raised     {raised}")
    print(f"  confirmations     {confirmed}")
    print(f"  accepted plans    {accepted}")
    print("\n  why nothing traded, by count:")
    for reason, count in blockers.most_common(12):
        print(f"    {count:>7}  {reason}")
    print("\n  Research output. No order was placed and none may be derived "
          "retrospectively from this run.")
    return accepted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--bars", type=int, default=3000,
                        help=f"{CONFIRM_TF} candles to replay")
    parser.add_argument("--strategy", choices=sorted(STRATEGY_CHOICES), default="both")
    parser.add_argument("--equity", type=float, default=10_000.0,
                        help="conservative equity for chapter 11 sizing")
    parser.add_argument("--verbose", action="store_true",
                        help="print each setup as it is raised")
    args = parser.parse_args()
    return replay(args.symbol, args.bars, args.strategy, args.equity, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
