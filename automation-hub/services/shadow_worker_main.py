"""Independently startable, credentials-free-client observation worker.

Operator CLI paths are trusted configuration, NEVER accepted by a research API.
An external supervisor/OS resource policy must be configured separately. This
entrypoint does not import app, webhook_api, accounts, brokers or order clients.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading

from data.market_data_v2 import MarketDataService
from services.active_decision_evidence import ActiveDecisionSources
from services.forward_paper_hub import ForwardPaperMarketDataHub
from services.shadow_observation_store import ShadowObservationStore
from services.shadow_observation_worker import ShadowObservationWorker
from services.shadow_research import ShadowResearchStore
from services.shadow_router import ShadowRouter


def build_worker(*, shadow_db, journal_db, market_data_dir, feed_journal=None, feed_revision=None, feed_cursors=None):
    from services.research_worker_main import FORBIDDEN_CREDENTIALS
    if any(os.environ.get(k) for k in FORBIDDEN_CREDENTIALS):raise ValueError('FORBIDDEN_EXECUTION_CREDENTIAL')
    if any(str(value).lower().startswith(("postgres://","postgresql://")) for value in (shadow_db,journal_db)):
        raise ValueError("UNSUPPORTED_POSTGRESQL_OBSERVATION_BACKEND")
    shadow, journal = Path(shadow_db).resolve(), Path(journal_db).resolve()
    if shadow==journal or (shadow.exists() and journal.exists() and shadow.samefile(journal)):
        raise ValueError("FINANCIAL_DATABASE_FORBIDDEN")
    if shadow.exists():
        with ActiveDecisionSources.connection(shadow) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(name.startswith('v2_') for name in tables) or tables & {"paper_trades","paper_positions","orders","positions","pa_sessions","smc_sessions","trade_decision_journal"}:
            raise ValueError("FINANCIAL_DATABASE_FORBIDDEN")
    def candidate_reader(cid, owner):
        try:
            with ActiveDecisionSources.connection(journal) as db:
                row=db.execute("SELECT * FROM strategy_research_candidates WHERE candidate_id=? AND owner_id=?",(cid,owner)).fetchone()
                if not row: return None
                result=dict(row)
                result["frozen_config"]=json.loads(result.pop("frozen_config_json"))
                result["validation_run_ids"]=json.loads(result.pop("validation_run_ids_json"))
                result["scope"]=json.loads(result.pop("scope_json"))
                return result
        except sqlite3.Error: return None  # unavailable is not permission to invent identity
    research=None
    from services.worker_backends import shadow_store as selected_shadow_store
    if os.environ.get('NEXUS_RESEARCH_WORKER_BACKEND','sqlite')=='postgresql':
        from services.postgres_workers import PgResearchContext
        research=PgResearchContext(os.environ.get('NEXUS_RESEARCH_WORKER_DSN'))
        candidate_reader=lambda cid,owner:research.get_research_candidate(cid,owner_id=owner)
    store=selected_shadow_store(lambda:ShadowResearchStore(shadow))
    router=ShadowRouter(store,candidate_reader=candidate_reader)
    if any((feed_journal,feed_revision,feed_cursors)):
        if not all((feed_journal,feed_revision,feed_cursors)):raise ValueError('COMPLETE_DISTRIBUTED_FEED_BINDING_REQUIRED')
        from services.market_data_distribution import DistributedMarketDataHub
        hub=DistributedMarketDataHub(feed_journal,feed_cursors,revision=feed_revision)
    else:
        public=MarketDataService(market_data_dir)
        hub=ForwardPaperMarketDataHub(public.public_usdm_window)
    worker=ShadowObservationWorker(router,hub)
    worker.candidate_metadata_context=research
    return worker


def main(argv=None):
    parser=argparse.ArgumentParser(description="Read-only strategy observation; no trading runtime")
    parser.add_argument("--shadow-db",required=True)
    parser.add_argument("--journal-db",required=True,help="Existing candidate journal, opened read-only")
    parser.add_argument("--market-data-dir",required=True)
    parser.add_argument("--once",action="store_true",help="One bounded local sweep and health report")
    parser.add_argument("--health-file",help="Trusted operator JSON path; never an API input")
    parser.add_argument('--feed-journal',help='Opt-in finalized public candle journal, mounted read-only')
    parser.add_argument('--feed-revision',help='Exact reviewed journal transformation revision')
    parser.add_argument('--feed-cursors',help='Separate writable observation cursor namespace')
    parser.add_argument('--operational-spool',help='Trusted bounded local outbox path on a durable mounted filesystem')
    args=parser.parse_args(argv)
    if args.operational_spool:os.environ['NEXUS_OPERATIONAL_SPOOL_PATH']=args.operational_spool
    elif not os.environ.get('NEXUS_OPERATIONAL_SPOOL_PATH') and args.health_file:
        os.environ['NEXUS_OPERATIONAL_SPOOL_PATH']=str(Path(args.health_file).with_suffix('.operational-spool.sqlite'))
    if os.environ.get('NEXUS_SHADOW_WORKER_BACKEND')=='postgresql' and not os.environ.get('NEXUS_OPERATIONAL_SPOOL_PATH'):raise ValueError('DURABLE_OPERATIONAL_SPOOL_REQUIRED')
    if args.health_file:
        health_path=Path(args.health_file).resolve()
        if health_path.suffix!='.json' or health_path in {Path(args.shadow_db).resolve(),Path(args.journal_db).resolve()}:
            raise ValueError("HEALTH_FILE_NAMESPACE")
    logging.basicConfig(level=logging.INFO)
    worker=build_worker(shadow_db=args.shadow_db,journal_db=args.journal_db,market_data_dir=args.market_data_dir,
        feed_journal=args.feed_journal,feed_revision=args.feed_revision,feed_cursors=args.feed_cursors)
    from services.worker_operational_monitor import WorkerOperationalMonitor
    monitor=WorkerOperationalMonitor(worker.telemetry,worker_kind='shadow',backend=os.environ.get('NEXUS_SHADOW_WORKER_BACKEND','sqlite'))
    stop=threading.Event()
    def shutdown(signum,frame): stop.set()
    signal.signal(signal.SIGTERM,shutdown); signal.signal(signal.SIGINT,shutdown)
    def report():
        candidate_status="UNAVAILABLE"
        database_status='UNAVAILABLE'
        try:
            if os.environ.get('NEXUS_RESEARCH_WORKER_BACKEND','sqlite')=='postgresql':
                context=worker.candidate_metadata_context
                with context.journal._lock:
                    context.journal.execute('SELECT candidate_id FROM strategy_research_candidates LIMIT 0')
                candidate_status='AVAILABLE'
            else:
                with ActiveDecisionSources.connection(args.journal_db) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_research_candidates'").fetchone():
                        candidate_status="AVAILABLE"
        except Exception: pass
        try:
            raw=worker.router.store._store
            with raw._lock:raw._db.execute('SELECT 1')
            database_status='AVAILABLE'
        except Exception:pass
        payload={**worker.health(),"candidate_store_status":candidate_status,
                 'database_status':database_status,
                 'backend':os.environ.get('NEXUS_SHADOW_WORKER_BACKEND','sqlite'),
                 'metadata_authority_id':getattr(worker.router.store._store,'authority_id',None),
                 "execution_runtime_loaded":any(name in sys.modules for name in ("app","webhook_api","execution.paper_engine","services.signal_pipeline")),
                 "observation_readiness":"NO_AUTHORIZED_REGISTRATIONS" if not worker.health()["owned_registrations"] else "CONSULT_PER_DECISION_DATA_GATES"}
        monitor.sample(database_available=database_status=='AVAILABLE',ready=database_status=='AVAILABLE' and worker.health()['readiness']=='READY',stopped=worker.health()['readiness']=='STOPPED')
        payload['operational_spool']=monitor.health()
        if args.health_file:
            from services.shadow_worker_health import write_health
            write_health(args.health_file,payload)
        print(json.dumps(payload),flush=True)
    try:
        if args.once: worker.tick(); report()
        else:
            worker.start()
            while not stop.wait(5): report()
    finally:
        result=worker.stop()
        if args.health_file: report()
    return 1 if result["errors"] else 0


if __name__=="__main__": raise SystemExit(main())
