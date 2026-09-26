"""A public forward paper track record, per instance, off by default.

The landing site's Performance page promised that a forward paper result
would be published "with its sample and costs" once there was one, but
nothing could publish it. This lets the owner switch one Trading Instance at
a time into a public, read-only record.

What goes out is the instance's own ledger, exactly as the dashboard's
metrics compute it (services/trading_instances.metrics), cut down to:

* percentages only -- returns and drawdown relative to the paper account's
  starting balance, and an equity index that starts at 100. No balance,
  P&L, position size or capital allocation ever leaves the server;
* closed trades of the current simulation session, after simulated fees,
  with the session number shown, so a reset account is visible as a reset;
* forward paper instances only (never research replays, never anything but
  paper execution), and only while the instance still exists.

Instances are named by an opaque public id, not their internal id. The owner
decides which instances to publish, and the response says so.
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone

from services.instance_switches import InstanceSwitches

NOTE = ("Paper trading: every order is simulated against live Binance market data and no real "
        "money was traded. Figures are closed trades only, after simulated fees, as a percentage "
        "of the paper account's starting balance. The owner chooses which instances to publish. "
        "Past paper results do not predict future returns.")
#: Below this many closed trades a record is labelled as too small to judge.
MIN_SAMPLE = 30
CURVE_POINTS = 200
CACHE_S = 60.0


def public_id(instance_id: str) -> str:
    return hashlib.sha256(f"track-record:{instance_id}".encode()).hexdigest()[:12]


def _thin(points: list, limit: int = CURVE_POINTS) -> list:
    """At most ``limit`` points, keeping the first, the last and the lowest."""
    if len(points) <= limit:
        return points
    lowest = min(range(len(points)), key=lambda i: points[i]["index"])
    step = (len(points) - 1) / (limit - 2)
    keep = {round(i * step) for i in range(limit - 1)} | {len(points) - 1, lowest}
    return [points[i] for i in sorted(keep)]


class PublicTrackRecord:
    def __init__(self, path: str | None, *, cache_s: float = CACHE_S):
        self.switches = InstanceSwitches(path)
        self.cache_s = float(cache_s)
        self._cache: tuple[float, dict] | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ switch
    def published(self, instance_id: str) -> bool:
        return self.switches.enabled(instance_id)

    def set(self, instance_id: str, published: bool, *, by: str = "") -> dict:
        self.switches.set(instance_id, published, by=by)
        self._cache = None
        return self.switches.row(instance_id)

    def forget(self, instance_id: str) -> None:
        self.switches.forget(instance_id)
        self._cache = None

    def state(self, manager, instance_id: str) -> dict:
        """The owner's view: the switch, and exactly what the public sees."""
        row = self.switches.row(instance_id)
        return {"published": bool(row.get("enabled")), "since": row.get("since"),
                "updated_at": row.get("updated_at"), "eligible": self._eligible(manager, instance_id),
                "preview": self.entry(manager, instance_id), "note": NOTE}

    # ------------------------------------------------------------ record
    @staticmethod
    def _eligible(manager, instance_id: str) -> bool:
        inst = manager._instances.get(instance_id)
        return bool(inst and inst.mode == "trading" and inst.execution_mode == "paper")

    def entry(self, manager, instance_id: str) -> dict | None:
        """One instance's public record, or None when it cannot be shown."""
        if not self._eligible(manager, instance_id):
            return None
        inst = manager._instances[instance_id]
        metrics = manager.metrics(instance_id)
        base = float(metrics.get("starting_balance") or 0)
        if base <= 0:
            return None

        def pct(value) -> float:
            return round(float(value or 0) / base * 100, 2)

        session = {}
        try:
            session = manager.store.simulation_session(inst.simulation_session_id) or {}
        except Exception:  # noqa: BLE001 -- the record stands without the session row
            session = {}
        started = session.get("started_at") or inst.started_at or inst.created_at
        curve = [{"t": point.get("t") or started,
                  "index": round(float(point.get("equity") or 0) / base * 100, 3)}
                 for point in metrics.get("equity_curve") or []]
        runtime = manager._runtime.get(instance_id)
        open_positions = None
        if runtime is not None:
            try:
                open_positions = len(runtime[1].positions())
            except Exception:  # noqa: BLE001
                open_positions = None
        trades = int(metrics.get("trades") or 0)
        return {
            "id": public_id(instance_id),
            "strategy": inst.strategy_label,
            "strategy_version": inst.strategy_version,
            "symbol": inst.symbol,
            "timeframe": inst.timeframe,
            "execution": "paper",
            "market_data": "live Binance",
            "fill_model": inst.fill_model,
            "entry_mode": inst.entry_mode,
            "risk_per_trade_pct": round(float(inst.risk_per_trade_pct) * 100, 3),
            "state": inst.state,
            "session_number": int(session.get("session_number") or inst.simulation_session_number or 1),
            "session_started_at": started,
            "published_since": self.switches.row(instance_id).get("since"),
            "closed_trades": trades,
            "wins": int(metrics.get("wins") or 0),
            "losses": int(metrics.get("losses") or 0),
            "win_rate_pct": float(metrics.get("win_rate") or 0),
            # No losing trade yet means no ratio, not a ratio of 99.
            "profit_factor": metrics.get("profit_factor") if float(metrics.get("gross_loss") or 0) > 0 else None,
            "return_pct": pct(metrics.get("realized_pnl")),
            "max_drawdown_pct": float(metrics.get("max_drawdown_pct") or 0),
            "longest_losing_streak": int(metrics.get("longest_losing_streak") or 0),
            "open_positions": open_positions,
            "last_trade_at": curve[-1]["t"] if trades else None,
            "sample_note": (f"Fewer than {MIN_SAMPLE} closed trades: too few to judge."
                            if trades < MIN_SAMPLE else None),
            "equity_index": _thin(curve),
        }

    def build(self, manager) -> dict:
        """Every published record, cached for ``cache_s`` because the route is
        public and each record reads the ledger."""
        now = time.monotonic()
        cached = self._cache
        if cached and now - cached[0] < self.cache_s:
            return cached[1]
        with self._lock:
            records = []
            for instance_id in self.switches.enabled_ids():
                try:
                    record = self.entry(manager, instance_id)
                except Exception:  # noqa: BLE001 -- one broken record must not hide the rest
                    record = None
                if record is not None:
                    records.append(record)
            records.sort(key=lambda r: r.get("session_started_at") or "")
            view = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "instances": records, "note": NOTE, "min_sample": MIN_SAMPLE}
            self._cache = (now, view)
        return view
