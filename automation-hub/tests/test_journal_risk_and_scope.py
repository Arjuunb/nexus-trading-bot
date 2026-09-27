"""What a record says about risk, and how a review names its scope.

Every case runs real code: the 3-Candle Rejection strategy through
AutoStrategyEngine, the signal pipeline and the paper engines, or the frozen
SMC strategy placed and filled by the SMC lab's own broker.
"""
from __future__ import annotations

from data.trade_record_store import TradeRecordStore
from services.journal_labs import SMCLabProjector
from tests.test_journal_integrity import _real_smc_trade
from tests.test_journal_record_timing import _forward_trade


# ---------------------------------------------------------------- D12
def test_the_record_shows_the_risk_it_aimed_for_and_the_risk_it_took(tmp_path):
    """The sizer targeted 1% of $10,000; the 5% exposure cap then cut the
    size, so the trade risked about 0.15%. The record keeps both."""
    record, ledger, _ = _forward_trade(tmp_path)
    [(entry, stop, size, equity)] = ledger._c.execute(
        "SELECT entry, stop, size, equity_before_trade FROM paper_trades").fetchall()
    risk = record["risk_check"]["risk"]
    sizing = record["risk_check"]["sizing"]
    assert record["risk_percent"] == 0.01 and risk["target_pct"] == 1.0
    assert record["equity_before"] == equity == 10_000
    assert abs(record["risk_amount"] - abs(entry - stop) * size) < 0.01
    assert risk["taken_pct"] == round(record["risk_amount"] / equity * 100, 4)
    assert 0 < risk["taken_pct"] < 0.2                      # far below the 1% target
    assert sizing["accepted_size"] < sizing["computed_size"]
    assert risk["reduced_after_sizing"] is True


def test_a_lab_records_its_risk_as_a_fraction_like_every_other_record(tmp_path):
    """The SMC lab is configured in percent (0.5 means 0.5%). Stored as-is,
    the record read 0.5 where an instance record reads 0.01 for 1%, and the
    Trade page showed a 50% risk."""
    account, _ = _real_smc_trade(tmp_path)
    store = TradeRecordStore()
    SMCLabProjector(account).project(store)
    [row] = store.query_trades()
    record = store.get(row["journal_record_id"])
    session = account.session()
    assert session["risk_pct"] == 0.5
    assert record["risk_percent"] == 0.005
    # the lab sized for 0.5% of its balance, give or take rounding of the quantity
    assert abs(record["risk_amount"] / session["starting_balance"] - record["risk_percent"]) < 0.001


# ---------------------------------------------------------------- D10
def test_two_instances_never_share_a_review_scope_label(tmp_path):
    """Instance ids are uuid4 hex. Two that begin with the same 8 characters
    were both labelled "Instance 3f2a9c1e · three_candle_rejection"."""
    from datetime import datetime, timezone

    from data.journal_store import JournalStore
    from data.ledger import SqliteLedger
    from services.decision_journal import DecisionJournal
    from services.journal_recorder import JournalRecorder, LedgerSource
    from services.journal_reviews import review_scopes
    from tests.test_journal_legacy_provenance import _trade

    first, second = "3f2a9c1e5b7d4e0f9a1b2c3d4e5f6a7b", "3f2a9c1e0c8d4b1a9e2f3a4b5c6d7e8f"
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    journal = DecisionJournal(JournalStore(str(tmp_path / "journal.db")))
    _trade(ledger, journal, first, "live", datetime(2026, 3, 2, tzinfo=timezone.utc))
    _trade(ledger, journal, second, "live", datetime(2026, 3, 5, tzinfo=timezone.utc))
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    recorder = JournalRecorder(store)
    recorder.add_ledger(LedgerSource("MAIN", ledger))
    recorder.reconcile()
    assert {r["record_origin"] for r in store.query_trades()} == {"FORWARD_PAPER"}
    scopes = {s["agent_id"]: s["label"] for s in review_scopes(store)}
    assert set(scopes) == {f"instance_agent:{first}", f"instance_agent:{second}"}
    labels = list(scopes.values())
    assert labels[0] != labels[1]
    for instance_id in (first, second):
        assert scopes[f"instance_agent:{instance_id}"] == (
            f"Instance · BTCUSDT 5m · three_candle_rejection · {instance_id}")
