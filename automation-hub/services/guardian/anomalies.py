"""Anomaly detection (PRD §21; Phase 3).

Guardian learns what is normal from its own evidence and reports meaningful
deviations. An anomaly is *not* a failure, and every one says so. None is
claimed without a baseline: a detector with too little history reports
nothing rather than guessing.

Detectors:

* **evaluations_stopped** -- a strategy that evaluates every closed candle
  has not evaluated for three candle intervals while its component and feed
  both report healthy. Deterministic: no baseline needed, only the
  timeframe.
* **evaluation_latency** -- the time from a candle's close to its evaluation,
  against the same stream's own 7-day 95th percentile (at least 50 samples).
* **setup_drought** -- no setups today where the stream's own 7-day rate
  makes that improbable (Poisson p < 0.01 at today's evaluation count).
* **rejection_mix** -- the share of a rejection reason moved by more than
  20 percentage points and three standard errors from its 7-day share (at
  least 50 evaluations in each window).

Each anomaly is reported once when it appears and once when it clears.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from services.guardian import health as h

TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
              "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200, "1d": 86400, "1w": 604800}
NOT_A_FAILURE = "An anomaly is a deviation from this stream's own normal, not a failure."
_REASON_FINALS = ("NO_SETUP", "REJECTED", "MISSED")
_SETUP_FINALS = ("ENTERED", "ORDER_PENDING", "APPROVAL_REQUIRED", "SIGNAL_ONLY", "SIGNAL", "SETUP_PENDING")


def _day(ts: float, offset_days: int = 0) -> str:
    return (datetime.fromtimestamp(ts, timezone.utc) - timedelta(days=offset_days)).date().isoformat()


def _age(stamp: str, now: float) -> float:
    return now - datetime.fromisoformat(stamp).timestamp()


class AnomalyDetector:
    def __init__(self, store, *, min_samples: int = 50):
        self.store = store
        self.min_samples = int(min_samples)

    # ------------------------------------------------------------ scopes
    def _latest_scopes(self, now: float) -> dict[str, dict]:
        """Each component's most recent strategy stream (a lab that changed
        market is judged on the market it runs now)."""
        rows = self.store.strategy_rollup(since_day=_day(now, 2))
        latest: dict[str, dict] = {}
        for r in rows:
            cur = latest.get(r["source_component"])
            if cur is None or r["last_at"] > cur["last_at"]:
                latest[r["source_component"]] = dict(r)
        return latest

    @staticmethod
    def _judgeable(scope: str, nodes: dict[str, h.Component]) -> Optional[str]:
        """Why this stream is expected to be evaluating right now, or None."""
        node = nodes.get(scope)
        feed = nodes.get(f"feed:{scope}")
        if node is None or feed is None:
            return None
        if node.effective != h.HEALTHY or feed.effective != h.HEALTHY:
            return None
        if node.kind == "lab" and not node.facts.get("session_active"):
            return None
        if node.kind == "instance" and not feed.facts.get("live"):
            return None
        return f"{node.label} and its feed report healthy"

    # --------------------------------------------------------- detectors
    def evaluations_stopped(self, nodes, now) -> list[dict]:
        out = []
        for scope, r in self._latest_scopes(now).items():
            seconds = TF_SECONDS.get(r["timeframe"] or "")
            why = self._judgeable(scope, nodes)
            if not seconds or not why:
                continue
            gap = _age(r["last_at"], now)
            if gap > 3 * seconds + 60:
                out.append({"key": f"evaluations_stopped:{scope}", "detector": "evaluations_stopped",
                            "scope": scope, "strategy_id": r["strategy_id"], "symbol": r["symbol"],
                            "timeframe": r["timeframe"], "value": round(gap), "unit": "s since last evaluation",
                            "baseline": f"one evaluation every {seconds}s ({r['timeframe']} candles)",
                            "detail": f"no evaluation for {int(gap)}s while {why}"})
        return out

    def evaluation_latency(self, nodes, now) -> list[dict]:
        out = []
        since = datetime.fromtimestamp(now - 7 * 86400, timezone.utc).isoformat()
        recent = datetime.fromtimestamp(now - 3600, timezone.utc).isoformat()
        with self.store._lock:
            rows = [dict(r) for r in self.store._c.execute(
                "SELECT source_component, timestamp, latency_ms FROM guardian_events "
                "WHERE category='strategy' AND latency_ms IS NOT NULL AND timestamp>=? "
                "ORDER BY timestamp", (since,))]
        by: dict[str, list[dict]] = {}
        for r in rows:
            by.setdefault(r["source_component"], []).append(r)
        for scope, samples in by.items():
            base = [s["latency_ms"] for s in samples if s["timestamp"] < recent]
            now_ = [s["latency_ms"] for s in samples if s["timestamp"] >= recent][-5:]
            if len(base) < self.min_samples or len(now_) < 3:
                continue
            p95 = statistics.quantiles(base, n=20)[-1]
            current = statistics.median(now_)
            limit = max(3 * p95, p95 + 30_000)
            if current > limit:
                out.append({"key": f"evaluation_latency:{scope}", "detector": "evaluation_latency",
                            "scope": scope, "value": round(current), "unit": "ms from candle close",
                            "baseline": f"95th percentile {round(p95)} ms over {len(base)} evaluations",
                            "detail": f"evaluations run {round(current / 1000, 1)}s after the candle closes; "
                                      f"normally under {round(p95 / 1000, 1)}s"})
        return out

    def _days(self, now: float) -> dict[str, dict[str, list[dict]]]:
        rows = self.store.strategy_rollup(since_day=_day(now, 8))
        out: dict[str, dict[str, list[dict]]] = {}
        for r in rows:
            out.setdefault(r["source_component"], {}).setdefault(r["day"], []).append(r)
        return out

    def setup_drought(self, nodes, now) -> list[dict]:
        out = []
        today = _day(now)
        base_days = [_day(now, k) for k in range(1, 8)]
        for scope, days in self._days(now).items():
            if not all(d in days for d in base_days) or not self._judgeable(scope, nodes):
                continue
            evals = [sum(r["count"] for r in days[d]) for d in base_days]
            setups = [sum(r["count"] for r in days[d] if r["decision"] in _SETUP_FINALS) for d in base_days]
            today_rows = days.get(today, [])
            evals_today = sum(r["count"] for r in today_rows)
            setups_today = sum(r["count"] for r in today_rows if r["decision"] in _SETUP_FINALS)
            mean_evals = statistics.mean(evals)
            if mean_evals <= 0 or setups_today:
                continue
            expected = statistics.mean(setups) * evals_today / mean_evals
            p_zero = math.exp(-expected)
            if p_zero < 0.01:
                out.append({"key": f"setup_drought:{scope}", "detector": "setup_drought", "scope": scope,
                            "value": 0, "unit": "setups today",
                            "baseline": f"{round(statistics.mean(setups), 1)} setups a day over 7 days",
                            "detail": f"no setup in {evals_today} evaluations today; about {round(expected, 1)} "
                                      f"would be normal (chance of none: {p_zero:.4f})"})
        return out

    def rejection_mix(self, nodes, now) -> list[dict]:
        out = []
        current_days = {_day(now), _day(now, 1)}
        for scope, days in self._days(now).items():
            cur = [r for d, rows in days.items() if d in current_days for r in rows
                   if r["decision"] in _REASON_FINALS]
            base = [r for d, rows in days.items() if d not in current_days for r in rows
                    if r["decision"] in _REASON_FINALS]
            n_cur, n_base = sum(r["count"] for r in cur), sum(r["count"] for r in base)
            if n_cur < self.min_samples or n_base < self.min_samples:
                continue
            codes = {r["blocker_code"] for r in cur + base if r["blocker_code"]}
            for code in sorted(codes):
                p_cur = sum(r["count"] for r in cur if r["blocker_code"] == code) / n_cur
                p_base = sum(r["count"] for r in base if r["blocker_code"] == code) / n_base
                se = math.sqrt(max(p_base * (1 - p_base), 1e-9) / n_cur)
                if abs(p_cur - p_base) > max(0.20, 3 * se):
                    out.append({"key": f"rejection_mix:{scope}:{code}", "detector": "rejection_mix",
                                "scope": scope, "value": round(p_cur * 100, 1), "unit": "% of non-trades",
                                "baseline": f"{round(p_base * 100, 1)}% over the previous days ({n_base} evaluations)",
                                "detail": f"{code} is now {round(p_cur * 100, 1)}% of this stream's non-trades, "
                                          f"normally {round(p_base * 100, 1)}%"})
        return out

    # ------------------------------------------------------------- cycle
    def cycle(self, nodes: dict[str, h.Component], *, now: float,
              publish: Callable[..., None]) -> list[dict]:
        found: list[dict] = []
        for detector in (self.evaluations_stopped, self.evaluation_latency,
                         self.setup_drought, self.rejection_mix):
            try:
                found += detector(nodes, now)
            except Exception as exc:  # noqa: BLE001 -- one detector cannot stop the others
                publish("collector_failed", source_component=f"guardian.anomalies.{detector.__name__}",
                        severity="WARNING", reason=f"{type(exc).__name__}: {exc}"[:300])
        before = {a["key"]: a for a in (self.store.meta("anomalies.active") or [])}
        current = {a["key"]: {**a, "note": NOT_A_FAILURE,
                              "since": before.get(a["key"], {}).get("since")
                              or datetime.fromtimestamp(now, timezone.utc).isoformat()}
                   for a in found}
        for key in sorted(set(current) - set(before)):
            a = current[key]
            publish("anomaly_detected", source_component=a["scope"], severity="WATCH",
                    reason=f"{a['detector']}: {a['detail']}", strategy_id=a.get("strategy_id"),
                    symbol=a.get("symbol"), timeframe=a.get("timeframe"), evidence=a)
        for key in sorted(set(before) - set(current)):
            a = before[key]
            publish("anomaly_cleared", source_component=a["scope"], severity="INFO",
                    reason=f"{a['detector']}: back within its normal range", evidence=a)
        self.store.set_meta("anomalies.active", list(current.values()))
        return list(current.values())

    def active(self) -> list[dict]:
        return list(self.store.meta("anomalies.active") or [])
