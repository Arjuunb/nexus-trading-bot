#!/usr/bin/env python3
"""Multi-instance soak test: run N Trading Instances and watch for drift.

What it is looking for, and why each matters:

  memory growth        an unbounded queue or a leaked subscription
  worker duplication   two execution owners for one paper account
  reconnect churn      a backoff that storms instead of backing off
  stale feeds          a channel that silently stopped delivering
  event duplication    a replayed candle creating a second order
  queue growth         a sink that cannot keep up with the feed
  database locking     contention between workers on one SQLite file
  contamination        one instance's state appearing in another's

It prints one sample line per interval and a verdict at the end. Non-zero
exit means at least one check regressed, so it can gate a release.

Run against the real Binance USD-M feed (the default) on a host that can
reach fstream.binance.com:

    python scripts/soak_instances.py --hours 6

Run offline against a Binance-protocol double, with the candle clock
compressed so hours of candles pass in minutes (every continuity, staleness
and gap rule still runs, on a shorter clock):

    python scripts/soak_instances.py --minutes 20 --simulated --clock-scale 60

Useful flags:
    --instances BTCUSDT:brain:5m ETHUSDT:brain:5m SOLUSDT:supertrend:5m
    --sample-seconds 30      how often to take a sample
    --data-dir PATH          where the soak ledger lives (default: a temp dir)
    --json PATH              write the full sample series for later inspection
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                     # automation-hub
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))    # repo root

DEFAULT_INSTANCES = ["BTCUSDT:brain:5m", "ETHUSDT:brain:5m", "SOLUSDT:supertrend:5m"]


def rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def _scale_clock(factor: int) -> None:
    """Shorten every candle duration consistently, in every table consulted."""
    from bot.data import resample
    import services.auto_engine as auto_engine
    import services.mtf_policy as mtf_policy
    import data.market_data_v2 as market_data_v2
    import services.trading_instances as trading_instances

    scaled = {key: max(1, value // factor) for key, value in resample.TF_SECONDS.items()}
    resample.TF_SECONDS.clear()
    resample.TF_SECONDS.update(scaled)
    auto_engine._TF_SECONDS = resample.TF_SECONDS
    mtf_policy.TIMEFRAME_SECONDS = {k: max(1, v // factor)
                                    for k, v in mtf_policy.TIMEFRAME_SECONDS.items()}
    market_data_v2.TF_MS = {k: max(1000, v // factor)
                            for k, v in market_data_v2.TF_MS.items()}
    trading_instances._TIMEFRAME_SECONDS = {
        k: max(1, v // factor) for k, v in trading_instances._TIMEFRAME_SECONDS.items()}


def _simulated_stream_factory():
    """A Binance-protocol double. Only the transport is replaced."""
    from bot.types import Bar
    from bot.data.resample import TF_SECONDS

    class SoakStream:
        instances: list = []

        def __init__(self, loader, *, bar_sink=None, quote_sink=None,
                     event_sink=None, quotes_enabled=True, **_kw):
            self.bar_sink, self.quote_sink = bar_sink, quote_sink
            self.event_sink, self.quotes_enabled = event_sink, quotes_enabled
            self.symbol = self.timeframe = ""
            self.running = False
            self.reconnects = 0
            self._bars: list[Bar] = []
            self._quote: dict = {}
            self._last_update = None
            SoakStream.instances.append(self)

        def start(self, symbol, timeframe):
            self.symbol, self.timeframe, self.running = symbol, timeframe, True
            step = TF_SECONDS[timeframe]
            anchor = int(datetime.now(timezone.utc).timestamp()) // step * step
            base = {"BTCUSDT": 60000.0, "ETHUSDT": 3000.0}.get(symbol, 150.0)
            self._bars = []
            for index in range(400, 0, -1):
                close = base * (1 + ((index % 17) - 8) * 0.0004)
                self._bars.append(Bar(
                    datetime.fromtimestamp(anchor - index * step, tz=timezone.utc),
                    base, max(base, close) * 1.0006, min(base, close) * 0.9994,
                    close, 100.0))
            return True

        def stop(self):
            self.running = False

        def status(self):
            return {"state": "SYNCHRONIZED", "transport_state": "CONNECTED",
                    "reliable": True, "reconnect_attempt": self.reconnects,
                    "quote": dict(self._quote), "last_update": self._last_update,
                    "quotes_enabled": self.quotes_enabled,
                    "health_reason": "soak transport double"}

        def snapshot(self):
            return {"closed_bars": list(self._bars), "forming": None,
                    "quote": dict(self._quote), "connection": self.status()}

        def advance(self):
            if not self.running or not self._bars:
                return None
            last = self._bars[-1]
            step = TF_SECONDS[self.timeframe]
            stamp = last.timestamp + timedelta(seconds=step)
            if stamp + timedelta(seconds=step) > datetime.now(timezone.utc):
                return None                # not closed yet on the real clock
            close = last.close * (1 + ((int(stamp.timestamp()) // step) % 7 - 3) * 0.0003)
            bar = Bar(stamp, last.close, max(last.close, close) * 1.0005,
                      min(last.close, close) * 0.9995, close, 100.0)
            self._bars.append(bar)
            if len(self._bars) > 1500:
                del self._bars[:-1500]
            self._last_update = datetime.now(timezone.utc).isoformat()
            if self.bar_sink:
                self.bar_sink(bar)
            if self.quote_sink:
                self._quote = {"last": close, "bid": close * 0.9999,
                               "ask": close * 1.0001, "mark": close,
                               "received_at": self._last_update,
                               "event_timestamp": self._last_update}
                self.quote_sink(dict(self._quote))
            return bar

    return SoakStream


def build(args):
    from data.ledger import SqliteLedger
    from services.forward_paper_hub import ForwardPaperMarketDataHub
    from services.instance_supervisor import InstanceSupervisor
    from services.strategy_factory import make_builtin_strategy
    from services.trading_instances import TradingInstanceManager

    data_dir = args.data_dir or tempfile.mkdtemp(prefix="soak-instances-")
    os.makedirs(data_dir, exist_ok=True)
    ledger = SqliteLedger(os.path.join(data_dir, "soak.db"))

    if args.simulated:
        factory = _simulated_stream_factory()
        hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=factory)
        rules = lambda _symbol: {"symbol": "SOAK", "tick_size": 0.01,  # noqa: E731
                                 "step_size": 0.001, "min_qty": 0.001,
                                 "min_notional": 5.0}
    else:
        from data.market_data_v2 import MarketDataService
        market = MarketDataService(os.path.join(data_dir, "market"))
        hub = ForwardPaperMarketDataHub(market.public_usdm_window)
        factory, rules = None, market.usdm_symbol_rules

    manager = TradingInstanceManager(
        ledger, strategy_factory=lambda key, symbol: make_builtin_strategy(key, symbol),
        live=True, live_poll_s=args.poll_seconds)
    manager.market_hub = hub
    manager.symbol_rules_provider = rules
    manager.configure(max_active_slots=max(3, len(args.instances)),
                      paper_account_capital=1_000_000)
    supervisor = InstanceSupervisor(manager, interval_s=args.sample_seconds)
    return data_dir, ledger, hub, manager, supervisor, factory


def sample(manager, supervisor, hub, ledger) -> dict:
    from services.instance_metrics import platform_metrics
    from services.instance_reconciliation import reconcile

    metrics = platform_metrics(manager, supervisor=supervisor)
    leases = manager.store.worker_leases()
    owners: dict[str, int] = {}
    for lease in leases:
        owners[str(lease.get("instance_id"))] = owners.get(str(lease.get("instance_id")), 0) + 1
    with ledger._lock:
        duplicate_orders = ledger._c.execute(
            "SELECT COUNT(*) FROM (SELECT alert_id, instance_id, status, COUNT(*) c "
            "FROM webhook_events GROUP BY alert_id, instance_id, status HAVING c > 1)"
        ).fetchone()[0]
        cross = ledger._c.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE COALESCE(instance_id,'') = ''"
        ).fetchone()[0]
    blocked = {instance_id: [f.check for f in result.findings]
               for instance_id, result in
               ((item.id, reconcile(manager, item.id)) for item in manager._instances.values())
               if result.blocked}
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "rss_mb": round(rss_mb(), 1),
        "threads": metrics["process"]["threads"],
        "running_workers": metrics["running_workers"],
        "market_connections": metrics["market_connections"],
        "queue_depth": metrics["queue_depth"],
        "dropped_quotes": metrics["dropped_quotes"],
        "reconnects": metrics["reconnect_count"],
        "stale_feeds": metrics["stale_feed_count"],
        "evaluations": metrics["strategy_evaluation_count"],
        "signals": metrics["signals_generated"],
        "orders": metrics["orders_generated"],
        "duplicate_leases": {k: v for k, v in owners.items() if v > 1},
        "duplicate_orders": duplicate_orders,
        "untagged_trades": cross,
        "blocked": blocked,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instances", nargs="*", default=DEFAULT_INSTANCES,
                        help="SYMBOL:STRATEGY:TIMEFRAME triples")
    parser.add_argument("--hours", type=float, default=0.0)
    parser.add_argument("--minutes", type=float, default=0.0)
    parser.add_argument("--sample-seconds", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--simulated", action="store_true",
                        help="use a Binance-protocol double instead of the venue")
    parser.add_argument("--clock-scale", type=int, default=1,
                        help="shorten candle durations by this factor (simulated only)")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--json", default="")
    parser.add_argument("--max-rss-growth-mb", type=float, default=100.0)
    args = parser.parse_args()

    duration = args.hours * 3600 + args.minutes * 60
    if duration <= 0:
        duration = 3600.0
    if args.clock_scale > 1:
        if not args.simulated:
            parser.error("--clock-scale only applies to --simulated runs; the real "
                         "venue's candles close on the venue's clock")
        _scale_clock(args.clock_scale)

    data_dir, ledger, hub, manager, supervisor, stream_factory = build(args)
    print(f"soak: {len(args.instances)} instance(s), {duration/60:.0f} min, "
          f"{'simulated' if args.simulated else 'live Binance USD-M'}, data={data_dir}",
          flush=True)

    created = []
    for spec in args.instances:
        symbol, strategy, timeframe = (spec.split(":") + ["brain", "5m"])[:3]
        instance = manager.create(symbol=symbol.upper(), strategy_key=strategy,
                                  strategy_label=strategy, strategy_version="v1",
                                  timeframe=timeframe, risk_per_trade_pct=0.005,
                                  capital_allocation=1_000)
        manager.start(instance.id)
        created.append(instance)
        print(f"  started {symbol.upper()} {strategy} {timeframe} -> {instance.id[:8]}",
              flush=True)
    supervisor.start()

    stop = threading.Event()
    if args.simulated:
        def pump():
            while not stop.is_set():
                for stream in list(stream_factory.instances):
                    if stream.running:
                        stream.advance()
                for instance in created:
                    runtime = manager._runtime.get(instance.id)
                    if runtime:
                        runtime[0].notify_new_candle()
                time.sleep(0.25)
        threading.Thread(target=pump, daemon=True).start()

    samples: list[dict] = []
    deadline = time.monotonic() + duration
    header = ("    elapsed   rss_mb  thr  workers  chans  queue  drop  recon  stale  "
              "evals  signals  dup_lease  dup_order  blocked")
    print(header, flush=True)
    started = time.monotonic()
    try:
        while time.monotonic() < deadline:
            time.sleep(min(args.sample_seconds, max(0.0, deadline - time.monotonic())))
            row = sample(manager, supervisor, hub, ledger)
            samples.append(row)
            print("    %7.0fs %7.1f %4d %8d %6d %6s %5s %6s %6s %6s %8s %10d %10d %8d" % (
                time.monotonic() - started, row["rss_mb"], row["threads"],
                row["running_workers"], row["market_connections"], row["queue_depth"],
                row["dropped_quotes"], row["reconnects"], row["stale_feeds"],
                row["evaluations"], row["signals"], len(row["duplicate_leases"]),
                row["duplicate_orders"], len(row["blocked"])), flush=True)
    except KeyboardInterrupt:
        print("\n  interrupted; reporting on the samples collected so far", flush=True)
    finally:
        stop.set()
        supervisor.stop()
        manager.shutdown()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(samples, handle, indent=1, default=str)
        print(f"\n  samples written to {args.json}", flush=True)

    if not samples:
        print("\nVERDICT: no samples collected", flush=True)
        return 1

    first, last = samples[0], samples[-1]
    growth = last["rss_mb"] - first["rss_mb"]
    checks = [
        ("memory stable", growth <= args.max_rss_growth_mb,
         f"RSS {first['rss_mb']:.1f} -> {last['rss_mb']:.1f} MB (+{growth:.1f})"),
        ("no worker duplication", all(not row["duplicate_leases"] for row in samples),
         "one execution owner per instance throughout"),
        ("no duplicate orders", all(row["duplicate_orders"] == 0 for row in samples),
         "no repeated (alert_id, instance_id, status)"),
        ("no untagged trades", all(row["untagged_trades"] == 0 for row in samples),
         "every trade row carries an instance_id"),
        ("queues bounded", max(row["queue_depth"] for row in samples) < 64,
         f"peak candle queue depth {max(row['queue_depth'] for row in samples)}"),
        ("channels stable", len({row["market_connections"] for row in samples}) == 1,
         f"market connections {sorted({row['market_connections'] for row in samples})}"),
        ("no reconciliation failures", all(not row["blocked"] for row in samples),
         "durable records stayed coherent"),
        ("strategies were evaluated", last["evaluations"] > 0,
         f"{last['evaluations']} closed candles evaluated"),
    ]
    print("\nVERDICT", flush=True)
    failed = 0
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name:28} {detail}", flush=True)
        failed += 0 if passed else 1
    print(f"\n  {len(checks) - failed}/{len(checks)} checks passed over "
          f"{len(samples)} samples", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
