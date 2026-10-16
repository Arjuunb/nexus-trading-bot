#!/usr/bin/env python3
"""Market-data acceptance evidence, read from the running application.

    docker compose exec -T app python scripts/freshness_report.py
    docker compose exec -T app python scripts/freshness_report.py --watch 60
    docker compose exec -T app python scripts/freshness_report.py --interrupt pa

Every number printed is read from the live objects the Price Action Lab, the
SMC Lab and the Trading Instances are actually deciding on, and every verdict
comes from services/market_data_freshness.py -- the same function those
surfaces consult. Nothing here re-derives freshness, so this cannot report a
state the platform is not itself in.

``--interrupt`` performs the controlled feed-interruption acceptance test. It
stops ONE stream and restarts it. It does not stop the application, and it does
not touch databases, sessions, journals, positions or history. Live routing is
never enabled by this script; it has no order path at all.

Exit code: 0 when every required timeframe is FRESH, 1 when anything blocks, 2
when nothing is running to measure.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# The app image sets WORKDIR /app/automation-hub, but do not depend on the cwd:
# this script is also run from a shell that may be anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.market_data_freshness import (  # noqa: E402
    assess_feed, is_live_source,
)

UTC = timezone.utc


def _parse(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age(stamp, now):
    stamp = _parse(stamp)
    return None if stamp is None else max(0.0, (now - stamp).total_seconds())


def _secs(value):
    return "-" if value is None else f"{float(value):.0f}s"


class Observation:
    """One component's live market-data state, as the component holds it."""

    def __init__(self, component, symbol, entry_tf, required, *, status,
                 source, now):
        self.component = component
        self.symbol = symbol or "?"
        self.entry_tf = entry_tf
        self.status = status or {}
        self.source = source
        self.now = now

        s = self.status
        # Continuity: the stream tracks candles it knows are missing and
        # whether its reconciliation finished. Both must be clean.
        unresolved = s.get("unresolved_missing_candles")
        self.reconciled = bool(s.get("reconciliation_complete", True))
        self.unresolved = int(unresolved or 0)
        self.history_loaded = bool(s.get("history_loaded", True))
        self.backfilling = (not self.history_loaded) or (not self.reconciled) \
            or self.unresolved > 0
        self.continuity = ("OK" if self.reconciled and not self.unresolved
                           else f"{self.unresolved} MISSING")

        transport = str(s.get("transport_state") or s.get("state") or "UNKNOWN").upper()
        self.transport = transport
        self.last_event = _parse(s.get("last_update"))
        self.last_kline = _parse(s.get("last_candle_update"))
        self.reconnects = s.get("reconnect_attempt", 0) or 0
        self.connection_age = s.get("connecting_age_seconds")

        self.feed = assess_feed(
            self.symbol, required, now=now,
            connection_state=transport,
            last_event_at=self.last_event,
            backfilling=self.backfilling,
            gaps=() if not self.unresolved else tuple(required),
            entry_timeframe=entry_tf,
            verified=is_live_source(source) if source else True,
            source=source or "", reconnects=int(self.reconnects))

    def _subscription_state(self):
        """Never claim a healthy subscription on a feed that is not delivering.

        Saying HEALTHY merely because the blocker happened to be a different
        code would be exactly the kind of untrue green this repair exists to
        remove.
        """
        if self.transport not in ("CONNECTED", "SYNCHRONIZED"):
            return f"NOT SUBSCRIBED ({self.transport})"
        if self.last_event is None:
            return "SUBSCRIBED, NO EVENT RECEIVED"
        if self.feed.blocker.startswith("SUBSCRIPTION"):
            return "UNHEALTHY"
        return "HEALTHY"

    def rows(self):
        for row in self.feed.timeframes:
            yield {
                "component": self.component,
                "symbol": self.symbol,
                "timeframe": row.timeframe,
                "last_close": (row.last_close.strftime("%Y-%m-%d %H:%M:%S")
                               if row.last_close else "-"),
                "age": _secs(row.age_seconds),
                "source": self.source or "-",
                "continuity": self.continuity,
                "freshness": row.status,
                "blocker": row.blocker or (self.feed.blocker or "-"),
            }

    def detail(self):
        out = [
            f"  transport state      : {self.transport}",
            f"  connection age       : {_secs(self.connection_age)}",
            f"  last Binance event   : {_secs(_age(self.last_event, self.now))} ago",
            f"  last kline           : {_secs(_age(self.last_kline, self.now))} ago",
            f"  subscription         : {self._subscription_state()}",
            f"  reconnect count      : {self.reconnects}",
            f"  backfill state       : {'RUNNING' if self.backfilling else 'COMPLETE'}"
            f"  (history_loaded={self.history_loaded}, reconciled={self.reconciled},"
            f" missing={self.unresolved})",
            f"  provenance           : {self.source or 'unattested'}"
            f"  -> {'LIVE' if is_live_source(self.source) else 'NOT ATTESTED LIVE'}",
            f"  Trading Data Gate    : "
            + ("READY" if self.feed.allow_new_entry
               else f"BLOCKED · {self.feed.blocker} · {self.feed.detail}"),
        ]
        return out


def _lab(name, runtime, component):
    """Read a lab runtime's stream, if it has a live one."""
    now = datetime.now(UTC)
    stream = getattr(runtime, "stream", None)
    if stream is None:
        return None, f"{component}: no live stream (no LIVE_PAPER session running)"
    try:
        status = stream.status()
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not crash
        return None, f"{component}: stream.status() failed ({type(exc).__name__}: {exc})"

    symbol = status.get("symbol") or getattr(stream, "symbol", "")
    entry_tf = status.get("timeframe") or getattr(stream, "timeframe", "")
    required = {}
    if entry_tf:
        required[entry_tf] = _parse(status.get("last_closed_update"))
        if required[entry_tf] is not None:
            # last_closed_update is the candle's CLOSE; the authority takes the
            # open, so step back one interval.
            from bot.data.resample import TF_SECONDS
            from datetime import timedelta
            required[entry_tf] -= timedelta(seconds=TF_SECONDS.get(entry_tf, 300))
    return Observation(component, symbol, entry_tf, required, status=status,
                       source="live (binance usdm websocket)", now=now), None


def _instances(component="Trading Instance"):
    """Every running Trading Instance the manager holds."""
    now = datetime.now(UTC)
    out, notes = [], []
    try:
        import webhook_api as wa
        manager = getattr(wa, "instance_manager", None)
        if manager is None:
            return out, ["Trading Instances: no manager"]
        items = list(getattr(manager, "_instances", {}).values())
        if not items:
            return out, ["Trading Instances: none created"]
        for inst in items:
            try:
                status = manager.status(inst.id) or {}
            except Exception as exc:  # noqa: BLE001
                notes.append(f"  instance {inst.id[:8]}: status failed ({exc})")
                continue
            engine = status.get("engine", status)
            required = {}
            tf = getattr(inst, "timeframe", "") or engine.get("timeframe", "")
            if tf:
                required[tf] = _parse(engine.get("last_closed_candle"))
            out.append(Observation(
                f"{component} {inst.id[:8]}", getattr(inst, "symbol", "?"), tf,
                required,
                status={"transport_state": engine.get("market_data_status", "UNKNOWN"),
                        "last_update": engine.get("last_received_candle"),
                        "last_candle_update": engine.get("last_received_candle"),
                        "reconnect_attempt": engine.get("reconnect_attempt", 0)},
                source=engine.get("last_source") or "", now=now))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Trading Instances: unavailable ({type(exc).__name__}: {exc})")
    return out, notes


def collect():
    observations, notes = [], []
    try:
        import webhook_api as wa
    except Exception as exc:  # noqa: BLE001
        return [], [f"cannot import the application: {type(exc).__name__}: {exc}"]

    for attr, component in (("price_action_runtime", "Price Action Lab"),
                            ("smc_runtime", "SMC Lab")):
        runtime = getattr(wa, attr, None)
        if runtime is None:
            notes.append(f"{component}: runtime not present")
            continue
        obs, note = _lab(attr, runtime, component)
        (observations.append(obs) if obs else notes.append(note))

    rows, more = _instances()
    observations.extend(rows)
    notes.extend(more)
    return observations, notes


def _table(rows):
    cols = ["component", "symbol", "timeframe", "last_close", "age", "source",
            "continuity", "freshness", "blocker"]
    head = ["component", "symbol", "tf", "last candle close UTC", "age",
            "source/provenance", "continuity", "freshness", "blocker"]
    widths = [max(len(head[i]), max((len(str(r[c])) for r in rows), default=0))
              for i, c in enumerate(cols)]
    out = [" | ".join(h.ljust(w) for h, w in zip(head, widths)),
           "-+-".join("-" * w for w in widths)]
    for r in rows:
        out.append(" | ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    return "\n".join(out)


def run_once() -> int:
    now = datetime.now(UTC)
    print(f"server UTC time : {now.isoformat()}")
    observations, notes = collect()
    for note in notes:
        print(f"  ! {note}")
    if not observations:
        print("\nNothing live to measure. Start a lab LIVE_PAPER session or a "
              "Trading Instance, then re-run.")
        return 2

    rows = [row for obs in observations for row in obs.rows()]
    print()
    print(_table(rows))
    print()
    blocked = 0
    for obs in observations:
        print(f"--- {obs.component} ({obs.symbol} {obs.entry_tf}) ---")
        for line in obs.detail():
            print(line)
        print()
        if not obs.feed.allow_new_entry:
            blocked += 1
    ready = len(observations) - blocked
    print(f"Trading Data Gate: {ready}/{len(observations)} component(s) READY")
    return 0 if blocked == 0 else 1


def interrupt(target: str, settle: float) -> int:
    """Controlled feed-level interruption: stop ONE stream, then restart it.

    The application keeps running. No database, session, journal, position or
    historical record is touched, and no order path is exercised.
    """
    import webhook_api as wa

    attr = {"pa": "price_action_runtime", "smc": "smc_runtime"}.get(target)
    if attr is None:
        print(f"unknown target '{target}'; use pa or smc")
        return 2
    runtime = getattr(wa, attr, None)
    stream = getattr(runtime, "stream", None)
    if stream is None:
        print(f"{target}: no live stream to interrupt (start a LIVE_PAPER session)")
        return 2

    status = stream.status()
    symbol = status.get("symbol") or getattr(stream, "symbol", "BTCUSDT")
    timeframe = status.get("timeframe") or getattr(stream, "timeframe", "5m")

    print("=== 1. BEFORE (expect FRESH / READY) ===")
    run_once()

    print(f"\n=== 2. INTERRUPT: stopping the {target} stream only ===")
    stream.stop()
    print(f"    stream.stop() called; app still running. Waiting {settle:.0f}s "
          "for the silence to be detected...")
    time.sleep(settle)

    print("\n=== 3. DURING OUTAGE (expect BLOCKED) ===")
    during = run_once()
    if during == 0:
        print("\n!! FAIL: the gate stayed READY through a stopped feed.")
        return 1

    print(f"\n=== 4. RESTORE: restarting the {target} stream ===")
    started = stream.start(symbol, timeframe)
    print(f"    stream.start({symbol!r}, {timeframe!r}) -> {started}")
    print("    resubscribe + REST backfill + reconciliation now run.")

    for attempt in range(1, 13):
        time.sleep(10)
        print(f"\n=== 5.{attempt} RECOVERY CHECK (+{attempt * 10}s) ===")
        code = run_once()
        if code == 0:
            print("\n=== RESULT: recovered. Gate reopened only after every "
                  "required timeframe verified fresh and continuous. ===")
            return 0
        print("    still blocked (this is correct while backfill/continuity "
              "is unverified)")
    print("\n!! Did not recover within 120s. Report the blocker above.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", type=float, default=0.0,
                        help="repeat every N seconds")
    parser.add_argument("--interrupt", metavar="pa|smc", default="",
                        help="run the controlled feed-interruption acceptance test")
    parser.add_argument("--settle", type=float, default=45.0,
                        help="seconds to wait for the outage to be detected")
    args = parser.parse_args()

    if args.interrupt:
        return interrupt(args.interrupt.lower(), args.settle)
    if args.watch <= 0:
        return run_once()
    try:
        while True:
            run_once()
            print(f"\n--- next check in {args.watch:.0f}s ---\n")
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
