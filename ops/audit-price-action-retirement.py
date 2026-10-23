"""Read-only PA retirement inventory for the existing staging and production APIs.

Prints selected metadata only. No application import, secrets, financial rows,
service change, backup, deletion, database checkpoint or vacuum is performed.
"""
import json
import os
import subprocess
from datetime import datetime, timezone

PROJECTS = {'staging': ('nexus-remote-stage-rc3-28acdbc', 'api'),
            'production': ('nexus-trading-bot', 'app')}

INSPECT_CODE = r'''
from pathlib import Path
import json,os,sqlite3,stat,time,urllib.request
root=Path('/var/lib/tradexa'); results=[]
protected={Path(os.environ.get(key,str(root/name))).resolve() for key,name in (
 ('HUB_ACCOUNT_DB','account.db'),('HUB_JOURNAL_DB','journal.db'),
 ('HUB_LEDGER_PATH','ledger.db'),('HUB_PAPER_BROKER_V2_DB','paper_broker_v2.db'),
 ('HUB_SMC_PAPER_DB','smc_strategy_paper.db'),('HUB_MARKET_DB','market_data.db'),
 ('HUB_SHADOW_RESEARCH_DB','shadow_research.db'),('HUB_DECISIONS_DB','decisions.db'))}
for key,name in (('HUB_PRICE_ACTION_PAPER_DB','price_action_paper.db'),
                 ('HUB_PRICE_ACTION_RESEARCH_DB','price_action_research.db')):
 p=Path(os.environ.get(key,str(root/name))); item={'binding':key,'path':str(p),'exists':p.exists()}
 if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
  item['status']='UNSAFE_PATH_REQUIRES_REVIEW';results.append(item);continue
 if p.resolve() in protected:
  item['status']='SHARED_PATH_MUST_NOT_DELETE';results.append(item);continue
 if not p.exists():
  item['status']='MISSING';results.append(item);continue
 info=p.stat()
 if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
  item['status']='NON_REGULAR_OR_HARDLINK_REQUIRES_REVIEW';results.append(item);continue
 item.update(bytes=info.st_size,allocated_bytes=info.st_blocks*512,uid=info.st_uid,gid=info.st_gid,
             mode=oct(stat.S_IMODE(info.st_mode)),inode=info.st_ino,device=info.st_dev)
 item['sidecars']=[{'path':str(q),'bytes':q.stat().st_size,'symlink':q.is_symlink()}
                  for q in (Path(str(p)+suffix) for suffix in ('-wal','-shm','-journal')) if q.exists()]
 try:
  deadline=time.monotonic()+5
  db=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
  try:
   db.execute('PRAGMA query_only=ON');db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
   item['page_size']=db.execute('PRAGMA page_size').fetchone()[0]
   item['page_count']=db.execute('PRAGMA page_count').fetchone()[0]
   item['journal_mode']=db.execute('PRAGMA journal_mode').fetchone()[0]
   objects=db.execute("SELECT type,name,tbl_name FROM sqlite_master ORDER BY type,name LIMIT 513").fetchall()
   if len(objects)>512:raise RuntimeError('SCHEMA_OBJECT_LIMIT')
   item['objects']=[dict(zip(('type','name','table'),r)) for r in objects]
   names={r[1] for r in objects if r[0]=='table'}
   if 'v2_account' in names:
    columns={r[1] for r in db.execute('PRAGMA table_info(v2_account)')}
    item['isolated_account_type']=db.execute('SELECT account_type FROM v2_account WHERE id=1').fetchone()[0] if 'account_type' in columns else 'UNVERIFIED'
   item['status']='READ_ONLY_METADATA; OWNERSHIP_AND_BACKUP_REVIEW_REQUIRED'
  finally:db.close()
 except Exception as exc:item.update(status='DATABASE_METADATA_UNVERIFIED',error_type=type(exc).__name__)
 results.append(item)
paths={r['path'] for r in results};opened=[];unreadable=0
for proc in Path('/proc').iterdir():
 if not proc.name.isdigit():continue
 try:
  for fd in (proc/'fd').iterdir():
   try:target=str(fd.readlink()).removesuffix(' (deleted)')
   except FileNotFoundError:continue
   if any(target==p or target.startswith(p+'-') for p in paths):
    opened.append({'pid':int(proc.name),'fd':fd.name,'path':target})
 except FileNotFoundError:continue
 except PermissionError:unreadable+=1
health={'status':'UNVERIFIED'}
try:
 with urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=5) as response:
  value=json.load(response)
 health={'status':value.get('status'),'features':value.get('features'),
         'acceptance_read_only':value.get('acceptance_read_only')}
except Exception as exc:health={'status':'UNVERIFIED','error_type':type(exc).__name__}
print(json.dumps({'configured_flag':os.environ.get('PRICE_ACTION_LAB_ENABLED','UNSET'),
 'databases':results,'open_pa_file_descriptors':opened,'unreadable_process_fd_directories':unreadable,
 'health':health,'financial_values_printed':False,'application_imported':False}))
'''


def docker(args, timeout=35):
    environment = {k: v for k, v in os.environ.items() if k not in
                   ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')}
    result = subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', *args],
                            capture_output=True, text=True, timeout=timeout, env=environment)
    if result.returncode:
        raise RuntimeError('AUDIT_DOCKER_OPERATION_FAILED:' + args[0])
    return result.stdout


def select_api(rows, project, service):
    candidates = [r for r in rows if r['Config'].get('Labels', {}).get('com.docker.compose.project') == project
                  and r['Config']['Labels'].get('com.docker.compose.service') == service]
    if len(candidates) != 1 or candidates[0]['State']['Status'] != 'running':
        raise RuntimeError('ONE_RUNNING_EXISTING_API_REQUIRED:' + project)
    return candidates[0]


def identity(rows):
    return sorted((r['Id'], r['Image'], r['State']['StartedAt'],
                   sorted((m['Source'], m['Destination'], m['RW']) for m in r['Mounts'])) for r in rows)


def collect(command=docker):
    ids = command(['ps', '-aq']).split()
    if not ids or len(ids) > 256:
        raise RuntimeError('BOUNDED_EXISTING_CONTAINER_INVENTORY_REQUIRED')
    rows = json.loads(command(['inspect', *ids]))
    before = identity(rows)
    reports = []
    for scope, (project, service) in PROJECTS.items():
        api = select_api(rows, project, service)
        mounts = [m for m in api['Mounts'] if m['Destination'] == '/var/lib/tradexa']
        if len(mounts) != 1:
            raise RuntimeError('ONE_EXISTING_STATE_MOUNT_REQUIRED:' + scope)
        mount = mounts[0]
        consumers = sorted(r['Name'] for r in rows if any(
            m['Source'] == mount['Source'] for m in r['Mounts']))
        details = json.loads(command(['exec', api['Id'], 'python', '-B', '-c', INSPECT_CODE]))
        samples = [json.loads(line) for line in command([
            'stats', '--no-stream', '--format', '{{json .}}', api['Id']]).splitlines() if line.strip()]
        reports.append({'scope': scope, 'container': api['Name'], 'container_id': api['Id'],
            'image': api['Image'], 'state_volume': mount.get('Name'),
            'containers_mounting_same_state': consumers, 'metadata': details,
            'resource_sample': [{k: v for k, v in row.items() if k in
                ('CPUPerc', 'MemUsage', 'MemPerc', 'BlockIO', 'PIDs')} for row in samples],
            'production_data_deletion_authorized': False,
            'exact_file_candidates': [d['path'] for d in details['databases'] if d['exists']],
            'cleanup_ready': False, 'backup_restore_proven': False})
    after = json.loads(command(['inspect', *ids]))
    if identity(after) != before:
        raise RuntimeError('CONTAINER_STATE_CHANGED_DURING_AUDIT')
    return {'status': 'READ_ONLY_PRICE_ACTION_RETIREMENT_AUDIT', 'environments': reports,
            'container_identities_and_mounts_unchanged': True, 'services_changed': False,
            'files_deleted': False, 'credentials_printed': False,
            'host_scheduler_audit': 'REQUIRED_BEFORE_ROLLOUT; NO_TRACKED_PA_SCHEDULE_FOUND',
            'at_utc': datetime.now(timezone.utc).isoformat()}


if __name__ == '__main__':
    try:
        print(json.dumps(collect(), indent=2))
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'reason': str(exc) if type(exc) is RuntimeError else type(exc).__name__,
                          'services_changed': False, 'files_deleted': False}))
        raise SystemExit(1)
