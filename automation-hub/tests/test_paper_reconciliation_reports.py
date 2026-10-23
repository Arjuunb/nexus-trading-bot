from copy import deepcopy
from datetime import timedelta
import sqlite3
import pytest

from test_paper_reconciliation import complete,T
from services.paper_reconciliation_store import PaperReconciliationService
from services.shadow_research import ShadowResearchStore
from services.shadow_observation_store import ShadowObservationStore


@pytest.fixture
def service(tmp_path,complete):
    class Reader:
        def read(self,source_id, *, owner_id,**_):
            if source_id!='owned-paper' or owner_id!='alice':raise KeyError(source_id)
            return deepcopy(complete)
    raw=ShadowResearchStore(tmp_path/'metadata.sqlite');shadow=ShadowObservationStore(raw)
    return PaperReconciliationService(shadow,Reader(),clock=lambda:T+timedelta(minutes=10)),raw,complete


def request():return {'source_id':'owned-paper','start':T.isoformat(),'end':(T+timedelta(minutes=5)).isoformat(),'idempotency_key':'paper-report-original'}


def test_preview_commit_restart_immutability_and_owner_isolation(service):
    svc,raw,bundle=service;original=deepcopy(bundle)
    assert svc.assess(request(),owner_id='alice')['commitment_status']=='NOT_COMMITTED'
    assert svc.history(owner_id='alice')==[]
    report=svc.assess(request(),owner_id='alice',commit=True)
    assert report['status']=='VERIFIED'
    assert svc.assess(request(),owner_id='alice',commit=True)==report
    restarted=PaperReconciliationService(svc.shadow,svc.reader,clock=svc.clock)
    assert restarted.assess(request(),owner_id='alice',commit=True)==report
    assert svc.get(report['report_id'],owner_id='bob') is None
    assert svc.history(owner_id='bob')==[]
    with pytest.raises(sqlite3.IntegrityError):raw._db.execute("UPDATE paper_reconciliation_reports SET payload_json='{}'")
    assert bundle==original
    assert raw.table_counts()['shadow_orders']==0 and raw.table_counts()['shadow_fills']==0


def test_financial_snapshot_and_source_inputs_cannot_be_supplied_by_client(service):
    svc,_,_=service
    for field in ('snapshot','fees','funding','verified','database_path','place_order'):
        with pytest.raises(ValueError):svc.assess({**request(),field:True},owner_id='alice',commit=True)


def test_new_cost_evidence_requires_a_new_report_and_preserves_original(service):
    svc,_,bundle=service;first=svc.assess(request(),owner_id='alice',commit=True)
    bundle['episodes'][0]['funding_coverage']='UNKNOWN'
    second=svc.assess({**request(),'idempotency_key':'corrected-paper-report','supersedes_report_id':first['report_id']},owner_id='alice',commit=True)
    assert second['report_id']!=first['report_id'] and second['performance_eligible'] is False
    assert svc.get(first['report_id'],owner_id='alice')==first


def test_financial_database_rejected_before_reconciliation_schema(tmp_path):
    from data.journal_store import JournalStore
    journal=JournalStore(tmp_path/'financial.sqlite');before=journal._c.execute('SELECT name FROM sqlite_master').fetchall()
    class Raw:
        _db=journal._c;_lock=journal._lock
    class Facade:
        _store=Raw()
    with pytest.raises(ValueError,match='FINANCIAL_DATABASE_FORBIDDEN'):PaperReconciliationService(Facade(),None)
    assert journal._c.execute('SELECT name FROM sqlite_master').fetchall()==before
