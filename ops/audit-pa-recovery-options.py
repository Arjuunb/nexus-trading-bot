"""Read-only backup capacity, rollback-image and host-schedule evidence.

Requires the corrected audit companion in this directory. Never provisions,
backs up, restores, installs, imports the application, changes a service or
deletes a file. No configuration values or schedule commands are printed.
"""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

COMPANION_SHA256 = '074b3c9db10aa23366fa9f01a021b47a6d2fa017e67c48838437807e8c65d876'
COMPANION = Path(__file__).with_name('audit-price-action-retirement.py')
if COMPANION.is_symlink() or hashlib.sha256(COMPANION.read_bytes()).hexdigest() != COMPANION_SHA256:
    raise RuntimeError('PINNED_READ_ONLY_AUDIT_COMPANION_REQUIRED')
SPEC = importlib.util.spec_from_file_location('pa_inventory', COMPANION)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)

CHILD = r'''
import ast,hashlib,json,os,sqlite3,stat,time,urllib.request
from pathlib import Path
root=Path('/var/lib/tradexa');databases=[]
bindings=(('HUB_ACCOUNT_DB','account.db'),('HUB_JOURNAL_DB','journal.db'),
 ('HUB_LEDGER_PATH','ledger.db'),('HUB_PAPER_BROKER_V2_DB','paper_broker_v2.db'),
 ('HUB_PRICE_ACTION_PAPER_DB','price_action_paper.db'),
 ('HUB_PRICE_ACTION_RESEARCH_DB','price_action_research.db'),
 ('HUB_SMC_PAPER_DB','smc_strategy_paper.db'))
for key,name in bindings:
 p=Path(os.environ.get(key,str(root/name)));row={'binding':key,'path':str(p),'exists':p.exists()}
 if p.is_symlink() or not p.resolve().is_relative_to(root):
  row['status']='PATH_REVIEW_REQUIRED';databases.append(row);continue
 if p.exists():
  info=p.stat()
  if not stat.S_ISREG(info.st_mode):
   row['status']='REGULAR_FILE_REQUIRED';databases.append(row);continue
  row.update(bytes=info.st_size,allocated_bytes=info.st_blocks*512)
  row['wal_bytes']=Path(str(p)+'-wal').stat().st_size if Path(str(p)+'-wal').exists() else 0
  try:
   deadline=time.monotonic()+3
   db=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
   try:
    db.execute('PRAGMA query_only=ON');db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
    row['logical_bytes']=db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]
    row['status']='READ_ONLY_METADATA'
   finally:db.close()
  except Exception as exc:row.update(status='UNVERIFIED',error_type=type(exc).__name__)
 else:row['status']='MISSING; NO_REPLACEMENT_CREATED'
 databases.append(row)
backups=[]
for name in ('backups','verified-backup','verified-restore'):
 directory=root/name;row={'path':str(directory),'exists':directory.exists()}
 if directory.is_symlink():row['status']='SYMLINK_NOT_FOLLOWED';backups.append(row);continue
 if directory.is_dir():
  count=0;size=0;allocated=0;complete=True;deadline=time.monotonic()+3;snapshots=[]
  try:
   for base,dirs,files in os.walk(directory,followlinks=False):
    dirs[:]=sorted(d for d in dirs if not (Path(base)/d).is_symlink())
    if len(Path(base).relative_to(directory).parts)>=3:
     if dirs:complete=False
     dirs[:]=[]
    for filename in files:
     p=Path(base)/filename
     if p.is_symlink():complete=False;continue
     info=p.stat();count+=1;size+=info.st_size;allocated+=info.st_blocks*512
     if count>=4096 or time.monotonic()>deadline:complete=False;break
    if not complete:break
   for p in sorted(directory.iterdir(),reverse=True)[:12]:
    if p.is_dir() and not p.is_symlink():snapshots.append({'directory':p.name,'mtime_ns':p.stat().st_mtime_ns})
   row.update(files=count,bytes=size,allocated_bytes=allocated,inventory_complete=complete,newest_names=snapshots)
  except (OSError,ValueError) as exc:row.update(status='UNVERIFIED',error_type=type(exc).__name__)
 row['restore_proven_by_this_audit']=False;backups.append(row)
module=Path('/app/automation-hub/services/backup.py');policy={'status':'UNVERIFIED'}
if module.is_file() and not module.is_symlink() and module.stat().st_size<262144:
 try:
  data=module.read_bytes();tree=ast.parse(data);keep=None
  for node in tree.body:
   if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='KEEP' for t in node.targets):
    value=ast.literal_eval(node.value)
    if type(value) is int:keep=value
  policy={'module_sha256':hashlib.sha256(data).hexdigest(),'default_keep':keep,
          'status':'STATIC_SOURCE_ONLY; OPERATOR_OVERRIDE_NOT_PROVEN'}
 except (ValueError,SyntaxError):pass
health={'status':'UNVERIFIED'}
try:
 with urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=3) as response:
  value=json.load(response)
 health={'status':value.get('status'),'features':value.get('features')}
except Exception as exc:health['error_type']=type(exc).__name__
print(json.dumps({'databases':databases,'backup_directories':backups,'retention_policy':policy,
 'backup_related_environment_names':[k for k in sorted(os.environ) if any(t in k.upper() for t in ('BACKUP','RESTIC','BORG','RCLONE'))],
 'remote_primary_configured':bool(os.environ.get('SUPABASE_URL') and os.environ.get('SUPABASE_KEY')),
 'remote_primary_is_not_backup_evidence':True,'sqlite_runtime_version':sqlite3.sqlite_version,
 'health':health,'authenticated_shared_feature_acceptance':'PENDING; NOT_ATTEMPTED',
 'financial_rows_read':False,'application_imported':False,'backup_or_restore_performed':False}))
'''


def local_command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=8)
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ''


def schedule_summary(text):
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    pa = ('price_action', 'price-action', 'pa-replay', 'pa_lab')
    nexus = (*pa, 'nexus', 'tradexa')
    return {'noncomment_lines':len(lines),
            'explicit_pa_matches':sum(any(t in line.lower() for t in pa) for line in lines),
            'nexus_or_shared_matches':sum(any(t in line.lower() for t in nexus) for line in lines),
            'sha256':hashlib.sha256(text.encode()).hexdigest(), 'commands_printed':False}


def host_evidence():
    mounts=[]
    code, output = local_command(['findmnt','--json','--output','TARGET,FSTYPE,MAJ:MIN'])
    def visit(rows):
        for row in rows:
            target=row.get('target','');fstype=row.get('fstype','')
            if fstype in {'ext4','ext3','xfs','btrfs','zfs'} and not target.startswith(('/proc/','/sys/','/dev/')):
                item={'target':target,'fstype':fstype,'device':row.get('maj:min')}
                try:
                    space=shutil.disk_usage(target)
                    item.update(total_bytes=space.total,free_bytes=space.free)
                except OSError:item['capacity']='UNREADABLE'
                mounts.append(item)
            visit(row.get('children',[]))
    if code==0:
        try:visit(json.loads(output).get('filesystems',[]))
        except ValueError:mounts=[]
    block_code,block_output=local_command(['lsblk','--json','--bytes','--output','NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS'])
    devices=[]
    if block_code==0:
        try:devices=json.loads(block_output).get('blockdevices',[])
        except ValueError:pass
    schedules=[]
    for owner,args in [('current_user',['crontab','-l']),('root',['crontab','-u','root','-l'])]:
        code,content=local_command(args)
        schedules.append({'source':owner+'_crontab','status':'READ' if code==0 else 'UNVERIFIED',
                          **(schedule_summary(content) if code==0 else {})})
    paths=[Path('/etc/crontab')]
    for directory in ('/etc/cron.d','/etc/cron.hourly','/etc/cron.daily','/etc/cron.weekly','/etc/cron.monthly',
                      '/etc/systemd/system',str(Path.home()/'.config/systemd/user')):
        root=Path(directory)
        try:
            paths.extend(sorted(root.iterdir())[:256])
        except OSError:schedules.append({'source':directory,'status':'UNVERIFIED'})
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size>65536:continue
            if path.parent.name in {'system','user'} and path.suffix not in {'.service','.timer'}:continue
            summary=schedule_summary(path.read_text())
            if summary['nexus_or_shared_matches']:
                schedules.append({'source':str(path),'status':'READ',**summary})
        except (OSError,UnicodeError):schedules.append({'source':str(path),'status':'UNVERIFIED'})
    timer_code,timers=local_command(['systemctl','list-timers','--all','--no-pager','--no-legend'])
    schedules.append({'source':'systemd_timer_listing','status':'READ' if timer_code==0 else 'UNVERIFIED',
                      **(schedule_summary(timers) if timer_code==0 else {})})
    return {'filesystems':mounts,'filesystems_query_status':'READ' if mounts else 'UNVERIFIED',
            'block_devices':devices,'block_device_inventory_status':'READ' if devices else 'UNVERIFIED',
            'installed_tool_names':[name for name in ('sqlite3_rsync','sqlite3','rsync','restic','borg','rclone','zfs','btrfs','lvs','lsblk') if shutil.which(name)],
            'schedules':schedules,'scheduler_coverage':'BOUNDED_TOKEN_SCAN; ROOT_ACCESS_AND_DYNAMIC_JOBS_NOT_PROVEN',
            'filesystem_or_provider_snapshot_support':'NOT_PROVEN; NO_SNAPSHOT_CREATED'}


def collect(command=AUDIT.docker, host=host_evidence):
    ids=command(['ps','-aq']).split()
    if not ids or len(ids)>256:raise RuntimeError('BOUNDED_EXISTING_CONTAINER_INVENTORY_REQUIRED')
    rows=json.loads(command(['inspect',*ids]));before=AUDIT.identity(rows);environments=[]
    for scope,(project,service) in AUDIT.PROJECTS.items():
        api=AUDIT.select_api(rows,project,service)
        mounts=[m for m in api['Mounts'] if m['Destination']=='/var/lib/tradexa']
        if len(mounts)!=1:raise RuntimeError('ONE_EXISTING_STATE_MOUNT_REQUIRED:'+scope)
        consumers=[{'container':r['Name'],'container_id':r['Id'],'status':r['State']['Status'],
                    'restart_policy':r.get('HostConfig',{}).get('RestartPolicy',{}).get('Name','UNVERIFIED')}
                   for r in rows if any(m['Source']==mounts[0]['Source'] for m in r['Mounts'])]
        details=json.loads(command(['exec',api['Id'],'python','-B','-c',CHILD]))
        image={'requested_image_id':api['Image'],'available':False}
        try:
            values=json.loads(command(['image','inspect',api['Image']]))
            if len(values)==1 and values[0].get('Id')==api['Image']:
                labels=values[0].get('Config',{}).get('Labels') or {}
                image.update(available=True,source_commit=labels.get('org.opencontainers.image.revision'),
                             source_sha256=labels.get('nexus.source.sha256'))
        except (RuntimeError,ValueError,TypeError):pass
        limits=api.get('HostConfig',{})
        environments.append({'scope':scope,'container_id':api['Id'],'image':image,
            'memory_limit_bytes':limits.get('Memory'),'memory_swap_limit_bytes':limits.get('MemorySwap'),
            'state_volume':mounts[0].get('Name'),'consumers':consumers,'metadata':details,
            'application_rollback_tested':False})
    host_report=host()
    if AUDIT.identity(json.loads(command(['inspect',*ids])))!=before:
        raise RuntimeError('CONTAINER_STATE_CHANGED_DURING_AUDIT')
    return {'status':'READ_ONLY_RECOVERY_OPTIONS_AUDIT','environments':environments,'host':host_report,
            'container_identities_and_mounts_unchanged':True,'services_changed':False,'files_deleted':False,
            'backup_restore_proven':False,'provisioning_performed':False,'credentials_printed':False,
            'at_utc':datetime.now(timezone.utc).isoformat()}


if __name__=='__main__':
    try:print(json.dumps(collect(),indent=2))
    except Exception as exc:
        print(json.dumps({'status':'BLOCKED','reason':str(exc) if type(exc) is RuntimeError else type(exc).__name__,
                          'services_changed':False,'files_deleted':False,'provisioning_performed':False}))
        raise SystemExit(1)
