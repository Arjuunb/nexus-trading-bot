from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.types import Bar
from data.cycle_store import CycleStore
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.mtf_policy import ENTRY_HTF, evidence_at, material_evidence, policy_for
from services.native_price_action import NativePriceActionEngine, PriceActionConfig
from services.native_smc import SMCConfig, SMCMarketStructureEngine
from services.signal_pipeline import SignalPipeline
from services.smc_strategy_v1 import evaluate as evaluate_smc
from strategies.brain_strategy import DecisionBrain


UTC = timezone.utc


def _bar(opened: datetime, close: float = 100.0) -> Bar:
    return Bar(opened, close, close + 1, close - 1, close, 1)


def _native_context() -> dict[str, list[Bar]]:
    return {
        "1h": [
            _bar(datetime(2026, 9, 7, 10, tzinfo=UTC), 99),
            _bar(datetime(2026, 9, 7, 11, tzinfo=UTC), 101),
            _bar(datetime(2026, 9, 7, 12, tzinfo=UTC), 500),  # closes 13:00
        ],
        "4h": [
            _bar(datetime(2026, 9, 7, 4, tzinfo=UTC), 102),
            _bar(datetime(2026, 9, 7, 8, tzinfo=UTC), 100),
            _bar(datetime(2026, 9, 7, 12, tzinfo=UTC), 500),  # closes 16:00
        ],
    }


def test_five_minute_decision_uses_only_one_hour_and_four_hour_closes_at_noon():
    evidence = evidence_at(
        "BTCUSDT", "5m", _native_context(),
        datetime(2026, 9, 7, 12, 35, tzinfo=UTC),
    )
    assert evidence["primary"]["htf_close_timestamp"] == "2026-09-07T12:00:00+00:00"
    assert evidence["secondary"]["htf_close_timestamp"] == "2026-09-07T12:00:00+00:00"
    assert evidence["primary"]["htf_candle_id"].endswith(":1788778800000")
    assert evidence["secondary"]["htf_candle_id"].endswith(":1788768000000")


def test_instance_pa_and_smc_persist_the_same_native_candle_ids():
    context = _native_context()
    decision_open = datetime(2026, 9, 7, 12, 30, tzinfo=UTC)
    decision_close = decision_open + timedelta(minutes=5)
    evidence = evidence_at("BTCUSDT", "5m", context, decision_close)

    instance = DecisionBrain("BTCUSDT")
    instance.set_native_mtf_context(context, evidence)

    pa = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m"))
    pa.set_native_mtf_context(context)
    pa.process_closed_bar(_bar(decision_open))

    smc_engine = SMCMarketStructureEngine(SMCConfig(symbol="BTCUSDT", timeframe="5m"))
    smc_engine.set_native_mtf_context(context)
    smc_engine.process_closed_bar(_bar(decision_open))
    smc = evaluate_smc(smc_engine, mtf_evidence=evidence)

    for role in ("primary", "secondary"):
        expected = evidence[role]["htf_candle_id"]
        assert instance._native_mtf_evidence[role]["htf_candle_id"] == expected
        assert pa._mtf_evidence[role]["htf_candle_id"] == expected
        assert smc["mtf_evidence"][role]["htf_candle_id"] == expected
    assert smc_engine._htf_key is None
    assert smc_engine.latest_snapshot.htf_completed_at.isoformat() == \
        evidence["primary"]["htf_close_timestamp"]


def test_missing_entry_bars_cannot_invent_a_four_hour_close():
    context = {"5m": [_bar(datetime(2026, 9, 7, 12, 30, tzinfo=UTC))], "1h": [], "4h": []}
    evidence = evidence_at(
        "BTCUSDT", "5m", context,
        datetime(2026, 9, 7, 12, 35, tzinfo=UTC),
    )
    assert evidence["primary"] is None
    assert evidence["secondary"] is None


def _instance_engine() -> AutoStrategyEngine:
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, starting_balance=10_000)
    pipeline = SignalPipeline(
        ledger, paper, TradingControl(), equity=10_000,
    )
    engine = AutoStrategyEngine(
        pipeline, paper, ledger, symbols=["BTCUSDT"], timeframe="5m",
        strategy_factory=lambda symbol: DecisionBrain(symbol),
    )
    engine.reports = CycleStore(":memory:")
    return engine


def test_secondary_bias_unavailable_does_not_become_an_entry_gate():
    engine = _instance_engine()
    strategy = DecisionBrain("BTCUSDT")
    context = _native_context()
    context["4h"] = []
    engine._multi_timeframe_context["BTCUSDT"] = context

    engine._apply_multi_timeframe_context(
        strategy, datetime(2026, 9, 7, 12, 30, tzinfo=UTC),
    )

    assert strategy._native_mtf_evidence["primary"] is not None
    assert strategy._native_mtf_evidence["secondary"] is None


def test_instance_cycle_persists_primary_and_secondary_native_identity():
    engine = _instance_engine()
    strategy = DecisionBrain("BTCUSDT")
    context = _native_context()
    evidence = evidence_at(
        "BTCUSDT", "5m", context,
        datetime(2026, 9, 7, 12, 35, tzinfo=UTC),
    )
    strategy.set_native_mtf_context(context, evidence)

    engine._process_bar(
        "BTCUSDT", _bar(datetime(2026, 9, 7, 12, 30, tzinfo=UTC)), strategy,
    )

    row = engine.reports.list(limit=1)[0]
    saved = engine.reports.get(row["id"])["report"]
    assert saved["mtf_evidence"] == material_evidence(evidence)


def test_brain_htf_mult_is_not_used_without_explicit_legacy_test_flag(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("relative htf_mult resampling reached REAL_PAPER path")

    monkeypatch.setattr("strategies.brain_strategy._htf_trend_vote", forbidden)
    strategy = DecisionBrain("BTCUSDT", htf_mult=12)
    context = _native_context()
    evidence = evidence_at(
        "BTCUSDT", "5m", context,
        datetime(2026, 9, 7, 12, 35, tzinfo=UTC),
    )
    strategy.set_native_mtf_context(context, evidence)
    strategy.bars = [
        _bar(datetime(2026, 9, 7, 7, 35, tzinfo=UTC) + timedelta(minutes=5 * index),
             100 + index * 0.1)
        for index in range(61)
    ]
    strategy.generate(strategy.bars[-1])


def test_fifteen_minute_entry_never_requests_a_three_hour_clock():
    assert policy_for("15m") == ("1h", "4h")
    assert "3h" not in {tf for pair in ENTRY_HTF.values() for tf in pair if tf}


def test_policy_is_exact_and_has_no_marketing_only_four_hour_special_case():
    assert dict(ENTRY_HTF) == {
        "1m": ("15m", "1h"),
        "5m": ("1h", "4h"),
        "15m": ("1h", "4h"),
        "1h": ("4h", "1d"),
        "4h": ("1d", None),
    }


def test_persisted_mtf_projection_contains_only_auditable_native_identity():
    evidence = evidence_at(
        "BTCUSDT", "5m", _native_context(),
        datetime(2026, 9, 7, 12, 35, tzinfo=UTC),
    )
    evidence["runtime_quote"] = {"bid": 100, "ask": 101}
    evidence["primary"]["subscription_heartbeat"] = "transient"

    persisted = material_evidence(evidence)

    assert persisted == {
        "entry_timeframe": "5m",
        "primary": {
            "htf_timeframe": "1h",
            "htf_candle_id": evidence["primary"]["htf_candle_id"],
            "htf_close_timestamp": "2026-09-07T12:00:00+00:00",
            "htf_bias": "BULLISH",
        },
        "secondary": {
            "htf_timeframe": "4h",
            "htf_candle_id": evidence["secondary"]["htf_candle_id"],
            "htf_close_timestamp": "2026-09-07T12:00:00+00:00",
            "htf_bias": "BEARISH",
        },
    }
