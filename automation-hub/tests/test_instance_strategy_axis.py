"""The STRATEGY badge on a Trading Instance reads BLOCKED only when something
is holding entries back.

It used to read BLOCKED for every blocker code except NO_SETUP and WARMUP. The
strategies now say why a candle had no setup (NO_ELIGIBLE_ZONE,
NO_CONFIRMATION, EMA_TREND_NOT_ALIGNED, ...), so on ordinary candles the badge
flickered red while the strategy was simply waiting. It now follows the
per-code states the Instance Visual Lab already shows. A code no one has
classified still reads BLOCKED.

The Decision Brain's BRAIN code covers a one-candle refusal and its 24-hour
losing-streak pause alike; the engine's reason tells them apart, and the pause
holds the badge at BLOCKED until it ends, on quiet candles too.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services import instance_status as status
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.signal_pipeline import SignalPipeline
from services.strategy_factory import make_builtin_strategy
from services.strategy_registry import all_entries
from services.strategy_visual_registry import ADAPTERS, BLOCKER_EXPLANATIONS, blocker_state, gate_sequence
from strategies.three_candle_rejection import ThreeCandleRejectionStrategy
from tests.test_three_candle_rejection import _bar, _history, _long_pattern


def _axis(blocker, *, position_open: bool = False, reason: str = "") -> str:
    return status.strategy_status(market=status.LIVE, worker_state="running", warmup_bars=400,
                                  warmup_required=400, blocker=blocker, htf_ready=True,
                                  position_open=position_open, blocker_reason=reason)[0]


def _engine():
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, 10_000)
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=10_000,
                              risk_per_trade_pct=0.01, exposure_limit_pct=0.5)
    engine = AutoStrategyEngine(pipeline, paper, ledger, symbols=["BTCUSDT"], timeframe="5m",
                                live=False, fetcher=lambda *_a, **_k: ([], "test"), entry_mode="market")
    return engine, paper


def _drive(*, quality_gate_off: bool) -> tuple[list[tuple[str | None, str]], PaperExecutionEngine]:
    """The real engine, candle by candle, on the 3-candle rejection strategy:
    warm-up, a quiet range, a long setup at support, then a slow drift."""
    engine, paper = _engine()
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
        seen.append((engine.last_blocker, _axis(engine.last_blocker, reason=engine.last_blocker_reason,
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


def test_a_signal_the_decision_brain_refuses_on_one_candle_reads_refused_not_blocked():
    seen, paper = _drive(quality_gate_off=False)
    assert not paper.positions()
    assert ("GATE_REJECTED: BRAIN", status.SIGNAL_REFUSED) in seen     # a ranging regime, this candle
    assert status.BLOCKED not in {axis for _, axis in seen}


@pytest.mark.parametrize("reason, expected", [
    ("Hard block: ranging / unclear regime for a trend setup", status.SIGNAL_REFUSED),
    ("Quality score 52 below minimum 60 (failed: RSI 71, losing streak 3)", status.SIGNAL_REFUSED),
    ("Hard block: losing-streak cooldown (5 in a row); SOLUSDT resumes 2026-10-03 14:05 UTC",
     status.BLOCKED),
    ("Hard block: native primary HTF context unavailable", status.WAITING_FOR_HTF),
    ("", status.BLOCKED),                       # no reason: never rounded to fine
])
def test_the_brains_reason_decides_what_its_refusal_means(reason, expected):
    assert _axis("GATE_REJECTED: BRAIN", reason=reason) == expected


def test_every_instance_strategy_reads_blocked_only_in_a_losing_streak_pause():
    """Every strategy an instance can run, through the real engine with the
    Decision Brain on: warm-up, waiting, refusals, trades, resting orders and
    the pause. Only the pause, a real 24-hour hold, may read BLOCKED."""
    from bot.data.synthetic import generate_bars
    bars = generate_bars(n=1000, timeframe="5m", seed=3)
    for entry in all_entries():
        engine, paper = _engine()
        strategy = make_builtin_strategy(entry.strategy_id, "BTCUSDT")
        for bar in bars:
            engine._process_bar("BTCUSDT", bar, strategy)
            axis = _axis(engine.last_blocker, reason=engine.last_blocker_reason,
                         position_open=paper.open_position("BTCUSDT") is not None)
            if axis == status.BLOCKED:
                assert "losing-streak cooldown" in engine.last_blocker_reason, (
                    entry.strategy_id, engine.last_blocker, engine.last_blocker_reason)


def _lose(paper, n: int) -> None:
    for _ in range(n):
        paper.open(symbol="SOLUSDT", side="BUY", size=1, entry=100, stop=95)
        paper.close(symbol="SOLUSDT", exit_price=96)


def _card(engine: dict) -> dict:
    instance = SimpleNamespace(state="running", desired_running=True, symbol="SOLUSDT",
                               timeframe="5m", mode="trading", execution_mode="paper")
    return status.build(instance=instance, engine={"warmup_bars": 400, "warmup_required": 400, **engine},
                        market={"_worker_state": "running", "market_data_age_seconds": 10},
                        timeframe_seconds=300, worker_alive=True, entries_armed=True,
                        htf_policy={"requires_htf": False})


def test_the_pause_holds_the_badge_until_it_ends_on_quiet_candles_too(monkeypatch):
    engine, paper = _engine()
    assert engine._entry_pause_until("SOLUSDT") is None
    _lose(paper, 4)
    assert engine._entry_pause_until("SOLUSDT") is None              # four is not a pause
    _lose(paper, 1)
    until = datetime.fromisoformat(engine._entry_pause_until("SOLUSDT"))
    assert timedelta(hours=23) < until - datetime.now(timezone.utc) <= timedelta(hours=24)

    # a candle with no signal at all, in the middle of the pause
    card = _card({"last_blocker": "GATE_REJECTED: NO_SETUP", "entry_pause_until": until.isoformat()})
    assert card["strategy_status"] == status.BLOCKED
    assert card["current_blocker"] == "GATE_REJECTED: LOSS_COOLDOWN"
    assert until.strftime("%Y-%m-%d %H:%M UTC") in card["current_blocker_explanation"]
    # an open trade is still that, pause or not
    assert _card({"last_blocker": "GATE_REJECTED: POSITION_MANAGED",
                  "entry_pause_until": until.isoformat()})["strategy_status"] == status.IN_POSITION

    # once 24 hours have passed since the latest loss, it is over
    monkeypatch.setattr(engine, "_symbol_last_loss_at",
                        lambda _sym: datetime.now(timezone.utc) - timedelta(hours=25))
    assert engine._entry_pause_until("SOLUSDT") is None
    over = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert _card({"last_blocker": "GATE_REJECTED: NO_SETUP",
                  "entry_pause_until": over})["strategy_status"] == status.WAITING_FOR_SETUP


def test_with_the_quality_gate_score_at_zero_there_is_no_pause_to_report():
    engine, paper = _engine()
    _lose(paper, 6)
    engine.min_quality_score = 0
    assert engine._entry_pause_until("SOLUSDT") is None


@pytest.mark.parametrize("blocker, expected", [
    ("GATE_REJECTED: NO_CONFIRMATION", status.WAITING_FOR_SETUP),
    ("GATE_REJECTED: EMA_TREND_NOT_ALIGNED", status.WAITING_FOR_SETUP),
    ("GATE_REJECTED: REGIME_NOT_ALIGNED", status.WAITING_FOR_SETUP),
    ("GATE_REJECTED: INSUFFICIENT_RR", status.SIGNAL_REFUSED),       # a setup, refused this candle
    ("GATE_REJECTED: LIMIT_EXPIRED", status.SIGNAL_REFUSED),
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
    ("GATE_REJECTED: BRAIN", status.BLOCKED),                         # with no reason
    ("GATE_REJECTED: CONTEXT", status.SIGNAL_REFUSED),
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


def test_a_wanted_instance_with_no_free_slot_says_so():
    """Four instances wanted, three slots: the fourth never starts. The card
    said only "no market worker is running"; the supervisor's reason was on
    the row all along."""
    why = ("NO_FREE_SLOT: all 3 trading slots are in use (SOLUSDT 5m, XRPUSDT 5m, XRPUSDT 5m). "
           "It starts on its own once one is stopped.")
    instance = SimpleNamespace(state="stopped", desired_running=True, symbol="LINKUSDT", timeframe="5m",
                               mode="trading", execution_mode="paper", last_error=why)
    card = status.build(instance=instance, engine=None, market={"_worker_state": "stopped"},
                        timeframe_seconds=300, worker_alive=False, entries_armed=False,
                        htf_policy={"requires_htf": False})
    assert card["market_status"] == status.DISCONNECTED
    assert card["current_blocker"] == f"no market worker is running — {why}"

    stopped = SimpleNamespace(**{**vars(instance), "desired_running": False, "last_error": ""})
    card = status.build(instance=stopped, engine=None, market={"_worker_state": "stopped"},
                        timeframe_seconds=300, worker_alive=False, entries_armed=False,
                        htf_policy={"requires_htf": False})
    assert card["current_blocker"] == "no market worker is running"      # stopped by its owner
