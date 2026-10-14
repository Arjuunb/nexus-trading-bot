#!/usr/bin/env python3
"""Offline by default. No SSH/deployment/provider/execution operation exists."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'automation-hub'))
from services.remote_staging import remote_readiness,verify_review_archive,validate_remote_profile


def local_host():
    disk=shutil.disk_usage('/workspace');st=os.statvfs('/workspace')
    memory={}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key,value=line.split(':',1)
        if key in {'MemTotal','MemAvailable'}:memory[key]=int(value.strip().split()[0])*1024
    result={'scope':'THIS_DISPOSABLE_CLOUD_HOST; NOT_VPS','os':platform.platform(),'cpus':os.cpu_count(),
        'disk_total_bytes':disk.total,'disk_free_bytes':disk.free,'free_inodes':st.f_favail,'memory':memory,
        'firewall':'UNKNOWN','remote_volume_permissions':'UNKNOWN','remote_tls':'UNKNOWN'}
    for key,cmd in [('docker',['docker','info','--format','{{json .}}']),('compose',['docker','compose','version','--short'])]:
        try:
            r=subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=10)
            if key=='docker':
                raw=json.loads(r.stdout);result[key]={k:raw.get(k) for k in ('ServerVersion','Driver','DockerRootDir','NCPU','MemTotal','ContainersRunning')}
            else:result[key]=r.stdout.strip()
        except (OSError,ValueError,subprocess.SubprocessError):result[key]='UNKNOWN'
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description='Read-only offline readiness; never deploys or connects to a VPS')
    p.add_argument('--release-archive',required=True);p.add_argument('--output',required=True)
    p.add_argument('--resolved-profile',help='Operator-generated local Compose JSON; values are never copied into report')
    p.add_argument('--collect-local-host',action='store_true')
    args=p.parse_args(argv);out=Path(args.output)
    if out.exists():raise ValueError('NEW_READINESS_REPORT_REQUIRED')
    report={'schema_version':'sprint13-offline-assessment.v1','created_at':datetime.now(timezone.utc).isoformat(),
        'previous_release':verify_review_archive(args.release_archive),'remote_measurements':'UNKNOWN',
        'live_soak':'NOT_RUN','deployment_authorization':False,'readiness':remote_readiness(owner_id='local-operator')}
    if args.resolved_profile:
        profile=Path(args.resolved_profile)
        if profile.is_symlink() or profile.stat().st_size>256*1024:raise ValueError('PROFILE_LIMIT')
        report['remote_design']=validate_remote_profile(json.loads(profile.read_text()))
    if args.collect_local_host:report['local_host']=local_host()
    out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2));out.chmod(0o600)
    print(json.dumps({'status':'PARTIAL','remote_staging':'NOT_READY','production':'NOT_READY','report':str(out)}));return 0


if __name__=='__main__':raise SystemExit(main())
