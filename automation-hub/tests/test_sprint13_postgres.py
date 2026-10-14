"""Populated local fixtures, never production records or an authority switch."""
from datetime import datetime,timezone
import json
import sqlite3
import time
import pytest
from test_postgres_workers import pg,postgres_server,job,result
from data.journal_store import JournalStore
from services.metadata_cutover import frozen_export,import_rehearsal
from services.postgres_workers import PgResearchContext


def test_populated_multi_owner_cutover_committed_cancelled_expired_jobs(pg,tmp_path):
    source=JournalStore(tmp_path/'source.sqlite');ctx=source.context
    done=ctx.create_research_job(job('completed','alice'))
    claim=ctx.claim_research_job(worker_id='original',lease_seconds=60,max_active=1)
    ctx.transition_research_job(done['job_id'],to_status='RUNNING',worker_id='original',lease_token=claim['lease_token'])
    report,validation=result(done)
    ctx.commit_research_result(done['job_id'],worker_id='original',lease_token=claim['lease_token'],report=report,validation=validation)
    cancelled=ctx.create_research_job(job('cancelled','bob'));ctx.request_research_cancellation(cancelled['job_id'],owner_id='bob')
    interrupted=ctx.create_research_job(job('expired','bob'))
    claim=ctx.claim_research_job(worker_id='expired',lease_seconds=60,max_active=1)
    ctx.transition_research_job(interrupted['job_id'],to_status='RUNNING',worker_id='expired',lease_token=claim['lease_token'])
    with source._lock:
        source._c.execute("UPDATE research_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",(interrupted['job_id'],));source._c.commit()
    blocked=frozen_export([source.path],tmp_path/'blocked')
    assert any(x['code']=='ACTIVE_RESEARCH_LEASE' for x in blocked['blockers'])
    ctx.recover_expired_research_jobs()
    expected={j['job_id']:ctx.get_research_job(j['job_id']) for j in (done,cancelled,interrupted)}
    m=frozen_export([source.path],tmp_path/'frozen')
    assert m['status']=='PREFLIGHT_VALID'
    start=time.monotonic();x=import_rehearsal(m,pg[1],dry_run=False);duration=time.monotonic()-start
    target=PgResearchContext(pg[0])
    for jid,record in expected.items():assert target.get_research_job(jid)==record
    assert target.get_research_job(cancelled['job_id'])['status']=='CANCELLED'
    assert target.get_validation_run(validation['run_id'])['report']==report
    assert import_rehearsal(m,pg[1],dry_run=False)['imported_rows']==0
    assert x['backend_authority_changed'] is False
    (tmp_path/'cutover-measurement.json').write_text(json.dumps({'fixture_import_seconds':duration,'remote_rto':'UNKNOWN','row_hashes':x['table_hashes']}))


def test_missing_frozen_backup_blocks_before_pg_import(pg,tmp_path):
    source=JournalStore(tmp_path/'original.sqlite');j=source.context.create_research_job(job())
    m=frozen_export([source.path],tmp_path/'frozen')
    (tmp_path/'frozen/source-0.sqlite').unlink()
    with pytest.raises(ValueError,match='BACKUP_HASH'):import_rehearsal(m,pg[1],dry_run=False)
    assert PgResearchContext(pg[0]).get_research_job(j['job_id']) is None


def test_migration_rerun_preserves_metadata_and_role_restrictions(pg):
    import psycopg
    from services.postgres_workers import migrate_worker_database
    target=PgResearchContext(pg[0]);j=target.create_research_job(job('preserved'))
    migrate_worker_database(pg[1],target='research_shadow_metadata',worker_role='nexus_worker')
    assert target.get_research_job(j['job_id'])['config_fingerprint']==j['config_fingerprint']
    with psycopg.connect(pg[0]) as db:
        role=db.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
    assert role==(False,False,False,False)

