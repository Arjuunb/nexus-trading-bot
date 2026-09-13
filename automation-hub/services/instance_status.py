"""The truthful status contract for one Trading Instance.

The dashboard used to collapse everything into a single badge, so "running"
had to mean four different things at once: the worker thread is alive, the
Binance feed is fresh, the strategy has warmed up, and execution is armed.
When any one of them was false the badge still had to pick a word, and the
word it picked could not say which one had failed.

This module derives four independent axes from state the runtime already
owns, plus the market detail an operator needs to check the feed themselves.
It is pure: it reads a snapshot and returns a payload. Nothing here can change
a worker, and nothing here invents a value it was not given -- an unknown
field is ``None``, never a plausible-looking default.
"""
from __future__ import annotations

from datetime import datetime, timezone

# ---------------------------------------------------------------- runtime
RUNNING, PAUSED, STOPPED, RUNTIME_ERROR = "RUNNING", "PAUSED", "STOPPED", "ERROR"
STARTING = "STARTING"

# ----------------------------------------------------------------- market
CONNECTING = "CONNECTING"
SYNCHRONIZING = "SYNCHRONIZING"
LIVE = "LIVE"
STALE = "STALE"
DISCONNECTED = "DISCONNECTED"
RECONNECTING = "RECONNECTING"
FAILED = "FAILED"

# --------------------------------------------------------------- strategy
READY = "READY"
WARMING_UP = "WARMING_UP"
WAITING_FOR_DATA = "WAITING_FOR_DATA"
WAITING_FOR_HTF = "WAITING_FOR_HTF"
WAITING_FOR_SETUP = "WAITING_FOR_SETUP"
BLOCKED = "BLOCKED"
STRATEGY_ERROR = "ERROR"

# -------------------------------------------------------------- execution
SIGNALS_ONLY, FORWARD_PAPER, EXECUTION_DISABLED = "SIGNALS_ONLY", "FORWARD_PAPER", "DISABLED"

_WARMING_STATES = {"starting", "bootstrapping", "warming", "syncing", "rebooting"}


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


def runtime_status(*, worker_state: str, desired_running: bool,
                   worker_alive: bool) -> str:
    """Is there an execution runtime, and does the operator want one?"""
    state = str(worker_state or "").lower()
    if state == "error":
        return RUNTIME_ERROR
    if state == "paused":
        return PAUSED
    if not desired_running:
        return STOPPED
    if not worker_alive:
        # Desired, but nothing is executing. Saying RUNNING here is exactly the
        # lie this contract exists to prevent; the supervisor is repairing it.
        return STARTING if state in _WARMING_STATES else RUNTIME_ERROR
    if state in _WARMING_STATES:
        return STARTING
    if state in ("stopped", "created"):
        return STOPPED
    return RUNNING


def market_status(*, worker_state: str, feed: dict | None,
                  subscription: dict | None, data_age_seconds: float | None,
                  timeframe_seconds: int) -> tuple[str, str]:
    """(status, reason) for the market feed, from the feed's own facts."""
    state = str(worker_state or "").lower()
    transport = str((subscription or {}).get("transport_state") or "").upper()
    health = str((subscription or {}).get("state") or "").upper()
    reconnects = int((subscription or {}).get("reconnect_attempt") or 0)

    if state in ("stopped", "created") :
        return DISCONNECTED, "no market worker is running"
    if state == "error" or health in ("ERROR", "DATA_ERROR"):
        return FAILED, str((subscription or {}).get("health_reason")
                           or (feed or {}).get("last_error") or "market data failed")
    if transport in ("RECONNECTING",) or state == "recovering" or reconnects > 0:
        return RECONNECTING, f"transport reconnecting (attempt {reconnects})"
    if transport == "DISCONNECTED" or health == "DISCONNECTED":
        return DISCONNECTED, str((subscription or {}).get("health_reason") or "transport disconnected")
    if transport == "CONNECTING" or health == "CONNECTING" or state == "bootstrapping":
        return CONNECTING, "opening the Binance USD-M websocket"
    if health in ("LOADING_HISTORY", "RECONCILING") or state in ("warming", "syncing", "starting"):
        return SYNCHRONIZING, "loading and reconciling completed candles"
    if state == "data_stale" or health in ("STALE_CANDLES", "STALE_QUOTE", "STALE_MARK", "DELAYED"):
        return STALE, str((subscription or {}).get("health_reason") or "market data is stale")
    if data_age_seconds is None:
        return WAITING_FOR_DATA_MARKET, "no closed candle observed yet"
    if data_age_seconds > timeframe_seconds * 3:
        return DISCONNECTED, f"no closed candle for {data_age_seconds:.0f}s"
    if data_age_seconds > timeframe_seconds * 1.5:
        return STALE, f"newest closed candle is {data_age_seconds:.0f}s old"
    return LIVE, "closed candles are current"


#: A feed that has connected but not yet delivered a closed candle is neither
#: live nor broken. It gets its own value rather than being rounded to one.
WAITING_FOR_DATA_MARKET = "WAITING_FOR_DATA"


def strategy_status(*, market: str, worker_state: str, warmup_bars: int,
                    warmup_required: int, blocker: str | None,
                    htf_ready: bool, health_status: str | None) -> tuple[str, str]:
    state = str(worker_state or "").lower()
    if state == "error":
        return STRATEGY_ERROR, "worker error"
    if market in (DISCONNECTED, FAILED, STALE, RECONNECTING):
        return WAITING_FOR_DATA, f"market feed is {market}"
    if market in (CONNECTING, SYNCHRONIZING, WAITING_FOR_DATA_MARKET):
        return WARMING_UP, "waiting for reconciled closed candles"
    if warmup_required and warmup_bars < warmup_required:
        return WARMING_UP, f"{warmup_bars}/{warmup_required} warm-up candles"
    if not htf_ready:
        return WAITING_FOR_HTF, "native higher-timeframe context is not available"
    if str(health_status or "").lower() == "unhealthy":
        return BLOCKED, "strategy health guard"
    text = str(blocker or "")
    if text and "WARMUP" in text.upper():
        return WARMING_UP, text
    if text and "NO_SETUP" not in text.upper():
        return BLOCKED, text
    if state in ("ready", "running"):
        return WAITING_FOR_SETUP, text or "no qualifying setup on the last closed candle"
    return WARMING_UP, text or "worker is not evaluating yet"


def execution_status(*, mode: str, execution_mode: str, entries_armed: bool,
                     market: str) -> tuple[str, str]:
    if str(mode) == "research":
        return SIGNALS_ONLY, "research instance: signals are recorded, no orders are created"
    if str(execution_mode or "").lower() != "paper":
        return EXECUTION_DISABLED, "no simulated execution engine is attached"
    if not entries_armed:
        return EXECUTION_DISABLED, "entry gate is closed"
    if market != LIVE:
        # Fail closed: an entry must never be created from a feed that is not
        # demonstrably current.
        return EXECUTION_DISABLED, f"entries blocked while market data is {market}"
    return FORWARD_PAPER, "forward paper execution on live Binance USD-M data; no exchange routing"


def build(*, instance, engine: dict | None, market: dict, timeframe_seconds: int,
          worker_alive: bool, entries_armed: bool, health_status: str | None,
          htf_policy: dict | None) -> dict:
    """Assemble the full status contract for one instance."""
    engine = engine or {}
    subscription = engine.get("websocket") or {}
    quote = subscription.get("quote") or {}
    last_prices = engine.get("last_prices") or {}
    data_age = market.get("market_data_age_seconds")
    if data_age is None:
        raw = _age_seconds(market.get("last_market_data_timestamp"))
        data_age = None if raw is None else max(0.0, raw - timeframe_seconds)

    worker_state = str(market.get("_worker_state") or instance.state)
    runtime = runtime_status(worker_state=worker_state,
                             desired_running=bool(instance.desired_running),
                             worker_alive=bool(worker_alive))
    feed, feed_reason = market_status(
        worker_state=worker_state, feed=engine, subscription=subscription,
        data_age_seconds=data_age, timeframe_seconds=timeframe_seconds)
    htf = (htf_policy or {}).get("evidence") or {}
    strategy, strategy_reason = strategy_status(
        market=feed, worker_state=worker_state,
        warmup_bars=int(engine.get("warmup_bars") or 0),
        warmup_required=int(engine.get("warmup_required") or 0),
        blocker=engine.get("last_blocker") or market.get("last_blocker"),
        htf_ready=bool(htf) or not (htf_policy or {}).get("requires_htf", True),
        health_status=health_status)
    execution, execution_reason = execution_status(
        mode=instance.mode, execution_mode=instance.execution_mode,
        entries_armed=bool(entries_armed), market=feed)

    primary = (htf_policy or {}).get("primary_timeframe")
    secondary = (htf_policy or {}).get("secondary_timeframe")
    return {
        "runtime_status": runtime,
        "market_status": feed,
        "market_status_reason": feed_reason,
        "strategy_status": strategy,
        "strategy_status_reason": strategy_reason,
        "execution_status": execution,
        "execution_status_reason": execution_reason,
        # Precedence matters: a stale worker blocker from the last evaluation
        # must not outrank a feed that is currently disconnected. The reason an
        # operator needs is the one that is true NOW, and the market axis wins
        # whenever the feed is not live, because nothing downstream of a dead
        # feed can be the real cause.
        "current_blocker": (
            feed_reason if feed != LIVE else
            (engine.get("last_blocker") or market.get("last_blocker")
             or (None if strategy == WAITING_FOR_SETUP else strategy_reason))),
        "feed": {
            "exchange": market.get("exchange"),
            "market_type": market.get("market_type"),
            "symbol": instance.symbol,
            "execution_timeframe": instance.timeframe,
            "htf_primary_timeframe": primary,
            "htf_secondary_timeframe": secondary,
            "last_trade_price": last_prices.get(instance.symbol),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "mark_price": quote.get("mark"),
            "last_closed_candle_timestamp": (engine.get("last_closed_candle")
                                             or market.get("last_market_data_timestamp")),
            "last_processed_candle_timestamp": (engine.get("last_processed_candle_timestamp")
                                                or market.get("last_processed_candle_timestamp")),
            "last_websocket_message_timestamp": subscription.get("last_update"),
            "last_quote_timestamp": subscription.get("last_quote_update"),
            "data_age_seconds": None if data_age is None else round(float(data_age), 1),
            "quote_age_seconds": subscription.get("quote_age_seconds"),
            "data_source": engine.get("data_source") or market.get("data_source"),
            "warmup_bars": engine.get("warmup_bars"),
            "warmup_required": engine.get("warmup_required"),
            "duplicate_candles": market.get("duplicate_candles"),
            "missing_candles": market.get("missing_candles"),
            "out_of_order_candles": market.get("out_of_order_candles"),
        },
        "subscription": {
            "consumer_id": subscription.get("consumer_id"),
            "channel": (f"{instance.symbol}:{instance.timeframe}"
                        if subscription else None),
            "state": subscription.get("state"),
            "transport_state": subscription.get("transport_state"),
            "transport_channels": subscription.get("transport_channels"),
            "reliable": subscription.get("reliable"),
            "health_reason": subscription.get("health_reason"),
            "failing_dependency": subscription.get("failing_dependency"),
            "reconnect_attempts": subscription.get("reconnect_attempt"),
            "retry_state": subscription.get("retry_state"),
            "pending_candle_ids": (subscription.get("subscriber_delivery") or {}).get("pending_candle_ids"),
        },
        "worker": {
            "alive": bool(worker_alive),
            "lifecycle_state": worker_state,
            # Falls back to the persisted heartbeat, so "when did this worker
            # last do anything?" is answerable after a restart from storage
            # alone -- with no worker object to ask.
            "last_heartbeat": engine.get("last_heartbeat") or market.get("worker_heartbeat"),
            "persisted_heartbeat": market.get("worker_heartbeat"),
            "last_transition": engine.get("last_transition"),
            "started_at": engine.get("started_at"),
            "uptime_seconds": engine.get("uptime_s"),
            "engine_reconnect_attempt": engine.get("reconnect_attempt"),
            "engine_max_reconnect_attempts": engine.get("max_reconnect_attempts"),
            "engine_reconnect_next_at": engine.get("reconnect_next_at"),
        },
    }
