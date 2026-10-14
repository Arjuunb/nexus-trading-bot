from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import selectors

import pytest

from test_shadow_router import setup, T, frame
from test_shadow_observation_worker import Hub
from services.shadow_observation_worker import ShadowObservationWorker
from services.shadow_observation_store import ShadowObservationStore
from services.shadow_research import ShadowResearchStore


def test_worker_health_reports_actual_sweep_and_shutdown(setup):
    router,_,_,_,clock,_ = setup
    worker = ShadowObservationWorker(router,Hub())
    assert worker.health()["readiness"] == "UNKNOWN"
    clock[0] = T-timedelta(seconds=1); worker.tick()
    assert worker.health()["last_successful_sweep"] == clock[0].isoformat()
    assert worker.health()["readiness"] == "READY"
    clock[0] += timedelta(seconds=30)
    assert worker.health()["readiness"] == "STALE"
    assert worker.stop()["errors"] == []
    assert worker.health()["readiness"] == "STOPPED"


def test_independent_entrypoint_has_no_trading_runtime(tmp_path):
    hub = Path(__file__).parents[1]
    cmd = [sys.executable,"-m","services.shadow_worker_main","--shadow-db",str(tmp_path/"isolated.sqlite"),"--journal-db",str(tmp_path/"missing-journal.sqlite"),"--market-data-dir",str(tmp_path/"market"),"--once"]
    result = subprocess.run(cmd,cwd=hub,capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    health = json.loads(result.stdout.splitlines()[-1])
    assert health["readiness"] == "READY"
    assert health["execution_runtime_loaded"] is False
    assert health["database_backend"] == "sqlite"
    assert not (tmp_path/"missing-journal.sqlite").exists()


def test_independent_entrypoint_refuses_financial_database(tmp_path):
    from services.shadow_worker_main import build_worker
    path = tmp_path/"financial.sqlite"
    db=sqlite3.connect(path); db.execute("CREATE TABLE paper_trades(id TEXT)"); db.commit(); db.close()
    with pytest.raises(ValueError,match="FINANCIAL_DATABASE_FORBIDDEN"):
        build_worker(shadow_db=path,journal_db=tmp_path/"journal.sqlite",market_data_dir=tmp_path/"market")
    db=sqlite3.connect(path)
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[("paper_trades",)]
    db.close()


def test_independent_worker_gracefully_handles_process_sigterm(tmp_path):
    hub=Path(__file__).parents[1]
    cmd=[sys.executable,"-m","services.shadow_worker_main","--shadow-db",str(tmp_path/"shadow.sqlite"),"--journal-db",str(tmp_path/"absent.sqlite"),"--market-data-dir",str(tmp_path/"market")]
    process=subprocess.Popen(cmd,cwd=hub,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    selector=selectors.DefaultSelector()
    try:
        selector.register(process.stdout,selectors.EVENT_READ)
        assert selector.select(timeout=15),"Worker did not publish bounded health"
        health=json.loads(process.stdout.readline())
        assert health["readiness"]=="READY"
        assert health["execution_runtime_loaded"] is False
        process.terminate()
        _,stderr=process.communicate(timeout=15)
        assert process.returncode==0,stderr
    finally:
        selector.close()
        if process.poll() is None:
            process.kill();process.communicate(timeout=5)


def test_additive_comparison_migration_rerun_preserves_legacy_and_observations(setup):
    router,store,raw,reg,_,_ = setup
    d=router.evaluate(reg["registration_id"],frame(),owner_id="alice")
    raw._db.execute("INSERT INTO shadow_quote_cursors VALUES ('TEST', 'old', 1, 'q')"); raw._db.commit()
    for _ in range(3):
        reopened=ShadowObservationStore(ShadowResearchStore(raw.path))
        assert reopened.decision_at(reg["registration_id"],d["candle_close_timestamp"],owner_id="alice")==d
        assert reopened._store._db.execute("SELECT * FROM shadow_quote_cursors").fetchall()[0][0]=="TEST"
        assert reopened._store._db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        assert reopened._store._db.execute("PRAGMA foreign_key_check").fetchall()==[]
        reopened._store._db.close()


def test_interrupted_migration_forward_recovery(tmp_path):
    raw=ShadowResearchStore(tmp_path/"shadow.sqlite")
    def deny(action,table,*_):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_TABLE and table=="shadow_comparison_requests" else sqlite3.SQLITE_OK
    raw._db.set_authorizer(deny)
    with pytest.raises(sqlite3.DatabaseError): ShadowObservationStore(raw)
    raw._db.set_authorizer(None)
    store=ShadowObservationStore(raw)
    assert raw._db.execute("SELECT COUNT(*) FROM shadow_comparison_requests").fetchone()[0]==0
    assert raw._db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"


def test_postgres_migration_discovery_never_applies_unscoped_shadow_sql():
    from data.pg_migrations import discover
    assert all(m.version!="0002_shadow_research" for m in discover())
def test_native_v2_financial_target_rejected_before_any_shadow_schema(tmp_path,monkeypatch):
    import sqlite3
    from services.research_worker_main import FORBIDDEN_CREDENTIALS
    from services.shadow_worker_main import build_worker
    for name in FORBIDDEN_CREDENTIALS:monkeypatch.delenv(name,raising=False)
    path=tmp_path/'native-paper.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE v2_account(account_id TEXT,balance TEXT)')
        db.execute('INSERT INTO v2_account VALUES (?,?)',('original-paper-account','1000.10'))
        before=db.execute('SELECT name FROM sqlite_master').fetchall()
    with pytest.raises(ValueError,match='FINANCIAL_DATABASE_FORBIDDEN'):
        build_worker(shadow_db=path,journal_db=tmp_path/'candidate-journal.sqlite',market_data_dir=tmp_path/'market')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT name FROM sqlite_master').fetchall()==before
        assert db.execute('SELECT balance FROM v2_account').fetchone()[0]=='1000.10'
