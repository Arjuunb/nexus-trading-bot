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
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from typing import Optional                                        # noqa: E402

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


def _at(value: str):
    """Parse a window bound as UTC. A naive date means midnight UTC."""
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _candle(bar: Bar) -> dict:
    return {"t": bar.timestamp.isoformat(), "o": float(bar.open),
            "h": float(bar.high), "l": float(bar.low), "c": float(bar.close),
            "v": float(bar.volume)}


def _window(rows: list[Bar], centre, before: int, after: int) -> list[dict]:
    """Candles around a timestamp, for drawing the setup on a chart."""
    positions = [i for i, row in enumerate(rows) if row.timestamp == centre]
    if not positions:
        return [_candle(row) for row in rows[-(before + after):]]
    at = positions[0]
    return [_candle(row) for row in rows[max(0, at - before): at + after + 1]]


def _zone_row(zone) -> dict:
    return {"id": zone.id, "kind": zone.kind, "lower": zone.lower,
            "upper": zone.upper, "origin": zone.origin, "retired": zone.retired,
            "created_at": zone.created_at.isoformat()}


def _audit_record(index, decision, engine, setup_rows, confirm_rows) -> dict:
    """Everything needed to judge one confirmation on a chart, after the fact.

    The decomposition matters as much as the verdict. "Rejected at 2.1R" does
    not say whether the stop was too wide, the confirmation arrived too far
    from the zone, or a nearby opposing level capped the room -- three
    different problems with three different answers. Each is recorded as its
    own number so the chart does not have to be squinted at.
    """
    setup, plan = decision.setup, decision.plan
    rejection, confirmation = setup.rejection, setup.confirmation
    record = {
        "index": index,
        "at": decision.at.isoformat(),
        "strategy_id": setup.strategy_id,
        "direction": setup.direction,
        "regime": decision.regime.value,
        "verdict": ("ACCEPTED" if plan is not None and plan.accepted
                    else (plan.blocker.value if plan is not None and plan.blocker
                          else (decision.blocker.value if decision.blocker else "NO_PLAN"))),
        "zone": _zone_row(setup.zone),
        "original_zone": _zone_row(setup.original_zone),
        "setup_atr15": setup.setup_atr,
        "confirm_slot": setup.confirm_slots_used,
        "rejection": _candle(rejection) if rejection else None,
        "confirmation": _candle(confirmation) if confirmation else None,
        "breakout": _candle(setup.breakout) if setup.breakout else None,
        "evidence": setup.evidence,
        "candles": {
            SETUP_TF: _window(setup_rows, rejection.timestamp, 40, 8) if rejection else [],
            CONFIRM_TF: _window(confirm_rows, confirmation.timestamp, 24, 4) if confirmation else [],
        },
        "zones_in_view": [_zone_row(z) for z in engine.zones if not z.retired],
    }
    if plan is not None:
        drift = ((plan.entry_bound - float(rejection.close)) if setup.direction == "long"
                 else (float(rejection.close) - plan.entry_bound)) if rejection else None
        record["plan"] = {
            "accepted": plan.accepted,
            "entry_bound": plan.entry_bound, "stop": plan.stop, "target": plan.target,
            "stop_distance": plan.stop_distance,
            "stop_distance_atr": plan.stop_distance_atr,
            "net_rr": plan.net_rr, "costs_loss": plan.costs_loss,
            "costs_win": plan.costs_win, "quantity": plan.quantity,
            "planned_loss": plan.planned_loss,
            "blocker": plan.blocker.value if plan.blocker else None,
            "evidence": plan.evidence,
            # How far the confirmation dragged the entry away from the candle
            # that defined the setup. A late confirmation shows up here as a
            # large drift, which widens the stop and eats the target room at
            # the same time -- one cause, two symptoms.
            "entry_drift": drift,
            "entry_drift_atr": (drift / setup.setup_atr) if drift is not None
                               and setup.setup_atr else None,
        }
    return record


def replay(symbol: str, bars: int, strategy: str, equity: float,
           verbose: bool, loader=_load, audit_path: Optional[str] = None,
           start: Optional[str] = None, end: Optional[str] = None,
           progress: bool = False) -> int:
    config = RulebookConfig(symbol=symbol)
    config.validate()
    chosen = STRATEGY_CHOICES[strategy]
    engine = PriceActionRulebookEngine(
        config, CostModel(), strategies=(chosen,) if chosen else None)

    context, ctx_source = loader(symbol, CONTEXT_TF, max(bars // 12, config.warmup_bars + 50))
    setups, setup_source = loader(symbol, SETUP_TF, max(bars // 3, config.warmup_bars + 50))
    confirms, confirm_source = loader(symbol, CONFIRM_TF, bars)

    # A date window bounds the DECISIONS, not the history. The context and
    # setup frames keep everything before the window so the regime and the zone
    # registry are already warm when the first in-window candle closes --
    # truncating them would mean the first weeks of any window decide on
    # structure the engine had not seen yet, and no two windows would agree.
    if start or end:
        lower = _at(start) if start else None
        upper = _at(end) if end else None
        confirms = [row for row in confirms
                    if (lower is None or row.timestamp >= lower)
                    and (upper is None or row.timestamp < upper)]
        if upper is not None:
            context = [row for row in context if row.timestamp < upper]
            setups = [row for row in setups if row.timestamp < upper]

    # An empty frame must stop the run, not produce a tidy "0 accepted" report.
    # A negative result and no data at all look identical once summarised, and
    # the second one silently answers a question nobody asked.
    for timeframe, rows, source in ((CONTEXT_TF, context, ctx_source),
                                    (SETUP_TF, setups, setup_source),
                                    (CONFIRM_TF, confirms, confirm_source)):
        if not rows:
            raise SystemExit(f"no {timeframe} candles for {symbol} (source: {source})")

    print(f"Nexus PA rulebook v{RULEBOOK_VERSION} -- RESEARCH REPLAY, NO ORDERS",
          flush=True)
    print(f"  symbol    {symbol}   strategies {engine.strategies}")
    print(f"  {CONTEXT_TF:>4} {len(context):>6} candles  {ctx_source}")
    print(f"  {SETUP_TF:>4} {len(setups):>6} candles  {setup_source}")
    print(f"  {CONFIRM_TF:>4} {len(confirms):>6} candles  {confirm_source}")
    print()

    blockers: Counter = Counter()
    regimes: Counter = Counter()
    audit: list[dict] = []
    raised = confirmed = accepted = 0
    last_context = last_setup = None

    # Walk the three series together, appending as each candle closes, instead
    # of re-deriving "everything before now" on every 5M bar. The slicing
    # version is O(n^2): at fixture scale it is invisible, and at a year of 5M
    # candles it is hours of rebuilding lists that only ever grow by one. The
    # same trap already cost this repo a 141-second replay suite once.
    ctx_slice: list[Bar] = []
    setup_slice: list[Bar] = []
    confirm_slice: list[Bar] = []
    ctx_at = setup_at = 0
    total = len(confirms)

    for position, bar in enumerate(confirms):
        boundary = bar.timestamp
        while ctx_at < len(context) and context[ctx_at].timestamp < boundary:
            ctx_slice.append(context[ctx_at])
            ctx_at += 1
        while setup_at < len(setups) and setups[setup_at].timestamp < boundary:
            setup_slice.append(setups[setup_at])
            setup_at += 1
        confirm_slice.append(bar)

        if progress and position % 5000 == 0 and position:
            # flush=True because a run this long is normally redirected to a
            # file, and Python block-buffers stdout when it is not a terminal.
            # Without it the progress line exists only in an 8KB buffer, so a
            # working 19-minute run is indistinguishable from a crashed one.
            print(f"    ... {position:>7}/{total} 5M candles "
                  f"({boundary:%Y-%m-%d})  setups {raised}  confirmed {confirmed}",
                  flush=True)

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
            audit.append(_audit_record(len(audit) + 1, decision, engine,
                                       setup_slice, confirm_slice))
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
    if audit_path:
        payload = {
            "meta": {"symbol": symbol, "rulebook_version": RULEBOOK_VERSION,
                     "strategies": list(engine.strategies),
                     "timeframes": {"context": CONTEXT_TF, "setup": SETUP_TF,
                                    "confirm": CONFIRM_TF},
                     "equity": equity, "confirm_bars_replayed": len(confirms),
                     "sources": {CONTEXT_TF: ctx_source, SETUP_TF: setup_source,
                                 CONFIRM_TF: confirm_source},
                     "first_candle": confirms[0].timestamp.isoformat(),
                     "last_candle": confirms[-1].timestamp.isoformat(),
                     "execution": "RESEARCH REPLAY -- no orders"},
            "summary": {"setups_raised": raised, "confirmations": confirmed,
                        "accepted": accepted, "regimes": dict(regimes),
                        "blockers": dict(blockers)},
            "confirmations": audit,
        }
        Path(audit_path).write_text(json.dumps(payload, indent=2, default=str))
        print(f"\n  audit written to {audit_path} ({len(audit)} confirmations)")

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
    parser.add_argument("--audit", metavar="PATH",
                        help="write every confirmation, with its candles and its "
                             "reward-to-risk decomposition, as JSON for review")
    parser.add_argument("--start", metavar="DATE",
                        help="first decision candle, e.g. 2025-01-01 (UTC). "
                             "History before it still warms the context.")
    parser.add_argument("--end", metavar="DATE",
                        help="exclusive upper bound, e.g. 2026-01-01 (UTC)")
    parser.add_argument("--progress", action="store_true",
                        help="print a line every 5000 candles on long runs")
    args = parser.parse_args()
    return replay(args.symbol, args.bars, args.strategy, args.equity,
                  args.verbose, audit_path=args.audit, start=args.start,
                  end=args.end, progress=args.progress)


if __name__ == "__main__":
    raise SystemExit(main())
