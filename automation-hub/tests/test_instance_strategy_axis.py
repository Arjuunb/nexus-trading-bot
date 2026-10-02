"""The STRATEGY badge on a Trading Instance reads BLOCKED only when something
is holding entries back.

It used to read BLOCKED for every blocker code except NO_SETUP and WARMUP. The
strategies now say why a candle had no setup (NO_ELIGIBLE_ZONE,
NO_CONFIRMATION, EMA_TREND_NOT_ALIGNED, ...), so on ordinary candles the badge
flickered red while the strategy was simply waiting. It now follows the
per-code states the Instance Visual Lab already shows. A code no one has
classified still reads BLOCKED.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services import instance_status as status
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.signal_pipeline import SignalPipeline
from services.strategy_visual_registry import ADAPTERS, BLOCKER_EXPLANATIONS, blocker_state, gate_sequence
from strategies.three_candle_rejection import ThreeCandleRejectionStrategy
from tests.test_three_candle_rejection import _bar, _history, _long_pattern


def _axis(blocker, *, position_open: bool = False) -> str:
    return status.strategy_status(market=status.LIVE, worker_state="running", warmup_bars=400,
                                  warmup_required=400, blocker=blocker, htf_ready=True,
                                  position_open=position_open)[0]


def _drive(*, quality_gate_off: bool) -> tuple[list[tuple[str | None, str]], PaperExecutionEngine]:
    """The real engine, candle by candle, on the 3-candle rejection strategy:
    warm-up, a quiet range, a long setup at support, then a slow drift."""
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, 10_000)
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=10_000,
                              risk_per_trade_pct=0.01, exposure_limit_pct=0.5)
    engine = AutoStrategyEngine(pipeline, paper, ledger, symbols=["BTCUSDT"], timeframe="5m",
                                live=False, fetcher=lambda *_a, **_k: ([], "test"), entry_mode="market")
    if quality_gate_off:
        engine.quality_gate_bypass = lambda: True
    strategy = ThreeCandleRejectionStrategy("BTCUSDT")
    rows, i = _history()
    bars = rows + _long_pattern(i)
    price = bars[-1].close
    for k in range(1, 12):
        bars.append(_bar(i + 2 + k, price, price + 0.3, price - 0.3, price + 0.05))
        price += 0.05
    seen = []
    for bar in bars:
        engine._process_bar("BTCUSDT", bar, strategy)
        seen.append((engine.last_blocker, _axis(engine.last_blocker,
                                                position_open=bool(paper.positions()))))
    return seen, paper


def test_the_3_candle_strategy_never_reads_blocked_while_it_waits_or_trades():
    seen, paper = _drive(quality_gate_off=True)
    assert paper.positions(), "the setup should have opened a paper position"
    assert {"GATE_REJECTED: NO_ELIGIBLE_ZONE", "GATE_REJECTED: NO_SUPPORT_REJECTION"} <= {
        blocker for blocker, _ in seen}
    assert ("GATE_REJECTED: NO_ELIGIBLE_ZONE", status.WAITING_FOR_SETUP) in seen
    # warm-up, then waiting, then in the trade once it fills -- and nothing else
    assert {axis for _, axis in seen} == {status.WARMING_UP, status.WAITING_FOR_SETUP,
                                          status.IN_POSITION}, seen


def test_a_signal_the_decision_brain_refuses_still_reads_blocked():
    # BRAIN covers a low quality score and the 24-hour losing-streak pause
    # alike; the code cannot tell them apart, so it is not rounded down.
    seen, paper = _drive(quality_gate_off=False)
    assert not paper.positions()
    assert ("GATE_REJECTED: BRAIN", status.BLOCKED) in seen


@pytest.mark.parametrize("blocker, expected", [
    ("GATE_REJECTED: NO_CONFIRMATION", status.WAITING_FOR_SETUP),
    ("GATE_REJECTED: EMA_TREND_NOT_ALIGNED", status.WAITING_FOR_SETUP),
    ("GATE_REJECTED: REGIME_NOT_ALIGNED", status.WAITING_FOR_SETUP),
    ("GATE_REJECTED: INSUFFICIENT_RR", status.WAITING_FOR_SETUP),    # the strategy's own rule, one candle
    ("GATE_REJECTED: LIMIT_EXPIRED", status.WAITING_FOR_SETUP),
    ("GATE_REJECTED: SIGNALS_ONLY", status.WAITING_FOR_SETUP),       # the execution axis says so
    ("GATE_REJECTED: ATR_UNAVAILABLE", status.WARMING_UP),
    ("GATE_REJECTED: HTF_NOT_READY", status.WAITING_FOR_HTF),
    ("GATE_REJECTED: POSITION_MANAGED", status.IN_POSITION),
    ("GATE_REJECTED: POSITION_ALREADY_ALIGNED", status.IN_POSITION),
    ("GATE_REJECTED: ORDER_PENDING", status.ORDER_PENDING),
    ("GATE_REJECTED: APPROVAL_REQUIRED", status.ORDER_PENDING),
    # real holds on entries
    ("GATE_REJECTED: DAILY_LOSS_LIMIT", status.BLOCKED),
    ("GATE_REJECTED: LOSS_COOLDOWN", status.BLOCKED),
    ("GATE_REJECTED: EVENT_BLACKOUT", status.BLOCKED),
    ("GATE_REJECTED: PAUSED", status.BLOCKED),
    ("GATE_REJECTED: STALE_CANDLES", status.BLOCKED),
    ("GATE_REJECTED: PIPELINE_ERROR", status.BLOCKED),
    ("GATE_REJECTED: VENUE_RULES", status.BLOCKED),
    ("GATE_REJECTED: SIZING", status.BLOCKED),
    # ambiguous or unclassified: never rounded to fine
    ("GATE_REJECTED: EXECUTION", status.BLOCKED),
    ("GATE_REJECTED: BRAIN", status.BLOCKED),
    ("GATE_REJECTED: CONTEXT", status.BLOCKED),
    ("GATE_REJECTED: SOMETHING_NEW", status.BLOCKED),
    ("PENDING_ORDER_OWNERSHIP_INVALID", status.BLOCKED),
])
def test_each_code_reads_what_it_means(blocker, expected):
    assert _axis(blocker) == expected


def test_a_real_block_outranks_an_open_position():
    assert _axis("GATE_REJECTED: NO_ELIGIBLE_ZONE", position_open=True) == status.IN_POSITION
    assert _axis(None, position_open=True) == status.IN_POSITION
    assert _axis("GATE_REJECTED: STALE_CANDLES", position_open=True) == status.BLOCKED
    assert _axis("GATE_REJECTED: DAILY_LOSS_LIMIT", position_open=True) == status.BLOCKED


def test_every_code_a_strategy_adapter_declares_has_a_state():
    unclassified = {code for adapter in ADAPTERS.values() for gate in gate_sequence(adapter)
                    for code in gate.blockers if blocker_state(code) is None}
    assert not unclassified, sorted(unclassified)


def test_the_card_gets_the_same_sentence_the_visual_lab_shows():
    instance = SimpleNamespace(state="running", desired_running=True, symbol="SOLUSDT",
                               timeframe="5m", mode="trading", execution_mode="paper")
    from datetime import datetime, timezone
    engine = {"last_blocker": "GATE_REJECTED: NO_CONFIRMATION", "warmup_bars": 400,
              "warmup_required": 400, "last_closed_candle": datetime.now(timezone.utc).isoformat()}
    built = status.build(instance=instance, engine=engine,
                         market={"_worker_state": "running", "market_data_age_seconds": 10},
                         timeframe_seconds=300, worker_alive=True, entries_armed=True,
                         htf_policy={"requires_htf": False})
    assert built["market_status"] == status.LIVE
    assert built["strategy_status"] == status.WAITING_FOR_SETUP
    assert built["current_blocker"] == "GATE_REJECTED: NO_CONFIRMATION"
    assert built["current_blocker_explanation"] == BLOCKER_EXPLANATIONS["NO_CONFIRMATION"]
