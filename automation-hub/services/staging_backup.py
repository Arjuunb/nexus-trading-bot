"""Bounded consistent backups of explicitly disposable ORIGINAL SQLite stores.

Never used as a runtime ledger or financial repair. No production input accepted
without the separate operator workflow; this helper requires the staging marker.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def _proof(path):
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('SQLITE_BACKUP_INTEGRITY_FAILED')
        names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts={};rows={}
        for name in names:
            data=db.execute('SELECT * FROM "'+name.replace('"','""')+'"').fetchall()
            if len(data)>100000:raise ValueError('SQLITE_BACKUP_ROW_LIMIT')
            counts[name]=len(data)
            normalized=sorted(json.dumps(list(row),default=lambda v:{'bytes_hex':v.hex()},sort_keys=True) for row in data)
            rows[name]=hashlib.sha256(json.dumps(normalized).encode()).hexdigest()
        return {'row_counts':counts,'original_row_hashes':rows,'user_version':db.execute('PRAGMA user_version').fetchone()[0],
            'foreign_keys':'VERIFIED','integrity':'ok'}


def backup_sqlite_staging(source_root,destination):
    root=Path(source_root).resolve();marker=root/'.nexus-staging-scope.json'
    if not marker.is_file() or json.loads(marker.read_text()).get('scope')!='DISPOSABLE_LOCAL_STAGING':
        raise ValueError('DISPOSABLE_LOCAL_STAGING_SCOPE_REQUIRED')
    files=sorted({*root.glob('*.db'),*root.glob('*.sqlite'),*root.glob('*.sqlite3')})
    if not files or len(files)>32:raise ValueError('SQLITE_BACKUP_SOURCE_LIMIT')
    out=Path(destination);out.mkdir(parents=True,exist_ok=False);entries=[];total=0
    for path in files:
        if path.is_symlink():raise ValueError('SQLITE_BACKUP_SYMLINK')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            size=db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0];total+=size
            if total>256*1024*1024:raise ValueError('SQLITE_BACKUP_SIZE_LIMIT')
            with closing(sqlite3.connect(out/path.name)) as target:db.backup(target)
        proof=_proof(out/path.name)
        entries.append({'snapshot':path.name,'sha256':hashlib.sha256((out/path.name).read_bytes()).hexdigest(),**proof})
    manifest={'schema_version':'nexus_staging_backup.v1','scope':'DISPOSABLE_LOCAL_STAGING','method':'SQLITE_BACKUP_API',
        'financial_repair':False,'originals':entries}
    (out/'backup-manifest.json').write_text(json.dumps(manifest,indent=2));return manifest


def restore_sqlite_staging(backup_root,destination):
    root=Path(backup_root);m=json.loads((root/'backup-manifest.json').read_text())
    if m.get('scope')!='DISPOSABLE_LOCAL_STAGING':raise ValueError('DISPOSABLE_LOCAL_STAGING_SCOPE_REQUIRED')
    out=Path(destination);out.mkdir(parents=True,exist_ok=False)
    for row in m['originals']:
        name=row['snapshot'];p=Path(name)
        if p.name!=name or '..' in p.parts:raise ValueError('INVALID_SQLITE_SNAPSHOT_PATH')
        source=root/name
        if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest()!=row['sha256']:
            raise ValueError('SQLITE_BACKUP_HASH_MISMATCH')
        target=out/name;shutil.copy2(source,target)
        if _proof(target)!={k:row[k] for k in ('row_counts','original_row_hashes','user_version','foreign_keys','integrity')}:
            raise ValueError('SQLITE_RESTORE_REFERENCE_MISMATCH')
    return {'status':'VERIFIED_DISPOSABLE_LOCAL','restored_databases':len(m['originals']),'source_authority_changed':False}
