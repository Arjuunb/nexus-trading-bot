"""Trusted local release input freezing. No HTTP/agent path or execution hook."""
import hashlib
import json
from pathlib import Path, PurePosixPath

VERSION='nexus_release_source.v1'
EXCLUSION_POLICY_VERSION='nexus_source_exclusion.v2'
EXCLUDED_PARTS={'.git','.venv','node_modules','__pycache__','.pytest_cache','dist','logs'}
GENERATED_RESEARCH_JOURNALS=frozenset({
    'strategy_v2_research_ledger.jsonl', 'strategy_v2_verdict_ledger.jsonl',
    'strategy_v3_research_ledger.jsonl',
})


def generated_research_journal(path):
    """Exclude only the reviewed generated names, including accidental copies."""
    return PurePosixPath(path).name in GENERATED_RESEARCH_JOURNALS


def excluded(path):
    return (generated_research_journal(path)
            or any(part in EXCLUDED_PARTS or part.startswith('.env') for part in path.parts)
            or path.name.endswith(('.db','.sqlite','.sqlite3','.db-wal','.db-shm','.sqlite-wal','.sqlite-shm')))


def verify_build_inputs(directory):
    root=Path(directory).resolve();manifest_path=root/'input-manifest.json'
    manifest=json.loads(manifest_path.read_text());declared=set();total=0
    if manifest.get('scope')!='PUBLIC_LOCKED_PACKAGES' or len(manifest['files'])>20000:
        raise ValueError('PUBLIC_BUILD_INPUT_SCOPE_REQUIRED')
    for row in manifest['files']:
        name=row['path'];p=PurePosixPath(name)
        valid=((p.parts and p.parts[0]=='python' and p.name.endswith('.whl'))
               or p.parts[:2]==('npm-cache','_cacache'))
        if not valid or p.is_absolute() or '..' in p.parts or name in declared:
            raise ValueError('PUBLIC_BUILD_INPUT_PATH_INVALID')
        path=root/p
        if any((root/Path(*p.parts[:i])).is_symlink() for i in range(1,len(p.parts)+1)):
            raise ValueError('PUBLIC_BUILD_INPUT_SYMLINK')
        total+=path.stat().st_size
        if total>512*1024*1024 or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise ValueError('PUBLIC_BUILD_INPUT_INTEGRITY_FAILED')
        declared.add(name)
    actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p!=manifest_path}
    if actual!=declared:raise ValueError('UNDECLARED_PUBLIC_BUILD_INPUT')
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def bundle_source(root,destination,paths,*,head,dirty_paths,max_bytes=128*1024*1024):
    """Freeze all reviewed tracked AND untracked input paths before building.

    The caller discovers source using read-only Git commands. Denied runtime
    state is never read. Corrections require a new destination, not overwrite.
    Source bytes, dependency locks and Dockerfile are retained together.
    """
    root=Path(root).resolve();destination=Path(destination)
    rows=[];denied=[];total=0
    for name in sorted(set(paths)):
        p=PurePosixPath(name)
        if not name or p.is_absolute() or '..' in p.parts or '\\' in name:
            raise ValueError('INVALID_SOURCE_PATH')
        if excluded(p):denied.append(name);continue
        source=root/p
        if any((root/Path(*p.parts[:i])).is_symlink() for i in range(1,len(p.parts)+1)):
            raise ValueError('SOURCE_SYMLINK_FORBIDDEN')
        if not source.is_file():raise ValueError('SOURCE_PATH_UNAVAILABLE')
        size=source.stat().st_size;total+=size
        if total>max_bytes or size>32*1024*1024:raise ValueError('SOURCE_SIZE_LIMIT')
        payload=source.read_bytes()
        if len(payload)!=size:raise ValueError('SOURCE_CHANGED_DURING_FREEZE')
        rows.append((name,payload,0o755 if source.stat().st_mode&0o111 else 0o644))
    content=[{'path':name,'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload),'mode':mode}
             for name,payload,mode in rows]
    identity=hashlib.sha256(json.dumps(content,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    destination.mkdir(mode=0o700,parents=True,exist_ok=False);context=destination/'context';context.mkdir()
    for name,payload,mode in rows:
        target=context/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload);target.chmod(mode)
    manifest={'schema_version':VERSION,'source_exclusion_policy':EXCLUSION_POLICY_VERSION,
        'head':head,'source_sha256':identity,'files':content,
        'working_tree':dict(sorted(dirty_paths.items())),
        'required_untracked_files':[row['path'] for row in content if dirty_paths.get(row['path'])=='??'],
        'excluded_files':denied,'human_release_approval':False,
        'reproducibility':'EXACT_RETAINED_SOURCE_AND_LOCKS; NOT_BIT_IDENTICAL_IMAGE_CERTIFICATION'}
    text=json.dumps(manifest,indent=2,sort_keys=True)
    (destination/'source-manifest.json').write_text(text)
    (context/'release-source.json').write_text(text)
    return manifest
