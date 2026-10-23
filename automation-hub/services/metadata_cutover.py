"""Trusted operator LOCAL cutover rehearsal; never changes backend selection.

Only the explicit worker metadata tables are copied. Financial/journal source
tables are never imported. Source SQLite snapshots are frozen through backup,
verified and retained before target inspection. PostgreSQL import is atomic.
"""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from services.strategy_identity import canonical_configuration_json as encoded,configuration_fingerprint as digest

TABLES = (
    'research_jobs','research_job_attempts','research_job_events','strategy_validation_runs',
    'retained_research_datasets','retained_research_dataset_refs','strategy_research_candidates',
    'research_champion_registry','strategy_comparisons','research_campaigns','matched_experiments',
    'matched_experiment_candidates','matched_experiment_events','holdout_exposure_events',
    'research_agent_hypotheses','research_agent_approvals','research_agent_events',
    'shadow_registrations','shadow_observations','shadow_observation_events',
    'shadow_comparison_artifacts','shadow_comparison_requests','forward_qualification_policies',
    'forward_observation_windows','forward_qualification_reports','forward_qualification_requests',
    'forward_qualification_reviews','forward_qualification_events','operational_sources','operational_events',
    'paper_reconciliation_policies','paper_reconciliation_reports','paper_reconciliation_requests',
    'operational_retention_segments','local_readiness_assessments','paper_evidence_reviews')


def frozen_export(sources,backup_dir,*,max_rows=100000,max_bytes=128*1024*1024):
    if not 1<=len(sources)<=2:raise ValueError('CUTOVER_SOURCE_LIMIT')
    root=Path(backup_dir)
    if root.exists() and any(root.iterdir()):raise ValueError('CUTOVER_BACKUP_DIRECTORY_NOT_EMPTY')
    root.mkdir(parents=True,exist_ok=True)
    manifest={'schema':'metadata-cutover.v1','tables':{},'backups':[],'blockers':[],
        'backup_root':str(root.resolve()),'backend_authority_changed':False,'financial_records_imported':False}
    total=0;size=0;backup_bytes=0;seen_keys={}
    for index,path in enumerate(sources):
        path=Path(path).resolve()
        if not path.is_file():raise ValueError('CUTOVER_SOURCE_UNAVAILABLE')
        backup=root/f'source-{index}.sqlite'
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(backup)) as frozen:
            backup_bytes+=source.execute('PRAGMA page_count').fetchone()[0]*source.execute('PRAGMA page_size').fetchone()[0]
            if backup_bytes>256*1024*1024:raise ValueError('CUTOVER_BACKUP_SIZE_LIMIT')
            source.backup(frozen)
            if frozen.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or frozen.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('CUTOVER_SOURCE_INTEGRITY_FAILED')
            frozen.row_factory=sqlite3.Row
            present={r[0] for r in frozen.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            unsupported=sorted(n for n in present-set(TABLES) if n.startswith(('research_','matched_','shadow_observation','forward_qualification','holdout_','strategy_validation')) and frozen.execute(f'SELECT 1 FROM "{n}" LIMIT 1').fetchone())
            if unsupported:manifest['blockers'].append({'code':'UNSUPPORTED_LEGACY_METADATA','tables':unsupported})
            for table in TABLES:
                if table not in present:continue
                rows=[]
                for record in frozen.execute('SELECT * FROM '+table+' ORDER BY rowid LIMIT ?', (max_rows+1,)):
                    row=dict(record);total+=1;size+=len(encoded(row).encode())
                    if total>max_rows or size>max_bytes:raise ValueError('CUTOVER_RESOURCE_LIMIT')
                    rows.append(row)
                keys=[r['name'] for r in sorted(frozen.execute('PRAGMA table_info('+table+')'),key=lambda r:r['pk']) if r['pk']]
                if keys:
                    seen=seen_keys.setdefault(table,set())
                    for row in rows:
                        key=encoded([row[k] for k in keys])
                        if key in seen:manifest['blockers'].append({'code':'DUPLICATE_METADATA_IDENTITY','table':table})
                        seen.add(key)
                if table=='research_jobs' and any(r['status'] in {'CLAIMED','RUNNING','CANCEL_REQUESTED'} for r in rows):manifest['blockers'].append({'code':'ACTIVE_RESEARCH_LEASE','table':table})
                if table=='shadow_registrations' and any(r.get('desired_observing') or r.get('lease_token') for r in rows):manifest['blockers'].append({'code':'ACTIVE_SHADOW_REGISTRATION','table':table})
                for row in rows:
                    for key,value in row.items():
                        if key.endswith('_json') and value is not None:json.loads(value)
                    if table in {'research_jobs','strategy_research_candidates'}:
                        if row.get('config_fingerprint') and row.get('frozen_config_json') and digest(json.loads(row['frozen_config_json']))!=row['config_fingerprint']:
                            manifest['blockers'].append({'code':'FROZEN_CONFIGURATION_HASH_MISMATCH','table':table})
                    if table=='retained_research_datasets' and __import__('hashlib').sha256(row['candles_json'].encode()).hexdigest()!=row['content_hash']:
                        manifest['blockers'].append({'code':'DATASET_CONTENT_HASH_MISMATCH','table':table})
                manifest['tables'].setdefault(table,[]).extend(rows)
        manifest['backups'].append({'snapshot_file':backup.name,'sha256':__import__('hashlib').sha256(backup.read_bytes()).hexdigest(),
            'integrity':'VERIFIED','source_name':path.name})
    jobs={r['job_id']:r for r in manifest['tables'].get('research_jobs',[]) if r.get('job_id')}
    runs={r['run_id']:r for r in manifest['tables'].get('strategy_validation_runs',[]) if r.get('run_id')}
    for j in jobs.values():
        if j['status']=='COMPLETED' and (not j.get('result_reference') or j['result_reference'] not in runs):
            manifest['blockers'].append({'code':'COMMITTED_RESULT_REFERENCE_UNAVAILABLE','table':'research_jobs'})
    for reference in manifest['tables'].get('retained_research_dataset_refs',[]):
        if reference.get('job_id') not in jobs or jobs[reference['job_id']]['owner_id']!=reference['owner_id']:
            manifest['blockers'].append({'code':'DATASET_JOB_TENANT_REFERENCE_CONFLICT','table':'retained_research_dataset_refs'})
    if not total:manifest['blockers'].append({'code':'NO_SUPPORTED_METADATA_RECORDS_TO_REHEARSE'})
    manifest['source_hash']=digest(manifest['tables'])
    manifest['status']='BLOCKED' if manifest['blockers'] else 'PREFLIGHT_VALID'
    (root/'manifest.json').write_text(encoded(manifest))
    return manifest


def import_rehearsal(manifest,operator_dsn,*,dry_run=True):
    from psycopg import sql
    from services.postgres_workers import _connect,_financial_target
    from urllib.parse import urlsplit
    if urlsplit(operator_dsn).hostname not in {'localhost','127.0.0.1','::1'}:raise ValueError('LOCAL_CUTOVER_REHEARSAL_ONLY')
    if manifest.get('schema')!='metadata-cutover.v1' or digest(manifest['tables'])!=manifest['source_hash']:raise ValueError('CUTOVER_MANIFEST_HASH_MISMATCH')
    if manifest.get('blockers'):raise ValueError('CUTOVER_PREFLIGHT_BLOCKED')
    if set(manifest['tables'])-set(TABLES):raise ValueError('CUTOVER_TABLE_FORBIDDEN')
    for reference in manifest.get('backups',[]):
        if Path(reference['snapshot_file']).name!=reference['snapshot_file']:raise ValueError('CUTOVER_BACKUP_REFERENCE_INVALID')
        path=Path(manifest['backup_root'])/reference['snapshot_file']
        if not path.is_file() or path.is_symlink() or __import__('hashlib').sha256(path.read_bytes()).hexdigest()!=reference['sha256']:raise ValueError('CUTOVER_BACKUP_HASH_MISMATCH')
    # Archive reference rows alone do not preserve their external snapshots.
    if manifest['tables'].get('operational_retention_segments'):raise ValueError('CUTOVER_ARCHIVE_COPY_REHEARSAL_REQUIRED')
    report={'schema':'metadata-cutover-result.v1','source_hash':manifest['source_hash'],'dry_run':dry_run,
        'imported_rows':0,'existing_identical_rows':0,'table_hashes':{},'backend_authority_changed':False,
        'production_certification':False}
    class DryRunRollback(Exception):pass
    with _connect(operator_dsn) as db:
        try:
            with db.transaction():
                _financial_target(db)
                db.execute('SET search_path TO nexus_workers,pg_catalog')
                if not db.execute("SELECT 1 FROM worker_store_identity WHERE purpose='research_shadow_metadata'").fetchone():raise ValueError('CUTOVER_TARGET_UNAVAILABLE')
                db.execute('SELECT pg_advisory_xact_lock(1060003)')
                active=db.execute("SELECT COUNT(*) AS n FROM research_jobs WHERE status IN ('CLAIMED','RUNNING','CANCEL_REQUESTED')").fetchone()['n']
                observing=db.execute('SELECT COUNT(*) AS n FROM shadow_registrations WHERE desired_observing<>0 OR lease_token IS NOT NULL').fetchone()['n']
                if active or observing:raise ValueError('CUTOVER_TARGET_WORKERS_ACTIVE')
                for table,rows in manifest['tables'].items():
                    for row in rows:
                        columns=list(row)
                        keys=[r['column_name'] for r in db.execute("SELECT k.column_name FROM information_schema.table_constraints t JOIN information_schema.key_column_usage k USING(constraint_name,table_schema,table_name) WHERE t.table_schema='nexus_workers' AND t.table_name=%s AND t.constraint_type='PRIMARY KEY' ORDER BY k.ordinal_position",(table,))]
                        if not keys or any(k not in row for k in keys):raise ValueError('CUTOVER_KEY_UNAVAILABLE')
                        condition=sql.SQL(' AND ').join(sql.SQL('{} IS NOT DISTINCT FROM %s').format(sql.Identifier(k)) for k in keys)
                        old=db.execute(sql.SQL('SELECT * FROM {} WHERE {}').format(sql.Identifier(table),condition),[row[k] for k in keys]).fetchone()
                        if old:
                            if any(old.get(k)!=row[k] for k in columns):raise ValueError('CUTOVER_RESULT_CONFLICT')
                            report['existing_identical_rows']+=1
                        else:
                            db.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(table),
                                sql.SQL(',').join(map(sql.Identifier,columns)),sql.SQL(',').join(sql.Placeholder() for _ in columns)),[row[k] for k in columns])
                            report['imported_rows']+=1
                    verified=[]
                    for row in rows:
                        condition=sql.SQL(' AND ').join(sql.SQL('{} IS NOT DISTINCT FROM %s').format(sql.Identifier(k)) for k in row)
                        found=db.execute(sql.SQL('SELECT {} FROM {} WHERE {}').format(sql.SQL(',').join(map(sql.Identifier,row)),sql.Identifier(table),condition),list(row.values())).fetchone()
                        if not found:raise ValueError('CUTOVER_TARGET_HASH_MISMATCH')
                        verified.append(found)
                    report['table_hashes'][table]=digest(verified)
                    if report['table_hashes'][table]!=digest(rows):raise ValueError('CUTOVER_TARGET_HASH_MISMATCH')
                report['status']='LOCAL_VERIFIED'
                if dry_run:raise DryRunRollback()
        except DryRunRollback:pass
    report['validated_new_rows']=report['imported_rows']
    if dry_run:report['imported_rows']=0
    report['commitment_status']='DRY_RUN_ROLLED_BACK' if dry_run else 'COMMITTED'
    report['evidence_hash']=digest(report)
    return report


def main(argv=None):
    import argparse,os
    parser=argparse.ArgumentParser(description='LOCAL immutable metadata cutover rehearsal; no backend switching')
    parser.add_argument('--source',action='append',required=True)
    parser.add_argument('--backup-dir',required=True)
    parser.add_argument('--apply-disposable',action='store_true',help='Explicit local-only import; default is transaction rollback')
    args=parser.parse_args(argv)
    manifest=frozen_export(args.source,args.backup_dir)
    if manifest['blockers']:
        print(encoded({'status':'BLOCKED','blockers':manifest['blockers']}));return 2
    dsn=os.environ.get('NEXUS_LOCAL_CUTOVER_OPERATOR_DSN')
    if not dsn:print(encoded({'status':'PREFLIGHT_VALID','source_hash':manifest['source_hash'],'target':'NOT_CONFIGURED'}));return 0
    report=import_rehearsal(manifest,dsn,dry_run=not args.apply_disposable)
    print(encoded(report));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(encoded({'status':'BLOCKED','error_category':type(exc).__name__,'backend_authority_changed':False}));raise SystemExit(2)
