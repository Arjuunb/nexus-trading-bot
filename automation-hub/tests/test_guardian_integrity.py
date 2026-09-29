"""Guardian Phase 4: every stage of a trade has its counterpart, completed
trades are journalled, and exposure is added up across instances and labs
without ever mixing paper and live.

Trades are real: the 3-Candle Rejection strategy through AutoStrategyEngine,
the pipeline and the paper engine; lab positions through the labs' own paper
broker. The journal is the real recorder. Guardian reads all of it through
read-only connections.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from data.trade_record_store import TradeRecordStore
from services.guardian.bus import EventBus
from services.guardian.incidents import CLOSED, OPEN
from services.guardian.integrity import IntegrityMonitor, Source
from services.guardian.service import GuardianService
from services.guardian.store import GuardianStore
from services.journal_recorder import JournalRecorder, LedgerSource


def _open_trade(tmp_path):
    """A real long taken by the strategy and left open."""
    from tests.test_guardian_strategy import _instance, _run
    from tests.test_three_candle_rejection import _history, _long_pattern
    ledger, engine, paper = _instance(tmp_path)
    rows, i = _history()
    _run(engine, rows + _long_pattern(i))
    assert paper.open_position("BTCUSDT") is not None
    return ledger, engine, paper


def _closed_trade(tmp_path):
    from tests.test_loss_streak_order import _attempt, _engine
    ledger, engine, paper = _engine(tmp_path)
    assert _attempt(engine, paper, 0, "win")
    return ledger


def _journal(ledger, path):
    store = TradeRecordStore(str(path))
    recorder = JournalRecorder(store)
    recorder.add_ledger(LedgerSource("MAIN", ledger))
    assert not recorder.reconcile()["errors"]
    return store


def _age(ledger, hours=2):
    """Move every trade back in time (the journal gets a grace period)."""
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    ledger._c.execute("UPDATE paper_trades SET opened_at=?, closed_at=CASE WHEN closed_at IS NULL "
                      "THEN NULL ELSE ? END", (old, old))
    ledger._c.commit()


def _monitor(store, ledger, journal_path, **kw):
    return IntegrityMonitor(store, [Source("MAIN", ledger.path, kind="ledger")],
                            journal_path=str(journal_path), every=1, **kw)


def _digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------ §23: every stage reconciles
def test_a_real_trade_reconciles_from_fill_to_journal(tmp_path):
    ledger = _closed_trade(tmp_path)
    _journal(ledger, tmp_path / "records.db")
    _age(ledger)
    before = _digest(ledger.path)
    report = _monitor(GuardianStore(), ledger, tmp_path / "records.db").run(now=datetime.now(timezone.utc).timestamp())
    assert report["findings"] == [] and report["errors"] == {}
    assert report["journal_checked"] and report["sources"]["MAIN"]["trades"] == 1
    assert _digest(ledger.path) == before                 # read, never written


def test_a_completed_trade_the_journal_never_recorded_is_a_confirmed_incident(tmp_path):
    ledger = _closed_trade(tmp_path)
    TradeRecordStore(str(tmp_path / "records.db"))       # the journal exists but missed it
    _age(ledger)
    store = GuardianStore()
    bus = EventBus(store)
    svc = GuardianService(store, bus, integrity=_monitor(store, ledger, tmp_path / "records.db"),
                          incident_verify_s=0)
    svc.cycle()
    bus.flush()
    [finding] = svc.integrity.last["findings"]
    assert finding["rule"] == "trade_not_journalled" and finding["severity"] == "HIGH"
    [violation] = store.events(event_type="integrity_violation")
    assert violation["source_component"] == "integrity:MAIN:trade_not_journalled"
    svc.cycle()
    [incident] = svc.incidents.list()
    assert incident["state"] == OPEN and incident["diagnosis"]["confidence"] == "CONFIRMED"
    assert incident["kind"] == "execution_failure"

    _journal(ledger, tmp_path / "records.db")            # the recorder catches up
    svc.cycle()
    bus.flush()
    assert store.events(event_type="integrity_resolved")
    svc.cycle()
    svc.cycle()
    assert svc.incidents.list()[0]["state"] == CLOSED


def test_a_fill_whose_position_is_missing_is_found(tmp_path):
    ledger = _closed_trade(tmp_path)
    _journal(ledger, tmp_path / "records.db")
    ledger._c.execute("DELETE FROM positions")           # the corruption being detected
    ledger._c.commit()
    report = _monitor(GuardianStore(), ledger, tmp_path / "records.db").run(now=datetime.now(timezone.utc).timestamp())
    rules = {f["rule"] for f in report["findings"]}
    assert rules == {"fill_without_position"}
    assert all(f["severity"] == "HIGH" for f in report["findings"])


def test_an_open_trade_and_its_position_must_agree(tmp_path):
    ledger, _, _ = _open_trade(tmp_path)
    _journal(ledger, tmp_path / "records.db")
    ledger._c.execute("UPDATE positions SET status='closed'")
    ledger._c.commit()
    report = _monitor(GuardianStore(), ledger, tmp_path / "records.db").run(now=datetime.now(timezone.utc).timestamp())
    assert [f["rule"] for f in report["findings"]] == ["position_trade_mismatch"]


def test_a_journal_that_cannot_be_read_is_said_not_assumed(tmp_path):
    ledger = _closed_trade(tmp_path)
    bad = tmp_path / "records.db"
    bad.write_text("not a database")
    report = _monitor(GuardianStore(), ledger, bad).run(now=datetime.now(timezone.utc).timestamp())
    assert "journal" in report["errors"] and report["journal_checked"] is False
    assert not [f for f in report["findings"] if f["rule"].endswith("journalled")]   # no guessing


# --------------------------------------------- §24: global risk, paper only
def test_open_risk_is_entry_to_stop_times_size_from_the_positions_themselves(tmp_path):
    ledger, _, paper = _open_trade(tmp_path)
    pos = paper.open_position("BTCUSDT")
    report = IntegrityMonitor(GuardianStore(), [Source("MAIN", ledger.path, kind="ledger")],
                              every=1).run(now=datetime.now(timezone.utc).timestamp())
    [row] = report["exposure"]["positions"]
    assert row["side"] == "long" and row["account_type"] == "PAPER" and row["paper"] is True
    assert row["risk"] == pytest.approx(abs(pos["entry"] - pos["stop"]) * pos["size"], abs=0.01)
    paper_view = report["exposure"]["paper"]
    assert paper_view["risk"] == row["risk"] and paper_view["by_cluster"]["crypto"]["net"] > 0
    assert report["exposure"]["live"]["positions"] == 0


def _broker_position(path, account_type):
    from execution.paper_broker_v2 import PaperBrokerV2
    from tests.test_journal_integrity import _at
    broker = PaperBrokerV2(str(path), starting_balance=10_000, account_type=account_type,
                           execution_engine="TEST")
    broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=0.01,
                  protection_stop_loss=99.0, protection_take_profit=102.0, signal_timestamp=_at(0),
                  decision_timestamp=_at(0), signal_price=100.0, requested_price=100.0,
                  strategy="LAB", strategy_version="1.0", timeframe="5m",
                  market_data_source="Binance USD-M public stream", candle_id="c1")
    broker.process_tick("BTCUSDT", {"bid": 100.0, "ask": 100.02, "mark": 100.01,
                                    "received_at": _at(0.1), "sequence": 1})
    assert broker.positions()
    return broker


def test_paper_and_live_exposure_are_never_added_together(tmp_path):
    """Acceptance test 17. A paper lab and an account whose own record says
    LIVE: the paper totals hold only the paper position, the other is listed
    apart, and with live routing locked it is a CRITICAL finding."""
    _broker_position(tmp_path / "paper.db", "PAPER")
    _broker_position(tmp_path / "live.db", "LIVE")
    monitor = IntegrityMonitor(GuardianStore(), [Source("SMC_LAB", str(tmp_path / "paper.db"), kind="lab_broker"),
                                                 Source("OTHER", str(tmp_path / "live.db"), kind="lab_broker")],
                               live_status=lambda: {"locked": True}, every=1)
    report = monitor.run(now=datetime.now(timezone.utc).timestamp())
    paper, live = report["exposure"]["paper"], report["exposure"]["live"]
    assert paper["positions"] == 1 and paper["by_symbol"][0]["accounts"] == ["SMC_LAB"]
    assert live["positions"] == 1 and live["routing_locked"] is True
    [critical] = [f for f in report["findings"] if f["rule"] == "live_exposure_while_locked"]
    assert critical["severity"] == "CRITICAL" and critical["source"] == "OTHER"


def test_the_labs_own_paper_labels_count_as_paper(tmp_path):
    """The labs label their paper accounts SMC_LAB and PA_LAB. A lab
    position must never read as live exposure."""
    _broker_position(tmp_path / "smc.db", "SMC_LAB")
    _broker_position(tmp_path / "pa.db", "PA_LAB")
    report = IntegrityMonitor(GuardianStore(), [Source("SMC_LAB", str(tmp_path / "smc.db"), kind="lab_broker"),
                                                Source("PA_LAB", str(tmp_path / "pa.db"), kind="lab_broker")],
                              live_status=lambda: {"locked": True}, every=1).run(
        now=datetime.now(timezone.utc).timestamp())
    assert report["exposure"]["paper"]["positions"] == 2 and report["exposure"]["live"]["positions"] == 0
    assert report["findings"] == []


def test_the_platforms_own_live_lock_is_what_guardian_reads():
    import webhook_api
    assert webhook_api.guardian.integrity is not None
    assert webhook_api.guardian.integrity.live_status()["locked"] is True
    assert webhook_api.broker_registry.live_locked() is True


def test_the_integrity_api_is_read_only():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    import routers.guardian
    assert all(r.methods == {"GET"} for r in routers.guardian.router.routes)
    app = FastAPI()
    app.include_router(webhook_api.router)
    client = TestClient(app)
    body = client.get("/guardian/integrity").json()
    assert {"findings", "exposure", "errors"} <= set(body)
    assert body["exposure"]["note"].startswith("Paper and live are never added together")
    assert client.post("/guardian/integrity").status_code == 405
