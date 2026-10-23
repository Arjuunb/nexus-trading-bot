"""Read-only deployment diagnostics. Availability is not trading authorization."""
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3

VERSION='nexus_deployment_readiness.v1'


def deployment_readiness(*,owner_id,auth_verified=False,metadata_probe=None,
                         worker_paths=None,spool_path=None,shadow_enabled=False,now=None):
    if not owner_id:raise PermissionError('AUTHENTICATED_OWNER_REQUIRED')
    now=now or datetime.now(timezone.utc);stamp=now.isoformat();checks={}
    def put(name,status,reason,*,source='READ_ONLY_LOCAL_PROBE',authority=None,reference=None,**extra):
        checks[name]={'status':status,'observed_at':stamp,'source':source,
            'authority_identity':authority,'reason':reason,'evidence_reference':reference,**extra}
    put('HTTP_LIVE','READY','AUTHENTICATED_REQUEST_RECEIVED')
    put('AUTH_READY','READY' if auth_verified else 'UNKNOWN',
        'CURRENT_REQUEST_AUTHORIZED' if auth_verified else 'AUTHORIZATION_NOT_VERIFIED',
        source='EXISTING_AUTHENTICATION_AND_RBAC; NOT_EXTERNAL_IDENTITY_SOAK')
    authority=None;postgres=False
    if metadata_probe is None:
        put('POSTGRES_READY','UNKNOWN','POSTGRES_PROBE_UNAVAILABLE')
    else:
        try:
            data=metadata_probe()
            if data.get('backend')!='postgresql':put('POSTGRES_READY','UNKNOWN','POSTGRES_NOT_SELECTED')
            elif not data.get('available'):put('POSTGRES_READY','BLOCKED','METADATA_UNAVAILABLE')
            elif not data.get('research_authority') or not data.get('shadow_authority'):
                put('POSTGRES_READY','UNKNOWN','METADATA_AUTHORITY_UNVERIFIED')
            elif data['research_authority']!=data['shadow_authority']:
                put('POSTGRES_READY','BLOCKED','METADATA_AUTHORITY_MISMATCH')
            else:
                postgres=True;authority=data['research_authority']
                put('POSTGRES_READY','READY','SELECTED_RUNTIME_ROLE_AND_COMMON_AUTHORITY_PROBED',authority=authority)
        except Exception:
            put('POSTGRES_READY','BLOCKED','METADATA_PROBE_FAILED')
    put('API_READY','READY' if postgres and auth_verified else 'BLOCKED' if checks['POSTGRES_READY']['status']=='BLOCKED' else 'UNKNOWN',
        'API_AUTH_AND_SELECTED_METADATA_PROBED' if postgres and auth_verified else 'REQUIRED_METADATA_OR_AUTH_UNVERIFIED',authority=authority)
    for kind in ('research','shadow'):
        name=kind.upper()+'_WORKER_READY';path=(worker_paths or {}).get(kind)
        if kind=='shadow' and not shadow_enabled:
            put(name,'BLOCKED','SHADOW_NOT_ENABLED_OR_AUTHORIZED');continue
        if not path or not Path(path).is_file():put(name,'UNKNOWN','WORKER_HEARTBEAT_UNAVAILABLE');continue
        try:
            path=Path(path)
            if path.is_symlink() or path.stat().st_size>16*1024:raise ValueError('INVALID_HEARTBEAT')
            record=json.loads(path.read_text())
            at=datetime.fromisoformat(record['recorded_at']);age=(now-at).total_seconds()
            if record.get('schema_version')!='shadow-worker-health.v1' or not at.tzinfo:raise ValueError('INVALID_HEARTBEAT')
            if not 0<=age<=20:put(name,'BLOCKED','STALE_WORKER_HEARTBEAT');continue
            worker_authority=record.get('metadata_authority_id')
            if not worker_authority:put(name,'UNKNOWN','WORKER_AUTHORITY_UNVERIFIED');continue
            if not postgres or worker_authority!=authority:
                put(name,'BLOCKED','WORKER_AUTHORITY_MISMATCH');continue
            good=(record.get('readiness')=='READY' and record.get('database_status')=='AVAILABLE'
                  and record.get('backend')=='postgresql' and record.get('execution_runtime_loaded') is False)
            put(name,'READY' if good else 'BLOCKED','RECENT_TRUSTED_HEARTBEAT' if good else 'WORKER_REPORTED_UNREADY',
                source='READ_ONLY_SUPERVISOR_MOUNT',authority=authority,reference=kind+'-health.v1',
                heartbeat_at=at.isoformat(),process_verification='RECENT_SELF_REPORT; NOT_EXTERNAL_PID_CERTIFICATION')
        except (OSError,ValueError,TypeError,KeyError):put(name,'UNKNOWN','WORKER_HEARTBEAT_UNVERIFIABLE')
    put('FEED_READY','BLOCKED','NO_AUTHORIZED_SUSTAINED_FEED_OR_CROSS_PROCESS_PROOF')
    put('FINANCIAL_EVIDENCE_READY','UNKNOWN','COHORT_SPECIFIC_COMMITTED_RECONCILIATION_REQUIRED')
    if not spool_path or not Path(spool_path).is_file():
        put('OPERATIONAL_SPOOL_READY','UNKNOWN','EXISTING_DURABLE_SPOOL_UNAVAILABLE')
    else:
        try:
            path=Path(spool_path)
            if path.is_symlink():raise ValueError('SPOOL_NAMESPACE')
            with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1) as db:
                counts=dict(db.execute('SELECT status,COUNT(*) FROM spool_events WHERE owner_id=? GROUP BY status',(owner_id,)))
                failures=db.execute('SELECT COALESCE(SUM(count),0) FROM spool_failures WHERE owner_id=?',(owner_id,)).fetchone()[0]
            blocked=failures or counts.get('CORRUPT') or counts.get('CONFLICT')
            status='BLOCKED' if blocked else 'DEGRADED' if counts.get('PENDING') else 'READY'
            put('OPERATIONAL_SPOOL_READY',status,'OWNED_SPOOL_FAILURE' if blocked else 'OWNED_REPLAY_PENDING' if status=='DEGRADED' else 'EXISTING_OWNED_SPOOL_READABLE',
                source='EXISTING_SQLITE_TRANSPORT_QUERY_ONLY; NOT_OFF_HOST_DURABILITY')
        except (OSError,ValueError,sqlite3.Error):put('OPERATIONAL_SPOOL_READY','BLOCKED','SPOOL_READ_FAILED')
    return {'schema_version':VERSION,'checks':checks,'observed_at':stamp,
        'scope':'READ_ONLY_DEPLOYMENT_DIAGNOSTICS; NOT_STRATEGY_PROFITABILITY',
        'production_certification':False,'execution_authorization':False}
