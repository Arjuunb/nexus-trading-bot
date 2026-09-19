"""Keep the local real-candle cache current, on a timer.

``/data/sync`` and ``/data/sync-all`` fetch real Binance candles into the
local store, and both are manual POSTs. Nothing ever called them on a
schedule, so the cache only moved when a person remembered. Measured on the
running host it was 15.8 hours behind the venue for BTCUSDT and held nothing
at all for BNBUSDT, while roughly thirty call sites read it -- the market
scanner ranking today's setups, position sizing reading ATR, the symbol
universe picking what is tradable.

What this does NOT do is as important as what it does:

  * **It does not decide freshness.** ``services.market_data_freshness`` is the
    one authority and this asks it, so the timer and the dashboard can never
    disagree about whether a series is behind.

  * **It syncs only what is actually behind.** Refetching every pair every
    cycle would be seventy requests to the venue to replace data that had not
    changed, and would hide a failing pair inside the noise.

  * **It does not make staleness invisible.** A sync that fails leaves the data
    stale and says so in the ledger. The consumers that mark stale data keep
    marking it; this removes the cause when it can, and reports when it cannot.
    A scheduler that silently swallowed its own failures would be worse than no
    scheduler, because the staleness would still be there and nobody would be
    looking for it.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from services.market_data_freshness import assess_timeframe

#: Slower than any timeframe it maintains, so a cycle cannot outrun the candles
#: it is fetching. The per-pair freshness check is what decides work, not this.
DEFAULT_INTERVAL_S = 300.0
#: One venue page. sync() fetches in 1,000-row batches and the store upserts on
#: its primary key, so a single page closes any gap shorter than 1,000 candles
#: -- about three and a half days at 5m -- in exactly one request. Asking for
#: more would refetch days of unchanged candles every few minutes to add one.
DEFAULT_TARGET_CANDLES = 1000
#: A first sync of an empty pair has nothing to extend and needs real depth.
DEFAULT_BACKFILL_CANDLES = 3000
#: Pairs refreshed in one pass. A cold cache has every pair stale at once, and
#: syncing forty of them in a single pass means forty sequential venue calls
#: before the loop reports anything -- with an unreachable venue, forty hanging
#: ones. Whatever is left is still stale next cycle and gets picked up then, so
#: a cold start spreads over a few minutes instead of blocking on one pass.
DEFAULT_MAX_PER_PASS = 8
#: Consecutive failures that end a pass early. When the venue is unreachable
#: every remaining pair will fail the same way, slowly; there is nothing to
#: learn from the other thirty-seven attempts and the loop should go back to
#: waiting rather than hang.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CandleSyncRunner:
    """Refresh stale cached timeframes from the venue on a schedule."""

    def __init__(self, store, ledger, *, symbols: Sequence[str],
                 timeframes: Sequence[str],
                 sync_fn: Optional[Callable] = None,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 target_candles: int = DEFAULT_TARGET_CANDLES,
                 backfill_candles: int = DEFAULT_BACKFILL_CANDLES,
                 max_per_pass: int = DEFAULT_MAX_PER_PASS,
                 max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES):
        self.store = store
        self.ledger = ledger
        self.symbols = tuple(symbols)
        self.timeframes = tuple(timeframes)
        self.interval_s = interval_s
        self.target_candles = target_candles
        self.backfill_candles = backfill_candles
        self.max_per_pass = max_per_pass
        self.max_consecutive_failures = max_consecutive_failures
        self._sync_fn = sync_fn
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_result: Optional[dict] = None
        self.last_check: Optional[str] = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="candle-sync",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:  # noqa: BLE001 — a dead loop syncs nothing
                pass
            self._stop.wait(self.interval_s)

    # ---------------------------------------------------------------- work
    def _sync(self, symbol: str, timeframe: str, target: int) -> dict:
        if self._sync_fn is not None:
            return self._sync_fn(self.store, symbol, timeframe,
                                 target_candles=target)
        from data.historical import sync
        return sync(self.store, symbol, timeframe, target_candles=target)

    def stale_pairs(self, now: Optional[datetime] = None) -> list[dict]:
        """Every cached timeframe the freshness authority judges behind.

        Asked rather than decided: a second opinion compiled in here would
        drift from the one the dashboard shows and the gates enforce.
        """
        out = []
        for symbol in self.symbols:
            for timeframe in self.timeframes:
                try:
                    last = self.store.last_open_time(symbol, timeframe)
                except Exception:  # noqa: BLE001 — an unreadable pair is stale
                    last = None
                verdict = assess_timeframe(symbol, timeframe, last, now=now)
                if not verdict.fresh:
                    out.append({"symbol": symbol, "timeframe": timeframe,
                                "status": verdict.status,
                                "age_seconds": verdict.age_seconds,
                                "held": last.isoformat() if last else None})
        return out

    def check(self, now: Optional[datetime] = None) -> dict:
        """One pass: find what is behind, refresh it, report what happened."""
        stale = self.stale_pairs(now)
        synced, failed = [], []
        consecutive, attempted, gave_up = 0, 0, False
        for pair in stale:
            if attempted >= self.max_per_pass:
                break
            if consecutive >= self.max_consecutive_failures:
                gave_up = True
                break
            attempted += 1
            # An empty pair is backfilled, not topped up: there is no history
            # to extend and the consumers need depth, not the last few bars.
            target = (self.backfill_candles if pair["held"] is None
                      else self.target_candles)
            try:
                res = self._sync(pair["symbol"], pair["timeframe"], target)
            except Exception as exc:  # noqa: BLE001
                res = {"error": f"{type(exc).__name__}: {exc}"}
            row = {**pair, "target": target,
                   "stored": res.get("stored"), "error": res.get("error")}
            if res.get("error"):
                failed.append(row)
                consecutive += 1
            else:
                synced.append(row)
                consecutive = 0

        out = {"checked": len(self.symbols) * len(self.timeframes),
               "stale": len(stale), "synced": len(synced),
               "failed": len(failed), "pairs": synced + failed,
               # Deferred, not fixed: still stale and picked up next cycle.
               "deferred": len(stale) - len(synced) - len(failed),
               "gave_up_early": gave_up,
               "checked_at": _now_iso()}
        self._report(out, failed)
        with self._lock:
            self.last_result, self.last_check = out, out["checked_at"]
        return out

    def _report(self, out: dict, failed: list[dict]) -> None:
        """A failed refresh leaves real data stale, so it is said out loud."""
        if not self.ledger:
            return
        try:
            if failed:
                names = ", ".join(f"{p['symbol']} {p['timeframe']}"
                                  for p in failed[:6])
                self.ledger.log(
                    level="warning", stage="data",
                    message=(f"Candle sync could not refresh {len(failed)} "
                             f"stale timeframe(s): {names}. Those series stay "
                             "behind and anything reading them is reading old "
                             "candles."))
            elif out["synced"]:
                self.ledger.log(
                    level="info", stage="data",
                    message=(f"Candle sync refreshed {out['synced']} stale "
                             f"timeframe(s) of {out['checked']} checked."))
        except Exception:  # noqa: BLE001 — reporting must not kill the loop
            pass

    # -------------------------------------------------------------- status
    def status(self) -> dict:
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "interval_s": self.interval_s,
                "symbols": list(self.symbols),
                "timeframes": list(self.timeframes),
                "last_check": self.last_check,
                "result": self.last_result,
            }
