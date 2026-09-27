"""Price Action lab decisions: what the strategy formed and what the lab did.

The waiting-setup case runs the real, frozen Price Action engine over
deterministic candles and the lab's own per-candle evaluation, the sequence
the lab's replay loop uses. The order cases use the lab's synchronize path
with a proposal shaped as the engine emits it, as the lab's own tests do.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from bot.types import Bar
from data.trade_record_store import TradeRecordStore
from services.journal_labs import PALabProjector
from services.native_price_action import NativePriceActionEngine, PriceActionConfig
from services.price_action_lab import PaperExecutionConfig, PriceActionPaperAccount

RULES = {"tick_size": 0.01, "quantity_step": 0.001, "min_quantity": 0.001,
         "max_quantity": 1000, "min_notional": 5}
T0 = datetime(2026, 3, 2, tzinfo=timezone.utc)


def _candles(n: int = 300) -> list[Bar]:
    bars, price = [], 100.0
    for i in range(n):
        drift = math.sin(i / 17) * 0.9 + math.sin(i / 5.3) * 0.4
        o = price
        c = price + drift * 0.35 + ((i * 7919) % 13 - 6) * 0.03
        h = max(o, c) + 0.25 + ((i * 104729) % 7) * 0.05
        low = min(o, c) - 0.25 - ((i * 1299709) % 7) * 0.05
        bars.append(Bar(T0 + timedelta(minutes=5 * i), o, h, low, c, 1000.0))
        price = c
    return bars


def _account(tmp_path, operating_mode="automatic") -> PriceActionPaperAccount:
    account = PriceActionPaperAccount(str(tmp_path / "pa.db"))
    account.start(mode="LIVE_PAPER", symbol="BTCUSDT", timeframe="5m",
                  execution_config=PaperExecutionConfig(operating_mode=operating_mode,
                                                        strategy_id="PA1_SR_REJECTION"))
    return account


def test_a_setup_waiting_for_confirmation_is_one_decision_not_one_per_candle(tmp_path):
    account = _account(tmp_path)
    engine = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m"))
    bars = _candles()
    engine.ingest_closed_bars(bars[:150])
    for bar in bars[150:]:
        engine.process_closed_bar(bar, market_data_health="SYNCHRONIZED")
        account.record_evaluation(engine.visual_state(candle_window=3000), bar,
                                  {"state": "SYNCHRONIZED"})
    evaluations = [json.loads(r[0]) for r in account._db.execute(
        "SELECT payload_json FROM pa_evaluations")]
    pending = {e["trace"]["setup_id"] for e in evaluations
               if (e.get("trace") or {}).get("state") == "ORDER_PENDING"}
    assert len(evaluations) == 150 and len(pending) >= 5         # the real engine formed setups

    store = TradeRecordStore()
    PALabProjector(account).project(store)
    waiting = store.query_decisions(where="decision_type='WAITING_CONFIRMATION'", limit=500)
    assert {d["evidence"]["setup_id"] for d in waiting} == pending    # one per setup
    assert len(store.query_decisions(limit=500)) == len(pending)      # WATCHING candles: none
    assert store.query_trades() == []
    sample = waiting[0]
    trace = next(e["trace"] for e in evaluations
                 if (e.get("trace") or {}).get("setup_id") == sample["evidence"]["setup_id"])
    assert sample["conditions_passed"] == [c["key"] for c in trace["conditions"]
                                           if c["status"] == "PASS"]
    assert "confirmation" in sample["reason"] and sample["evidence"]["candles"] >= 1
    PALabProjector(account).project(store)                        # a second pass adds nothing
    assert len(store.query_decisions(limit=500)) == len(pending)


def _proposal_state(proposal_id="pa-proposal-1", setup_id="pa-setup-1"):
    return {"research_id": "PRICE_ACTION_NATIVE_V1_RESEARCH", "strategy_version": "1.1.0",
            "symbol": "BTCUSDT", "timeframe": "5m",
            "setups": [{"id": setup_id, "strategy_id": "PA1_SR_REJECTION", "direction": "bullish",
                        "phase": "ORDER_PENDING", "zone_id": "zone-1"}],
            "proposals": [{"id": proposal_id, "setup_id": setup_id, "strategy_id": "PA1_SR_REJECTION",
                           "direction": "bullish", "entry": 105, "stop": 100, "target": 117.5,
                           "valid_until_index": 20}],
            "metrics": {}}


def _trade_it(account, state):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=15)
    account.synchronize_strategy(state, contract_rules=RULES, candle=Bar(now, 100, 104, 99, 103, 1000),
                                 feed_reliable=True, feed_status={"state": "SYNCHRONIZED"})
    return now


def _fill(account, state, now):
    account.synchronize_strategy(
        state, contract_rules=RULES, candle=Bar(now + timedelta(minutes=5), 104, 106, 101, 105, 1000),
        feed_reliable=True, feed_status={"state": "SYNCHRONIZED", "last_quote_update": now.isoformat(),
                                         "last_mark_update": now.isoformat()},
        execution_quote={"bid": 104.9, "ask": 105.1, "mark": 105.0})


def test_an_order_the_lab_placed_by_itself_has_a_decision_linked_to_its_trade(tmp_path):
    account = _account(tmp_path, "automatic")
    state = _proposal_state()
    _fill(account, state, _trade_it(account, state))
    assert account._db.execute("SELECT COUNT(*) FROM pa_candidates").fetchone()[0] == 0
    store = TradeRecordStore()
    PALabProjector(account).project(store)
    [trade] = store.query_trades()
    [decision] = store.query_decisions()
    assert decision["decision_type"] == "TRADE_OPENED"
    assert decision["journal_record_id"] == trade["journal_record_id"]


def test_an_approved_candidate_links_to_the_trade_it_became(tmp_path):
    """The candidate row names the proposal "{session}:{proposal}", the trade
    record the proposal itself; the link used to compare the two and miss."""
    account = _account(tmp_path, "manual_approval")
    state = _proposal_state()
    now = _trade_it(account, state)
    account.approve_candidate("pa-proposal-1")
    _fill(account, state, now)
    store = TradeRecordStore()
    PALabProjector(account).project(store)
    [trade] = store.query_trades()
    [decision] = store.query_decisions()
    assert decision["journal_record_id"] == trade["journal_record_id"]
    assert decision["decision_type"] == "TRADE_OPENED"
