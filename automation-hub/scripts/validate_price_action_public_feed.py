#!/usr/bin/env python3
"""Bounded public Binance connectivity and paper-only soak validation."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
REPOSITORY = HUB.parent
for source_root in (REPOSITORY, HUB):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from data.market_data_v2 import MarketDataService  # noqa: E402
from services.price_action_lab import (  # noqa: E402
    PaperExecutionConfig, PriceActionLabRuntime, PriceActionPaperAccount,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Public USD-M REST/WebSocket validation; never submits a real order")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--seconds", type=int, default=130)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    from services.feature_flags import price_action_lab_enabled
    if not price_action_lab_enabled():
        raise SystemExit('PRICE_ACTION_LAB_DISABLED')
    if not 20 <= args.seconds <= 1800:
        raise SystemExit("--seconds must be between 20 and 1800")

    market = MarketDataService(args.cache_dir)
    account = PriceActionPaperAccount(args.state_db)
    account.configure(symbol=args.symbol, timeframe=args.timeframe,
                      execution_config=PaperExecutionConfig(operating_mode="automatic"))
    runtime = PriceActionLabRuntime(market, account)
    events, samples = [], []
    original_sink = runtime.stream.event_sink

    def capture(event):
        events.append(event)
        if original_sink:
            original_sink(event)
    runtime.stream.event_sink = capture

    result = {"started_at": datetime.now(timezone.utc).isoformat(),
              "symbol": args.symbol.upper(), "timeframe": args.timeframe,
              "execution_mode": "PAPER", "private_credentials_used": False,
              "real_execution_allowed": False, "checks": {}}
    try:
        result["checks"]["exchange_metadata"] = market.verify_binance_usdm()
        history = market.public_usdm_window(args.symbol, args.timeframe, limit=200)
        result["checks"]["historical_candles"] = {
            "count": len(history), "first": history[0].timestamp.isoformat(),
            "last": history[-1].timestamp.isoformat()}
        result["checks"]["public_quote"] = market.public_usdm_quote(args.symbol)
        now = datetime.now(timezone.utc)
        funding = market.download_usdm_funding_history(
            args.symbol, start_ms=int((now - timedelta(days=7)).timestamp() * 1000),
            end_ms=int(now.timestamp() * 1000))
        result["checks"]["funding_history"] = funding

        # An isolated paper position plus a far-away paper limit let the soak
        # prove state preservation. Neither object is an exchange order.
        quote = result["checks"]["public_quote"]
        market_order = account.broker.submit(
            symbol=args.symbol, side="buy", order_type="market", quantity=.001)
        account.broker.process_candle(args.symbol, history[-1])
        initial_positions = account.state()["positions"]
        if not initial_positions:
            raise RuntimeError("isolated PAPER position could not be opened for persistence validation")
        pending = account.broker.submit(
            symbol=args.symbol, side="buy", order_type="limit", quantity=.001,
            limit_price=float(quote["bid"]) * .5)
        pending_id = pending["id"]
        position_symbol = initial_positions[0]["symbol"]
        result["checks"]["paper_fixture"] = {
            "market_order_id": market_order["id"], "pending_order_id": pending_id,
            "position_symbol": position_symbol, "execution_mode": "PAPER"}
        runtime.ensure(args.symbol, args.timeframe)
        initial_closed_update = runtime.stream.status()["last_closed_update"]
        restarted = False
        gap_probe = {"attempted": False, "restored": False, "reconciled": 0}
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            status = runtime.stream.status()
            samples.append(status)
            if not restarted and time.monotonic() >= deadline - args.seconds / 2:
                gap_probe["attempted"] = True
                with runtime.stream._lock:
                    before = list(runtime.stream._bars)
                    if len(before) < 3:
                        raise RuntimeError("not enough completed REST candles for gap-repair probe")
                    removed = before[-2]
                    runtime.stream._bars = deque(
                        [row for row in before if row.timestamp != removed.timestamp],
                        maxlen=runtime.stream.max_bars)
                    runtime.stream.missing_candles += 1
                    runtime.stream._set_state(
                        "DELAYED", "controlled validation gap; REST reconciliation required")
                loop = runtime.stream._loop
                if loop is None or not loop.is_running():
                    raise RuntimeError("public stream loop unavailable for gap-repair probe")
                repaired = asyncio.run_coroutine_threadsafe(
                    runtime.stream.reconcile(), loop).result(timeout=30)
                gap_probe.update({
                    "removed_candle": removed.timestamp.isoformat(),
                    "reconciled": repaired,
                    "restored": removed.timestamp in {
                        row.timestamp for row in runtime.stream.snapshot()["closed_bars"]},
                })
                disconnected_orders = {row["id"] for row in account.state()["orders"]}
                disconnected_positions = {row["symbol"] for row in account.state()["positions"]}
                result["checks"]["paper_state_during_disconnect"] = {
                    "pending_order_preserved": pending_id in disconnected_orders,
                    "position_preserved": position_symbol in disconnected_positions,
                    "positions": len(disconnected_positions)}
                socket = runtime.stream._socket
                if socket is None:
                    raise RuntimeError("market WebSocket unavailable for controlled reconnect")
                asyncio.run_coroutine_threadsafe(
                    socket.close(code=1012, reason="controlled validation reconnect"),
                    loop).result(timeout=30)
                restarted = True
            time.sleep(1)
        final_status = runtime.stream.status()
        snapshot = runtime.stream.snapshot()
        closed_times = [row.timestamp.isoformat() for row in snapshot["closed_bars"]]
        result["checks"]["websocket"] = {
            "final_status": final_status,
            "states_seen": [row["state"] for row in samples],
            "connection_events": events,
            "closed_candles": len(closed_times),
            "duplicate_completed_candles_in_snapshot": len(closed_times) - len(set(closed_times)),
            "completed_candle_advanced": bool(
                initial_closed_update and final_status["last_closed_update"] and
                final_status["last_closed_update"] > initial_closed_update),
            "controlled_reconnect_performed": restarted,
        }
        market_channel_states = [
            row.get("state") for row in events
            if row.get("kind") == "connection_channel" and row.get("channel") == "market"]
        public_channel_states = [
            row.get("state") for row in events
            if row.get("kind") == "connection_channel" and row.get("channel") == "public"]
        health_states = [row.get("state") for row in events if row.get("kind") == "market_data_health"]
        result["checks"]["rest_gap_reconciliation"] = gap_probe
        result["checks"]["websocket"]["reconnect_completed"] = (
            market_channel_states.count("CONNECTED") >= 2 and
            public_channel_states.count("CONNECTED") >= 1)
        result["checks"]["websocket"]["synchronized_after_reconnect"] = (
            health_states.count("SYNCHRONIZED") >= 2 and
            final_status["state"] == "SYNCHRONIZED" and
            final_status["transport_state"] == "CONNECTED" and
            final_status.get("transport_channels") == {
                "market": "CONNECTED", "public": "CONNECTED"} and
            final_status["reliable"] is True and
            all(final_status.get(field) is not None for field in (
                "last_candle_update", "last_quote_update", "last_mark_update"))
        )
        stale_now = ((runtime.stream.last_update or datetime.now(timezone.utc)) +
                     timedelta(seconds=runtime.stream.stale_after_seconds + 1))
        stale = runtime.stream.status(now=stale_now)
        result["checks"]["stale_feed"] = {
            "state": stale["state"], "new_entries_paused": stale["new_entries_paused"]}
        funding_once = account.apply_funding_once(
            symbol=args.symbol, funding_time=quote.get("last_funding_time"),
            rate=quote.get("funding_rate") or 0, mark_price=quote["mark"])
        funding_twice = account.apply_funding_once(
            symbol=args.symbol, funding_time=quote.get("last_funding_time"),
            rate=quote.get("funding_rate") or 0, mark_price=quote["mark"])
        result["checks"]["funding_deduplication"] = {
            "first": funding_once, "second": funding_twice,
            "deduplicated": funding_twice.get("reason") == "funding event already processed"}
        state = account.state()
        strategy_keys = [(row["kind"], row.get("object_id")) for row in state["activity"]
                         if row.get("object_id") and row["kind"] == "strategy_candidate"]
        order_keys = [(row["zone_id"], row["direction"]) for row in state["order_metadata"]]
        result["checks"]["strategy_and_order_deduplication"] = {
            "strategy_events": len(strategy_keys),
            "duplicate_strategy_events": len(strategy_keys) - len(set(strategy_keys)),
            "automatic_orders": len(order_keys),
            "duplicate_zone_direction_orders": len(order_keys) - len(set(order_keys)),
        }
        session_id = account.session()["id"]
        runtime.stop()
        reopened = PriceActionPaperAccount(args.state_db)
        funding_after_reopen = reopened.apply_funding_once(
            symbol=args.symbol, funding_time=quote.get("last_funding_time"),
            rate=quote.get("funding_rate") or 0, mark_price=quote["mark"])
        result["checks"]["restart_persistence"] = {
            "session_id": session_id,
            "same_session": reopened.session()["id"] == session_id,
            "pending_order_preserved": pending_id in {row["id"] for row in reopened.state()["orders"]},
            "position_preserved": position_symbol in {
                row["symbol"] for row in reopened.state()["positions"]},
            "orders": len(reopened.state()["orders"]),
            "positions": len(reopened.state()["positions"]),
            "funding_deduplicated_after_reopen": (
                funding_after_reopen.get("reason") == "funding event already processed"),
        }
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        result["outcome"] = "VALIDATED" if (
            final_status["last_update"] and final_status["reliable"] and
            result["checks"]["websocket"]["duplicate_completed_candles_in_snapshot"] == 0 and
            result["checks"]["websocket"]["completed_candle_advanced"] and
            result["checks"]["websocket"]["reconnect_completed"] and
            result["checks"]["websocket"]["synchronized_after_reconnect"] and
            gap_probe["restored"] and gap_probe["reconciled"] >= 1 and
            result["checks"]["strategy_and_order_deduplication"]["duplicate_strategy_events"] == 0 and
            result["checks"]["strategy_and_order_deduplication"]["duplicate_zone_direction_orders"] == 0 and
            result["checks"]["stale_feed"]["new_entries_paused"] and
            result["checks"]["restart_persistence"]["same_session"] and
            result["checks"]["restart_persistence"]["pending_order_preserved"] and
            result["checks"]["restart_persistence"]["position_preserved"] and
            result["checks"]["restart_persistence"]["funding_deduplicated_after_reopen"]
        ) else "INCOMPLETE"
    except Exception as exc:
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        result["outcome"] = "FAILED"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        runtime.stop()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, sort_keys=True, indent=2, default=str) + "\n",
                      encoding="utf-8")
    print(json.dumps(result, sort_keys=True, indent=2, default=str))
    return 0 if result["outcome"] == "VALIDATED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
