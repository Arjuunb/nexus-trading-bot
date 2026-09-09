"""Tier 2 — execution quality: maker (limit) entries in simulator + live
engine, maker fills in the fill model, slippage tracking + model calibration."""
from datetime import datetime, timezone

from bot.types import Bar, Signal, SignalType
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services.controls import TradingControl
from services.execution_quality import ExecutionQuality, slippage_bps
from services.fill_model import RealisticFill
from services.signal_pipeline import SignalPipeline

TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(close, high=None, low=None, open_=None):
    return Bar(TS, open_ if open_ is not None else close,
               high if high is not None else close,
               low if low is not None else close, close, 1.0)


# ─────────────────────────── fill model maker fills ───────────────────────────
def test_maker_fills_pay_no_spread_or_slippage():
    fm = RealisticFill(spread_pct=0.001, slippage_pct=0.001, latency_pct=0.0)
    taker = fm.apply("buy", 100.0, 1.0)
    maker = fm.apply("buy", 100.0, 1.0, maker=True)
    assert taker["price"] > 100.0
    assert maker["price"] == 100.0 and maker["cost_pct"] == 0.0


# ─────────────────────────── simulator limit entries ───────────────────────────
def test_simulator_limit_entry_fills_and_misses():
    from data.market_data import get_bars
    from strategies.brain_strategy import DecisionBrain
    from strategies.custom import simulate_strategy
    bars, _ = get_bars("ZZZUSDT", n=1800, timeframe="1h")
    market = simulate_strategy(DecisionBrain("ZZZUSDT"), bars, entry_mode="market")
    limit = simulate_strategy(DecisionBrain("ZZZUSDT"), bars, entry_mode="limit")
    assert limit["entry_mode"] == "limit" and "missed_entries" in limit
    # same signal stream; the limit path may skip some entries but never adds
    assert limit["total_trades"] <= market["total_trades"] + 1
    assert market.get("entry_mode") is None      # market path shape unchanged


# ─────────────────────────── live engine pending limits ───────────────────────────
def _engine(entry_mode="limit", pending_checkpoint=None):
    from services.auto_engine import AutoStrategyEngine
    led = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(led)
    pipe = SignalPipeline(led, paper, TradingControl(), equity=10_000,
                          risk_per_trade_pct=0.01, exposure_limit_pct=0.5,
                          max_total_exposure_pct=1.0)
    eng = AutoStrategyEngine(pipe, paper, led, symbols=["BTCUSDT"],
                             entry_mode=entry_mode,
                             pending_orders_checkpoint=pending_checkpoint)
    return eng, paper


class _Stub:
    def __init__(self, sigs):
        self._s = list(sigs)

    def on_bar(self, bar):
        return self._s.pop(0) if self._s else None


def _sig(entry=100.0, stop=95.0, tp=115.0):
    return Signal(timestamp=TS, symbol="BTCUSDT", type=SignalType.LONG,
                  entry=entry, stop_loss=stop, take_profit=tp, reason="x")


def test_engine_parks_limit_then_fills_when_touched():
    eng, paper = _engine()
    eng._process_bar("BTCUSDT", _bar(100), _Stub([_sig()]))
    assert paper.positions() == [] and eng.status()["pending_orders"] == 1
    # next bar dips to the limit price -> maker fill at exactly 100
    eng._process_bar("BTCUSDT", _bar(101, high=102, low=99.8), _Stub([]))
    pos = paper.open_position("BTCUSDT")
    assert pos is not None and pos["entry"] == 100.0
    assert eng.status()["pending_orders"] == 0


def test_limit_entry_cannot_claim_ambiguous_same_candle_target():
    eng, paper = _engine()
    eng._process_bar("BTCUSDT", _bar(101), _Stub([_sig(entry=100, stop=95, tp=105)]))
    # The next candle touches entry and target, but OHLC cannot prove the target
    # happened after entry. Conservative forward paper keeps the trade open.
    eng._process_bar("BTCUSDT", _bar(102, high=106, low=99), _Stub([]))
    assert paper.open_position("BTCUSDT") is not None
    assert paper.history() == []


def test_limit_entry_same_candle_stop_is_enforced_pessimistically():
    eng, paper = _engine()
    eng._process_bar("BTCUSDT", _bar(101), _Stub([_sig(entry=100, stop=95, tp=115)]))
    # Price must cross the long entry before reaching the lower stop, so this
    # adverse path is valid even though the candle's full path is unknown.
    eng._process_bar("BTCUSDT", _bar(101, high=116, low=94), _Stub([]))
    assert paper.open_position("BTCUSDT") is None
    assert paper.history()[0]["exit"] == 95


def test_existing_position_gap_through_stop_uses_adverse_open():
    eng, paper = _engine(entry_mode="market")
    paper.open(symbol="BTCUSDT", side="BUY", size=1, entry=100, stop=95,
               target=115)
    # This is an already-open trade, so a gap below the stop cannot fill at the
    # stale stop price. It exits at the opening price (before fill costs).
    eng._process_bar("BTCUSDT", _bar(91, open_=90, high=92, low=89), _Stub([]))
    assert paper.open_position("BTCUSDT") is None
    assert paper.history()[0]["exit"] == 90


def test_engine_checkpoints_pending_order_on_create_and_fill():
    saved = []
    eng, _paper = _engine(pending_checkpoint=lambda rows: saved.append(rows))
    eng._process_bar("BTCUSDT", _bar(100), _Stub([_sig()]))
    assert saved[-1]["BTCUSDT"]["ttl"] == 3
    eng._process_bar("BTCUSDT", _bar(101, high=102, low=99.8), _Stub([]))
    assert saved[-1] == {}


def test_engine_limit_expires_unfilled():
    eng, paper = _engine()
    eng._process_bar("BTCUSDT", _bar(100), _Stub([_sig()]))
    for px in (101, 102, 103):                    # price runs away for ttl bars
        eng._process_bar("BTCUSDT", _bar(px, high=px + 1, low=px - 0.5), _Stub([]))
    assert paper.positions() == []
    st = eng.status()
    assert st["pending_orders"] == 0 and st["missed_entries"] == 1


def test_engine_market_mode_unchanged():
    eng, paper = _engine(entry_mode="market")
    eng._process_bar("BTCUSDT", _bar(100), _Stub([_sig()]))
    assert paper.open_position("BTCUSDT") is not None   # immediate taker entry


def test_forward_ids_are_stable_but_research_replay_ids_are_repeatable():
    forward, _ = _engine()
    forward.live = True
    first = forward._auto_execution_id("BTCUSDT", TS, "buy")
    assert first == forward._auto_execution_id("BTCUSDT", TS, "buy")
    assert first.startswith("auto:")

    replay, _ = _engine()
    first_replay = replay._auto_execution_id("BTCUSDT", TS, "buy")
    second_replay = replay._auto_execution_id("BTCUSDT", TS, "buy")
    assert first_replay != second_replay
    assert not first_replay.startswith("auto:")


# ─────────────────────────── execution quality ───────────────────────────
def test_slippage_sign_convention():
    assert slippage_bps("buy", 100.0, 100.1) > 0        # paid up = bad
    assert slippage_bps("sell", 100.0, 100.1) < 0       # sold higher = good


def test_quality_report_and_model_calibration():
    q = ExecutionQuality()
    fm = RealisticFill(spread_pct=0.0004, slippage_pct=0.0003, latency_pct=0.0001)
    for _ in range(10):
        q.record(symbol="BTCUSDT", side="buy", intended=100.0, filled=100.5)  # 50bps!
    q.record(symbol="BTCUSDT", side="buy", intended=100.0, filled=100.0, maker=True)
    rep = q.report(fm)
    assert rep["overall"]["fills"] == 11
    assert rep["maker"]["avg_bps"] == 0.0
    assert rep["taker"]["avg_bps"] == 50.0
    assert "optimistic" in rep["model_calibration"]["verdict"]


def test_paper_engine_records_fills_into_quality():
    led = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(led, fill_model=RealisticFill(seed=3))
    paper.quality = ExecutionQuality()
    paper.open(symbol="BTCUSDT", side="BUY", size=1.0, entry=100.0, stop=95.0)
    paper.close(symbol="BTCUSDT", exit_price=110.0)
    rep = paper.quality.report()
    kinds = {r["kind"] for r in rep["recent"]}
    assert rep["overall"]["fills"] == 2 and kinds == {"entry", "exit"}
    assert rep["overall"]["avg_bps"] > 0                # friction is measured


def test_pipeline_floors_quantity_to_selected_venue_step():
    from bot.brokers.symbol_rules import SymbolRules

    led = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(led)
    pipe = SignalPipeline(
        led, paper, TradingControl(), equity=10_000,
        position_sizing_mode="fixed", fixed_position_size=0.03712941,
        exposure_limit_pct=1.0, max_total_exposure_pct=1.0,
    )
    pipe.symbol_rules_provider = lambda _symbol: SymbolRules(
        "BTC/USDT", step_size=0.001, min_qty=0.001, min_notional=1.0)
    result = pipe.process({"alert_id": "venue-1", "symbol": "BTCUSDT",
                           "side": "BUY", "entry": 100.0, "stop": 95.0,
                           "target": 115.0})
    assert result.accepted
    assert result.fill["size"] == 0.037
    assert any(step.rule == "venue_rules" for step in result.steps)


def test_pipeline_rejects_below_venue_minimum_notional():
    from bot.brokers.symbol_rules import SymbolRules

    led = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(led)
    pipe = SignalPipeline(
        led, paper, TradingControl(), equity=10_000,
        position_sizing_mode="fixed", fixed_position_size=0.01,
        exposure_limit_pct=1.0, max_total_exposure_pct=1.0,
    )
    pipe.symbol_rules_provider = lambda _symbol: SymbolRules(
        "BTC/USDT", step_size=0.001, min_qty=0.001, min_notional=10.0)
    result = pipe.process({"alert_id": "venue-2", "symbol": "BTCUSDT",
                           "side": "BUY", "entry": 100.0, "stop": 95.0,
                           "target": 115.0})
    assert not result.accepted
    assert result.stage == "venue_rules"
    assert "minimum" in result.reason


def test_usdm_dict_spec_is_normalized_before_pipeline_sizing(tmp_path, monkeypatch):
    from data.market_data_v2 import MarketDataService

    market = MarketDataService(tmp_path / "market")
    monkeypatch.setattr(market, "usdm_contract_rules", lambda symbol: {
        "symbol": symbol, "quantity_step": .001, "tick_size": .1,
        "min_quantity": .001, "min_notional": 1.0,
    })
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger)
    pipe = SignalPipeline(
        ledger, paper, TradingControl(), equity=10_000,
        position_sizing_mode="fixed", fixed_position_size=.03712941,
        exposure_limit_pct=1.0, max_total_exposure_pct=1.0,
    )
    pipe.symbol_rules_provider = market.usdm_symbol_rules
    result = pipe.process({"alert_id": "usdm-dict", "symbol": "ETHUSDT",
                           "side": "BUY", "entry": 100., "stop": 95., "target": 115.})
    assert result.accepted, result.reason
    assert result.fill["size"] == .037
    assert paper.positions()[0]["symbol"] == "ETHUSDT"
