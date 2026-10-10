"""Derived memories must preserve exact decision links and truthful costs."""
from __future__ import annotations

import copy

import pytest

from data.decision_store import DecisionStore
from data.journal_store import JournalStore
from data.trade_memory_store import TradeMemoryStore
from services.trade_memory import compose_memory
from services.trade_memory_manager import TradeMemoryManager


_AT = "2026-09-07T12:00:00+00:00"


@pytest.fixture
def stores():
    journal = JournalStore(":memory:")
    memory = TradeMemoryStore(":memory:")
    decisions = DecisionStore(":memory:")
    return TradeMemoryManager(memory, journal, decisions), journal, memory, decisions


def _decision(decisions, *, instance_id="one", **extra):
    return decisions.record({
        "ts": _AT, "symbol": "XRPUSDT", "side": "long",
        "decision": "accepted", "executed": True, "instance_id": instance_id,
        "strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
        "execution_mode": "paper", "owner_id": "owner", "account_id": "account-one",
        "htf_bias": instance_id, "passed_rules": ["context from " + instance_id], **extra,
    })


def _closed_journal(journal, *, trade_id="trade", decision_id=None, sections=None, **extra):
    journal.record_entry({
        "trade_id": trade_id, "created_at": _AT, "symbol": "XRPUSDT", "side": "long",
        "mode": "paper", "execution_mode": "paper", "strategy": "Adaptive MTF",
        "strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
        "instance_id": "one", "owner_id": "owner", "account_id": "account-one",
        "entry": 1.0, "stop": 0.9, "target": 1.3, "size": 10.0, "risk_amount": 1.0,
        "decision_id": decision_id, "sections": sections or {}, **extra,
    })
    journal.close_trade(trade_id, exit=1.2, pnl=1.87654322, actual_rr=1.87654322,
                        result="win", grade="A", extra_sections={})


def test_exact_decision_id_wins_over_same_symbol_time_in_another_instance(stores):
    manager, journal, memory, decisions = stores
    wanted = _decision(decisions, instance_id="one")
    _decision(decisions, instance_id="two", account_id="account-two")
    _closed_journal(journal, decision_id=wanted)

    result = manager.remember("trade")

    assert result["sections"]["strategy"]["htf_bias"] == "one"
    assert result["sections"]["execution"]["conditions_passed"] == ["context from one"]
    assert result["sections"]["execution"]["decision_linkage"] == {
        "status": "VERIFIED", "basis": "decision_id", "decision_id": wanted,
    }
    assert memory.count() == 1


def test_persisted_entry_decision_reference_is_an_exact_compatibility_link(stores):
    manager, journal, _, decisions = stores
    wanted = _decision(decisions)
    _decision(decisions, instance_id="two")
    _closed_journal(journal, sections={"entry_decision": {"decision_reference": wanted}})

    result = manager.remember("trade")

    assert result["sections"]["strategy"]["htf_bias"] == "one"
    assert result["sections"]["execution"]["decision_linkage"]["basis"] == "decision_reference"
    assert result["sections"]["execution"]["decision_linkage"]["status"] == "VERIFIED"


@pytest.mark.parametrize("reference", (999, "not-an-id"))
def test_broken_explicit_reference_does_not_fall_back_to_a_nearby_decision(stores, reference):
    manager, journal, _, decisions = stores
    _decision(decisions)
    _closed_journal(journal, decision_id=reference)

    result = manager.remember("trade")

    assert result["sections"]["strategy"]["htf_bias"] == "not captured"
    assert result["sections"]["execution"]["decision_linkage"]["status"] == "UNVERIFIED"


def test_conflicting_explicit_references_are_not_guessed(stores):
    manager, journal, _, decisions = stores
    first, second = _decision(decisions), _decision(decisions)
    _closed_journal(journal, decision_id=first,
                    sections={"entry_decision": {"decision_reference": second}})

    assert manager._match_decision(journal.get("trade")) is None


@pytest.mark.parametrize("field,value", (
    ("instance_id", "other"), ("simulation_session_id", "replay-other"),
    ("owner_id", "other"), ("account_id", "other"), ("tenant_id", "other"),
    ("strategy_id", "other"), ("strategy_version", "2.0.0"),
    ("strategy_config_hash", "other-fingerprint"), ("execution_mode", "backtest"),
    ("symbol", "BTCUSDT"), ("side", "short"),
))
def test_exact_link_cannot_cross_a_captured_scope(stores, field, value):
    manager, journal, _, decisions = stores
    wanted = _decision(decisions)
    _closed_journal(journal, decision_id=wanted)
    row = journal.get("trade")
    row[field] = value

    assert manager._match_decision(row) is None


def test_unexecuted_or_rejected_decision_is_not_entry_evidence(stores):
    manager, journal, _, decisions = stores
    wanted = _decision(decisions, decision="rejected", executed=False)
    _closed_journal(journal, decision_id=wanted)

    assert manager._match_decision(journal.get("trade")) is None


def test_legacy_heuristic_is_scope_checked_and_labelled_unverified(stores):
    manager, journal, _, decisions = stores
    wanted = _decision(decisions)
    _decision(decisions, instance_id="two", account_id="account-two")
    _closed_journal(journal)

    result = manager.remember("trade")

    assert result["sections"]["strategy"]["htf_bias"] == "one"
    assert result["sections"]["execution"]["decision_linkage"] == {
        "status": "UNVERIFIED", "basis": "legacy_symbol_side_time", "decision_id": wanted,
    }


def test_unknown_legacy_version_is_not_backfilled_from_a_heuristic(stores):
    manager, journal, _, decisions = stores
    _decision(decisions)
    _closed_journal(journal, strategy_version=None)

    result = manager.remember("trade")

    assert result["sections"]["strategy"]["version"] == "not captured"
    assert result["sections"]["execution"]["decision_linkage"]["status"] == "UNVERIFIED"


def test_unscoped_legacy_trade_does_not_inherit_an_instance_decision(stores):
    manager, journal, _, decisions = stores
    _decision(decisions)
    _closed_journal(journal, instance_id=None, owner_id=None, account_id=None)

    assert manager._match_decision(journal.get("trade")) is None


def _journal_with_receipt(receipt=None):
    return {
        "trade_id": "trade", "status": "closed", "mode": "paper", "symbol": "XRPUSDT",
        "side": "long", "created_at": _AT, "closed_at": "2026-09-07T13:00:00+00:00",
        "entry": "1", "stop": "0.9", "size": "10", "pnl": "1.87654322",
        "sections": {"exit_decision": {"execution_receipt": receipt} if receipt else {}},
    }


def test_booked_fees_keep_precision_and_are_not_subtracted_twice():
    row = _journal_with_receipt({
        "booked_fees": "0.12345678", "gross_pnl": "2", "net_pnl": "1.87654322",
        "funding": None, "funding_coverage": "not_modeled",
    })
    original = copy.deepcopy(row)

    result = compose_memory(row)

    info = result["sections"]["trade_information"]
    assert info["fees"] == "0.12345678"
    assert info["fees_coverage"] == "BOOKED"
    assert info["funding"] is None
    assert info["funding_coverage"] == "UNMODELED"
    outcome = result["sections"]["trade_outcome"]
    assert outcome["gross_pnl"] == "2"
    assert outcome["net_pnl"] == "1.87654322"
    assert result["pnl"] == 1.88  # Existing summary display calculation remains.
    assert row == original


@pytest.mark.parametrize("coverage", ("UNKNOWN", "not_modeled"))
def test_zero_in_an_unverified_funding_field_is_not_verified_zero(coverage):
    result = compose_memory(_journal_with_receipt({
        "booked_fees": "0", "funding": "0", "funding_coverage": coverage,
    }))

    info = result["sections"]["trade_information"]
    assert info["fees"] == "0"
    assert info["fees_coverage"] == "BOOKED"
    assert info["funding"] is None
    assert info["funding_coverage"] == ("UNMODELED" if coverage == "not_modeled" else "UNKNOWN")


def test_missing_cost_receipt_keeps_fee_and_funding_coverage_unknown():
    result = compose_memory(_journal_with_receipt())

    info = result["sections"]["trade_information"]
    assert "not captured" in info["fees"]
    assert "0.00" not in info["fees"]
    assert info["fees_coverage"] == "UNKNOWN"
    assert info["funding"] is None
    assert info["funding_coverage"] == "UNKNOWN"
    assert result["sections"]["trade_outcome"]["gross_pnl"] is None


@pytest.mark.parametrize("value", ("NaN", "Infinity", "-Infinity", "invalid"))
def test_nonfinite_or_invalid_cost_receipts_remain_unknown(value):
    result = compose_memory(_journal_with_receipt({"booked_fees": value}))

    assert result["sections"]["trade_information"]["fees_coverage"] == "UNKNOWN"
    assert "not captured" in result["sections"]["trade_information"]["fees"]


def test_explicit_booked_funding_is_attributed_without_changing_net_pnl():
    result = compose_memory(_journal_with_receipt({
        "booked_fees": "0.1", "funding": "-0.02", "funding_coverage": "BOOKED",
        "net_pnl": "1.87654322",
    }))

    info = result["sections"]["trade_information"]
    assert info["funding"] == "-0.02"
    assert info["funding_coverage"] == "BOOKED"
    assert result["sections"]["trade_outcome"]["net_pnl"] == "1.87654322"


def test_verified_zero_funding_cannot_assert_a_nonzero_charge():
    result = compose_memory(_journal_with_receipt({"funding": "0.02", "funding_coverage": "VERIFIED_ZERO"}))

    assert result["sections"]["trade_information"]["funding"] is None
    assert result["sections"]["trade_information"]["funding_coverage"] == "UNKNOWN"


def test_memory_scope_collision_does_not_overwrite_another_accounts_notes(stores):
    manager, journal, memory, _ = stores
    _closed_journal(journal)
    manager.remember("trade", notes="Human note belongs to account one")
    stored = memory.get("trade")
    stored["sections"]["trade_information"]["account_id"] = "another-account"
    memory.upsert(stored)
    original = copy.deepcopy(memory.get("trade"))

    assert manager.remember("trade") is None
    assert memory.get("trade") == original


def test_rebuild_is_idempotent_preserves_manual_notes_and_captured_scope(stores):
    manager, journal, memory, decisions = stores
    wanted = _decision(decisions)
    _closed_journal(journal, decision_id=wanted)
    before = copy.deepcopy(journal.get("trade"))
    manager.remember("trade", notes="Do not discard this human note")

    assert manager.rebuild() == {"rebuilt": 1, "failed": 0, "total": 1}
    assert manager.rebuild() == {"rebuilt": 1, "failed": 0, "total": 1}

    result = memory.get("trade")
    assert result["notes"] == "Do not discard this human note"
    assert result["sections"]["emotion_journal"]["manual_notes"] == result["notes"]
    assert result["sections"]["trade_information"]["owner_id"] == "owner"
    assert result["sections"]["trade_information"]["account_id"] == "account-one"
    assert result["sections"]["trade_information"]["instance_id"] == "one"
    assert journal.get("trade") == before


def test_committed_close_hook_composes_actual_costs_without_writing_accounting(stores):
    from data.ledger import SqliteLedger
    from execution.paper_engine import PaperExecutionEngine
    from services.controls import TradingControl
    from services.decision_journal import DecisionJournal
    from services.fill_model import RealisticFill
    from services.signal_pipeline import SignalPipeline
    from services.strategy_evidence_capture import StrategyEvidenceCapture

    manager, journal, memory, decisions = stores
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, fill_model=RealisticFill(
        spread_pct=0, slippage_pct=0, latency_pct=0, taker_fee_pct=0.001))
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=10_000)
    pipeline.journal = DecisionJournal(journal)
    pipeline.journal_context = {"instance_id": "one", "strategy_id": "adaptive_trend_pullback",
                                "strategy_version": "1.0.0", "execution_mode": "paper"}
    capture = StrategyEvidenceCapture(pipeline.journal, decisions=decisions, trade_memory=manager)
    pipeline.evidence = capture
    paper.evidence_listener = capture.observe_fill
    paper.evidence_prepare_listener = capture.prepare_exit
    decision_id = _decision(decisions)
    result = pipeline.process({
        "alert_id": "order-one", "symbol": "XRPUSDT", "side": "BUY",
        "entry": 100, "stop": 95, "target": 110, "strategy": "Adaptive MTF",
        "timeframe": "5m", "timestamp": _AT, "journal_decision_id": decision_id,
        "decision_identity": "decision-one",
    })
    assert result.accepted
    closed = paper.close(symbol="XRPUSDT", exit_price=106, execution_id="close-one")
    accounting = copy.deepcopy(ledger.get_paper_trades())

    remembered = memory.get(closed.trade_id)
    assert remembered is not None
    assert remembered["sections"]["trade_information"]["fees_coverage"] == "BOOKED"
    assert float(remembered["sections"]["trade_information"]["fees"]) == closed.fee > 0
    assert float(remembered["sections"]["trade_outcome"]["net_pnl"]) == closed.pnl
    assert remembered["sections"]["execution"]["decision_linkage"]["decision_id"] == decision_id
    assert manager.rebuild()["failed"] == 0
    assert ledger.get_paper_trades() == accounting


def test_scope_rejected_explicit_reference_cannot_fall_back_to_another_legacy_decision(stores):
    manager, journal, memory, decisions = stores
    _decision(decisions)
    _closed_journal(journal, sections={"entry_decision": {
        "decision_reference_status": "CONFLICT", "claimed_decision_reference": 999}})
    result = manager.remember("trade")
    assert result["sections"]["execution"]["decision_linkage"]["decision_id"] is None
    assert result["sections"]["execution"]["decision_linkage"]["status"] == "UNVERIFIED"
