"""Collect bounded startup-intent evidence; never certify deployment readiness.

Uses Docker inspection and stdlib SQLite reads only. Does not import the app,
query a remote database, change a service, modify a strategy or activate a feed.
Operator output stays private; only this generic helper belongs in git.
"""
import hmac
import json
import re
import subprocess
import sys
from urllib.parse import urlsplit

STAGE='nexus-remote-stage-rc3-28acdbc'
PROD='nexus-trading-bot'
APPROVED_SOURCE='4eceb973e98e1350dd625153753637629453c401'


class Blocked(RuntimeError):
    pass


def canonical_project(value):
    """Only canonical public Supabase project origins prove project identity."""
    try:
        parsed=urlsplit(value.strip())
        host=(parsed.hostname or '').lower().rstrip('.')
        if (parsed.scheme.lower()!='https' or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None,443)
                or parsed.path not in ('','/') or parsed.query or parsed.fragment):
            return None
        match=re.fullmatch(r'([a-z0-9]{20})\.supabase\.co',host)
        return match.group(1) if match else None
    except (ValueError,AttributeError):
        return None


def provider_identity(stage,production):
    stage_pair=bool(stage.get('SUPABASE_URL')) and bool(stage.get('SUPABASE_KEY'))
    prod_pair=bool(production.get('SUPABASE_URL')) and bool(production.get('SUPABASE_KEY'))
    report={'stage_primary_pair_present':stage_pair,'production_primary_pair_present':prod_pair,
            'distinct_canonical_projects_proven':False,'same_canonical_project':None,
            'financial_credentials_equal':None,'remote_database_queries_performed':False,
            'effective_primary_instance_intent':'NOT_CHECKED'}
    if not stage_pair or not prod_pair:
        report['identity_status']='UNPROVEN_PRIMARY_CONFIGURATION_INCOMPLETE'
        return report
    first=canonical_project(stage['SUPABASE_URL']);second=canonical_project(production['SUPABASE_URL'])
    report['financial_credentials_equal']=hmac.compare_digest(
        stage['SUPABASE_KEY'].encode(),production['SUPABASE_KEY'].encode())
    if first is None or second is None:
        report['identity_status']='UNPROVEN_NONCANONICAL_PROJECT_ORIGIN'
        return report
    report['same_canonical_project']=first==second
    report['distinct_canonical_projects_proven']=first!=second
    report['identity_status']='SAME_PROJECT' if first==second else 'DISTINCT_CANONICAL_PROJECTS'
    return report


CHILD=r'''
from contextlib import closing
import json,os,resource,sqlite3,time
from pathlib import Path
resource.setrlimit(resource.RLIMIT_AS,(96*1024**2,96*1024**2))
root=Path('/var/lib/tradexa')
job_deadline=time.monotonic()+60
deadline=job_deadline

def progress():
    if time.monotonic()>deadline:raise RuntimeError('READ_ONLY_QUERY_TIME_BOUND_EXCEEDED')
    return 0

def state_path(variable,default):
    path=Path((os.environ.get(variable) if variable else None) or str(root/default))
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('EXPECTED_REGULAR_STAGING_STATE_FILE_REQUIRED')
    return path

def read_query(variable,default,table,columns,sql):
    global deadline
    deadline=min(job_deadline,time.monotonic()+3)
    try:
        path=state_path(variable,default)
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1,isolation_level=None)) as db:
            db.execute('PRAGMA query_only=ON');db.execute('PRAGMA cache_size=-2048')
            db.set_progress_handler(progress,10000)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() is None:
                return {'status':'TABLE_NOT_PRESENT'},[]
            found={row[1] for row in db.execute('PRAGMA table_info("'+table+'")')}
            if not set(columns)<=found:return {'status':'REQUIRED_COLUMNS_NOT_PRESENT'},[]
            rows=db.execute(sql).fetchall()
            return {'status':'READ_ONLY_QUERY_OK'},rows
    except Exception as error:
        return {'status':'UNVERIFIED','error_type':type(error).__name__,
                'sqlite_name':getattr(error,'sqlite_errorname',None)},[]

def lab(variable,default,prefix):
    result,rows=read_query(variable,default,prefix+'_sessions',('status','mode'),
        'SELECT mode FROM '+prefix+"_sessions WHERE status='active' LIMIT 2")
    if result['status']=='READ_ONLY_QUERY_OK':
        result.update(active_session_observed=bool(rows),multiple_active_sessions_observed=len(rows)>1,
            active_live_paper_observed=any(str(row[0])=='LIVE_PAPER' for row in rows),
            constructor_autoseed_requires_review=not rows,
            absence_of_new_paper_execution_proven=False)
    return result

def grid():
    try:
        path=state_path(None,'grid.json')
        if path.stat().st_size>1024**2:return {'status':'UNVERIFIED_FILE_SIZE_BOUND'}
        value=json.loads(path.read_text())
        if value is not None and not isinstance(value,dict):return {'status':'UNVERIFIED_JSON_TYPE'}
        return {'status':'LOCAL_JSON_READ_OK','running':bool((value or {}).get('running')),
                'remote_fallback_possible':False}
    except Exception as error:
        return {'status':'UNVERIFIED','error_type':type(error).__name__,'remote_fallback_possible':True}

report={'scope':'READ_ONLY_LOCAL_STARTUP_INTENT','application_imported':False,
        'financial_records_written':False,'remote_database_queries_performed':False,
        'services_updated':False,'new_paper_execution':False,'task6_complete':False}
try:
    configured=Path(os.environ.get('HUB_DATA_DIR') or '')
    if configured!=root:raise RuntimeError('EXISTING_API_STATE_ROOT_REQUIRED')
    memory_max=Path('/sys/fs/cgroup/memory.max');memory_stat=Path('/sys/fs/cgroup/memory.stat')
    if memory_max.is_file() and memory_stat.is_file():
        limit=memory_max.read_text().strip();stats=dict(line.split() for line in memory_stat.read_text().splitlines())
        resident=int(stats.get('anon','0'))+int(stats.get('kernel','0'))
        if limit.isdigit() and int(limit)-resident<192*1024**2:
            raise RuntimeError('API_MEMORY_HEADROOM_BELOW_192_MIB')
    report['price_action']=lab('HUB_PRICE_ACTION_PAPER_DB','price_action_paper.db','pa')
    report['smc']=lab('HUB_SMC_PAPER_DB','smc_strategy_paper.db','smc')
    report['grid']=grid()
    result,rows=read_query('HUB_LEDGER_PATH','ledger.db','trading_instances',('desired_running',),
                          'SELECT 1 FROM trading_instances WHERE desired_running<>0 LIMIT 1')
    if result['status']=='READ_ONLY_QUERY_OK':result['desired_instance_observed']=bool(rows)
    result['is_primary_authority']=not bool(os.environ.get('SUPABASE_URL') or os.environ.get('SUPABASE_KEY'))
    report['local_instances']=result
    result,rows=read_query('HUB_SHADOW_RESEARCH_DB','shadow_research.db','shadow_registrations',('desired_observing',),
                          'SELECT 1 FROM shadow_registrations WHERE desired_observing<>0 LIMIT 1')
    if result['status']=='READ_ONLY_QUERY_OK':result['desired_observation_observed']=bool(rows)
    result['postgres_or_other_effective_backend']='NOT_CHECKED'
    report['local_shadow']=result
    report['status']='LOCAL_EVIDENCE_COLLECTED_REMAINING_AUTHORITIES_UNVERIFIED'
except Exception as error:
    report.update(status='BLOCKED',reason=str(error) if isinstance(error,RuntimeError) else None,
                  error_type=type(error).__name__)
print(json.dumps(report))
'''


def docker(args,input_text=None):
    result=subprocess.run(['docker',*args],input=input_text,text=True,capture_output=True,timeout=90)
    if result.returncode:raise Blocked('DOCKER_READ_OPERATION_FAILED:'+args[0])
    return result.stdout


def project(project_name):
    ids=docker(['ps','-aq','--filter','label=com.docker.compose.project='+project_name]).split()
    if not ids:raise Blocked('EXPECTED_EXISTING_PROJECT_MISSING')
    rows=json.loads(docker(['inspect',*ids]))
    if any(row['Config'].get('Labels',{}).get('com.docker.compose.project')!=project_name for row in rows):
        raise Blocked('PROJECT_IDENTITY_MISMATCH')
    return rows


def selected(rows,service):
    found=[r for r in rows if r['Config'].get('Labels',{}).get('com.docker.compose.service')==service]
    if len(found)!=1:raise Blocked('ONE_EXISTING_'+service.upper()+'_REQUIRED')
    return found[0]


def environment(row):
    return dict(value.split('=',1) for value in row['Config'].get('Env',[]) if '=' in value)


def snapshot(rows):
    return sorted([(r['Id'],r['Image'],r['State']['StartedAt'],r['RestartCount'],r['Mounts']) for r in rows],key=lambda r:r[0])


def main():
    if sys.argv[1:]:raise Blocked('NO_SERVICE_OR_TARGET_OVERRIDES_ACCEPTED')
    before=project(STAGE);production_before=project(PROD)
    api=selected(before,'api');production_api=selected(production_before,'app')
    if api['State']['Status']!='running' or api['State'].get('Health',{}).get('Status')!='healthy':
        raise Blocked('HEALTHY_EXISTING_STAGING_API_REQUIRED')
    if api['HostConfig']['Memory']!=1610612736 or api['HostConfig']['MemorySwap']!=1610612736:
        raise Blocked('EXISTING_1536_MIB_API_MEMORY_AND_SWAP_OVERRIDE_REQUIRED')
    sources={m['Source'] for r in production_before for m in r['Mounts']}
    if any(m['Source'] in sources for r in before for m in r['Mounts']):
        raise Blocked('STAGING_AND_PRODUCTION_SHARE_STORAGE')
    env=environment(api)
    controls=('HUB_AUTO_ENGINE','HUB_ENABLE_EXTERNAL_LIVE','HUB_USE_LIVE_DATA',
              'HUB_RESEARCH_AUTOSTART','NEXUS_SHADOW_SUPERVISED')
    disabled={name:env.get(name)=='0' for name in controls}
    if not all(disabled.values()):raise Blocked('EXPECTED_DISABLED_EXECUTION_CONTROL_CHANGED')
    local=json.loads(docker(['exec','-i',api['Id'],'python','-B','-'],input_text=CHILD))
    after=project(STAGE);production_after=project(PROD)
    if snapshot(before)!=snapshot(after) or snapshot(production_before)!=snapshot(production_after):
        raise Blocked('CONTAINER_IDENTITY_OR_STORAGE_CHANGED_DURING_READ_ONLY_AUDIT')
    report={'status':'READ_ONLY_EVIDENCE_ONLY_DEPLOYMENT_NOT_AUTHORIZED_BY_THIS_REPORT',
            'approved_application_source':APPROVED_SOURCE,'provider':provider_identity(env,environment(production_api)),
            'disabled_controls_verified':disabled,'local_startup_intent':local,
            'api_health_after':selected(after,'api')['State'].get('Health',{}).get('Status'),
            'production_container_identities_unchanged':True,'shared_storage_detected':False,
            'services_updated':False,'new_paper_execution':False,'task6_complete':False,
            'remaining_checks':['effective primary trading-instance intent','remote grid fallback if applicable',
                                'effective shadow observation intent','startup financial reconciliation effects']}
    print(json.dumps(report,indent=2))
    return 0


if __name__=='__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({'status':'BLOCKED','error_type':type(error).__name__,
            'reason':str(error) if isinstance(error,Blocked) else None,
            'services_updated':False,'new_paper_execution':False,'task6_complete':False}))
        sys.exit(1)
