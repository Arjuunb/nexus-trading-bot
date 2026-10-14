"""Offline release/design verification and owner-scoped operational diagnostics.

No SSH, Docker mutation, provider connection, financial connection or secret
inspection. Locally verified gates never certify an unknown remote target.
"""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path,PurePosixPath
import re


def sha256_file(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def verify_review_archive(directory):
    root=Path(directory).resolve();manifest=json.loads((root/'artifact-integrity.json').read_text())
    if len(manifest['files'])>10000:raise ValueError('ARCHIVE_FILE_LIMIT')
    seen=set()
    for item in manifest['files']:
        name=PurePosixPath(item['path'])
        if name.is_absolute() or '..' in name.parts or '\\' in str(name) or str(name) in seen:raise ValueError('ARCHIVE_PATH_INVALID')
        if any((root/Path(*name.parts[:n])).is_symlink() for n in range(1,len(name.parts)+1)):raise ValueError('ARCHIVE_PATH_SYMLINK')
        path=root/name
        if not path.is_file() or path.stat().st_size!=item['bytes'] or sha256_file(path)!=item['sha256']:raise ValueError('ARCHIVE_INTEGRITY_MISMATCH')
        seen.add(str(name))
    return {'review_id':manifest['review_id'],'verified_files':len(seen),'artifact_manifest_sha256':sha256_file(root/'artifact-integrity.json'),
        'verification':'LOCAL_BYTES_ONLY; NOT_HUMAN_APPROVAL_OR_OFF_HOST_DURABILITY'}


def validate_remote_profile(config):
    name=config.get('name','')
    if not re.fullmatch(r'nexus-remote-stage-[a-z0-9][a-z0-9-]{0,40}',name):raise ValueError('INDEPENDENT_PROJECT_REQUIRED')
    services=config.get('services',{})
    if not {'api','postgres','ingress','research-worker','state-init','metadata-init'}<=set(services):raise ValueError('REQUIRED_STAGING_SERVICES')
    if set(services)-{'api','postgres','ingress','research-worker','shadow-worker','state-init','metadata-init'}:raise ValueError('UNREVIEWED_SERVICE')
    for kind,entries in (('VOLUME',config.get('volumes',{})),('NETWORK',config.get('networks',{}))):
        for value in entries.values():
            if value.get('external') or value.get('name') and not value['name'].startswith(name+'_'):raise ValueError('EXTERNAL_OR_SHARED_'+kind)
    if not config.get('networks',{}).get('metadata',{}).get('internal'):raise ValueError('PRIVATE_METADATA_NETWORK_REQUIRED')
    for service,item in services.items():
        image=item.get('image','')
        if not re.fullmatch(r'(?:sha256:|[A-Za-z0-9_./:-]+@sha256:)[0-9a-f]{64}',image):raise ValueError('IMMUTABLE_IMAGE_REQUIRED')
        if item.get('privileged') or item.get('network_mode')=='host':raise ValueError('UNSAFE_RUNTIME_CAPABILITY')
        if service!='ingress' and item.get('ports'):raise ValueError('DATABASE_OR_API_PUBLIC_PORT_FORBIDDEN')
        for port in item.get('ports',[]):
            if port.get('host_ip')!='127.0.0.1':raise ValueError('LOOPBACK_INGRESS_REQUIRED')
            published=int(port.get('published',0))
            if published in {80,443,5432} or not 1024<=published<=65535:raise ValueError('PRODUCTION_PORT_OR_INVALID_PORT')
        networks=set(item.get('networks',{}))
        if service.endswith('worker') and networks!={'metadata'}:raise ValueError('WORKER_NETWORK_RESTRICTION')
        for volume in item.get('volumes',[]):
            target=volume['target']
            if volume['type']=='bind' and (service!='ingress' or not volume.get('read_only') or target not in {'/etc/nginx/nginx.conf','/run/fixture'}):
                raise ValueError('STATE_BIND_OR_UNREVIEWED_MOUNT')
            if volume['type']=='volume' and volume.get('source') not in config.get('volumes',{}):raise ValueError('UNDECLARED_STATE_VOLUME')
        environment=item.get('environment',{})
        if service in {'research-worker','shadow-worker'} and set(environment)&{'HUB_PASSWORD','HUB_SECRET','HUB_CONTROL_KEY','SUPABASE_KEY','SUPABASE_SERVICE_ROLE_KEY','HUB_EXCHANGE_API_KEY','HUB_EXCHANGE_API_SECRET'}:
            raise ValueError('WORKER_CREDENTIAL_BOUNDARY')
    environment=services['api'].get('environment',{})
    for flag in ('HUB_AUTO_ENGINE','HUB_ENABLE_EXTERNAL_LIVE','HUB_USE_LIVE_DATA','HUB_RESEARCH_AUTOSTART','HUB_MONITOR_AGENT'):
        if str(environment.get(flag))!='0':raise ValueError('EXECUTION_MUST_REMAIN_DISABLED')
    return {'schema_version':'remote-staging-design.v1','status':'LOCAL_DESIGN_VALIDATED','project':name,
        'deployment_authorization':False,'remote_capacity':'UNKNOWN','customer_authentication':'UNKNOWN'}


def remote_readiness(*,owner_id,diagnostics=None,now=None):
    if not owner_id:raise PermissionError('AUTHENTICATED_OPERATOR_REQUIRED')
    now=now or datetime.now(timezone.utc);diagnostics=diagnostics or {};gates={}
    def gate(name,status,reason):gates[name]={'status':status,'reason':reason,'observed_at':now.isoformat()}
    for name in ('REMOTE_CAPACITY','REMOTE_PERMISSIONS','CUSTOMER_AUTHENTICATION','REMOTE_BACKUP_RESTORE','REMOTE_SUPERVISION','REMOTE_POSTGRES_AUTHORITY'):
        gate(name,'UNKNOWN','NO_AUTHORIZED_REMOTE_MEASUREMENT')
    gate('REMOTE_ACCESS_AUTHORIZATION','BLOCKED','SEPARATE_EXPLICIT_AUTHORIZATION_REQUIRED')
    gate('LIVE_FEED_SOAK','BLOCKED','AUTHORIZED_LIVE_PUBLIC_SOAK_NOT_RUN')
    gate('FINANCIAL_EVIDENCE','UNKNOWN','COHORT_SPECIFIC_AUTHORITATIVE_RECONCILIATION_REQUIRED')
    for name in ('BACKUP_FRESHNESS','DISK_CAPACITY','MEMORY_PRESSURE','WORKER_RESTARTS','MIGRATION_VERSION','RELEASE_IDENTITY'):
        gate(name,'UNKNOWN','TRUSTED_OWNER_SCOPED_DIAGNOSTIC_UNAVAILABLE')
    for key,record in diagnostics.items():
        if not isinstance(record,dict) or record.get('owner_id')!=owner_id:continue
        try:
            at=datetime.fromisoformat(record['recorded_at']);age=(now-at).total_seconds()
            if not at.tzinfo or age<0:continue
        except (ValueError,KeyError,TypeError):continue
        if key=='backup':
            gate('BACKUP_FRESHNESS','READY' if age<=86400 and record.get('verified') is True else 'BLOCKED',
                'RECENT_LOCAL_RESTORE_PROOF; NOT_OFF_HOST_CERTIFICATION' if age<=86400 and record.get('verified') is True else 'STALE_OR_UNVERIFIED_BACKUP')
        elif key=='resource' and age<=60:
            free=record.get('free_bytes')
            if isinstance(free,int) and free>=0:
                gate('DISK_CAPACITY','READY' if free>=4*1024**3 else 'BLOCKED','LOCAL_MEASUREMENT; 4_GIB_REVIEW_HEADROOM')
            pressure=record.get('memory_pressure')
            if isinstance(pressure,(int,float)) and 0<=pressure<=1:gate('MEMORY_PRESSURE','READY' if pressure<.9 else 'BLOCKED','LOCAL_SAMPLED_MEMORY_USAGE; NOT_HARD_ISOLATION')
        elif key=='worker' and age<=20:
            count=record.get('restart_count')
            if isinstance(count,int) and count>=0:
                gate('WORKER_RESTARTS','READY' if count<=2 else 'BLOCKED','LOCAL_SUPERVISOR_SAMPLE; NOT_UPTIME_CERTIFICATION')
        elif key=='migration' and record.get('version')=='workers.2':gate('MIGRATION_VERSION','READY','TRUSTED_LOCAL_WORKER_SCHEMA_PROBE')
        elif key=='release' and record.get('verified') is True and re.fullmatch('sha256:[0-9a-f]{64}',str(record.get('image_digest',''))):
            gate('RELEASE_IDENTITY','READY','TRUSTED_LOCAL_ARTIFACT_PROOF; NOT_SOURCE_BUILD_EQUIVALENCE_OR_APPROVAL')
    return {'schema_version':'nexus_remote_staging_readiness.v1','gates':gates,'remote_staging':'NOT_READY',
        'production_deployment':'NOT_READY','live_public_feed_soak':'NOT_RUN','execution_authorization':False,
        'deployment_authorization':False,'scope':'OPERATOR_DIAGNOSTICS; LOCAL_EVIDENCE_CANNOT_CERTIFY_REMOTE_TARGET'}


def read_operator_diagnostics(path):
    """Trusted configuration path only; never supplied by a client."""
    if not path:return {}
    p=Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size>64*1024:return {}
    try:
        value=json.loads(p.read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}
