"""What the forward-paper engine does and does not model, pinned.

The point is not that every market mechanic is simulated -- several are
deliberately not -- but that what IS claimed holds, and that the gaps stay
visible instead of quietly becoming assumptions.
"""
from datetime import datetime, timedelta, timezone

import pytest

from data.ledger import SqliteLedger
from execution.paper_engine import ForwardPaperExecutionEngine, PaperExecutionEngine
from services.fill_model import RealisticFill, from_name
from services.trading_instances import InstanceLedger


def _now():
    return datetime.now(timezone.utc)


def _engine(balance=10_000.0, model="RealisticFill"):
    ledger = SqliteLedger(":memory:")
    return ForwardPaperExecutionEngine(InstanceLedger(ledger, "inst-1"), balance,
                                       fill_model=from_name(model))


def _quote(price, sequence=1, at=None):
    stamp = (at or _now()).isoformat()
    return {"symbol": "BTCUSDT", "last": price, "bid": price * 0.9999,
            "ask": price * 1.0001, "mark": price, "sequence": sequence,
            "received_at": stamp, "event_timestamp": stamp,
            "quote_event_id": f"q{sequence}",
            "candle_id": f"BINANCE_USDM:BTCUSDT:5m:{sequence}"}


def test_an_entry_is_an_intent_until_a_later_quote_arrives():
    """No fill may use a price from the decision's own moment."""
    engine = _engine()
    decided_at = _now()

    result = engine.open(symbol="BTCUSDT", side="BUY", size=0.01, entry=100.0,
                         stop=95.0, alert_id="a1",
                         sizing_context={"decision_timestamp": decided_at.isoformat()})

    assert result.action == "intent"
    assert engine.positions() == []
    # A quote from before the decision cannot fill it.
    assert engine.process_quote(_quote(101.0, 1, decided_at - timedelta(seconds=1))) == []
    assert engine.positions() == []

    fills = engine.process_quote(_quote(101.0, 2, decided_at + timedelta(seconds=1)))
    assert [fill.action for fill in fills] == ["opened"]
    assert len(engine.positions()) == 1


def test_a_quote_missing_bid_ask_or_mark_is_refused():
    engine = _engine()
    for missing in ("bid", "ask", "mark"):
        quote = _quote(100.0)
        quote.pop(missing)
        with pytest.raises(ValueError, match="requires bid, ask and mark"):
            engine.process_quote(quote)
    crossed = _quote(100.0)
    crossed["ask"] = crossed["bid"] - 1
    with pytest.raises(ValueError, match="invalid"):
        engine.process_quote(crossed)


def test_the_fill_model_charges_the_spread_and_the_right_side_of_it():
    model = RealisticFill()
    buy = model.apply("buy", 100.0, 1.0, execution_id="x")
    sell = model.apply("sell", 100.0, 1.0, execution_id="x")

    assert buy["price"] > 100.0 > sell["price"]
    # A resting limit is a maker fill: it executes AT the limit and pays less.
    assert model.apply("buy", 100.0, 1.0, maker=True, execution_id="x")["price"] == 100.0
    assert model.fee_pct(maker=True) < model.fee_pct()


def test_a_rejecting_fill_model_opens_nothing():
    ledger = SqliteLedger(":memory:")
    engine = PaperExecutionEngine(InstanceLedger(ledger, "inst-1"), 10_000,
                                  fill_model=RealisticFill(reject_prob=1.0))

    result = engine.open(symbol="BTCUSDT", side="BUY", size=0.01, entry=100.0, stop=95.0,
                         alert_id="reject-me")

    assert result.action == "rejected"
    assert engine.positions() == []


def test_an_order_larger_than_the_account_is_rejected():
    """A simulated account may not spend money it does not have.

    The sizing pipeline normally keeps an entry well inside the balance, but
    the execution engine had no check of its own -- so a fixed-quantity
    configuration, a recovered intent, or a direct call could park an order
    worth more than the account and drive available capital arbitrarily
    negative. This is a fail-closed guard, not a change to how anything sizes.
    """
    engine = _engine(balance=10.0)

    result = engine.open(symbol="BTCUSDT", side="BUY", size=100.0, entry=60_000.0,
                         stop=59_000.0, alert_id="too-big",
                         sizing_context={"decision_timestamp": _now().isoformat()})
    engine.process_quote(_quote(60_000.0, 1))

    assert result.action == "rejected"
    assert engine.positions() == []
    assert engine.available_balance() == pytest.approx(10.0)


def test_fees_are_deducted_from_a_closed_trade():
    ledger = SqliteLedger(":memory:")
    scoped = InstanceLedger(ledger, "inst-1")
    engine = PaperExecutionEngine(scoped, 10_000, fill_model=from_name("RealisticFill"))
    engine.open(symbol="BTCUSDT", side="BUY", size=0.5, entry=100.0, stop=95.0,
                alert_id="fee-1")

    engine.close(symbol="BTCUSDT", exit_price=110.0)

    assert engine.fees_paid() > 0
    # Net is what the account actually keeps; gross is before commission.
    assert engine.realized_pnl() == pytest.approx(
        engine.gross_realized_pnl() - engine.fees_paid(), abs=1e-6)


def test_venue_rules_come_from_the_exchange_not_from_a_constant():
    """tickSize/stepSize/minQty/minNotional must be provider-declared."""
    import inspect

    from data.market_data_v2 import MarketDataService

    source = inspect.getsource(MarketDataService.usdm_contract_rules)
    assert "exchangeInfo" in source
    for binance_filter in ("PRICE_FILTER", "LOT_SIZE", "MIN_NOTIONAL"):
        assert binance_filter in source
    # Only actively trading USDT perpetuals are eligible.
    assert 'contract.get("status") != "TRADING"' in source
    # Nothing numeric is baked in for precision.
    assert "tickSize" in source and "stepSize" in source


def test_the_pipeline_refuses_an_entry_that_breaks_venue_rules():
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline

    ledger = SqliteLedger(":memory:")
    scoped = InstanceLedger(ledger, "inst-1")
    pipeline = SignalPipeline(scoped, PaperExecutionEngine(scoped, 10_000),
                              TradingControl(), equity=10_000)
    pipeline.symbol_rules_provider = lambda _symbol: {
        "symbol": "BTCUSDT", "tick_size": 0.1, "step_size": 0.001,
        "min_qty": 1.0,          # far above anything this equity can size
        "min_notional": 5.0}

    result = pipeline.process({"alert_id": "venue-1", "symbol": "BTCUSDT",
                               "side": "BUY", "entry": 60_000.0, "stop": 59_000.0})

    assert result.accepted is False


def test_leverage_margin_and_liquidation_are_not_claimed():
    """The gaps must stay visible rather than become silent assumptions.

    Trading Instances run on USD-M perpetuals, but the paper engine models an
    unleveraged cash account: no margin, no liquidation, no funding accrual.
    Status reports leverage as None for exactly this reason, and this test
    fails if anything starts implying otherwise without modelling it.
    """
    import inspect

    source = inspect.getsource(ForwardPaperExecutionEngine)
    source += inspect.getsource(PaperExecutionEngine)
    lowered = source.lower()
    assert "liquidation" not in lowered
    assert "maintenance_margin" not in lowered
    # And the instance status contract says so out loud.
    instances = inspect.getsource(
        __import__("services.trading_instances", fromlist=["x"]))
    assert "leverage is not modelled by the paper engine" in instances
