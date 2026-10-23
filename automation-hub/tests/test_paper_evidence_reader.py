from copy import deepcopy
from datetime import timedelta
from test_active_evidence_capture import captured,financial
from test_shadow_router import T
from services.paper_evidence_reader import PaperEvidenceReader
from services.paper_reconciliation import reconcile_paper
import pytest


def test_native_source_read_is_financially_inert_and_cannot_backfill_unknowns(captured):
    account=captured[0];sources=captured[-2];sid=captured[-1];before=financial(account)
    reader=PaperEvidenceReader(sources)
    availability=reader.availability(sid,owner_id='alice')
    assert availability['fields']['configuration_fingerprint']['status']=='UNKNOWN'
    bundle=reader.read(sid,owner_id='alice',start=T,end=T+timedelta(minutes=5))
    report=reconcile_paper(bundle,owner_id='alice',start=T,end=T+timedelta(minutes=5))
    assert report['performance_eligible'] is False
    assert report['metrics']['net_pnl'] is None
    assert bundle['scope']['config_fingerprint'] is None
    assert financial(account)==before
    with pytest.raises(KeyError):reader.read(sid,owner_id='bob',start=T,end=T+timedelta(minutes=5))


def test_instance_reader_preserves_original_committed_ledger_receipts_without_equity_inference():
    from data.ledger import SqliteLedger
    from test_paper_evidence_outbox import open_pair
    ledger=SqliteLedger();open_pair(ledger);before=ledger.get_authoritative_evidence_snapshot()
    class Sources:
        def sources(self,owner_id,limit):
            return [{'source_id':'instance-one','kind':'instance','instance_id':'one','session_id':'session-a'}] if owner_id=='alice' else []
    reader=PaperEvidenceReader(Sources(),instance_snapshot=lambda *_:ledger.get_authoritative_evidence_snapshot())
    bundle=reader.read('instance-one',owner_id='alice',start=T,end=T+timedelta(minutes=5))
    assert len(bundle['fills'])==len(before['outbox'])>0
    assert bundle['fills'][0]['source_reference'].startswith('paper_evidence_outbox:')
    assert bundle['account']=={} and reconcile_paper(bundle,owner_id='alice',start=T,end=T+timedelta(minutes=5))['performance_eligible'] is False
    assert ledger.get_authoritative_evidence_snapshot()==before
