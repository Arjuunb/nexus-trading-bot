"""Bounded read-only inventory before choosing current financial recovery.

No application imports, service updates, backup, pause, stop or financial writes.
Only selected file/page sizes and configuration-presence booleans are printed.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess

BASE = Path('/home/ubuntu/nexus-rc3-staging-28acdbc')
STAGE = 'nexus-remote-stage-rc3-28acdbc'
API = STAGE + '-api-1'
WORKER = STAGE + '-research-worker-1'
EXPECTED_API = 'sha256:f2343e76a896ec3718328f3318adc3c2a40c93774194602858de4bdd4dce5397'
EXPECTED_WORKER = 'sha256:6cfd023a115f8985b6298e6fd1f476064b40fda8781d604175e4d6cde20e0cc8'

CHILD = r'''
import json, os, sqlite3, time
from pathlib import Path
ROOT = Path('/var/lib/tradexa')
files=[]
for key,default in (
 ('HUB_ACCOUNT_DB','account.db'),('HUB_JOURNAL_DB','journal.db'),
 ('HUB_PAPER_BROKER_V2_DB','paper_broker_v2.db'),
 ('HUB_PRICE_ACTION_PAPER_DB','price_action_paper.db'),
 ('HUB_SMC_PAPER_DB','smc_strategy_paper.db'),('HUB_LEDGER_PATH','ledger.db')):
 path=Path(os.environ.get(key,str(ROOT/default)))
 row={'binding':key}
 try:
  resolved=path.resolve()
  if not resolved.is_relative_to(ROOT.resolve()):
   row['status']='OUTSIDE_EXPECTED_API_VOLUME';files.append(row);continue
  row.update(path=str(resolved.relative_to(ROOT)),exists=resolved.is_file())
  if not row['exists']:
   row['status']='MISSING';files.append(row);continue
  row['bytes']=resolved.stat().st_size
  row['wal_bytes']=Path(str(resolved)+'-wal').stat().st_size if Path(str(resolved)+'-wal').is_file() else 0
  deadline=time.monotonic()+3
  with sqlite3.connect(resolved.as_uri()+'?mode=ro',uri=True,timeout=1) as db:
   db.execute('PRAGMA query_only=ON')
   db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
   page_size=db.execute('PRAGMA page_size').fetchone()[0]
   row.update(page_size=page_size,page_count=db.execute('PRAGMA page_count').fetchone()[0],
    freelist_pages=db.execute('PRAGMA freelist_count').fetchone()[0],
    journal_mode=db.execute('PRAGMA journal_mode').fetchone()[0],status='READ_ONLY_METADATA')
 except Exception as exc:
  row.update(status='UNVERIFIED',error_type=type(exc).__name__)
 files.append(row)
print(json.dumps({'files':files,'remote_primary_url_configured':bool(os.environ.get('SUPABASE_URL')),
 'remote_primary_key_configured':bool(os.environ.get('SUPABASE_KEY')),'financial_rows_read':False,
 'application_imported':False,'financial_recovery_proven':False}))
'''


def docker(args, timeout=25):
    result = subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', *args],
        capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Docker errors can include interpolated credentials. Never echo them.
        raise RuntimeError('READ_ONLY_DOCKER_OPERATION_FAILED:' + args[0])
    return result.stdout


def inspect(name):
    return json.loads(docker(['inspect', name]))[0]


def fingerprint(project):
    ids = docker(['ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project]).split()
    return sorted((row['Id'], row['Image'], sorted(
        (m.get('Name', m.get('Source')), m['Destination'], m['RW']) for m in row['Mounts']))
        for row in json.loads(docker(['inspect', *ids]))) if ids else []


def main():
    before = {p: fingerprint(p) for p in (STAGE, 'nexus-trading-bot')}
    api, worker = inspect(API), inspect(WORKER)
    if api['Image'] != EXPECTED_API or worker['Image'] != EXPECTED_WORKER:
        raise RuntimeError('EXPECTED_EXISTING_API_AND_DEPLOYED_WORKER_REQUIRED')
    for row in (api, worker):
        if row['State'].get('Health', {}).get('Status') != 'healthy':
            raise RuntimeError('HEALTHY_EXISTING_SERVICES_REQUIRED')
    if api['HostConfig']['Memory'] != 1610612736 or api['HostConfig']['MemorySwap'] != 1610612736:
        raise RuntimeError('API_1536_MIB_LIMITS_REQUIRED')
    data = [m for m in api['Mounts'] if m['Destination'] == '/var/lib/tradexa']
    if len(data) != 1 or data[0].get('Name') != STAGE + '_api-state':
        raise RuntimeError('EXISTING_ISOLATED_API_STATE_VOLUME_REQUIRED')
    result = json.loads(docker(['exec', API, 'python', '-B', '-c', CHILD]))
    if before != {p: fingerprint(p) for p in before}:
        raise RuntimeError('CONTAINER_IDENTITY_OR_MOUNT_CHANGED')
    print(json.dumps({'status':'READ_ONLY_FINANCIAL_INVENTORY_ONLY', **result,
        'free_disk_bytes':shutil.disk_usage(BASE).free,
        'api_image':api['Image'],'worker_image':worker['Image'],
        'production_and_staging_identities_unchanged':True,
        'service_updates':False,'task6_complete':False,
        'at_utc':datetime.now(timezone.utc).isoformat()}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        reason = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        print(json.dumps({'status':'BLOCKED','reason':reason,'service_updates':False,'task6_complete':False}))
        raise SystemExit(1)
