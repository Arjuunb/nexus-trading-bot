"""Instance provenance is passive evidence, not another trading authority."""
import copy
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from data.decision_store import DecisionStore
from services.guardian_instance_read_model import instance_decision_page
from tradexa.guardian.instance_decisions import GuardianInstanceDecisions
from tradexa.guardian.instance_decision_traces import instance_decision_traces
from tradexa.guardian.reports import _summarize
from tradexa.guardian.store import GuardianStore

KEY = "guardian-instance-provenance-observer-key-12345"


def decision(identity="one", **settings):
    return {"ts": "2026-10-09T12:00:00+00:00", "symbol": "BTCUSDT", "timeframe": "5m",
            "strategy": "Human label 1", "decision": "accepted", "side": "long",
            "instance_id": "instance-1", "decision_identity": identity,
            "applied_settings": {"strategy_key": "adaptive_mtf", "strategy_version": "1",
                "config_revision": 2, "entry_mode": "limit", "trading_mode": "full",
                "min_quality_score": 60, **settings}}


def counts(path):
    with sqlite3.connect(path) as db:
        return tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in
                     ("decisions", "guardian_decision_lifecycle", "guardian_instance_decision_provenance"))


def collector(source, guardian, mutate=lambda view: view):
    def fetch(after, anchor):
        return mutate({"schema_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
            "scope": "POST_INSTALL_INSTANCE_DECISION_LIFECYCLE", "feed_health_verified": False,
            "execution_integrity_verified": False,
            "page": instance_decision_page(source, after=after, anchor=anchor)})
    return GuardianInstanceDecisions(guardian, "http://app:8000/guardian/instance-decisions", KEY, fetch=fetch)


@pytest.mark.parametrize("min_score", [0, 101])
def test_actual_engine_captures_applied_settings_without_changing_execution(monkeypatch, min_score):
    from test_decision_gate import _engine_with_decisions, _fire_signal
    monkeypatch.setenv("GIT_COMMIT", "a" * 40)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    engine, paper = _engine_with_decisions(min_score=min_score)
    engine.ledger.instance_id = engine.instance_id = "instance-1"
    engine.strategy_key, engine.strategy_version, engine.config_revision = "test", "version-1", 7
    records = _fire_signal(engine)
    assert len(records) == 1
    assert records[0]["decision"] == ("rejected" if min_score else "accepted")
    assert len(paper.positions()) == (0 if min_score else 1)
    assert engine.stats["trades"] == (0 if min_score else 1)
    config = json.loads(records[0]["applied_settings_json"])
    assert config == {"symbol": "BTCUSDT", "timeframe": engine.timeframe,
        "strategy_key": "test", "strategy_version": "version-1", "config_revision": 7,
        "entry_mode": "market", "trading_mode": "full", "min_quality_score": min_score}


@pytest.mark.parametrize("invalid", [{"strategy_key": {"api_key": "NEVER_STORE"}},
    {"min_quality_score": float("nan")}, {"config_revision": True},
    {"strategy_version": "x" * 3000}])
def test_malformed_extra_telemetry_does_not_block_decision_or_persist_nested_secret(tmp_path, invalid):
    path = tmp_path / "decisions.db"
    source = DecisionStore(str(path))
    did = source.record(decision(**invalid))
    assert source.get(did)["decision"] == "accepted" and counts(path) == (1, 1, 1)
    p = instance_decision_page(path)["transitions"][0]["instance_provenance"]
    assert p["state"] == "INCOMPLETE_APPLIED_SETTINGS" and p["saved_config"] is None
    assert "NEVER_STORE" not in str(source.get(did))


def test_snapshot_and_reported_commit_are_per_writer_and_survive_pruning_restart(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.setenv("GIT_COMMIT", "a" * 40)
    first = DecisionStore(str(path))
    monkeypatch.setenv("GIT_COMMIT", "b" * 40)
    second = DecisionStore(str(path))
    original = decision(api_key="NEVER_EXPORT", rolling_candles=list(range(1000)))
    did = first.record(original)
    second.record(decision("two", config_revision=3, min_quality_score=0))
    original["applied_settings"]["min_quality_score"] = 99
    assert second.record(original) == did
    first.finalize(did, final_state="GATE_REJECTED", gate_stage="risk", reason="risk cap")
    page = instance_decision_page(path)
    p, q, repeated = [row["instance_provenance"] for row in page["transitions"]]
    assert p == repeated and p["code_commit"] == "a" * 40 and q["code_commit"] == "b" * 40
    assert p["saved_config_hash"] != q["saved_config_hash"]
    assert p["state"] == q["state"] == "CAPTURED_APPLIED_SETTINGS"
    assert q["saved_config"]["min_quality_score"] == 0
    assert "NEVER_EXPORT" not in json.dumps(page) and "rolling_candles" not in json.dumps(page)
    assert p["full_strategy_config_verified"] is False and p["source_attestation_verified"] is False
    for _ in range(100):
        assert instance_decision_page(path) == page
    assert counts(path) == (2, 3, 2)
    first.prune(keep=0)
    first._c.close()
    second._c.close()
    restarted = DecisionStore(str(path))
    assert instance_decision_page(path) == page and counts(path) == (0, 3, 2)
    restarted._c.close()


@pytest.mark.parametrize("commit,render", [(None, None), ("short", None),
    ("a" * 40, "b" * 40), ("a" * 40, "a" * 40)])
def test_missing_invalid_or_conflicting_commits_never_become_attestations(tmp_path, monkeypatch, commit, render):
    for key, value in (("GIT_COMMIT", commit), ("RENDER_GIT_COMMIT", render)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    path = tmp_path / "decisions.db"
    DecisionStore(str(path)).record(decision())
    p = instance_decision_page(path)["transitions"][0]["instance_provenance"]
    assert p["code_commit"] == (commit if commit == render and commit else None)
    assert p["source_attestation_verified"] is False


def test_legacy_rows_stay_unknown_and_uninstrumented_writers_do_not_borrow_current_commit(tmp_path):
    path = tmp_path / "decisions.db"
    source = DecisionStore(str(path))
    source._c.execute("DROP TRIGGER temp.guardian_capture_instance_provenance")
    did = source.record(decision())
    source._c.close()
    restarted = DecisionStore(str(path))
    restarted.mark_executed(did)
    rows = instance_decision_page(path)["transitions"]
    assert all(row["instance_provenance"]["state"] == "UNKNOWN" for row in rows)
    assert counts(path) == (1, 2, 0)
    restarted.record(decision("partial", strategy_version=None))
    assert instance_decision_page(path)["transitions"][-1]["instance_provenance"]["state"] == "INCOMPLETE_APPLIED_SETTINGS"


def test_capture_failure_is_atomic_with_source_and_outbox_then_retry_succeeds(tmp_path):
    path = tmp_path / "decisions.db"
    source = DecisionStore(str(path))
    source._c.execute("""CREATE TRIGGER injected_failure BEFORE INSERT ON guardian_instance_decision_provenance
        BEGIN SELECT RAISE(ABORT,'injected provenance failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected provenance failure"):
        source.record(decision())
    assert counts(path) == (0, 0, 0)
    source._c.execute("DROP TRIGGER injected_failure")
    did = source.record(decision())
    assert source.record(decision()) == did and counts(path) == (1, 1, 1)
    for operation in ("UPDATE", "DELETE"):
        statement = ("UPDATE guardian_instance_decision_provenance SET code_commit=NULL" if operation == "UPDATE"
                     else "DELETE FROM guardian_instance_decision_provenance")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            source._c.execute(statement)


def test_import_trace_report_and_restarted_cursor_preserve_config_identity(tmp_path):
    path = tmp_path / "decisions.db"
    source = DecisionStore(str(path))
    did = source.record(decision())
    source.record(decision("two", min_quality_score=0, strategy_version="2"))
    source.mark_executed(did)
    guardian = GuardianStore(tmp_path / "guardian.db")
    assert collector(path, guardian).poll() == 3
    assert collector(path, GuardianStore(guardian.path)).poll() == 0
    traces = instance_decision_traces(guardian)["traces"]
    assert len(traces) == 2 and all(trace["strategy_id"] == "adaptive_mtf" for trace in traces)
    assert {trace["strategy_version"] for trace in traces} == {"1", "2"}
    assert all(trace["instance_provenance"]["state"] == "CAPTURED_APPLIED_SETTINGS" for trace in traces)
    events = guardian.recent(source_service="guardian_instance_decisions")
    summary = _summarize([event | {"_sequence": i} for i, event in enumerate(events)], truncated=False)
    assert len(summary["strategies"]) == 2
    assert all(group["saved_config_hash"] and not group["exact_version_verified"] for group in summary["strategies"])


@pytest.mark.parametrize("mutation", [{"saved_config_hash": "a" * 64}, {"instance_id": "other"},
    {"decision_identity": "other"}, {"source_attestation_verified": True},
    {"full_strategy_config_verified": True}, {"schema_version": True},
    {"saved_config": {"api_key": "secret"}}, {"saved_config_scope": "FULL_CONFIG"}])
def test_forged_provenance_cannot_advance_cursor(tmp_path, mutation):
    path = tmp_path / "decisions.db"
    DecisionStore(str(path)).record(decision())
    guardian = GuardianStore(tmp_path / "guardian.db")
    def mutate(view):
        view = copy.deepcopy(view)
        view["page"]["transitions"][0]["instance_provenance"].update(mutation)
        return view
    with pytest.raises(ValueError):
        collector(path, guardian, mutate).poll()
    assert guardian.count() == 0 and guardian.observer_cursor("instance_decisions") == (0, "")
    assert collector(path, guardian).poll() == 1
