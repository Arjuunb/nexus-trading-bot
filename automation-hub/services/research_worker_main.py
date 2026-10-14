"""Independently startable existing research worker; no trading credentials.

Backend/paths are trusted operator configuration. Never accepts code or dataset
paths from HTTP. PostgreSQL migrations must have been installed separately.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

from services.research_jobs import ResearchJobWorker

FORBIDDEN_CREDENTIALS={'BINANCE_API_KEY','BINANCE_API_SECRET','SUPABASE_KEY','ALPACA_API_KEY',
    'ALPACA_SECRET_KEY','BYBIT_API_KEY','BYBIT_API_SECRET','OANDA_API_KEY',
    # The application uses these names; rejecting only venue aliases would
    # still let an inherited app environment expose execution/control access.
    'HUB_EXCHANGE_API_KEY','HUB_EXCHANGE_API_SECRET','SUPABASE_SERVICE_ROLE_KEY',
    'HUB_CONTROL_KEY','HUB_WEBHOOK_SECRET','HUB_PASSWORD','HUB_SECRET','HUB_API_KEY',
    'ANTHROPIC_API_KEY','TELEGRAM_BOT_TOKEN'}


def build_worker(*,backend,journal_db=None):
    if any(os.environ.get(k) for k in FORBIDDEN_CREDENTIALS):raise ValueError('FORBIDDEN_EXECUTION_CREDENTIAL')
    if backend=='postgresql':
        from services.postgres_workers import PgResearchContext
        dsn=os.environ.get('NEXUS_RESEARCH_WORKER_DSN')
        if not dsn:raise ValueError('POSTGRESQL_WORKER_DSN_REQUIRED')
        context=PgResearchContext(dsn)
    elif backend=='sqlite':
        if not journal_db or not Path(journal_db).is_file():raise ValueError('EXISTING_RESEARCH_JOURNAL_REQUIRED')
        from data.journal_store import JournalStore
        from data.journal_context_store import JournalContextStore
        context=JournalContextStore(JournalStore(Path(journal_db).resolve()))
    else:raise ValueError('INVALID_RESEARCH_WORKER_BACKEND')
    return ResearchJobWorker(context)


def main(argv=None):
    parser=argparse.ArgumentParser(description='NEXUS research-only worker')
    parser.add_argument('--backend',choices=['sqlite','postgresql'],default=os.environ.get('NEXUS_RESEARCH_WORKER_BACKEND','sqlite'))
    parser.add_argument('--journal-db');parser.add_argument('--health-file');parser.add_argument('--once',action='store_true')
    parser.add_argument('--operational-spool',help='Trusted bounded local outbox path on a durable mounted filesystem')
    args=parser.parse_args(argv)
    if args.operational_spool:os.environ['NEXUS_OPERATIONAL_SPOOL_PATH']=args.operational_spool
    elif not os.environ.get('NEXUS_OPERATIONAL_SPOOL_PATH') and args.health_file:
        os.environ['NEXUS_OPERATIONAL_SPOOL_PATH']=str(Path(args.health_file).with_suffix('.operational-spool.sqlite'))
    if args.backend=='postgresql' and not os.environ.get('NEXUS_OPERATIONAL_SPOOL_PATH'):raise ValueError('DURABLE_OPERATIONAL_SPOOL_REQUIRED')
    worker=build_worker(backend=args.backend,journal_db=args.journal_db);stop=threading.Event()
    monitor=None
    if worker.telemetry:
        from services.worker_operational_monitor import WorkerOperationalMonitor
        monitor=WorkerOperationalMonitor(worker.telemetry,worker_kind='research',backend=args.backend)
    def shutdown(*_):stop.set();worker._stop.set()
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    def report(readiness='READY'):
        database='UNKNOWN';backlog=None
        try:
            with worker.context.journal._lock:
                worker.context.journal._c.execute('SELECT 1')
                backlog=worker.context.journal._c.execute("SELECT COUNT(*) FROM research_jobs WHERE status IN ('QUEUED','RETRY_PENDING')").fetchone()[0]
            database='AVAILABLE'
        except Exception:readiness='DEGRADED'
        if monitor:
            monitor.sample(database_available=database=='AVAILABLE',ready=readiness=='READY',queue_backlog=backlog,stopped=readiness=='STOPPED')
        payload={'worker_kind':'research','worker_id':worker.worker_id,'readiness':readiness,
            'database_status':database,'backend':args.backend,'concurrency':1,
            'metadata_authority_id':getattr(worker.context.journal,'authority_id',None),
            'execution_runtime_loaded':any(name in sys.modules for name in ('app','webhook_api','execution.paper_engine','services.signal_pipeline')),
            'scope':'PROCESS_AND_METADATA_READINESS; NOT JOB_OR_MARKET_PROFITABILITY'}
        payload['queue_backlog']=backlog
        payload['operational_spool']=monitor.health() if monitor else {'status':'UNKNOWN','reason':'OPERATOR_MONITOR_NOT_CONFIGURED'}
        if args.health_file:
            from services.shadow_worker_health import write_health
            write_health(args.health_file,payload)
        print(json.dumps(payload),flush=True)
    try:
        if args.once:worker.context.recover_expired_research_jobs();worker.process_once();report()
        else:
            worker.start()
            while not stop.wait(5):report()
    finally:
        worker.stop();report('STOPPED')
    return 0


if __name__=='__main__':raise SystemExit(main())
