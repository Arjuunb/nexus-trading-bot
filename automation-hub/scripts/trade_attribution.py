#!/usr/bin/env python3
"""Which strategy actually produced each instance's paper trades.

The dashboard labels an instance's record with the strategy it is running
*now*. The record itself is every paper trade that instance ever closed:
services/trading_instances.py scopes them by instance_id and never by strategy.
Switch an instance to a new strategy and the old strategy's wins and losses
keep being reported under the new one's name -- including in the "best measured
instance" banner.

This says whether that has happened, by grouping the same trades the dashboard
counts by the strategy_id stored on each one.

Read-only. The ledger is opened mode=ro and this script cannot write to it.

    python scripts/trade_attribution.py
    python scripts/trade_attribution.py --symbol BNBUSDT
    python scripts/trade_attribution.py --instance 5038a3d8e5974a09b3f7106d2ba7127d
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB = "/var/lib/tradexa/ledger.db"


def _pnl(row: dict) -> float:
    for key in ("pnl", "realized_pnl", "net_pnl"):
        if row.get(key) is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def attribute(db_path: str, *, symbol: str | None, instance: str | None, out) -> dict:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    where, params = [], []
    if symbol:
        where.append("symbol=?")
        params.append(symbol.upper())
    if instance:
        where.append("instance_id=?")
        params.append(instance)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = [dict(r) for r in db.execute(f"SELECT * FROM paper_trades{clause}", params)]
    db.close()

    by_instance: dict = defaultdict(lambda: defaultdict(list))
    for row in rows:
        key = str(row.get("instance_id") or "(no instance)")
        by_instance[key][str(row.get("strategy_id") or "(unattributed)")].append(row)

    print(f"Paper trade attribution -- READ ONLY\n  ledger  {db_path}", file=out)
    if symbol or instance:
        print(f"  filter  {symbol or ''} {instance or ''}".rstrip(), file=out)
    print(f"  trades  {len(rows)}\n", file=out)

    mixed = []
    for instance_id, strategies in sorted(by_instance.items()):
        total = sum(len(v) for v in strategies.values())
        flag = "  <-- MIXED" if len(strategies) > 1 else ""
        print(f"instance {instance_id}   {total} trades{flag}", file=out)
        for strategy_id, trades in sorted(strategies.items(),
                                          key=lambda kv: -len(kv[1])):
            closed = [t for t in trades if str(t.get("status")) != "open"]
            wins = [t for t in closed if _pnl(t) > 0]
            losses = [t for t in closed if _pnl(t) < 0]
            gross_win = sum(_pnl(t) for t in wins)
            gross_loss = -sum(_pnl(t) for t in losses)
            pf = (gross_win / gross_loss) if gross_loss else None
            head = f"    {len(trades):>5}  {strategy_id:<40}  closed {len(closed):>4}"
            if not closed:
                print(head, file=out)
                continue
            print(f"{head}  win {100 * len(wins) / len(closed):>5.1f}%", file=out)
            # Average win against average loss is the number that says whether
            # a trade reached the target its own gate demanded. A strategy that
            # required 2.5R and whose winners average 0.6R is being closed
            # somewhere other than its target.
            print(f"           {'':<40}"
                  f"  PF {'n/a' if pf is None else round(pf, 2)}"
                  f"   net {gross_win - gross_loss:+.2f}"
                  f"   avg win {gross_win / max(len(wins), 1):.2f}"
                  f"   avg loss {gross_loss / max(len(losses), 1):.2f}", file=out)
        if len(strategies) > 1:
            mixed.append(instance_id)
        print(file=out)

    if mixed:
        print("MIXED ATTRIBUTION: the instances above hold trades from more than one"
              " strategy.", file=out)
        print("  The dashboard reports all of them under whichever strategy is"
              " configured now.", file=out)
    elif by_instance:
        print("Every instance's trades come from a single strategy: the dashboard's"
              " attribution is correct.", file=out)
    return {"trades": len(rows), "instances": len(by_instance), "mixed": mixed}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DB)
    parser.add_argument("--symbol")
    parser.add_argument("--instance")
    args = parser.parse_args(argv)
    if not Path(args.db).exists():
        print(f"no such ledger: {args.db}", file=sys.stderr)
        return 2
    attribute(args.db, symbol=args.symbol, instance=args.instance, out=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
