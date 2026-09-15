#!/usr/bin/env python3
"""Print what the platform actually believes about its market data, right now.

Run it on the box that has Binance access:

    docker compose exec -T app python scripts/freshness_report.py
    docker compose exec -T app python scripts/freshness_report.py --watch 60

It reads the live feeds rather than any cached status field, and prints the
table the operator needs:

    symbol | timeframe | last candle close | age | source | FRESH/STALE

plus the transport diagnostics and the resulting trading gate. Every verdict
comes from services/market_data_freshness.py -- the same function the Price
Action Lab, the SMC Lab and the Trading Instances consult -- so what this
prints is what those surfaces are deciding on, not a second opinion.

Exit code is 0 when every required timeframe is FRESH, 1 when anything blocks,
so it can be used as a health probe.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/app/automation-hub")

from services.market_data_freshness import assess_feed, report_rows  # noqa: E402


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.0f}s"
    return str(value)


def _table(rows: list[dict]) -> str:
    head = ("symbol", "timeframe", "last candle close", "age", "source", "status")
    widths = [max(len(head[i]),
                  max((len(str(_fmt(row[key]))) for row in rows), default=0))
              for i, key in enumerate(
                  ("symbol", "timeframe", "last_close", "age_seconds", "source", "status"))]
    out = [" | ".join(h.ljust(w) for h, w in zip(head, widths))]
    out.append("-+-".join("-" * w for w in widths))
    for row in rows:
        cells = [row["symbol"], row["timeframe"], _fmt(row["last_close"]),
                 _fmt(row["age_seconds"]), row["source"] or "-", row["status"]]
        out.append(" | ".join(str(c).ljust(w) for c, w in zip(cells, widths)))
    return "\n".join(out)


def _price_action_feeds() -> list:
    """The Price Action Lab's own stream, if a session is running."""
    feeds = []
    try:
        from services.price_action_lab import PriceActionLab  # noqa: F401
        from services import price_action_lab as pal
        runtime = getattr(pal, "RUNTIME", None) or getattr(pal, "_RUNTIME", None)
        stream = getattr(runtime, "stream", None) if runtime else None
        if stream is None:
            return feeds
        status = stream.status()
        connection = status.get("connection", status)
        feeds.append(assess_feed(
            getattr(stream, "symbol", "?"),
            {getattr(stream, "timeframe", "5m"):
                 getattr(stream, "last_closed_update", None)},
            connection_state=str(connection.get("state", "UNKNOWN")).upper(),
            last_event_at=getattr(stream, "last_update", None),
            source="price-action-lab-ws"))
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never crash
        print(f"  price action lab: unavailable ({type(exc).__name__}: {exc})")
    return feeds


def _smc_feeds() -> list:
    """The SMC Lab's live visual feed, if one is warm."""
    feeds = []
    try:
        from services import native_smc_live_visual as visual
        for key, feed in list(getattr(visual, "_LIVE_FEEDS", {}).items()):
            symbol, timeframe = key[0], key[1]
            bars = getattr(feed.engine, "bars", [])
            feeds.append(assess_feed(
                symbol, {timeframe: bars[-1].timestamp if bars else None},
                connection_state="CONNECTED" if bars else "DISCONNECTED",
                last_event_at=getattr(feed, "last_observed_at", None),
                source="smc-lab-visual"))
    except Exception as exc:  # noqa: BLE001
        print(f"  smc lab: unavailable ({type(exc).__name__}: {exc})")
    return feeds


def _instance_feeds() -> list:
    """Every running Trading Instance's engine view."""
    feeds = []
    try:
        import webhook_api as wa
        manager = getattr(wa, "instance_manager", None)
        if manager is None:
            return feeds
        for inst in manager.list_instances():
            status = manager.instance_status(inst.id) or {}
            required = {inst.timeframe: None}
            last = status.get("last_closed_candle")
            if last:
                required[inst.timeframe] = datetime.fromisoformat(
                    str(last).replace("Z", "+00:00"))
            feeds.append(assess_feed(
                inst.symbol, required,
                connection_state=str(status.get("market_data_status", "UNKNOWN")).upper()
                .replace("HEALTHY", "CONNECTED").replace("STALE", "CONNECTED"),
                last_event_at=None,
                source=f"instance:{inst.id[:8]}"))
    except Exception as exc:  # noqa: BLE001
        print(f"  trading instances: unavailable ({type(exc).__name__}: {exc})")
    return feeds


def run_once() -> int:
    now = datetime.now(timezone.utc)
    print(f"=== market data freshness @ {now.isoformat()} ===\n")
    feeds = []
    for label, loader in (("Price Action Lab", _price_action_feeds),
                          ("SMC Lab", _smc_feeds),
                          ("Trading Instances", _instance_feeds)):
        found = loader()
        print(f"{label}: {len(found)} feed(s)")
        feeds.extend(found)
    print()
    if not feeds:
        print("No live feed is running. Start a lab session or an instance first.")
        return 1

    print(_table(report_rows(feeds)))
    print()
    blocked = 0
    for feed in feeds:
        print(f"--- {feed.symbol} ({feed.source}) ---")
        for line in feed.describe():
            print(f"    {line}")
        if not feed.allow_new_entry:
            blocked += 1
        print()
    print(f"{len(feeds) - blocked}/{len(feeds)} feed(s) cleared to open a new entry.")
    return 0 if blocked == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", type=float, default=0.0,
                        help="repeat every N seconds instead of running once")
    args = parser.parse_args()
    if args.watch <= 0:
        return run_once()
    try:
        while True:
            code = run_once()
            print(f"\n(next check in {args.watch:.0f}s; exit code would be {code})\n")
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
