"""PostgreSQL metadata backend for the existing research/shadow interfaces.

No ledger connection or execution client is accepted. Migrations are an explicit
operator action; workers require an installed target and a least-privilege role.
"""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import json
import re
from pathlib import Path
import threading
from urllib.parse import urlsplit,parse_qs
import uuid

from data.journal_context_store import JournalContextStore
from services.shadow_observation_store import ShadowObservationStore
from services.strategy_identity import canonical_configuration_json as encoded

SCHEMA='nexus_workers'
PURPOSE='research_shadow_metadata'
ACTIVE={'CLAIMED','RUNNING','CANCEL_REQUESTED'}
FINANCIAL={'paper_trades','paper_positions','trade_decision_journal','orders','positions','balances','pa_sessions','smc_sessions','financial_journal'}


def validate_dsn(value):
    if not isinstance(value,str): raise ValueError('INVALID_WORKER_DSN')
    parsed=urlsplit(value)
    try:port=parsed.port
    except ValueError:raise ValueError('INVALID_WORKER_DSN') from None
    if parsed.scheme not in {'postgresql','postgres'} or not parsed.hostname or not parsed.username or not re.fullmatch(r'/[A-Za-z0-9_]+',parsed.path) or parsed.fragment:
        raise ValueError('INVALID_WORKER_DSN')
    if set(parse_qs(parsed.query))-{'sslmode','connect_timeout','application_name'}:raise ValueError('INVALID_WORKER_DSN_OPTIONS')
    if port is not None and not 1<=port<=65535:raise ValueError('INVALID_WORKER_DSN')
    return value


def _connect(dsn):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(validate_dsn(dsn),autocommit=True,row_factory=dict_row,connect_timeout=5)


def _financial_target(db):
    rows=db.execute("SELECT tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')").fetchall()
    if any(r['tablename'] in FINANCIAL or r['tablename'].startswith('v2_') for r in rows):
        raise ValueError('FINANCIAL_DATABASE_FORBIDDEN')


def migrate_worker_database(dsn, *, target, worker_role):
    from psycopg import sql
    if target!=PURPOSE or not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}',worker_role):raise ValueError('INVALID_MIGRATION_TARGET')
    with _connect(dsn) as db,db.transaction():
        _financial_target(db)
        db.execute('SELECT pg_advisory_xact_lock(1060001)')
        # The migration target is fixed code, never an API-selected path/schema.
        path=Path(__file__).parents[1]/'migrations/workers/0001_postgresql_worker_metadata.sql'
        db.execute(path.read_text())
        db.execute((path.parent/'0002_operational_resilience.sql').read_text())
        existing=db.execute('SELECT * FROM worker_store_identity').fetchall()
        if existing and any(r['purpose']!=PURPOSE or r['writer_role']!=worker_role or r['version']!=1 for r in existing):raise ValueError('WORKER_TARGET_CONFLICT')
        if not existing:db.execute('INSERT INTO worker_store_identity VALUES (%s,%s,1,%s)',(PURPOSE,uuid.uuid4().hex,worker_role))
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA nexus_workers TO {}').format(sql.Identifier(worker_role)))
        db.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA nexus_workers TO {}').format(sql.Identifier(worker_role)))
        db.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA nexus_workers TO {}').format(sql.Identifier(worker_role)))
        db.execute(sql.SQL('REVOKE INSERT,UPDATE ON worker_store_identity FROM {}').format(sql.Identifier(worker_role)))


class Row(dict):
    def __init__(self,names,values):super().__init__(zip(names,values));self.positions=values
    def __getitem__(self,key):return self.positions[key] if isinstance(key,int) else super().__getitem__(key)


class Cursor:
    def __init__(self,cursor):self.cursor=cursor
    @property
    def rowcount(self):return self.cursor.rowcount
    def fetchone(self):
        row=self.cursor.fetchone();return Row([c.name for c in self.cursor.description],row) if row is not None else None
    def fetchall(self):return [Row([c.name for c in self.cursor.description],r) for r in self.cursor.fetchall()]
    def __iter__(self):return iter(self.fetchall())


class PgMetadataHandle:
    """Small DBAPI binding seam; PostgreSQL coordination is explicit below.

    Only known metadata SQL is adapted (parameter style/null equality/time cast).
    SQLite DDL/PRAGMAs and locking commands are never executed on PostgreSQL.
    """
    backend='postgresql'
    def __init__(self,dsn):
        self._dsn=dsn;self._connection=_connect(dsn);self._lock=threading.RLock();self.depth=0
        self._c=self;self._db=self;self.journal=self
        try:self._validate_role()
        except BaseException:
            self._connection.close()
            raise

    def _validate_role(self):
        _financial_target(self._connection)
        role=self._connection.execute('SELECT current_user AS name,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
        if any(role[k] for k in ('rolsuper','rolcreatedb','rolcreaterole','rolbypassrls')):raise ValueError('UNSAFE_WORKER_DATABASE_ROLE')
        dangerous=self._connection.execute("SELECT bool_or(pg_has_role(current_user,rolname,'MEMBER')) AS unsafe FROM pg_roles WHERE rolname IN ('pg_read_server_files','pg_write_server_files','pg_execute_server_program','pg_read_all_data','pg_write_all_data','pg_signal_backend')").fetchone()
        if dangerous['unsafe']:raise ValueError('UNSAFE_WORKER_DATABASE_ROLE')
        self._connection.execute('SET search_path TO nexus_workers,pg_catalog')
        self._connection.execute("SET TIME ZONE 'UTC'")
        self._connection.execute("SET statement_timeout TO '10s'")
        self._connection.execute("SET lock_timeout TO '5s'")
        row=self._connection.execute('SELECT * FROM worker_store_identity WHERE purpose=%s',(PURPOSE,)).fetchone()
        if not row or row['writer_role']!=role['name'] or row['version']!=1:raise ValueError('WORKER_TARGET_UNAVAILABLE')
        if self._connection.execute("SELECT has_schema_privilege(current_user,'nexus_workers','CREATE') AS unsafe").fetchone()['unsafe']:raise ValueError('UNSAFE_WORKER_DATABASE_ROLE')
        self.authority_id=row['authority_id'];self.path='postgresql-worker-metadata:'+self.authority_id

    def execute(self,query,args=None):
        if not self.depth:self._ensure_connected()
        if re.search(r'\b(PRAGMA|sqlite_master|BEGIN IMMEDIATE|INSERT OR)\b',query,re.I):raise ValueError('SQLITE_OPERATION_ON_POSTGRESQL')
        query=re.sub(r'\bIS\s+\?', 'IS NOT DISTINCT FROM ?',query)
        query=re.sub(r'julianday\((\w+|\?)\)',r'CAST(\1 AS TIMESTAMPTZ)',query)
        if self.depth and re.search(r'^SELECT \* FROM (research_jobs|shadow_registrations) WHERE',query) and not re.search(r'FOR UPDATE',query,re.I):query+=' FOR UPDATE'
        if args is not None:query=query.replace('%','%%').replace('?','%s')
        from psycopg.rows import tuple_row
        return Cursor(self._connection.cursor(row_factory=tuple_row).execute(query,args))

    @contextmanager
    def transaction(self):
        with self._lock:
            if not self.depth:self._ensure_connected()
            with self._connection.transaction():
                self.depth+=1
                try:yield self
                finally:self.depth-=1

    def _ensure_connected(self):
        # Retry the next operation, never replay an uncertain mutation. Every
        # reconnect revalidates role and the original metadata authority.
        if not self._connection.closed:return
        with self._lock:
            if not self._connection.closed:return
            authority=self.authority_id
            self._connection=_connect(self._dsn)
            try:
                self._validate_role()
                if self.authority_id!=authority:raise ValueError('WORKER_AUTHORITY_MISMATCH')
            except BaseException:
                self._connection.close();self.authority_id=authority
                raise

    def clock(self):return self._connection.execute('SELECT clock_timestamp() AS now').fetchone()['now']
    def close(self):self._connection.close()


class PgResearchContext(JournalContextStore):
    def __init__(self,dsn):self.journal=PgMetadataHandle(dsn);self.backend='postgresql'

    def create_research_job(self,job):
        if job.get('backend_authority') not in {None,self.journal.authority_id}:raise ValueError('WORKER_AUTHORITY_MISMATCH')
        with self.journal.transaction() as db:
            db.execute('SELECT pg_advisory_xact_lock(1060002)')
            prior=db.execute('SELECT * FROM research_jobs WHERE owner_id=? AND idempotency_key=?',(job['owner_id'],job['idempotency_key'])).fetchone()
            if prior:
                original=json.loads(prior['request_json'])
                compare=lambda value:{k:v for k,v in value.items() if k not in {'job_id','requested_at','logical_result_key','backend_authority','max_attempts','progress'}}
                if encoded(compare(original))!=encoded(compare(job)):raise ValueError('RESEARCH_IDEMPOTENCY_CONFLICT')
                return self.get_research_job(prior['job_id'],owner_id=job['owner_id'])
            if db.execute('SELECT COUNT(*) FROM research_jobs WHERE owner_id=?',(job['owner_id'],)).fetchone()[0]>=200:raise ValueError('RESEARCH_OWNER_QUOTA')
            if db.execute('SELECT COUNT(*) FROM research_jobs').fetchone()[0]>=2000:raise ValueError('RESEARCH_GLOBAL_QUOTA')
            return super().create_research_job({**job,'backend_authority':self.journal.authority_id})

    def claim_research_job(self, *, worker_id,lease_seconds=60,max_active=1):
        if not worker_id or not 1<=max_active<=4:raise ValueError('INVALID_WORKER_LIMIT')
        with self.journal.transaction() as db:
            # A short quota lock bounds claims only; computation never holds it.
            db.execute('SELECT pg_advisory_xact_lock(1060003)')
            now=db.clock();stamp=now.isoformat()
            active=db.execute("SELECT COUNT(*) FROM research_jobs WHERE status IN ('CLAIMED','RUNNING','CANCEL_REQUESTED') AND lease_expires_at::timestamptz>clock_timestamp()").fetchone()[0]
            if active>=max_active:return None
            row=db.execute("SELECT * FROM research_jobs WHERE status IN ('QUEUED','RETRY_PENDING') AND attempt_number<max_attempts AND (retry_at IS NULL OR retry_at::timestamptz<=clock_timestamp()) ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
            if not row:return None
            token=uuid.uuid4().hex;attempt=row['attempt_number']+1;expiry=(now+timedelta(seconds=max(5,min(lease_seconds,300)))).isoformat()
            self._transition_job_locked(row,'CLAIMED',worker_id=worker_id,reason='postgres_worker_claimed',changes={
                'worker_id':worker_id,'lease_token':token,'attempt_number':attempt,'heartbeat_at':stamp,'lease_expires_at':expiry,'retry_at':None})
            db.execute('INSERT INTO research_job_attempts(attempt_id,job_id,attempt_number,worker_id,lease_token,status,claimed_at,heartbeat_at,lease_expires_at) VALUES (?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,row['job_id'],attempt,worker_id,token,'CLAIMED',stamp,stamp,expiry))
            return self.get_research_job(row['job_id'])

    def _owned(self,row,worker,token):
        if not row or row['worker_id']!=worker or row['lease_token']!=token or not row['lease_expires_at'] or self._job_dt(row['lease_expires_at'])<=self.journal.clock():
            raise ValueError('STALE_WORKER_FENCING_TOKEN')

    def transition_research_job(self,job_id, *, to_status,worker_id=None,lease_token=None,owner_id=None,reason=None,changes=None):
        with self.journal.transaction() as db:
            row=db.execute('SELECT * FROM research_jobs WHERE job_id=?',(job_id,)).fetchone()
            if not row or (owner_id is not None and row['owner_id']!=owner_id):raise KeyError(job_id)
            if row['status'] in ACTIVE:self._owned(row,worker_id,lease_token)
            self._transition_job_locked(row,to_status,worker_id=worker_id,lease_token=lease_token,reason=reason,changes=changes)
            if row['lease_token'] and to_status in {'RUNNING','CANCEL_REQUESTED','INTERRUPTED','CANCELLED','FAILED','TIMED_OUT'}:
                completed=db.clock().isoformat() if to_status in {'INTERRUPTED','CANCELLED','FAILED','TIMED_OUT'} else None
                db.execute('UPDATE research_job_attempts SET status=?,completed_at=COALESCE(?,completed_at) WHERE job_id=? AND lease_token=?',
                    (to_status,completed,job_id,row['lease_token']))
            return self.get_research_job(job_id)

    def heartbeat_research_job(self,job_id, *, worker_id,lease_token,lease_seconds=60):
        with self.journal.transaction() as db:
            row=db.execute('SELECT * FROM research_jobs WHERE job_id=?',(job_id,)).fetchone()
            try:self._owned(row,worker_id,lease_token)
            except ValueError:return False
            if row['status'] not in {'CLAIMED','RUNNING'}:return False
            stamp=db.clock();expiry=stamp+timedelta(seconds=max(5,min(lease_seconds,300)))
            db.execute('UPDATE research_jobs SET heartbeat_at=?,lease_expires_at=? WHERE job_id=?',(stamp.isoformat(),expiry.isoformat(),job_id))
            db.execute('UPDATE research_job_attempts SET heartbeat_at=?,lease_expires_at=? WHERE job_id=? AND lease_token=?',(stamp.isoformat(),expiry.isoformat(),job_id,lease_token))
            return True

    def commit_research_result(self,job_id, *, worker_id,lease_token,report,validation):
        if len(encoded(report).encode())>8*1024*1024:raise ValueError('RESEARCH_RESULT_SIZE_LIMIT')
        with self.journal.transaction() as db:
            row=db.execute('SELECT * FROM research_jobs WHERE job_id=?',(job_id,)).fetchone()
            if not row:raise KeyError(job_id)
            if row['status']=='COMPLETED':
                if row['worker_id']!=worker_id or row['lease_token']!=lease_token:raise ValueError('STALE_WORKER_FENCING_TOKEN')
                if encoded(json.loads(row['result_json']))!=encoded(report):raise ValueError('RESULT_COMMIT_CONFLICT')
                self.record_validation_run(validation)
                return self.get_research_job(job_id)
            self._owned(row,worker_id,lease_token)
            identity=('owner_id','strategy_id','strategy_version','config_fingerprint','account_id','instance_id','simulation_session_id')
            if row['status']!='RUNNING' or validation['run_id']!=row['logical_result_key'] or any(validation.get(k)!=row.get(k) for k in identity) or report.get('status')!='COMPLETE' or validation.get('status')!='COMPLETE' or validation.get('execution_mode')!='backtest':raise ValueError('RESULT_COMMIT_NOT_ELIGIBLE')
            return super().commit_research_result(job_id,worker_id=worker_id,lease_token=lease_token,report=report,validation=validation)

    def recover_expired_research_jobs(self, *, max_attempts=3):
        with self.journal.transaction() as db:
            rows=db.execute("SELECT * FROM research_jobs WHERE status IN ('CLAIMED','RUNNING','CANCEL_REQUESTED') AND lease_expires_at::timestamptz<=clock_timestamp() FOR UPDATE SKIP LOCKED").fetchall()
            for row in rows:
                stamp=db.clock().isoformat()
                if row['status']=='CANCEL_REQUESTED':
                    self._transition_job_locked(row,'CANCELLED',reason='cancelled_before_recovery',changes={'completed_at':stamp})
                else:
                    self._transition_job_locked(row,'INTERRUPTED',reason='lease_expired')
                    row['status']='INTERRUPTED'
                    exhausted=row['attempt_number']>=min(max_attempts,row['max_attempts'])
                    self._transition_job_locked(row,'FAILED' if exhausted else 'RETRY_PENDING',reason='retry_exhausted' if exhausted else 'postgres_recovery',changes={'retry_at':stamp,'error_class':'WORKER_INTERRUPTED'})
                db.execute('UPDATE research_job_attempts SET status=?,completed_at=? WHERE job_id=? AND lease_token=?',('CANCELLED' if row['status']=='CANCEL_REQUESTED' else 'INTERRUPTED',stamp,row['job_id'],row['lease_token']))
            return len(rows)


class PgShadowObservationStore(ShadowObservationStore):
    def __init__(self,dsn):self._store=PgMetadataHandle(dsn);self.backend='postgresql'
    @contextmanager
    def transaction(self):
        with self._store.transaction() as db:yield db

    def create(self,*args,**kwargs):
        with self.transaction() as db:
            db.execute('SELECT pg_advisory_xact_lock(1060004)')
            return super().create(*args,**kwargs)

    def claim(self,rid,worker_id, *, now,lease_seconds=60):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM shadow_registrations WHERE registration_id=? AND desired_observing=1 FOR UPDATE SKIP LOCKED',(rid,)).fetchone()
            server_now=db.clock().timestamp()
            if not row or (row['lease_expiry'] and row['lease_expiry']>server_now):return None
            return super().claim(rid,worker_id,now=server_now,lease_seconds=max(5,min(lease_seconds,300)))

    def heartbeat(self,rid,token, *, now,lease_seconds=60):
        with self.transaction() as db:return super().heartbeat(rid,token,now=db.clock().timestamp(),lease_seconds=max(5,min(lease_seconds,300)))

    def commit(self,decision, *, generation,lease_token=None,now,max_observations=100000):
        if lease_token is None:raise ValueError('POSTGRESQL_SHADOW_LEASE_REQUIRED')
        with self.transaction() as db:return super().commit(decision,generation=generation,lease_token=lease_token,now=db.clock().timestamp(),max_observations=max_observations)
