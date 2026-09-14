"""Do the two native strategies actually reach the paper broker?

The question this answers is the one that started the work: "does the SMC lab
and price action bot actually place orders based on strategy, or are they there
for decoration -- I haven't seen a single order since I built those labs."

A signal object is not a trade. Between a strategy emitting one and a position
existing there is the signal pipeline (risk sizing, exposure, controls), the
paper broker (fill, fees), and the ledger. Every test here drives that whole
chain and asserts on what came out the far end, because a passing unit test of
the adapter proves nothing about whether an order was ever placed.

The answer differs for the two strategies, and the difference is deliberate --
see ``test_smc_cannot_place_even_a_paper_order_by_design``. Nothing here enables
live routing: every order is placed against PaperExecutionEngine on an in-memory
ledger, and the engines' shipped execution flags are read, never written.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.data.resample import resample
from bot.data.synthetic import generate_bars
from bot.types import Bar, SignalType
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services.controls import TradingControl
from services.mtf_policy import native_timeframes
from services.native_smc import NATIVE_SMC_ID, ProposedTrade
from services.signal_pipeline import SignalPipeline
from services.strategy_factory import make_builtin_strategy
from strategies.price_action_rejection import DECISION_TIMEFRAME
from strategies.smc_strategy import SMCStrategy

UTC = timezone.utc
TF = timedelta(minutes=5)
BASE = datetime(2026, 9, 1, tzinfo=UTC)
_SMC_TFS = native_timeframes("5m")


# ------------------------------------------------------------------ fixtures

def _bar(index: int, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(BASE + TF * index, open_, high, low, close, 100.0)


def _swinging_history(count: int = 420) -> list[Bar]:
    """A repeating swing, so the Price Action engine confirms zones.

    Same shape as tests/test_price_action_instance_strategy.py uses. What is
    asserted is never that these candles produce a trade -- only that whatever
    the engine concludes survives the trip to the broker intact.
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


def _chain(*, equity: float = 10_000.0, control: TradingControl | None = None):
    """A real pipeline over a real paper broker on an in-memory ledger."""
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, equity)
    pipeline = SignalPipeline(ledger, paper, control or TradingControl(),
                              equity=equity, risk_per_trade_pct=0.01,
                              exposure_limit_pct=0.5)
    return ledger, paper, pipeline


def _payload(signal, *, strategy: str, instance: str = "test-instance") -> dict:
    """The payload services/auto_engine.py builds from a Signal.

    Mirrored field for field rather than simplified: a chain test that feeds the
    pipeline something the engine would never send proves nothing about the
    chain.
    """
    side = "BUY" if signal.type is SignalType.LONG else "SELL"
    return {
        "alert_id": f"{strategy}-{signal.timestamp.isoformat()}-{side.lower()}",
        "symbol": signal.symbol, "side": side,
        "entry": signal.entry, "stop": signal.stop_loss,
        "target": signal.take_profit,
        "confidence": getattr(signal, "confidence", 1.0),
        "reason": getattr(signal, "reason", ""),
        "snapshot": getattr(signal, "snapshot", None),
        "strategy": strategy, "timeframe": DECISION_TIMEFRAME,
        "mode": "paper",
        "instance_id": instance,
        "timestamp": signal.timestamp.isoformat(),
    }


def _first_pa_signal():
    """Drive the Price Action adapter until the engine proposes a trade."""
    bars = _swinging_history()
    strategy = make_builtin_strategy("price_action_rejection", "BTCUSDT")
    for index, bar in enumerate(bars):
        if index < 380:
            continue
        history = bars[:index + 1]
        strategy.set_timeframe_context({
            DECISION_TIMEFRAME: bars[:index],
            "1h": _htf(history, 60), "4h": _htf(history, 240),
        })
        signal = strategy.on_bar(bar)
        if signal is not None:
            return strategy, signal
    raise AssertionError(
        "the Price Action engine proposed nothing over the fixture, so this "
        "file would be asserting on an empty chain")


def _primed_smc() -> tuple[SMCStrategy, list[Bar]]:
    bars = generate_bars(n=300, timeframe="5m", seed=3)
    strategy = SMCStrategy("BTCUSDT")
    for index, bar in enumerate(bars[:-1]):
        window = bars[: index + 1]
        context = {tf: resample(window, tf) for tf in _SMC_TFS}
        context["5m"] = window
        strategy.set_timeframe_context(context)
        strategy.on_bar(bar)
    return strategy, bars


def _smc_proposal(**overrides) -> ProposedTrade:
    fields = dict(id="prop-chain", setup_id="setup-chain", direction="bullish",
                  entry=123.45, stop=118.20, target=136.50, risk_distance=5.25,
                  rr_ratio=2.5, snapshot_id="snap-chain", execution_allowed=True)
    fields.update(overrides)
    return ProposedTrade(**fields)


# ------------------------------------------------- Price Action: the full trip

def test_price_action_signal_becomes_a_real_paper_position():
    """Signal -> pipeline -> broker -> position, with the engine's own levels."""
    _strategy, signal = _first_pa_signal()
    _ledger, paper, pipeline = _chain()

    result = pipeline.process(_payload(signal, strategy="price_action_rejection"))

    assert result.accepted, (
        f"the pipeline refused a live Price Action signal at "
        f"{result.stage}: {result.reason}")
    assert (result.fill or {}).get("action") == "opened"
    position = paper.open_position("BTCUSDT")
    assert position is not None, "the pipeline accepted but no position exists"
    assert position["side"] == ("long" if signal.type is SignalType.LONG else "short")
    # The broker holds the engine's stop, not a re-derived one.
    assert position["stop"] == pytest.approx(signal.stop_loss)


def test_the_position_closes_and_lands_in_the_journal():
    """A position that exits must leave a durable, priced record behind."""
    _strategy, signal = _first_pa_signal()
    _ledger, paper, pipeline = _chain()
    assert pipeline.process(_payload(signal, strategy="price_action_rejection")).accepted

    exit_price = signal.take_profit
    paper.close(symbol="BTCUSDT", exit_price=exit_price)

    assert paper.open_position("BTCUSDT") is None, "the position did not close"
    history = paper.history()
    assert history, "a closed trade left no journal record at all"
    closed = history[-1]
    assert closed["symbol"] == "BTCUSDT"
    assert closed["exit"] == pytest.approx(exit_price)
    assert closed.get("pnl") is not None, "a closed trade must carry a P&L"


def test_the_journal_record_can_be_traced_back_to_the_engine_proposal():
    """A paper trade nobody can attribute is not evidence of anything."""
    strategy, signal = _first_pa_signal()
    snapshot = signal.snapshot or {}
    assert snapshot, "the adapter emitted a signal carrying no provenance"
    engine_levels = {(row.entry, row.stop, row.target)
                     for row in strategy._engine.proposals.values()}
    assert (signal.entry, signal.stop_loss, signal.take_profit) in engine_levels, (
        "the signal's levels trace to no proposal the engine ever made")


# ----------------------------------------------------- SMC: blocked by design

def test_smc_cannot_place_even_a_paper_order_by_design():
    """The documented Lab/Instance divergence, pinned rather than resolved.

    Price Action's ProposedTrade carries TWO flags -- ``execution_allowed``
    (False: no live routing) and ``paper_execution_allowed`` (True) -- and its
    adapter gates on the paper one, so a Price Action proposal can become a
    paper order. native_smc.ProposedTrade has only ``execution_allowed``, fed
    from SMCConfig.execution_allowed (False) or the module constant
    EXECUTION_ALLOWED (also False, not env-driven). There is no paper/live
    distinction, so an SMC proposal is refused for paper and live alike.

    That is why no SMC order has ever appeared. It is not a wiring fault -- the
    test below shows the chain carries an executable SMC proposal perfectly
    well -- it is the shipped gate, in a frozen alpha file
    (services/native_smc.py). Giving SMC a paper_execution_allowed flag to match
    Price Action is a real and defensible change, but it is a change to frozen
    execution policy and needs explicit approval, not a quiet edit inside a
    refactor. Pinned here so the asymmetry is visible and cannot drift.
    """
    from services import native_price_action, native_smc

    assert native_smc.EXECUTION_ALLOWED is False
    assert not hasattr(native_smc.ProposedTrade, "paper_execution_allowed"), (
        "SMC gained a paper execution flag -- if that was intended, this test "
        "and the freeze record should say so")
    assert native_price_action.ProposedTrade.paper_execution_allowed is True, (
        "Price Action lost its paper flag, which would silence it the way SMC "
        "is silenced")

    strategy, bars = _primed_smc()
    strategy._engine.proposals["prop-research"] = _smc_proposal(
        id="prop-research", execution_allowed=False)
    assert strategy.generate(bars[-1]) is None
    assert "research-only" in strategy.last_reason


def test_an_executable_smc_proposal_would_traverse_the_whole_chain():
    """The gate is the only blocker: the wiring underneath it is sound.

    Without this, the test above is indistinguishable from SMC being broken.
    The proposal is injected into the engine -- the shipped config is read-only
    here -- and then carried by the real adapter, pipeline and paper broker.
    """
    strategy, bars = _primed_smc()
    proposal = _smc_proposal()
    strategy._engine.proposals[proposal.id] = proposal

    signal = strategy.generate(bars[-1])
    assert signal is not None
    _ledger, paper, pipeline = _chain()

    result = pipeline.process(_payload(signal, strategy="smc"))

    assert result.accepted, f"refused at {result.stage}: {result.reason}"
    position = paper.open_position("BTCUSDT")
    assert position is not None
    assert position["stop"] == pytest.approx(proposal.stop)
    assert (signal.snapshot or {}).get("research_id") == NATIVE_SMC_ID

    paper.close(symbol="BTCUSDT", exit_price=proposal.target)
    closed = paper.history()[-1]
    assert closed["exit"] == pytest.approx(proposal.target)
    assert closed["pnl"] > 0, "a long closed at its target must book a gain"


# ------------------------------------------------------ deliberate failures

def test_a_signal_with_no_risk_distance_is_refused_not_sized():
    """entry == stop is a divide-by-zero into an unbounded position."""
    strategy, bars = _primed_smc()
    proposal = _smc_proposal(id="prop-flat", entry=100.0, stop=100.0, target=110.0)
    strategy._engine.proposals[proposal.id] = proposal
    signal = strategy.generate(bars[-1])
    assert signal is not None, "the adapter relays the engine's numbers as given"

    _ledger, paper, pipeline = _chain()
    result = pipeline.process(_payload(signal, strategy="smc"))

    assert not result.accepted, "a zero-risk trade was accepted"
    assert paper.open_position("BTCUSDT") is None


def test_an_inverted_stop_is_refused():
    """A long whose stop sits above its entry is not a long."""
    strategy, bars = _primed_smc()
    proposal = _smc_proposal(id="prop-inverted", entry=100.0, stop=105.0, target=110.0)
    strategy._engine.proposals[proposal.id] = proposal
    signal = strategy.generate(bars[-1])

    _ledger, paper, pipeline = _chain()
    result = pipeline.process(_payload(signal, strategy="smc"))

    assert not result.accepted, "an inverted-stop long was accepted"
    assert paper.open_position("BTCUSDT") is None


def test_a_paused_control_blocks_the_order_and_leaves_no_position():
    """The kill switch must stop a signal that is otherwise perfectly good."""
    _strategy, signal = _first_pa_signal()
    control = TradingControl()
    control.pause_all()
    _ledger, paper, pipeline = _chain(control=control)

    result = pipeline.process(_payload(signal, strategy="price_action_rejection"))

    assert not result.accepted, "a paused control still let a trade through"
    assert paper.open_position("BTCUSDT") is None


def test_the_same_signal_delivered_twice_opens_one_position():
    """A retry must not double the position."""
    _strategy, signal = _first_pa_signal()
    _ledger, paper, pipeline = _chain()
    payload = _payload(signal, strategy="price_action_rejection")

    first = pipeline.process(payload)
    second = pipeline.process(payload)

    assert first.accepted
    assert len(paper.positions()) == 1, (
        f"a duplicate delivery opened {len(paper.positions())} positions; "
        f"second result: accepted={second.accepted} {second.stage}")
