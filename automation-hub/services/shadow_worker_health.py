"""Bounded operator-only health file for the existing standalone observer.

Paths are trusted service configuration, never HTTP/agent input. A READY sweep
does not attest data freshness or candidate evidence qualification.
"""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys

SCHEMA='shadow-worker-health.v1'
MAX_BYTES=16*1024


def process_start_ticks(pid):
    """Linux process generation; a reused PID cannot revive old readiness."""
    try:
        return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(') ',1)[1].split()[19]
    except (OSError,IndexError): return None


def write_health(path, value, *, now=None):
    path=Path(path)
    if path.suffix!='.json' or path.is_symlink(): raise ValueError('HEALTH_FILE_NAMESPACE')
    if path.exists():
        if path.stat().st_size>MAX_BYTES or json.loads(path.read_text()).get('schema_version')!=SCHEMA:
            raise ValueError('HEALTH_FILE_NAMESPACE')
    payload={**value,'schema_version':SCHEMA,'pid':os.getpid(),'process_start_ticks':process_start_ticks(os.getpid()),
             'recorded_at':(now or datetime.now(timezone.utc)).isoformat(),
             'scope':'PROCESS_AND_CANDIDATE_STORE_READINESS; NOT MARKET_FEED_UPTIME'}
    encoded=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode())>MAX_BYTES: raise ValueError('HEALTH_FILE_SIZE_LIMIT')
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp.'+str(os.getpid()))
    try:
        with temporary.open('x') as target:
            os.chmod(temporary,0o600)
            target.write(encoded)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def check_health(path, *, now=None, max_age_seconds=20):
    try:
        path=Path(path)
        if path.stat().st_size>MAX_BYTES: raise ValueError('OVERSIZED')
        record=json.loads(path.read_text())
        if not isinstance(record,dict): raise ValueError('INVALID_HEALTH_SHAPE')
        at=datetime.fromisoformat(record['recorded_at'])
        if at.tzinfo is None: raise ValueError('INVALID_TIMESTAMP')
        age=((now or datetime.now(timezone.utc))-at).total_seconds()
        if record.get('schema_version')!=SCHEMA or not 0<=age<=max_age_seconds: raise ValueError('STALE_OR_UNKNOWN')
        if type(record.get('pid')) is not int or record['pid']<1: raise ValueError('INVALID_PID')
        os.kill(record['pid'],0)
        generation=process_start_ticks(record['pid'])
        if generation is None or record.get('process_start_ticks')!=generation: raise ValueError('STALE_PROCESS_GENERATION')
        storage=record.get('database_status') if record.get('worker_kind')=='research' else record.get('candidate_store_status')
        database=record.get('database_status') or 'UNKNOWN'
        # Current publishers probe observation metadata separately from the
        # candidate store. A healthy candidate reader cannot mask its outage.
        # Legacy v1 records retain their process-only contract, with database
        # verification explicitly UNKNOWN rather than invented as AVAILABLE.
        database_ready='database_status' not in record or database=='AVAILABLE'
        ready=record.get('readiness')=='READY' and storage=='AVAILABLE' and database_ready and record.get('execution_runtime_loaded') is False
        return {'ready':ready,'readiness':record.get('readiness'),'age_seconds':age,
                'database_status':database,'database_verified':database=='AVAILABLE',
                'scope':'WORKER_PROCESS_ONLY; CHECK_DECISION_DATA_GATES_SEPARATELY'}
    except (OSError,ValueError,KeyError,TypeError):
        return {'ready':False,'readiness':'UNKNOWN_OR_STALE',
                'database_status':'UNKNOWN','database_verified':False,'scope':'WORKER_PROCESS_ONLY'}


def main(argv=None):
    args=sys.argv[1:] if argv is None else argv
    if len(args)!=1: return 2
    result=check_health(args[0]);print(json.dumps(result))
    return 0 if result['ready'] else 1


if __name__=='__main__':raise SystemExit(main())
