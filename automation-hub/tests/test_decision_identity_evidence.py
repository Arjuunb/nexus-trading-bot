from data.decision_store import DecisionStore
import pytest


def test_existing_decision_cannot_be_linked_to_different_configuration(tmp_path):
    store = DecisionStore(str(tmp_path / "decisions.db"))
    decision = {"symbol": "XRPUSDT", "decision": "accepted",
                "decision_identity": "same-signal", "strategy_id": "adaptive_trend_pullback",
                "strategy_version": "1.0.0", "strategy_config_hash": "original"}
    original = store.record(decision)
    with pytest.raises(ValueError, match="decision identity conflict"):
        store.record({**decision, "strategy_config_hash": "changed"})
    assert store.get(original)["strategy_config_hash"] == "original"
    assert len(store.list()) == 1


def test_decision_identity_is_durable_without_duplicate_decision(tmp_path):
    store = DecisionStore(str(tmp_path / "decisions.db"))
    d = {"symbol": "XRPUSDT", "decision": "accepted", "decision_identity": "one",
         "strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
         "strategy_config_hash": "a" * 64, "instance_id": "instance",
         "simulation_session_id": "session", "source_kind": "forward_paper"}
    first = store.record(d)
    assert store.record(d) == first
    row = store.get(first)
    assert row["strategy_config_hash"] == "a" * 64
    assert row["decided_at"]
    assert row["simulation_session_id"] == "session"
    assert len(store.list()) == 1


def test_old_decisions_keep_unknown_config(tmp_path):
    store = DecisionStore(str(tmp_path / "decisions.db"))
    key = store.record({"symbol": "XRPUSDT", "decision": "rejected"})
    assert store.get(key)["strategy_config_hash"] is None
