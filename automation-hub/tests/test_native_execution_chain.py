"""Do the two native strategies actually reach the paper broker?

The question this answers is the one that started the work: "does the SMC lab
and price action bot actually place orders based on strategy, or are they there
for decoration -- I haven't seen a single order since I built those labs."

A signal object is not a trade. Between a strategy emitting one and a position
existing there is the signal pipeline (risk sizing, exposure, controls), the
paper broker (fill, fees), and the ledger. Every test here drives that whole
chain and asserts on what came out the far end, because a passing unit test of
the adapter proves nothing about whether an order was ever placed.

Both strategies now reach it. SMC did not until its engine gained the separate
paper-execution permission the Price Action engine already had -- an approved
non-alpha delta, recorded in data/native_smc_engine_freeze_manifest.json.

Nothing here enables live routing, and several tests exist to keep it that way:
every order is placed against PaperExecutionEngine on an in-memory ledger,
``execution_allowed`` is asserted False on both engines, and the engine
constructor is asserted to refuse a config that sets it.
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
    # execution_allowed stays False exactly as the engine ships it; paper
    # simulation is granted by paper_execution_allowed, which is what the
    # adapter gates on. A fixture that set the live flag would be testing a
    # state the engine's constructor refuses to run in.
    fields = dict(id="prop-chain", setup_id="setup-chain", direction="bullish",
                  entry=123.45, stop=118.20, target=136.50, risk_distance=5.25,
                  rr_ratio=2.5, snapshot_id="snap-chain")
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
    # Which level was retested, and what rejected off it -- not just that some
    # setup fired. The Price Action lab shows this beside the fill.
    assert snapshot["zone_id"], "the order does not name the S/R zone it traded"
    assert snapshot["trigger_event_id"], "the order does not name the rejection"
    assert snapshot["reasons"], "the order carries no stated reason"
    engine_levels = {(row.entry, row.stop, row.target)
                     for row in strategy._engine.proposals.values()}
    assert (signal.entry, signal.stop_loss, signal.take_profit) in engine_levels, (
        "the signal's levels trace to no proposal the engine ever made")


# ----------------------------------------------------- SMC: blocked by design

def test_both_engines_separate_paper_permission_from_live():
    """Paper and live are two permissions, and only one of them is granted.

    SMC used to carry a single ``execution_allowed`` flag with no paper/live
    distinction, so its proposals were refused for simulation exactly as they
    were for live routing -- which is why no SMC order had ever appeared. It
    now carries the same pair the native Price Action engine has, as an
    approved non-alpha delta recorded in the engine freeze manifest.

    What must not drift is the other half: paper permission is not a foothold
    for live execution. ``execution_allowed`` stays False on both engines, the
    module constants stay False, and both constructors refuse outright if a
    config ever sets it.
    """
    from services import native_price_action, native_smc

    assert native_smc.EXECUTION_ALLOWED is False
    assert native_price_action.EXECUTION_ALLOWED is False
    for model in (native_smc.ProposedTrade, native_price_action.ProposedTrade):
        assert model.paper_execution_allowed is True
        assert model.execution_allowed is False, (
            "live execution was enabled by default on a research engine")

    # The constructor guard is the real barrier, not the dataclass default.
    with pytest.raises(ValueError):
        native_smc.SMCMarketStructureEngine(
            native_smc.SMCConfig(symbol="BTCUSDT", execution_allowed=True))


def test_a_proposal_refused_for_paper_says_so_and_places_nothing():
    """The gate still exists; it is the paper flag that opens it, not nothing."""
    strategy, bars = _primed_smc()
    strategy._engine.proposals["prop-research"] = _smc_proposal(
        id="prop-research", paper_execution_allowed=False)

    assert strategy.generate(bars[-1]) is None
    assert "research-only" in strategy.last_reason


def test_an_smc_setup_now_reaches_the_paper_broker_end_to_end():
    """The change the owner asked for, asserted on the far end of the chain.

    Not a unit test of the flag: the proposal is carried by the real adapter,
    the real pipeline and the real paper broker, and the assertion is that a
    position exists holding the engine's own levels.
    """
    strategy, bars = _primed_smc()
    proposal = _smc_proposal(id="prop-live-paper")
    assert proposal.paper_execution_allowed is True
    assert proposal.execution_allowed is False, "this must NOT be a live order"
    strategy._engine.proposals[proposal.id] = proposal

    signal = strategy.generate(bars[-1])
    assert signal is not None, "an SMC setup must now produce a tradeable signal"

    _ledger, paper, pipeline = _chain()
    result = pipeline.process(_payload(signal, strategy="smc"))

    assert result.accepted, f"refused at {result.stage}: {result.reason}"
    position = paper.open_position("BTCUSDT")
    assert position is not None, "SMC still placed no paper order"
    assert position["entry"] == pytest.approx(proposal.entry)
    assert position["stop"] == pytest.approx(proposal.stop)


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
