"""Platform-level counters that separate "no setup" from "never evaluated".

The distinction this exists for: a strategy that looked at 288 candles today
and found nothing is working correctly, and a strategy that was never handed a
candle is broken. Both produce zero trades, and before these counters existed
the dashboard showed the same thing for each.

Everything here is read from live runtime objects and the durable store. No
value is cached and none is invented -- a counter the runtime cannot supply is
reported as ``None``, not as zero, because a confident zero is exactly the lie
that hides a dead worker.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone


def _closed_candle_age(value, timeframe: str) -> float | None:
    """Seconds since the candle CLOSED, using the one shared definition."""
    from services.market_data_freshness import assess_timeframe

    stamp = _parse_stamp(value)
    if stamp is None:
        return None
    try:
        return assess_timeframe("", timeframe, stamp).age_seconds
    except ValueError:
        # An unknown timeframe must not fabricate an age; say nothing instead.
        return None


def _parse_stamp(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


def _age_seconds(value) -> float | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def _process_resources() -> dict:
    out: dict = {"rss_mb": None, "threads": threading.active_count(),
                 "pid": os.getpid()}
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    out["rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                    break
    except OSError:
        pass
    return out


def instance_metrics(manager, instance_id: str) -> dict:
    """Per-instance counters, straight off the worker."""
    inst = manager._instances.get(instance_id)
    runtime = manager._runtime.get(instance_id)
    if inst is None:
        return {"instance_id": instance_id, "exists": False}
    engine = runtime[0] if runtime else None
    status = engine.status() if engine is not None else {}
    subscription = status.get("websocket") or {}
    delivery = subscription.get("subscriber_delivery") or {}
    return {
        "instance_id": instance_id,
        "symbol": inst.symbol,
        "strategy_id": inst.strategy_key,
        "timeframe": inst.timeframe,
        "state": inst.state,
        "worker_alive": manager.worker_alive(instance_id),
        # "Did the strategy get asked?" -- bars is the count of closed candles
        # actually handed to it. Zero bars with a live worker is the signature
        # of a feed that never delivered, which no P&L number can tell you.
        "strategy_evaluation_count": status.get("bars"),
        "signals_generated": status.get("signals"),
        "orders_generated": status.get("accepted_signals"),
        "orders_rejected": status.get("rejections"),
        "rejection_reasons": status.get("rejection_counts"),
        "trades": status.get("trades"),
        "reconnect_count": status.get("reconnect_attempt"),
        "duplicate_candles": status.get("duplicate_candles_ignored"),
        "missing_candles": status.get("missing_candles"),
        "out_of_order_candles": status.get("out_of_order_candles"),
        "market_message_age_seconds": _age_seconds(subscription.get("last_update")),
        # Age from the candle's CLOSE, not its open. last_closed_candle stores
        # the open (every provider and store in this codebase stamps a candle
        # there), so measuring raw age reported a 1h candle that had just
        # closed as 3600 seconds old -- a healthy feed looking a full interval
        # behind on the instance dashboard.
        "closed_candle_age_seconds": _closed_candle_age(
            status.get("last_closed_candle"), inst.timeframe),
        "processing_latency_seconds": _age_seconds(status.get("last_heartbeat")),
        "queue_depth": delivery.get("queue_depth"),
        "peak_queue_depth": delivery.get("peak_queue_depth"),
        "quote_queue_depth": delivery.get("quote_queue_depth"),
        "dropped_quotes": delivery.get("dropped_quotes"),
        "backlog_exceeded": delivery.get("backlog_exceeded"),
        "config_revision": inst.config_revision,
        "running_config_revision": status.get("config_revision"),
    }


def platform_metrics(manager, *, supervisor=None, owner_id: str | None = None) -> dict:
    """Process-wide counters, plus one row per instance."""
    instances = manager.owned_instances(owner_id)
    rows = [instance_metrics(manager, item.id) for item in instances]
    hub = getattr(manager, "market_hub", None)
    channels = hub.channel_report() if hasattr(hub, "channel_report") else []
    # Counted with the one shared rule, not a local interval * 1.5. This was
    # the sixth independent definition of stale on the platform, and with the
    # age above now measured from the close its old threshold no longer even
    # meant what it used to.
    stale = sum(1 for row in rows
                if row.get("worker_alive") and _row_is_stale(row))

    def total(key):
        values = [row.get(key) for row in rows if isinstance(row.get(key), (int, float))]
        return sum(values) if values else 0

    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "active_instances": len(instances),
        "running_workers": sum(1 for row in rows if row.get("worker_alive")),
        "desired_workers": sum(1 for item in instances if item.desired_running),
        "blocked_instances": sum(1 for item in instances if item.state == "blocked"),
        "market_connections": len(channels),
        "active_subscriptions": sum(int(row.get("consumer_count") or 0) for row in channels),
        "reconnect_count": total("reconnect_count"),
        "stale_feed_count": stale,
        "strategy_evaluation_count": total("strategy_evaluation_count"),
        "signals_generated": total("signals_generated"),
        "orders_generated": total("orders_generated"),
        "orders_rejected": total("orders_rejected"),
        "duplicate_candles": total("duplicate_candles"),
        "missing_candles": total("missing_candles"),
        "queue_depth": total("queue_depth"),
        "dropped_quotes": total("dropped_quotes"),
        "consumers_behind_the_feed": sum(
            1 for row in rows if row.get("backlog_exceeded")),
        "max_market_message_age_seconds": max(
            [row["market_message_age_seconds"] for row in rows
             if row.get("market_message_age_seconds") is not None] or [0]),
        "process": _process_resources(),
        "supervisor": supervisor.status() if supervisor is not None else None,
        "market_data_channels": channels,
        "instances": rows,
    }


def _row_is_stale(row) -> bool:
    """Is this instance's newest closed candle past its deadline?

    Uses services/market_data_freshness.py so the count on the platform
    dashboard can never disagree with the gate that blocks the trade.
    """
    from services.market_data_freshness import grace_for_interval
    from bot.data.resample import TF_SECONDS

    age = row.get("closed_candle_age_seconds")
    if age is None:
        return False          # unknown is not counted as stale; it is unknown
    interval = TF_SECONDS.get(row.get("timeframe") or "", None)
    if interval is None:
        return False
    return float(age) > interval + grace_for_interval(interval)


def _timeframe_seconds(manager, instance_id) -> int:
    from services.trading_instances import _TIMEFRAME_SECONDS
    inst = manager._instances.get(instance_id)
    return _TIMEFRAME_SECONDS.get(getattr(inst, "timeframe", "5m"), 300)
