"""Bounded, redacted diagnosis of an owned failed PA snapshot.

Compares schemas and selected financial/control columns without changing a
database, claiming recovery passed, or printing customer records/credentials.
Samples are explicitly incomplete evidence. No application imports or feeds.
"""
import json
from pathlib import Path
import re
import subprocess
import sys

STAGE='nexus-remote-stage-rc3-28acdbc'
PROD='nexus-trading-bot'
BASE=Path('/home/ubuntu/nexus-rc3-staging-28acdbc')

CHILD=r'''
from contextlib import closing
import hashlib,json,os,resource,sqlite3,time
from pathlib import Path
resource.setrlimit(resource.RLIMIT_AS,(96*1024**2,96*1024**2))
ROOT=Path('/var/lib/tradexa')
token=os.environ['NEXUS_TASK6_DRIFT_TOKEN']
assert len(token)==32 and all(c in '0123456789abcdef' for c in token)
work=ROOT/('.task6-sqlite-recovery-'+token)
raw=work/'owned-snapshot.sqlite'
started=time.monotonic();deadline=started+3
MAX_ROWS=128
TABLES=('v2_account','v2_quote_cursor','v2_positions','v2_orders','v2_fills',
        'v2_funding_events','pa_sessions','pa_order_meta','pa_funding_events')

def guard():
    if time.monotonic()-started>60 or time.monotonic()>deadline:
        raise RuntimeError('READ_ONLY_DIAGNOSTIC_TIME_BOUND_EXCEEDED')

def progress():guard();return 0
def quote(value):return '"'+value.replace('"','""')+'"'

def open_ro(path):
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1,isolation_level=None)
    db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-2048')
    db.execute('PRAGMA temp_store=MEMORY')
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,1024**2)
    db.set_progress_handler(progress,10000)
    db.execute('BEGIN')
    return db

def schema(db):
    rows=db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    return {(r[0],r[1]):r[2:] for r in rows},db.execute('PRAGMA user_version').fetchone()[0]

def table_info(db,table):
    return db.execute('PRAGMA table_info('+quote(table)+')').fetchall()

def cell_hash(value):
    encoded=json.dumps(value,separators=(',',':'),ensure_ascii=True,
                       default=lambda b:{'bytes_hex':b.hex()}).encode()
    return hashlib.sha256(encoded).hexdigest()

def sample(db,table,columns,keys):
    order=','.join(quote(k) for k in keys) if keys else 'rowid'
    selectors=[quote(c) for c in columns]
    if not keys:selectors.insert(0,'rowid')
    where=" WHERE status='active'" if table=='pa_sessions' else ''
    cursor=db.execute('SELECT '+','.join(selectors)+' FROM '+quote(table)+where+
                      ' ORDER BY '+order+' LIMIT '+str(MAX_ROWS+1))
    result={};truncated=False
    for number,row in enumerate(cursor):
        guard()
        if number==MAX_ROWS:truncated=True;break
        values=dict(zip(columns,row if keys else row[1:]))
        key=cell_hash([values[k] for k in keys] if keys else row[0])
        result[key]={name:cell_hash(value) for name,value in values.items()}
    return result,truncated

def compare_table(live,old,table):
    global deadline
    deadline=min(started+60,time.monotonic()+3)
    try:
        left=table_info(live,table);right=table_info(old,table)
        if not left or not right:
            return {'status':'UNVERIFIED_TABLE_MISSING','live_present':bool(left),'snapshot_present':bool(right)}
        left_names={r[1] for r in left};right_names={r[1] for r in right}
        common=left_names&right_names
        excluded=sorted(c for c in common if c.endswith('_json') or 'payload' in c.lower())
        columns=sorted(common-set(excluded))
        first_keys=[r[1] for r in sorted(left,key=lambda r:r[5]) if r[5]]
        second_keys=[r[1] for r in sorted(right,key=lambda r:r[5]) if r[5]]
        if first_keys!=second_keys or not set(first_keys)<=set(columns):
            return {'status':'UNVERIFIED_PRIMARY_KEY_DIFFERENCE'}
        a,limited_a=sample(live,table,columns,first_keys)
        b,limited_b=sample(old,table,columns,first_keys)
        shared=a.keys()&b.keys()
        changed=sorted({name for key in shared for name in columns if a[key][name]!=b[key][name]})
        return {'status':'SAMPLED_DIFFERENCES_FOUND' if changed or a.keys()!=b.keys() else 'SELECTED_SAMPLE_COLUMNS_MATCH',
            'row_scope':'ACTIVE_SESSIONS_ONLY' if table=='pa_sessions' else 'PRIMARY_KEY_PREFIX',
            'maximum_sample_rows':MAX_ROWS,'live_sample_rows':len(a),'snapshot_sample_rows':len(b),
            'sample_truncated':limited_a or limited_b,'changed_columns':changed,
            'sampled_keys_only_in_live':len(a.keys()-b.keys()),'sampled_keys_only_in_snapshot':len(b.keys()-a.keys()),
            'excluded_json_or_payload_columns_not_compared':excluded,
            'live_only_columns':sorted(left_names-right_names),'snapshot_only_columns':sorted(right_names-left_names)}
    except Exception as error:
        return {'status':'UNVERIFIED','error_type':type(error).__name__,
                'sqlite_name':getattr(error,'sqlite_errorname',None)}

report={'scope':'READ_ONLY_BOUNDED_SNAPSHOT_DIFFERENCE_DIAGNOSIS',
        'recovery_passed':False,'complete_financial_evidence_proven':False,
        'database_writes_performed':False,'application_imported':False,
        'services_updated':False,'new_paper_execution':False,'task6_complete':False}
try:
    source=Path(os.environ.get('HUB_PRICE_ACTION_PAPER_DB') or str(ROOT/'price_action_paper.db'))
    if Path(os.environ.get('HUB_DATA_DIR') or '')!=ROOT:raise RuntimeError('EXISTING_API_STATE_ROOT_REQUIRED')
    if work.is_symlink() or not work.is_dir():raise RuntimeError('OWNED_SNAPSHOT_DIRECTORY_REQUIRED')
    for path in (source,raw,work/'ownership.json'):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError('REGULAR_STAGING_SOURCE_AND_OWNED_SNAPSHOT_REQUIRED')
    if source.resolve().is_relative_to(work.resolve()):raise RuntimeError('LIVE_SOURCE_MUST_NOT_BE_OWNED_COPY')
    marker=json.loads((work/'ownership.json').read_text())
    if marker!={'token':token,'purpose':'TASK6_OWNED_TEMPORARIES_ONLY'}:
        raise RuntimeError('OWNED_SNAPSHOT_MARKER_MISMATCH')
    memory_max=Path('/sys/fs/cgroup/memory.max');memory_stat=Path('/sys/fs/cgroup/memory.stat')
    if memory_max.is_file() and memory_stat.is_file():
        limit=memory_max.read_text().strip();stats=dict(line.split() for line in memory_stat.read_text().splitlines())
        resident=int(stats.get('anon','0'))+int(stats.get('kernel','0'))
        if limit.isdigit() and int(limit)-resident<192*1024**2:
            raise RuntimeError('API_MEMORY_HEADROOM_BELOW_192_MIB')
    deadline=time.monotonic()+3
    with closing(open_ro(source)) as live,closing(open_ro(raw)) as old:
        current,version=schema(live);previous,previous_version=schema(old)
        report['schema_matches']=current==previous and version==previous_version
        report['schema_objects_added_count']=len(current.keys()-previous.keys())
        report['schema_objects_removed_count']=len(previous.keys()-current.keys())
        report['schema_objects_changed_count']=sum(current[k]!=previous[k] for k in current.keys()&previous.keys())
        report['selected_table_diagnostics']={table:compare_table(live,old,table) for table in TABLES}
    report.update(status='DIAGNOSTIC_ONLY_RECOVERY_NOT_PASSED',
        source_file_bytes=source.stat().st_size,snapshot_file_bytes=raw.stat().st_size,
        elapsed_seconds=round(time.monotonic()-started,2))
except Exception as error:
    report.update(status='BLOCKED',error_type=type(error).__name__,
                  reason=str(error) if isinstance(error,RuntimeError) else None,
                  sqlite_name=getattr(error,'sqlite_errorname',None))
print(json.dumps(report))
'''


class Blocked(RuntimeError):pass


def docker(args,input_text=None):
    result=subprocess.run(['docker',*args],input=input_text,text=True,capture_output=True,timeout=90)
    if result.returncode:raise Blocked('DOCKER_READ_OPERATION_FAILED:'+args[0])
    return result.stdout


def project(name):
    ids=docker(['ps','-aq','--filter','label=com.docker.compose.project='+name]).split()
    if not ids:raise Blocked('EXPECTED_EXISTING_PROJECT_MISSING')
    rows=json.loads(docker(['inspect',*ids]))
    if any(r['Config'].get('Labels',{}).get('com.docker.compose.project')!=name for r in rows):
        raise Blocked('PROJECT_IDENTITY_MISMATCH')
    return rows


def identity(rows):
    return sorted([(r['Id'],r['Image'],r['State']['StartedAt'],r['RestartCount'],r['Mounts']) for r in rows],key=lambda r:r[0])


def main():
    if len(sys.argv)!=2:raise Blocked('EXISTING_PRIVATE_RECOVERY_DIRECTORY_ARGUMENT_REQUIRED')
    directory=Path(sys.argv[1])
    if directory.parent!=BASE or directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode&0o077:
        raise Blocked('EXISTING_PRIVATE_RECOVERY_DIRECTORY_REQUIRED')
    if not re.fullmatch(r'task6-sqlite-recovery-\d{8}T\d{6}Z-[0-9a-f]{12}',directory.name):
        raise Blocked('EXISTING_PRIVATE_RECOVERY_DIRECTORY_REQUIRED')
    job=directory/'job.json'
    if job.is_symlink() or not job.is_file() or job.stat().st_size>8192:
        raise Blocked('ORIGINAL_PRIVATE_JOB_RECORD_REQUIRED')
    config=json.loads(job.read_text());token=config.get('token','')
    if not re.fullmatch('[0-9a-f]{32}',token) or not directory.name.endswith('-'+token[:12]):
        raise Blocked('ORIGINAL_RECOVERY_TOKEN_REQUIRED')
    stage=project(STAGE);production=project(PROD)
    found=[r for r in stage if r.get('Name')=='/'+STAGE+'-api-1'
           and r['Config'].get('Labels',{}).get('com.docker.compose.service')=='api']
    if len(found)!=1 or found[0]['Id']!=config.get('api_id'):raise Blocked('ORIGINAL_STAGING_API_REQUIRED')
    api=found[0]
    if api['State']['Status']!='running' or api['State'].get('Health',{}).get('Status')!='healthy':
        raise Blocked('HEALTHY_EXISTING_STAGING_API_REQUIRED')
    if api['HostConfig']['Memory']!=1610612736 or api['HostConfig']['MemorySwap']!=1610612736:
        raise Blocked('EXISTING_1536_MIB_API_MEMORY_OVERRIDE_REQUIRED')
    sources={m['Source'] for r in production for m in r['Mounts']}
    if any(m['Source'] in sources for r in stage for m in r['Mounts']):raise Blocked('SHARED_PRODUCTION_STORAGE')
    result=json.loads(docker(['exec','-i','--env','NEXUS_TASK6_DRIFT_TOKEN='+token,api['Id'],'python','-B','-'],input_text=CHILD))
    if identity(stage)!=identity(project(STAGE)) or identity(production)!=identity(project(PROD)):
        raise Blocked('CONTAINER_IDENTITY_CHANGED_DURING_READ_ONLY_DIAGNOSIS')
    result['production_container_identities_unchanged']=True
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as error:
        print(json.dumps({'status':'BLOCKED','error_type':type(error).__name__,
            'reason':str(error) if isinstance(error,Blocked) else None,
            'services_updated':False,'new_paper_execution':False,'task6_complete':False}));sys.exit(1)
