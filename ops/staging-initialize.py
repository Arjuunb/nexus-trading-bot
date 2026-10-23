"""One-shot disposable-local initialization, never a production migration."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0,'/app/automation-hub')


def main():
    if len(sys.argv)!=2:raise ValueError('EXPLICIT_LOCAL_ACTION_REQUIRED')
    if sys.argv[1]=='state':
        uid=int(os.environ['NEXUS_STAGING_UID'])
        if not 1<=uid<=65534:raise ValueError('NONROOT_STAGING_UID_REQUIRED')
        for name in ('api','research','shadow','datasets'):
            p=Path('/state')/name;p.mkdir(exist_ok=True)
            marker=p/'.nexus-staging-scope.json'
            if marker.exists() and json.loads(marker.read_text()).get('scope')!='DISPOSABLE_LOCAL_STAGING':
                raise ValueError('STAGING_NAMESPACE_CONFLICT')
            marker.write_text(json.dumps({'scope':'DISPOSABLE_LOCAL_STAGING','purpose':name}))
            os.chown(p,uid,uid);os.chown(marker,uid,uid)
        return
    if sys.argv[1]!='metadata':raise ValueError('INVALID_STAGING_ACTION')
    dsn=os.environ['NEXUS_STAGING_OPERATOR_DSN'];target=urlsplit(dsn)
    if target.hostname!='postgres' or target.path!='/nexus_staging_metadata':raise ValueError('DISPOSABLE_METADATA_TARGET_REQUIRED')
    import psycopg
    from psycopg import sql
    from services.postgres_workers import migrate_worker_database
    password=os.environ['NEXUS_STAGING_RUNTIME_PASSWORD']
    if len(password)<32:raise ValueError('GENERATED_RUNTIME_PASSWORD_REQUIRED')
    with psycopg.connect(dsn,autocommit=True) as db:
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname='nexus_staging_worker'").fetchone():
            db.execute(sql.SQL('CREATE ROLE nexus_staging_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}').format(sql.Literal(password)))
    migrate_worker_database(dsn,target='research_shadow_metadata',worker_role='nexus_staging_worker')
    print(json.dumps({'scope':'DISPOSABLE_LOCAL_STAGING','migration':'COMPLETE','runtime_role':'RESTRICTED'}))


if __name__=='__main__':main()
