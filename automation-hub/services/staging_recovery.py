"""Seal explicitly selected non-financial backup artifacts in a NEW namespace.

Source SQLite/PG backups must already have been made by the existing consistent
backup tools. Integrity is not a successful DB restore or an off-host guarantee.
"""
from datetime import datetime,timezone
import json
from pathlib import Path,PurePosixPath
import shutil
import time
import uuid
from services.remote_staging import sha256_file


def _safe(name):
    p=PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or not name:raise ValueError('BACKUP_PATH_INVALID')
    if any(part.startswith('.env') for part in p.parts) or p.suffix in {'.key','.pem'}:raise ValueError('PRIVATE_BACKUP_INPUT_FORBIDDEN')
    if p.suffix in {'.db','.sqlite','.sqlite3'}:raise ValueError('FINANCIAL_OR_RAW_SQLITE_INPUT_REQUIRES_EXISTING_BACKUP_WORKFLOW')
    return p


def seal_recovery_bundle(source,destination,*,files,authority,max_bytes=256*1024*1024):
    start=time.monotonic();root=Path(source).resolve();out=Path(destination)
    if not str(authority).startswith('disposable:'):raise ValueError('DISPOSABLE_AUTHORITY_REQUIRED')
    if out.exists():raise ValueError('NEW_BACKUP_DESTINATION_REQUIRED')
    if not 1<=len(files)<=1000 or len(set(files))!=len(files):raise ValueError('BACKUP_FILE_LIMIT')
    if not 1<=max_bytes<=256*1024*1024:raise ValueError('BACKUP_BYTE_LIMIT')
    selected=[_safe(name) for name in files]
    temporary=out.with_name(out.name+'.'+uuid.uuid4().hex+'.partial');temporary.mkdir(parents=True,mode=0o700)
    entries=[];size=0
    for p in selected:
        original=root/p
        if any((root/Path(*p.parts[:n])).is_symlink() for n in range(1,len(p.parts)+1)):raise ValueError('BACKUP_PATH_SYMLINK')
        size+=original.stat().st_size
        if size>max_bytes:raise ValueError('BACKUP_BYTE_LIMIT')
        digest=sha256_file(original);target=temporary/p;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(original,target)
        if sha256_file(target)!=digest or sha256_file(original)!=digest:raise ValueError('BACKUP_INTEGRITY_CHANGED_DURING_COPY')
        entries.append({'path':str(p),'sha256':digest,'bytes':target.stat().st_size})
    manifest={'schema_version':'staging-recovery-bundle.v1','status':'COMPLETE','authority':authority,
        'created_at':datetime.now(timezone.utc).isoformat(),'files':entries,'copy_duration_seconds':time.monotonic()-start,
        'off_host_durability':False,'database_restore_verified':False,'financial_authority_changed':False}
    (temporary/'backup-manifest.json').write_text(json.dumps(manifest,indent=2));temporary.rename(out)
    return manifest


def verify_recovery_bundle(directory):
    root=Path(directory);m=json.loads((root/'backup-manifest.json').read_text())
    if m.get('schema_version')!='staging-recovery-bundle.v1' or m.get('status')!='COMPLETE':raise ValueError('INCOMPLETE_BACKUP')
    if not 1<=len(m['files'])<=1000:raise ValueError('BACKUP_FILE_LIMIT')
    seen=set();total=0
    for row in m['files']:
        p=_safe(row['path']);source=root/p
        if str(p) in seen or any((root/Path(*p.parts[:n])).is_symlink() for n in range(1,len(p.parts)+1)):raise ValueError('BACKUP_PATH_INVALID')
        seen.add(str(p));total+=source.stat().st_size
        if total>256*1024*1024 or source.stat().st_size!=row['bytes'] or sha256_file(source)!=row['sha256']:raise ValueError('BACKUP_INTEGRITY_MISMATCH')
    return {'status':'INTEGRITY_VERIFIED','files':len(seen),'off_host_durability':False,'database_restore_verified':False}
