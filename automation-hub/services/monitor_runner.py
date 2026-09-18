"""Continuous monitoring — the agent, on a timer.

``services.monitor_agent`` answers "has this strategy drifted from its backtest?"
when asked. This runs that question on a schedule against whatever is ACTUALLY
deployed, and raises alerts through the same ledger + notifier path the watchdog
uses, so a deviation reaches the operator instead of waiting to be discovered.

Three design decisions worth stating, because each one is a trap avoided:

  * **The baseline is cached on the spec, not on the clock.** Re-running a
    one-year backtest every cycle would be minutes of CPU per hour to recompute
    a number that only changes when the strategy changes. The cache key is the
    spec's definition, so editing a rule invalidates it immediately and nothing
    else does. A long TTL still forces an eventual refresh, because the data
    behind the baseline does move.

  * **Alerts have a per-finding cooldown.** A strategy in drawdown produces the
    same finding every cycle. Without a cooldown the operator learns to ignore
    the channel, which is worse than not alerting at all.

  * **It watches the deployed spec, not the open editor.** ``engine.deployed_spec``
    is set at deploy time and cleared on every other reconfigure, so a built-in
    strategy is reported as "nothing to monitor" rather than silently compared
    against a spec that is no longer running.

Like the watchdog, this never modifies anything. It reads, evaluates, and tells
you.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from services import monitor_agent as ma

DEFAULT_INTERVAL_S = 900.0        # 15 min — deviation is a slow signal
DEFAULT_COOLDOWN_S = 3600.0       # re-alert the same finding at most hourly
DEFAULT_BASELINE_TTL_S = 86400.0  # refresh even an unchanged spec daily
DEFAULT_RANGE = "1Y"

_META = {"id", "created_at", "updated_at", "versions", "favorite", "tags",
         "folder", "tenant_id", "name", "description"}


def spec_key(spec: Optional[dict], range_key: str) -> str:
    """Identity of a backtest baseline: the strategy DEFINITION plus the window.

    Renaming or tagging a strategy does not change its behaviour, so it must not
    invalidate the baseline; changing a rule, a stop or the risk model must."""
    body = {k: v for k, v in (spec or {}).items() if k not in _META}
    return json.dumps({"spec": body, "range": range_key}, sort_keys=True, default=str)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MonitorRunner:
    """Periodically evaluate the deployed strategy and alert on deviation."""

    def __init__(self, engine, paper, ledger, *, exec_quality=None, notifier=None,
                 baseline_fn: Optional[Callable[[dict, str], dict]] = None,
                 volatility_fn: Optional[Callable[[dict, str], dict]] = None,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 cooldown_s: float = DEFAULT_COOLDOWN_S,
                 baseline_ttl_s: float = DEFAULT_BASELINE_TTL_S,
                 range_key: str = DEFAULT_RANGE):
        self.engine = engine
        self.paper = paper
        self.ledger = ledger
        self.exec_quality = exec_quality
        self.notifier = notifier
        self.range_key = range_key
        self.interval_s = interval_s
        self.cooldown_s = cooldown_s
        self.baseline_ttl_s = baseline_ttl_s
        # Injected so the runner is testable without touching market data.
        self._baseline_fn = baseline_fn or self._default_baseline
        self._volatility_fn = volatility_fn or self._default_volatility

        self._baseline_cache: dict[str, Any] = {}   # {key, baseline, vol, at}
        self._last_sent: dict[str, float] = {}
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
        self._thread = threading.Thread(target=self._run, name="monitor-agent",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:  # noqa: BLE001 — a monitor that dies monitors nothing
                pass
            self._stop.wait(self.interval_s)

    # ------------------------------------------------------------- defaults
    def _default_baseline(self, spec: dict, range_key: str) -> dict:
        """A real backtest through the same simulate path everything else uses.

        Also checks that the feed actually delivered the deployed timeframe. The
        whole comparison rests on both sides coming out of one engine on the same
        candles — if the source silently returns 1h bars for a 4h strategy, the
        baseline describes a different strategy and the deviation verdict is
        meaningless. That gets reported, not swallowed."""
        from services.spec_runner import run_spec_with_bars
        from services.strategy_review import bars_for_range
        from services.strategy_sweep import timeframe_matches
        tf = spec.get("timeframe") or "4h"
        results, rows = run_spec_with_bars(spec, bars_for_range(tf, range_key))
        base = ma.baseline_from_results(results) or {}
        if base and rows and not timeframe_matches(rows, tf):
            base["__tf_mismatch"] = tf
        return base

    def _default_volatility(self, spec: dict, range_key: str) -> dict:
        from data.market_data import get_bars
        from services.strategy_review import bars_for_range
        tf = spec.get("timeframe", "4h")
        rows, source = get_bars(spec.get("symbol", "BTCUSDT"),
                                n=bars_for_range(tf, range_key), timeframe=tf)
        if not rows:
            return {}
        band = ma.atr_pct_band(rows)
        recent = ma.atr_pct_band(rows[-120:]) if len(rows) > 60 else None
        return {"band": band, "current_atr_pct": (recent or {}).get("median"),
                "source": source}

    # -------------------------------------------------------------- caching
    def _baseline_for(self, spec: dict, now: float) -> tuple[dict, dict, bool]:
        """(baseline, volatility, cached?) — recomputed only when the strategy
        definition changed or the cache aged out."""
        key = spec_key(spec, self.range_key)
        c = self._baseline_cache
        if (c.get("key") == key and c.get("baseline") is not None
                and now - float(c.get("at") or 0) < self.baseline_ttl_s):
            return c["baseline"], c.get("vol") or {}, True
        baseline = self._baseline_fn(spec, self.range_key) or {}
        vol = self._volatility_fn(spec, self.range_key) or {}
        self._baseline_cache = {"key": key, "baseline": baseline, "vol": vol,
                                "at": now, "computed_at": _now_iso()}
        return baseline, vol, False

    # ---------------------------------------------------------------- check
    def check(self, now: Optional[float] = None) -> dict:
        now = now if now is not None else time.time()
        spec = getattr(self.engine, "deployed_spec", None)

        if not spec or not (spec.get("entry") or {}).get("rules"):
            out = {"available": False, "status": "no_strategy", "auto_modify": False,
                   "findings": [],
                   "note": ("No rule-based strategy is deployed, so there is no "
                            "backtest to compare live behaviour against. Deploy a "
                            "strategy from the Strategy Lab to enable monitoring.")}
            with self._lock:
                self.last_result, self.last_check = out, _now_iso()
            return out

        baseline, vol, cached = self._baseline_for(spec, now)

        # The feed may not honour the requested candle size. That does NOT
        # invalidate this comparison: the live engine fetches through the same
        # get_bars path, so both sides get the same candles and the deviation
        # verdict still holds. What it does invalidate is the LABEL — the
        # strategy is not trading the timeframe it says it is — so that is
        # reported alongside the result instead of blocking it.
        # (The sweep's identical check DOES block, correctly: there the whole
        # point is comparing one timeframe against another.)
        tf_mismatch = baseline.pop("__tf_mismatch", None)

        # The live sample must be THIS strategy's trades. paper.history() is
        # every closed trade from every strategy and symbol pooled together,
        # and comparing that against one strategy's backtest is not a loose
        # comparison but a different one: it reported a 36.75R live drawdown
        # against a 4.3R backtest for a strategy whose own backtest took a
        # single trade in a year. strategy_history() scopes it, and excludes
        # trades carrying no id rather than crediting them to whoever asked.
        strategy_id = str(spec.get("id") or "")
        pooled = self.paper.history()
        trades = self.paper.strategy_history(strategy_id)
        live = ma.live_metrics(trades)
        live["span_days"] = ma.span_days(trades)
        attribution = {"strategy_id": strategy_id, "scoped": len(trades),
                       "pooled": len(pooled)}

        execution = None
        if self.exec_quality is not None:
            try:
                execution = self.exec_quality.report(getattr(self.paper, "fill_model", None))
            except Exception:  # noqa: BLE001 — no execution report, no slippage check
                execution = None

        out = ma.evaluate(baseline=baseline or None, live=live,
                          attribution=attribution,
                          volatility_band=(vol or {}).get("band"),
                          current_atr_pct=(vol or {}).get("current_atr_pct"),
                          execution=execution)
        out.update({
            "strategy": spec.get("name") or getattr(self.engine, "strategy_label", None),
            "symbol": spec.get("symbol"), "timeframe": spec.get("timeframe"),
            "range": self.range_key,
            "baseline_cached": cached,
            "baseline_computed_at": self._baseline_cache.get("computed_at"),
            "data_source": (vol or {}).get("source"),
            # What the verdict was computed over. Without this the reader
            # cannot tell a strategy compared on its own 40 trades from one
            # compared on nothing, and the two read identically as "in_line".
            "attribution": attribution,
            "checked_at": _now_iso(),
        })
        if tf_mismatch:
            out["timeframe_warning"] = (
                f"The feed is not returning {tf_mismatch} candles. Live and backtest "
                "both read it, so the comparison below is still like-for-like — but "
                f"this strategy is not actually trading on {tf_mismatch}.")

        self._raise_alerts(out.get("findings") or [], now)
        with self._lock:
            self.last_result, self.last_check = out, out["checked_at"]
        return out

    def _raise_alerts(self, findings: list[dict], now: float) -> None:
        for f in findings:
            key = f.get("key", "?")
            last = self._last_sent.get(key)
            # "never sent" is not "sent recently". Defaulting the timestamp to 0
            # only looks correct because wall-clock time is huge — it would
            # swallow the first alert of any run whose clock starts near zero.
            if last is not None and now - last < self.cooldown_s:
                continue                       # already told you; not again yet
            self._last_sent[key] = now
            detail = f"{f.get('detail', '')} {f.get('recommendation', '')}".strip()
            try:
                self.ledger.add_alert(severity=f.get("severity", "warning"),
                                      category="monitor", title=f.get("title", "Deviation"),
                                      detail=detail)
                self.ledger.log(
                    level="error" if f.get("severity") == "critical" else "warning",
                    stage="monitor", message=f"{f.get('title')} — {f.get('detail')}")
            except Exception:  # noqa: BLE001 — alerting must not kill the loop
                pass
            if self.notifier:
                try:
                    self.notifier("risk", f"📉 {f.get('title')}", detail)
                except Exception:  # noqa: BLE001
                    pass

    # --------------------------------------------------------------- status
    def status(self) -> dict:
        """The last evaluation, without recomputing anything. Carries its own
        timestamp so a stale reading is visibly stale rather than passing as
        current."""
        with self._lock:
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "interval_s": self.interval_s,
                "cooldown_s": self.cooldown_s,
                "last_check": self.last_check,
                "result": self.last_result,
            }
