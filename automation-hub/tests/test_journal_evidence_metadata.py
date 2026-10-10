"""Failure recovery at the journal boundary, independent of paper accounting."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from data.journal_store import JournalStore
from services.decision_journal import DecisionJournal
from services.strategy_evidence import StrategyEvidence
from services.strategy_identity import configuration_fingerprint


SIGNAL_AT = "2026-01-01T00:00:00+00:00"
DECISION_AT = "2026-01-01T00:00:01+00:00"
FILL_AT = "2026-01-01T00:00:02+00:00"
EXIT_AT = "2026-01-01T00:01:00+00:00"


def _payload(store):
    configuration = {"threshold": 1}
    identity = {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
                "configuration": configuration,
                "strategy_config_hash": configuration_fingerprint(configuration),
                "source_hash": "source-1", "identity_status": "observed"}
    store.save_strategy_identity(identity)
    provenance = {**identity, "instance_id": "instance-1", "simulation_session_id": "session-1",
                  "execution_mode": "paper", "owner_id": "owner-1", "account_id": "account-1",
                  "lab_id": None, "source_kind": "forward_paper"}
    return {"journal_execution": provenance, "strategy_identity": identity,
            "timestamp": SIGNAL_AT, "decision_observed_at": DECISION_AT,
            "entry_timestamp": FILL_AT, "journal_decision_id": 1,
            "decision_identity": "signal-1", "alert_id": "order-1", "execution_id": "order-1"}


def _entry(journal, payload, **updates):
    arguments = {"trade_id": "trade-1", "position_id": "position-1", "mode": "paper",
                 "symbol": "XRPUSDT", "side": "long", "strategy": "Adaptive MTF",
                 "timeframe": "15m", "entry": 100, "stop": 95, "target": 110,
                 "size": 2, "equity": 1000, "confidence": 1, "brain_score": 75,
                 "regime": "trend", "steps": [], "payload": payload}
    journal.record_entry(**{**arguments, **updates})


def _exit(journal):
    return journal.record_exit(trade_id="trade-1", exit_price=110, pnl=19.8,
                               exit_reason="take-profit", instance_id="instance-1",
                               exit_timestamp=EXIT_AT, event_id="close-1", mfe_r=2.1, mae_r=-0.1)


@pytest.mark.parametrize("failed_kind", ["setup-detected", "risk-sized", "trade-opened"])
def test_entry_capture_failure_rolls_back_and_restart_repairs_complete_timeline(tmp_path, monkeypatch, failed_kind):
    path = str(tmp_path / "journal.db")
    store = JournalStore(path)
    payload = _payload(store)
    real_add = store.add_event

    def interrupted(trade_id, kind, *args, **kwargs):
        real_add(trade_id, kind, *args, **kwargs)
        if kind == failed_kind:
            raise RuntimeError("process stopped during journal capture")

    monkeypatch.setattr(store, "add_event", interrupted)
    with pytest.raises(RuntimeError, match="process stopped"):
        _entry(DecisionJournal(store), payload)
    recovered_store = JournalStore(path)
    assert recovered_store.get("trade-1") is None
    _entry(DecisionJournal(recovered_store), payload)
    _entry(DecisionJournal(recovered_store), payload)
    row = recovered_store.get("trade-1")
    assert [event["kind"] for event in row["events"]] == [
        "setup-detected", "quality-gate-passed", "risk-check-passed", "risk-sized", "trade-opened"]


@pytest.mark.parametrize("failed_kind", ["exit-triggered", "trade-closed", "review-generated"])
def test_exit_capture_failure_does_not_duplicate_evolution_on_restart(tmp_path, monkeypatch, failed_kind):
    path = str(tmp_path / "journal.db")
    store = JournalStore(path)
    _entry(DecisionJournal(store), _payload(store))
    real_add = store.add_event

    def interrupted(trade_id, kind, *args, **kwargs):
        real_add(trade_id, kind, *args, **kwargs)
        if kind == failed_kind:
            raise RuntimeError("process stopped during close capture")

    monkeypatch.setattr(store, "add_event", interrupted)
    with pytest.raises(RuntimeError, match="process stopped"):
        _exit(DecisionJournal(store))
    restarted = JournalStore(path)
    assert restarted.get("trade-1")["status"] == "open"
    assert restarted.evolution() == []
    _exit(DecisionJournal(restarted))
    _exit(DecisionJournal(restarted))
    assert restarted.evolution()[0]["trades"] == 1
    row = restarted.get("trade-1")
    assert row["closed_at"] == EXIT_AT
    assert len(row["events"]) == 8
    assert row["sections"]["exit_decision"]["exit_reason"] == "take-profit"
    assert row["sections"]["exit_decision"]["max_profit_r"] == 2.1


def test_concurrent_journal_close_is_one_timeline_and_one_evolution_sample(tmp_path):
    path = str(tmp_path / "journal.db")
    store = JournalStore(path)
    _entry(DecisionJournal(store), _payload(store))
    journals = [DecisionJournal(JournalStore(path)) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_exit, journals))
    assert store.evolution()[0]["trades"] == 1
    assert len(store.get("trade-1")["events"]) == 8


def test_episode_headers_and_distinct_causal_timestamps_are_retained():
    store = JournalStore()
    payload = _payload(store)
    observer = StrategyEvidence(store)
    context = {**payload["journal_execution"], "symbol": "XRPUSDT"}
    observer.observe_fill(SimpleNamespace(
        action="opened", symbol="XRPUSDT", side="long", trade_id="root-trade",
        position_id="root-position", execution_id="open-root", price=100, size=2,
        receipt={"initial_risk_amount": "10.123456789"}, executed_at=FILL_AT), context)
    observer.observe_fill(SimpleNamespace(
        action="reduced", symbol="XRPUSDT", side="long", trade_id="root-trade",
        position_id="root-position", execution_id="reduce-root", price=105, size=1,
        pnl=4.9, fee=0.1, receipt={}, executed_at=EXIT_AT,
        parent_trade_id="root-trade", remainder_trade_id="trade-1",
        remainder_position_id="position-1"), context)
    _entry(DecisionJournal(store), payload)
    row = store.get("trade-1")
    assert row["episode_id"] == store.episodes()[0]["episode_id"]
    assert row["parent_trade_id"] == "root-trade"
    assert row["initial_risk_amount_text"] == "10.123456789"
    assert row["identity_status"] == "observed"
    assert row["evidence_schema_version"] == 1
    assert row["owner_id"] == "owner-1" and row["account_id"] == "account-1"
    assert row["lab_id"] is None and row["source_kind"] == "forward_paper"
    assert row["signal_id"] == "signal-1"
    assert row["signal_timestamp"] == SIGNAL_AT
    assert row["decision_timestamp"] == DECISION_AT
    assert row["entry_timestamp"] == FILL_AT
    assert [event["ts"] for event in row["events"]] == [SIGNAL_AT, DECISION_AT, DECISION_AT, DECISION_AT, FILL_AT]


def test_legacy_entry_keeps_unknown_episode_and_configuration():
    store = JournalStore()
    _entry(DecisionJournal(store), {})
    row = store.get("trade-1")
    assert row["strategy_config_hash"] is None
    assert row["episode_id"] is None
    assert row["initial_risk_amount_text"] is None
    assert row["decision_timestamp"] is None
