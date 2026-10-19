"""Trusted operator: freeze the entire reviewed source and build the real image.

No deployment, Git mutation or production target. Optional build CA is a public
trust bundle, never an API credential; BuildKit does not bake it into layers.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'automation-hub'))
from services.release_source import bundle_source,verify_build_inputs


def run(args,**kwargs):
    return subprocess.run(args,check=True,capture_output=True,text=True,**kwargs).stdout


def main(argv=None):
    parser=argparse.ArgumentParser(description='Build only; does not deploy or authorize a release')
    parser.add_argument('--output',required=True);parser.add_argument('--image',required=True)
    parser.add_argument('--public-build-ca');parser.add_argument('--build-network',choices=('default','host','none'),default='default')
    parser.add_argument('--verified-build-inputs',help='Owned retained public-package context; never an HTTP input')
    parser.add_argument('--no-cache',action='store_true',help='Reexecute all build instructions with pinned base/package inputs')
    args=parser.parse_args(argv)
    if not args.image.startswith('nexus-api:sprint12-'):raise ValueError('DISPOSABLE_LOCAL_IMAGE_REQUIRED')
    root=Path(__file__).resolve().parents[1]
    paths=run(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).split('\0')
    status=run(['git','status','--porcelain=v1','-uall'],cwd=root)
    dirty={row[3:]:row[:2] for row in status.splitlines()}
    head=run(['git','rev-parse','HEAD'],cwd=root).strip();out=Path(args.output)
    source=bundle_source(root,out,[p for p in paths if p],head=head,dirty_paths=dirty)
    stamp=datetime.now(timezone.utc).isoformat()
    command=['docker','build','--network='+args.build_network,'--iidfile',str(out/'image-id'),'-t',args.image,
        '--build-arg','GIT_COMMIT='+head,'--build-arg','GIT_BRANCH='+run(['git','branch','--show-current'],cwd=root).strip(),
        '--build-arg','DEPLOYED_AT='+stamp,'--build-arg','NEXUS_SOURCE_SHA256='+source['source_sha256']]
    if args.no_cache:command+=['--no-cache']
    if args.public_build_ca:command+=['--secret','id=build_ca,src='+str(Path(args.public_build_ca).resolve())]
    input_identity=None
    if args.verified_build_inputs:
        inputs=Path(args.verified_build_inputs).resolve()
        input_identity=verify_build_inputs(inputs)
        command+=['--build-context','build_inputs='+str(inputs)]
    command+=[str(out/'context')]
    log=out/'build.log'
    with log.open('w') as output:
        result=subprocess.run(command,stdout=output,stderr=subprocess.STDOUT)
    if result.returncode:
        (out/'release-manifest.json').write_text(json.dumps({'status':'BLOCKED','source_sha256':source['source_sha256'],
            'reason':'ACTUAL_DOCKERFILE_BUILD_FAILED','human_release_approval':False,'build_log':'build.log'},indent=2))
        return result.returncode
    image=run(['docker','image','inspect',args.image,'--format','{{.Id}}']).strip()
    inventory_code="import json,sys,importlib.metadata as m; import psycopg; print(json.dumps({'python':sys.version,'postgres_driver':psycopg.__version__,'pq_implementation':psycopg.pq.__impl__,'dependencies':{d.metadata['Name']:d.version for d in m.distributions()}}))"
    runtime=json.loads(run(['docker','run','--rm','--network','none','--read-only','--entrypoint','python',args.image,'-B','-c',inventory_code]))
    if not runtime['python'].startswith('3.11.') or runtime['pq_implementation']!='binary':raise ValueError('PRODUCTION_RUNTIME_MISMATCH')
    files=json.loads(run(['docker','run','--rm','--network','none','--read-only','--entrypoint','python',args.image,'-B','-c',
        "import pathlib,hashlib,json; print(json.dumps({str(p.relative_to('/app')):hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('/app/automation-hub/webui','/app/automation-hub/landing') for p in pathlib.Path(folder).rglob('*') if p.is_file()}))"]))
    migrations={r['path']:r['sha256'] for r in source['files'] if '/migrations/' in r['path'] and r['path'].endswith('.sql')}
    lock_hash=hashlib.sha256((out/'context/ops/api-requirements.lock').read_bytes()).hexdigest()
    manifest={'schema_version':'nexus_release_manifest.v1','status':'BUILD_VERIFIED','created_at':stamp,
        'head':head,'source_sha256':source['source_sha256'],'source_manifest':'source-manifest.json',
        'required_untracked_files':source['required_untracked_files'],'image':args.image,'image_digest':image,
        'runtime':runtime,'frontend_bundle_hashes':files,'migration_hashes':migrations,
        'dependency_lock_sha256':lock_hash,'configuration_schema_version':'nexus_deployment_readiness.v1',
        'base_image_references':sorted(set(re.findall(r'docker.io/library/(?:node:20-slim|python:3.11-slim)@sha256:[a-f0-9]{64}',log.read_text()))),
        'build_configuration':{'network':args.build_network,'no_cache':args.no_cache,'optional_public_ca_sha256':hashlib.sha256(Path(args.public_build_ca).read_bytes()).hexdigest() if args.public_build_ca else None},
        'retained_build_input_manifest_sha256':input_identity,
        'worker_backend':'postgresql','test_report_references':[], 'human_release_approval':False,
        'production_certification':False,'limitations':['BUILD_ONLY; REQUIRES_ORIGINAL_STAGING_TEST_REPORTS',
            'NO_BIT_IDENTICAL_IMAGE_GUARANTEE; BUILD_TIMESTAMP_AND_BASE_TAG_RESOLUTION_RECORDED_SEPARATELY']}
    (out/'release-manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True))
    print(json.dumps({'status':manifest['status'],'image_digest':image,'source_sha256':source['source_sha256'],'output':str(out),'deploy':False}))
    return 0


if __name__=='__main__':raise SystemExit(main())
