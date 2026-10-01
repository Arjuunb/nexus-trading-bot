"""Historical Adaptive MTF decisions use independently closed native candles."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar, Signal, SignalType
from services.native_research_context import NativeResearchTimeline, load_adaptive_history
from strategies.custom import simulate_strategy


def _bar(opened: datetime, close: float = 100.0) -> Bar:
    return Bar(opened, close, close + 1, close - 1, close, 100)


def test_native_timeline_excludes_every_forming_higher_timeframe_bucket():
    utc = timezone.utc
    entry = _bar(datetime(2026, 9, 1, 12, 30, tzinfo=utc))
    native = {
        "1h": [_bar(datetime(2026, 9, 1, hour, tzinfo=utc)) for hour in (11, 12)],
        "15m": [_bar(datetime(2026, 9, 1, 12, minute, tzinfo=utc)) for minute in (15, 30)],
        "4h": [_bar(datetime(2026, 9, 1, hour, tzinfo=utc)) for hour in (8, 12)],
    }
    timeline = NativeResearchTimeline("BTCUSDT", "5m", native, required=("1h", "15m", "5m"))

    context, evidence = timeline.at([entry], 0)

    assert [b.timestamp.hour for b in context["1h"]] == [11]
    assert [b.timestamp.minute for b in context["15m"]] == [15]
    assert [b.timestamp.hour for b in context["4h"]] == [8]
    assert evidence["primary"]["htf_close_timestamp"] == "2026-09-01T12:00:00+00:00"
    assert evidence["secondary"]["htf_close_timestamp"] == "2026-09-01T12:00:00+00:00"


def test_simulator_supplies_native_context_before_each_adaptive_decision():
    utc = timezone.utc
    start = datetime(2026, 9, 1, 12, 30, tzinfo=utc)
    entry = [_bar(start + timedelta(minutes=5 * i)) for i in range(3)]
    native = {
        "1h": [_bar(datetime(2026, 9, 1, hour, tzinfo=utc)) for hour in (11, 12)],
        "15m": [_bar(datetime(2026, 9, 1, 12, minute, tzinfo=utc)) for minute in (15, 30, 45)],
    }

    class Strategy:
        symbol = "BTCUSDT"
        decision_timeframe = "5m"
        required_timeframes = ("1h", "15m", "5m")

        def __init__(self):
            self.seen = []

        def set_timeframe_context(self, context):
            self.context = context

        def set_native_mtf_context(self, context, evidence):
            self.evidence = evidence

        def on_bar(self, bar):
            self.seen.append((bar.timestamp, self.context["1h"][-1].timestamp,
                              self.context["15m"][-1].timestamp,
                              self.evidence["primary"]["htf_candle_id"]))
            return None

    strategy = Strategy()
    simulate_strategy(strategy, entry, native_context=native, manage=False)

    assert len(strategy.seen) == 3
    assert strategy.seen[0][1].hour == 11
    assert strategy.seen[0][2].minute == 15
    assert strategy.seen[-1][2].minute == 30
    assert len({row[3] for row in strategy.seen}) == 1


def test_native_timeline_rejects_missing_required_history_and_duplicate_candles():
    at = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="15m"):
        NativeResearchTimeline("BTCUSDT", "5m", {"1h": [_bar(at)]},
                               required=("1h", "15m", "5m"))
    with pytest.raises(ValueError, match="duplicate"):
        NativeResearchTimeline("BTCUSDT", "5m", {"1h": [_bar(at), _bar(at)],
                                                 "15m": [_bar(at)]},
                               required=("1h", "15m", "5m"))


def _cached_history(tmp_path, monkeypatch, *, provider="binance-usdt-perpetual",
                    entry_provider=None, include_15m=True, lag_1h=False):
    from config import settings
    from data.market_data_v2 import MarketDataService

    monkeypatch.setattr(settings, "market_data_v2_dir", str(tmp_path))
    service = MarketDataService(tmp_path)
    end = datetime(2026, 9, 1, 12, 35, tzinfo=timezone.utc)
    for timeframe, seconds, count in (("5m", 300, 90), ("1h", 3600, 90),
                                       ("15m", 900, 90)):
        if timeframe == "15m" and not include_15m:
            continue
        rows = []
        last_open = datetime.fromtimestamp(
            (int(end.timestamp()) // seconds - 1) * seconds, tz=timezone.utc)
        if timeframe == "1h" and lag_1h:
            last_open -= timedelta(hours=4)
        for index in range(count):
            opened = last_open - timedelta(seconds=seconds * (count - index - 1))
            rows.append((int(opened.timestamp() * 1000), 100.0, 101.0,
                         99.0, 100.0, 100.0))
        service.upsert("BTCUSDT", timeframe, rows,
                       provider=entry_provider if timeframe == "5m" and entry_provider else provider)


def test_research_loader_requires_verified_native_usdm_and_keeps_4h_optional(tmp_path, monkeypatch):
    _cached_history(tmp_path, monkeypatch)
    entry, native = load_adaptive_history("BTCUSDT", limit=80)
    assert len(entry) == 80
    assert len(native["1h"]) == len(native["15m"]) == 90
    assert native["4h"] == []

    # The same entry stream cannot be paired with spot or synthetic HTF bars.
    other = tmp_path / "spot"
    _cached_history(other, monkeypatch, provider="binance-spot")
    with pytest.raises(ValueError, match="Binance USD-M"):
        load_adaptive_history("BTCUSDT", limit=80)


def test_research_loader_rejects_gapped_entries_and_stale_required_context(
        tmp_path, monkeypatch):
    _cached_history(tmp_path, monkeypatch)
    entry, _ = load_adaptive_history("BTCUSDT", limit=80)
    with pytest.raises(ValueError, match="contain a gap"):
        load_adaptive_history("BTCUSDT", entry_rows=[*entry[:40], *entry[41:]])

    older = tmp_path / "stale_htf"
    _cached_history(older, monkeypatch, lag_1h=True)
    with pytest.raises(ValueError, match="native 1h history is stale"):
        load_adaptive_history("BTCUSDT", limit=80)

    mixed = tmp_path / "mixed"
    _cached_history(mixed, monkeypatch, entry_provider="binance-spot")
    with pytest.raises(ValueError, match="Binance USD-M"):
        load_adaptive_history("BTCUSDT", limit=80)


def test_missing_native_context_returns_unavailable_not_a_zero_trade_backtest(tmp_path, monkeypatch):
    from services.strategy_presets import run_simulation

    _cached_history(tmp_path, monkeypatch, include_15m=False)
    result = run_simulation("Adaptive MTF Trend Pullback", "BTCUSDT", "5m", bars=600)
    assert result["available"] is False
    assert "15m" in result["error"]
    assert "results" not in result


def test_control_simulation_uses_fixed_native_gate_without_row_index_resampling(tmp_path, monkeypatch):
    from services.strategy_presets import run_simulation
    import services.strategy_factory as factory

    created = []
    original = factory.make_builtin_strategy

    def capture(key, symbol):
        strategy = original(key, symbol)
        created.append(strategy)
        return strategy

    _cached_history(tmp_path, monkeypatch)
    monkeypatch.setattr(factory, "make_builtin_strategy", capture)
    result = run_simulation("Adaptive MTF Trend Pullback", "BTCUSDT", "5m", bars=600,
                            macro="4h", confirmation="15m")
    assert result["available"] is True
    assert result["mtf_gate"] == ["1h"]
    assert result["data_source"] == "Market Data V2 (native Binance USD-M futures)"
    assert result["results"]["entry_mode"] == "limit"
    assert created[0].decision_report()["blocker_code"] != "WARMUP"

    changed_clock = run_simulation("Adaptive MTF Trend Pullback", "BTCUSDT", "5m", bars=600,
                                   macro="1d", confirmation="15m")
    assert changed_clock["available"] is False
    assert "fixed native clocks" in changed_clock["error"]


def test_real_adaptive_orchestrator_can_reach_signal_stage_with_native_history(
        tmp_path, monkeypatch):
    from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy
    from strategies.adaptive_trend_pullback.models import (
        MarketRegime, RegimeAssessment, StageAssessment,
    )

    _cached_history(tmp_path, monkeypatch)
    entry, native = load_adaptive_history("BTCUSDT", limit=90)
    strategy = AdaptiveTrendPullbackStrategy("BTCUSDT")
    # Isolate the research clock plumbing; the separate strategy-fingerprint
    # tests protect the real regime/pullback/confirmation calculations.
    monkeypatch.setattr(strategy.regime_engine, "assess", lambda *_: RegimeAssessment(
        MarketRegime.BULL_TREND, 80, 80, 10, 25, 0.01))
    monkeypatch.setattr(strategy.trend_engine, "assess", lambda *_: StageAssessment(
        True, 100, "UP"))
    monkeypatch.setattr(strategy.pullback_detector, "assess", lambda *_: StageAssessment(
        True, 100, "VALID", swing_low=95, location="EMA20", volume_confirmed=True))
    monkeypatch.setattr(strategy.confirmation_engine, "assess", lambda *_: StageAssessment(
        True, 100, "CONFIRMED", volume_confirmed=True))

    simulate_strategy(strategy, entry, native_context=native, manage=False,
                      entry_mode="limit")
    report = strategy.decision_report()
    assert report["decision"] == "ENTER LONG"
    assert report["blocker_code"] is None
    assert strategy._native_mtf_evidence["primary"]["htf_timeframe"] == "1h"


def test_brain_receives_closed_native_primary_instead_of_legacy_resample():
    from types import SimpleNamespace

    utc = timezone.utc
    entry = [_bar(datetime(2026, 9, 1, 12, minute, tzinfo=utc))
             for minute in (30, 35, 40)]
    native = {
        "1h": [_bar(datetime(2026, 9, 1, hour, tzinfo=utc)) for hour in (11, 12)],
        "15m": [_bar(datetime(2026, 9, 1, 12, minute, tzinfo=utc)) for minute in (15, 30)],
    }

    class Strategy:
        symbol = "BTCUSDT"
        decision_timeframe = "5m"
        required_timeframes = ("1h", "15m", "5m")

        def set_timeframe_context(self, context):
            self.bars = context["5m"]

        def set_native_mtf_context(self, context, evidence):
            self._native_mtf_context = context

        def on_bar(self, bar):
            if bar is not entry[0]:
                return None
            return Signal(bar.timestamp, "BTCUSDT", SignalType.LONG, 100, 99, 103,
                          "test native gate", 1.0)

    class Brain:
        def __init__(self):
            self.calls = []

        def evaluate(self, *args, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(allowed=True, score=100, regime="trend",
                                   htf_bias="bullish", blocks=[])

    brain = Brain()
    simulate_strategy(Strategy(), entry, native_context=native, brain=brain, manage=False)
    assert len(brain.calls) == 1
    assert brain.calls[0]["require_native_htf"] is True
    assert brain.calls[0]["allow_legacy_htf_resample"] is False
    assert [bar.timestamp.hour for bar in brain.calls[0]["native_htf_bars"]] == [11]


def test_advanced_research_and_league_do_not_report_missing_native_data_as_zero_trades(
        tmp_path, monkeypatch):
    from config import settings
    from bot.data.synthetic import generate_bars
    from services import backtest_lab, strategy_league

    monkeypatch.setattr(settings, "market_data_v2_dir", str(tmp_path))
    strategy = "Adaptive MTF Trend Pullback"
    walk = backtest_lab.walk_forward(strategy, "BTCUSDT", "5m")
    assert walk["available"] is False
    assert "native Binance USD-M" in walk["error"]
    assert backtest_lab.sliced_performance(strategy)["available"] is False

    rows = generate_bars(n=420, timeframe="5m", seed=1)
    monkeypatch.setattr(strategy_league, "_candles",
                        lambda *_args: (rows, "legacy cache", {"live": False, "freshness": None}))
    league = strategy_league.league(symbols=("BTCUSDT",), timeframe="5m", bars=420,
                                   strategies=(strategy,))
    assert league["table"][0]["verdict"] == "unavailable"
    assert league["table"][0]["trades"] is None
    assert "native" in league["table"][0]["error"].lower()
