from copy import deepcopy
from datetime import datetime, timedelta, timezone

from bot.types import Bar
from services.price_action_lab import (
    PaperExecutionConfig,
    PriceActionLabRuntime,
    PriceActionPaperAccount,
)
from services.lab_lifecycle import blockers as lifecycle_blockers
from services.smc_strategy_lab import SMCPaperAccount, SMCPaperConfig, SMCStrategyLabRuntime
from services.smc_strategy_v1 import evaluate
from tests.test_smc_strategy_ladder import seeded_engine


RULES = {
    "tick_size": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
    "max_quantity": 1000.0, "min_notional": 5.0,
}
NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def pa_state(*, proposal=False):
    setups = [{
        "id": "setup-audit", "strategy_id": "PA1_SR_REJECTION",
        "direction": "bullish", "phase": "ORDER_PENDING", "zone_id": "zone-audit",
    }] if proposal else []
    proposals = [{
        "id": "proposal-audit", "setup_id": "setup-audit",
        "strategy_id": "PA1_SR_REJECTION", "direction": "bullish",
        "entry": 100.0, "stop": 95.0, "target": 112.5, "valid_until_index": 100,
        "signal_at": NOW.isoformat(),
    }] if proposal else []
    return {
        "research_id": "PRICE_ACTION_NATIVE_V1_RESEARCH", "strategy_version": "1.1.0",
        "symbol": "BTCUSDT", "timeframe": "5m", "setups": setups,
        "proposals": proposals, "snapshot": {"strategy_traces": [{
            "strategy_id": "PA1_SR_REJECTION", "missing_conditions": ([] if proposal else ["rejection"]),
            "next_required_event": "Await rejection",
        }]}, "metrics": {},
    }


def test_price_action_persists_one_decision_per_closed_candle_and_correlates_fill(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "pa-audit.db")
    account.configure(execution_config=PaperExecutionConfig(
        operating_mode="automatic", strategy_id="PA1_SR_REJECTION"))
    candle = Bar(NOW, 99, 101, 98, 100, 1000)
    first = account.synchronize_strategy(
        pa_state(proposal=True), contract_rules=RULES, candle=candle,
        feed_reliable=True, feed_status={"state": "SYNCHRONIZED", "reliable": True})
    second = account.synchronize_strategy(
        pa_state(proposal=True), contract_rules=RULES, candle=candle,
        feed_reliable=True, feed_status={"state": "SYNCHRONIZED", "reliable": True})
    state = account.state()
    assert len(state["evaluations"]) == 1
    assert len(state["orders"]) == 1
    correlation = state["evaluations"][0]["correlation_id"]
    assert state["order_metadata"][0]["config"]["correlation_id"] == correlation
    assert first["created"] and not second["created"]

    fill_bar = Bar(NOW + timedelta(minutes=5), 100, 101, 99, 100, 1000)
    account.synchronize_strategy(
        pa_state(proposal=True), contract_rules=RULES, candle=fill_bar,
        feed_reliable=True, feed_status={"state": "SYNCHRONIZED", "reliable": True,
                                        "last_quote_update": NOW.isoformat(),
                                        "last_mark_update": NOW.isoformat()},
        execution_quote={"bid": 99.9, "ask": 100.1, "mark": 100})
    protected = account.state()["positions"][0]
    assert protected["protection_status"] == "PROTECTED"
    assert protected["stop_loss"] is not None and protected["take_profit"] is not None
    assert protected["planned_rr"] == 2.5 and protected["effective_rr"] >= 2.5
    status = PriceActionLabRuntime(OfflineMarket(), account, autostart=False)
    try:
        assert status.bot_status()["latest_signal"]["correlation_id"] == correlation
    finally:
        status.stop()


def test_price_action_records_no_trade_and_stale_feed_blocks_execution(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "pa-watch.db")
    account.configure(execution_config=PaperExecutionConfig(
        operating_mode="automatic", strategy_id="PA1_SR_REJECTION"))
    account.synchronize_strategy(
        pa_state(), contract_rules=RULES, candle=Bar(NOW, 100, 101, 99, 100, 1000),
        feed_reliable=False, feed_status={"state": "STALE_CANDLES", "reliable": False,
                                         "health_reason": "completed candle is stale"})
    decision = account.state()["evaluations"][0]
    assert decision["state"] == "WATCHING"
    assert decision["missing_conditions"] == ["rejection"]
    assert account.broker.orders() == []


def test_smc_persists_no_trade_decisions_and_deduplicates_closed_candle(tmp_path):
    account = SMCPaperAccount(tmp_path / "smc-watch.db")
    watching = deepcopy(evaluate(seeded_engine()))
    watching.update({"state": "WATCHING", "proposal": None, "trade_plan": None,
                     "proposal_id": None, "missing_conditions": ["Bullish rejection"],
                     "next_required_event": "Await bullish rejection"})
    watching["data_identity"]["selected_candle"] = NOW.isoformat()
    first = account.synchronize_candidate(
        watching, rules=RULES, reference_price=100, feed_reliable=True)
    second = account.synchronize_candidate(
        watching, rules=RULES, reference_price=100, feed_reliable=True)
    assert first["decision_recorded"] is True and second["decision_recorded"] is True
    assert len(account.state()["evaluations"]) == 1
    assert account.state()["evaluations"][0]["missing_conditions"] == ["Bullish rejection"]
    assert account.broker.orders() == []


def test_smc_automatic_order_has_one_correlation_and_protected_fill(tmp_path):
    account = SMCPaperAccount(tmp_path / "smc-auto.db")
    account.configure(config=SMCPaperConfig(operating_mode="automatic"))
    decision = evaluate(seeded_engine())
    candle_time = decision["proposal"]["signal_timestamp"]
    result = account.synchronize_candidate(
        decision, rules=RULES, reference_price=decision["trade_plan"]["entry"],
        feed_reliable=True, closed_candle_time=candle_time,
        feed_status={"state": "SYNCHRONIZED", "reliable": True})
    duplicate = account.synchronize_candidate(
        decision, rules=RULES, reference_price=decision["trade_plan"]["entry"],
        feed_reliable=True, closed_candle_time=candle_time)
    state = account.state()
    assert len(state["evaluations"]) == 1 and len(account.broker.orders()) == 1
    assert duplicate["duplicate"] is True
    correlation = result["correlation_id"]
    assert state["order_metadata"][0]["config"]["correlation_id"] == correlation

    entry = float(decision["trade_plan"]["entry"])
    fill_time = decision["proposal"]["signal_timestamp"] + timedelta(minutes=5)
    account.process_candle("BTCUSDT", Bar(fill_time, entry, entry + 1, entry - 1, entry, 10_000))
    position = account.state()["positions"][0]
    assert position["protection_status"] == "PROTECTED"
    assert position["stop_loss"] is not None and position["take_profit"] is not None
    assert position["effective_rr"] >= position["planned_rr"]

    target = float(position["take_profit"])
    exit_bar = Bar(NOW + timedelta(minutes=5), entry, target + 1, entry, target, 10_000)
    account.process_candle("BTCUSDT", exit_bar)
    completed = account.state()
    assert completed["positions"] == []
    assert completed["evaluations"][0]["state"] == "EXITED"
    assert completed["order_metadata"][-1]["status"] == "COMPLETED"
    journal = account.journal()["journal"]
    assert journal[0]["status"] == "COMPLETED"
    assert any(abs(float(fill["realized_pnl"])) > 0 for fill in journal[0]["fills"])
    runtime = SMCStrategyLabRuntime(OfflineMarket(), account, autostart=False)
    try:
        performance = runtime.bot_status()["performance"]["live_paper"]
        assert performance["closed_trades"] >= 1
        assert performance["fees"] > 0
        assert performance["net_pnl"] == completed["account"]["realized_pnl"] - \
            completed["account"]["fees_paid"] - completed["account"]["funding_paid"]
    finally:
        runtime.stop()


class OfflineMarket:
    def public_usdm_window(self, *_args, **_kwargs):
        raise RuntimeError("offline fixture")


def test_dashboard_status_is_scope_accurate_and_fails_closed(tmp_path):
    pa_account = PriceActionPaperAccount(tmp_path / "pa-status.db")
    pa = PriceActionLabRuntime(OfflineMarket(), pa_account, autostart=False)
    smc_account = SMCPaperAccount(tmp_path / "smc-status.db")
    smc = SMCStrategyLabRuntime(OfflineMarket(), smc_account, autostart=False)
    try:
        pa_status = pa.bot_status()
        smc_status = smc.bot_status()
        assert pa_status["account_scope"] == "PRICE_ACTION_VISUAL_LAB_ONLY"
        assert smc_status["account_scope"] == "SMC_STRATEGY_LAB_ONLY"
        assert pa_status["session_state"] == smc_status["session_state"] == "BLOCKED"
        assert pa_status["execution_armed"] is False
        # Both labs default to signals_only. An offline session is BLOCKED and
        # must never claim execution authority merely because it exists.
        assert smc_status["execution_armed"] is False
        assert pa_status["execution_state"] == smc_status["execution_state"] == "BLOCKED"
        assert pa_status["feed"]["state"] == smc_status["feed"]["state"] == "DISCONNECTED"
        assert pa_status["performance"].keys() == {"backtest", "forward_validation", "live_paper"}
        assert smc_status["performance"]["backtest"]["available"] is False

        pa_account.configure(execution_config=PaperExecutionConfig(operating_mode="signals_only"))
        smc_account.configure(config=SMCPaperConfig(operating_mode="signals_only"))
        assert pa.bot_status()["execution_state"] == "BLOCKED"
        assert smc.bot_status()["execution_state"] == "BLOCKED"
    finally:
        pa.stop()
        smc.stop()


def test_existing_exposure_is_an_explicit_new_entry_blocker():
    reasons = lifecycle_blockers(
        connection={"reliable": True}, operating_mode="automatic",
        account={"balance": 10_000, "free_margin": 9_000}, strategy_valid=True,
        positions=[{"symbol": "BTCUSDT", "protection_status": "PROTECTED"}],
        pending_orders=[{"id": "entry-1", "reduce_only": False}],
        risk_pct=.5, max_risk_pct=1.0,
    )

    assert any("protected paper position" in reason for reason in reasons)
    assert any("pending paper entry" in reason for reason in reasons)
