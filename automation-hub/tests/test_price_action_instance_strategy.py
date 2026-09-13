"""The Price Action lab's engine, run as a Trading Instance strategy.

The lab and the instances were running different alpha: same Binance hub, same
paper broker, but the lab decided with NativePriceActionEngine while an
instance could only pick from the indicator catalog. There was no way to run
the lab's setup autonomously or stand it next to SMC on equal terms.

What these tests protect is the two things an adapter like this gets wrong:
inventing its own levels instead of carrying the engine's, and letting warm-up
history become a live order.
"""
from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar, SignalType
from services.native_price_action import (
    STRATEGIES, STRATEGY_VERSION, NativePriceActionEngine, PriceActionConfig,
)
from services.strategy_factory import make_builtin_strategy
from strategies.price_action_rejection import (
    DECISION_TIMEFRAME, PriceActionFlipRetestStrategy, PriceActionRejectionStrategy,
)

UTC = timezone.utc
TF = timedelta(minutes=5)
BASE = datetime(2026, 9, 1, tzinfo=UTC)


def _bar(index: int, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(BASE + TF * index, open_, high, low, close, 100.0)


def _swinging_history(count: int = 420) -> list[Bar]:
    """A repeating swing so the engine confirms swings and builds zones.

    Deterministic and reversible on purpose: what the tests assert is that the
    adapter relays whatever the engine concludes, never that these particular
    candles produce a trade.
    """
    bars: list[Bar] = []
    for index in range(count):
        phase = index % 20
        drift = 100.0 + (index // 20) * 0.05
        if phase < 10:
            base = drift + phase * 0.8
            bars.append(_bar(index, base, base + 0.6, base - 0.3, base + 0.4))
        else:
            base = drift + (19 - phase) * 0.8
            bars.append(_bar(index, base, base + 0.3, base - 0.6, base - 0.4))
    return bars


def _htf(entry: list[Bar], minutes: int) -> list[Bar]:
    step = minutes // 5
    rows = []
    for start in range(0, len(entry) - step + 1, step):
        bucket = entry[start:start + step]
        rows.append(Bar(bucket[0].timestamp, bucket[0].open,
                        max(row.high for row in bucket),
                        min(row.low for row in bucket),
                        bucket[-1].close, sum(row.volume for row in bucket)))
    return rows


def _feed(strategy, bars, *, decide_from):
    """Drive the strategy the way AutoStrategyEngine does, one candle at a time.

    set_timeframe_context is called immediately before on_bar for every bar,
    with series already trimmed at that decision boundary.
    """
    signals = []
    for index, bar in enumerate(bars):
        if index < decide_from:
            continue
        history = bars[:index + 1]
        strategy.set_timeframe_context({
            DECISION_TIMEFRAME: bars[:index],          # causal: excludes this bar
            "1h": _htf(history, 60), "4h": _htf(history, 240),
        })
        signal = strategy.on_bar(bar)
        if signal is not None:
            signals.append((bar, signal))
    return signals


# ------------------------------------------------------------------ wiring

@pytest.mark.parametrize("key,expected", [
    ("price_action_rejection", "PA1_SR_REJECTION"),
    ("price_action_flip_retest", "PA3_FLIP_RETEST"),
])
def test_the_factory_builds_each_price_action_setup(key, expected):
    strategy = make_builtin_strategy(key, "BTCUSDT")
    assert strategy.pa_strategy_id == expected
    assert strategy.decision_timeframe == "5m"
    assert strategy.strategy_version == STRATEGY_VERSION


def test_both_setups_exist_in_the_engine():
    """A typo in pa_strategy_id would otherwise be a strategy that never fires."""
    for cls in (PriceActionRejectionStrategy, PriceActionFlipRetestStrategy):
        assert cls.pa_strategy_id in STRATEGIES


def test_the_instance_catalog_offers_them_on_5m_only():
    import webhook_api

    catalog = {row["key"]: row for row in webhook_api._STRATEGY_CATALOG}
    for key in ("price_action_rejection", "price_action_flip_retest"):
        assert key in catalog, "not offered on the instance creation form"
        assert catalog[key]["supported_timeframes"] == ["5m"]
        assert catalog[key]["version"] == STRATEGY_VERSION


def test_an_unsupported_setup_id_fails_closed():
    class Bogus(PriceActionRejectionStrategy):
        pa_strategy_id = "PA9_DOES_NOT_EXIST"

    with pytest.raises(ValueError, match="unknown Price Action setup"):
        Bogus("BTCUSDT")


def test_the_secondary_timeframe_is_not_promoted_to_a_gate():
    """4h is bias in the MTF policy. Naming it in required_timeframes would
    make _refresh_multi_timeframe_context treat it as mandatory and let a
    failed 4h load block entries, which the lab never does."""
    assert "4h" not in PriceActionRejectionStrategy.required_timeframes
    assert PriceActionRejectionStrategy.required_timeframes == ("5m", "1h")


# ------------------------------------------------------- alpha is the engine's

def test_every_emitted_level_is_a_number_the_engine_computed():
    """The point of the adapter: relay the engine's levels, never re-derive.

    HubStrategy._signal would bracket with ATR, which throws away the
    rejection-extreme stop and the zone-derived target the engine produced.
    """
    bars = _swinging_history()
    strategy = make_builtin_strategy("price_action_rejection", "BTCUSDT")
    signals = _feed(strategy, bars, decide_from=380)

    engine = strategy._engine
    assert engine is not None, "the adapter never built an engine"
    # Without this the loop below passes on an empty list, which would hide the
    # adapter silently never firing.
    assert signals, "the fixture produced no signal, so nothing was checked"
    by_level = {(row.entry, row.stop, row.target)
                for row in engine.proposals.values()
                if row.strategy_id == "PA1_SR_REJECTION"}
    for bar, signal in signals:
        assert (signal.entry, signal.stop_loss, signal.take_profit) in by_level, (
            "emitted levels the engine never proposed: %r" % (signal,))
        assert signal.timestamp == bar.timestamp
        assert signal.symbol == "BTCUSDT"
        assert signal.type in (SignalType.LONG, SignalType.SHORT)


def test_warmup_history_can_never_become_an_order():
    """The lab's rule, carried over: replayed bootstrap history is not a signal.

    Every proposal the engine made while catching up to the first decision bar
    is recorded as history, and none of it may be emitted afterwards.
    """
    bars = _swinging_history()
    strategy = make_builtin_strategy("price_action_rejection", "BTCUSDT")
    signals = _feed(strategy, bars, decide_from=400)

    assert strategy._history_proposals, "nothing was ingested as warm-up at all"
    emitted = {signal.reason for _bar, signal in signals}
    for proposal_id in strategy._history_proposals:
        proposal = strategy._engine.proposals[proposal_id]
        assert proposal_id not in strategy._emitted
        # And no emitted signal can be that historical proposal's trade.
        assert not any(str(proposal.entry) in reason and str(proposal.stop) in reason
                       for reason in emitted)


def test_a_proposal_is_emitted_at_most_once():
    bars = _swinging_history()
    strategy = make_builtin_strategy("price_action_rejection", "BTCUSDT")
    _feed(strategy, bars, decide_from=380)
    assert strategy._emitted, "nothing was emitted, so uniqueness proves nothing"
    assert len(strategy._emitted) == len(set(strategy._emitted))


def test_re_feeding_the_same_candle_does_not_signal_twice():
    """A duplicate delivery must not open a second position."""
    bars = _swinging_history()
    strategy = make_builtin_strategy("price_action_rejection", "BTCUSDT")
    _feed(strategy, bars, decide_from=400)
    last = bars[-1]
    strategy.set_timeframe_context({
        DECISION_TIMEFRAME: bars[:-1],
        "1h": _htf(bars, 60), "4h": _htf(bars, 240),
    })
    assert strategy.on_bar(last) is None
    assert "already evaluated" in strategy.last_reason


def test_each_setup_only_claims_its_own_proposals():
    """Two instances on the same market must not both take the same trade."""
    bars = _swinging_history()
    rejection = make_builtin_strategy("price_action_rejection", "BTCUSDT")
    retest = make_builtin_strategy("price_action_flip_retest", "BTCUSDT")
    for strategy, wanted in ((rejection, "PA1_SR_REJECTION"),
                             (retest, "PA3_FLIP_RETEST")):
        _feed(strategy, bars, decide_from=380)
        for proposal_id in strategy._emitted:
            assert strategy._engine.proposals[proposal_id].strategy_id == wanted
    # This fixture happens to produce rejections and no flip retests, so only
    # the rejection half is load-bearing. Stated so a future fixture change
    # that stops producing either is visible rather than quietly vacuous.
    assert rejection._emitted, "the rejection setup emitted nothing to check"
    assert not retest._emitted, "fixture now produces flip retests; widen this test"


def test_generate_without_a_snapshot_says_so_instead_of_trading():
    strategy = make_builtin_strategy("price_action_rejection", "BTCUSDT")
    assert strategy.generate(_bar(0, 100, 101, 99, 100)) is None
    assert "no closed-candle snapshot" in strategy.last_reason


def test_the_adapter_runs_the_engine_with_its_frozen_defaults():
    """No env overrides set means exactly the alpha the frozen module describes."""
    strategy = make_builtin_strategy("price_action_rejection", "BTCUSDT")
    assert strategy.config == PriceActionConfig(symbol="BTCUSDT", timeframe="5m")
    assert strategy.config.execution_allowed is False


def test_env_overrides_reach_the_engine_config(monkeypatch):
    monkeypatch.setenv("HUB_PA_RR_RATIO", "3.5")
    monkeypatch.setenv("HUB_PA_TRIGGER_FILTER", "pin_bar_only")
    monkeypatch.setenv("HUB_PA_FIRST_TOUCH_ONLY", "true")
    monkeypatch.setenv("HUB_PA_CONFUSION_CANDLES", "2")
    strategy = make_builtin_strategy("price_action_rejection", "BTCUSDT")
    assert strategy.config.rr_ratio == 3.5
    assert strategy.config.trigger_filter == "pin_bar_only"
    assert strategy.config.first_touch_only is True
    assert strategy.config.confusion_candles == 2
    # The engine accepts the same object, so an override cannot desync them.
    assert NativePriceActionEngine(strategy.config).config is strategy.config


def test_execution_allowed_cannot_be_overridden_from_env(monkeypatch):
    """Live execution stays hard-off; env must not be a way around it."""
    monkeypatch.setenv("HUB_PA_EXECUTION_ALLOWED", "true")
    strategy = make_builtin_strategy("price_action_rejection", "BTCUSDT")
    assert strategy.config.execution_allowed is False


# --------------------------------------------------- inside the real engine

def test_it_warms_up_and_runs_inside_the_forward_engine():
    """The interface claims only hold if AutoStrategyEngine can drive it.

    required_timeframes, decision_timeframe and warmup_required all feed the
    forward loop's warm-up, MTF context and continuity checks. A mis-declared
    one does not show up in a unit test of the adapter; it shows up as an
    instance that will not start.
    """
    import time

    from data.ledger import SqliteLedger
    from execution.paper_engine import PaperExecutionEngine
    from services.auto_engine import AutoStrategyEngine
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline

    # Anchored to wall clock: the engine decides what is closed, and too stale,
    # against the real clock and this test cannot move it.
    newest = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=310)
    history = _swinging_history(600)
    shifted = [Bar(newest - TF * (len(history) - 1 - index), row.open, row.high,
                   row.low, row.close, row.volume)
               for index, row in enumerate(history)]

    def fetcher(_symbol, timeframe, _limit, **_kwargs):
        if timeframe == "5m":
            return list(shifted), "live (test hub)"
        return _htf(shifted, 60 if timeframe == "1h" else 240), "live (test hub)"

    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, 10_000)
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=10_000,
                              risk_per_trade_pct=0.01, exposure_limit_pct=0.5)
    engine = AutoStrategyEngine(
        pipeline, paper, ledger, symbols=["BTCUSDT"], timeframe="5m", live=True,
        strategy_factory=lambda symbol: make_builtin_strategy(
            "price_action_rejection", symbol),
        fetcher=fetcher, live_poll_s=600, entry_mode="market")

    assert engine.start() is True
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if engine.status().get("lifecycle_state") == "running":
                break
            time.sleep(0.05)
        status = engine.status()
        assert status["lifecycle_state"] == "running", (
            "engine never reached running: %s / %s"
            % (status.get("lifecycle_state"), status.get("last_error")))
        # Warm-up loaded the entry stream and traded none of it.
        assert status["warmup_bars"] >= 400
        assert paper.positions() == [], "a warm-up candle opened a position"
        assert str(engine.last_source or "").startswith("live")
    finally:
        engine.stop()
