"""Opt-in LOCAL database/cgroup fixtures; no staging application is started."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

import pytest
import yaml

from test_postgres_workers import postgres_server, pg, job, claim, result

ROOT = Path(__file__).parents[2]
WORKER = 'sha256:6ef0a094f2aa599bc5411d7f151512df28f3974912b51996d90611c1216728a5'


def invoke(command, *, input=None):
    completed = subprocess.run(['docker', *command], input=input, capture_output=True, timeout=90)
    if completed.returncode:
        # Runtime fixture passwords must never appear in assertion diagnostics.
        pytest.fail('OWNED_LOCAL_FIXTURE_COMMAND_FAILED: ' + command[0])
    return completed.stdout


@pytest.mark.skipif(os.environ.get('NEXUS_STAGING_PREREQUISITE_TEST') != '1',
                    reason='requires opt-in disposable local initializer cgroup validation')
def test_actual_bounded_initializers_preserve_financial_fixture_and_create_restricted_metadata(postgres_server, tmp_path):
    profile = yaml.safe_load((ROOT / 'ops/staging.local.compose.yaml').read_text())['services']
    network = 'nexus-prereq-net-' + uuid.uuid4().hex[:10]
    names = []
    financial = tmp_path / 'original-financial-fixture.bin'
    financial.write_bytes(b'ORIGINAL_FINANCIAL_FIXTURE; NEVER_MOUNTED_IN_INITIALIZERS')
    original_hash = hashlib.sha256(financial.read_bytes()).hexdigest()
    server = postgres_server[2]
    invoke(['network', 'create', '--internal', '--label', 'purpose=disposable-local-prerequisite', network])
    invoke(['network', 'connect', '--alias', 'postgres', network, server])
    try:
        invoke(['exec', server, 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-c',
                'CREATE DATABASE nexus_staging_metadata'])
        proofs = {}
        for kind in ('state', 'metadata'):
            definition = profile[kind + '-init']
            name = 'nexus-prereq-' + kind + '-' + uuid.uuid4().hex[:10]
            names.append(name)
            command = ['create', '--name', name, '--label', 'purpose=disposable-local-prerequisite',
                       '--read-only', '--cpus', str(definition['cpus']), '--memory', definition['mem_limit'],
                       '--pids-limit', str(definition['pids_limit']), '--cap-drop', 'ALL',
                       '--security-opt', 'no-new-privileges:true', '--network', 'none' if kind == 'state' else network]
            if kind == 'state':
                command += ['--user', '0:0', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE',
                            '--tmpfs', '/state:rw,size=16m', '-e', 'NEXUS_STAGING_UID=1000']
            else:
                command += ['-e', 'NEXUS_STAGING_OPERATOR_DSN=postgresql://postgres@postgres:5432/nexus_staging_metadata',
                            '-e', 'NEXUS_STAGING_RUNTIME_PASSWORD=' + secrets.token_hex(32)]
            code = ("import sys,runpy,json,pathlib;sys.argv=['staging-initialize.py'," + repr(kind) + "];"
                    "runpy.run_path('/app/ops/staging-initialize.py',run_name='__main__');"
                    "print(json.dumps({'memory_max':pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip(),"
                    "'pids_max':pathlib.Path('/sys/fs/cgroup/pids.max').read_text().strip(),"
                    "'cpu_max':pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().strip()}))")
            command += ['--entrypoint', 'python', WORKER, '-B', '-c', code]
            invoke(command)
            started = time.monotonic()
            output = invoke(['start', '-a', name])
            values = json.loads(output.decode().splitlines()[-1])
            assert int(values['memory_max']) == 256 * 1024 * 1024
            assert int(values['pids_max']) == 64
            quota, period = map(int, values['cpu_max'].split())
            assert quota / period == .5
            proofs[kind] = {'duration_seconds': time.monotonic() - started, 'cgroup_limits': values}
        query = "SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname='nexus_staging_worker'"
        privileges = invoke(['exec', server, 'psql', '-U', 'postgres', '-d', 'nexus_staging_metadata', '-Atc', query]).decode().strip()
        assert privileges == 'f|f|f|f'
        identity = invoke(['exec', server, 'psql', '-U', 'postgres', '-d', 'nexus_staging_metadata', '-Atc',
                           "SELECT purpose,writer_role FROM nexus_workers.worker_store_identity"]).decode().strip()
        assert identity == 'research_shadow_metadata|nexus_staging_worker'
        assert hashlib.sha256(financial.read_bytes()).hexdigest() == original_hash
        evidence = os.environ.get('NEXUS_PREREQUISITE_EVIDENCE_DIR')
        if evidence:
            Path(evidence, 'initializer-cgroup-proof.json').write_text(json.dumps({
                'scope': 'OWNED_DISPOSABLE_LOCAL_FIXTURES', 'initializers': proofs,
                'restricted_role': True, 'metadata_purpose_verified': True,
                'original_financial_fixture_unchanged': True, 'vps_validation': False}, indent=2))
    finally:
        for name in names:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        subprocess.run(['docker', 'network', 'disconnect', network, server], capture_output=True)
        subprocess.run(['docker', 'network', 'rm', network], capture_output=True)


def test_real_postgres_restore_retains_dataset_bytes_references_and_one_logical_result(pg, postgres_server):
    import psycopg
    from psycopg import sql
    from bot.types import Bar
    from data.historical import canonical_candle_manifest
    from services.postgres_workers import PgResearchContext
    from services.research_dataset_retention import ResearchDatasetRetention
    context = PgResearchContext(pg[0])
    created = context.create_research_job(job(key='staging-backup-reference-fixture'))
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = [Bar(start + timedelta(minutes=i * 5), 10, 12, 9, 11, 100) for i in range(10)]
    manifest = canonical_candle_manifest(bars, symbol='TESTUSDT', timeframe='5m',
        provider='synthetic-test-fixture', venue='binance_usdm', market_type='perpetual')
    retained = ResearchDatasetRetention(context).retain(created, manifest, bars)
    attempt = claim(context)
    report, validation = result(created)
    report['retained_input_reference'] = retained['snapshot_id']
    validation['report'] = report
    context.commit_research_result(created['job_id'], worker_id='A', lease_token=attempt['lease_token'],
                                   report=report, validation=validation)
    original_authority = context.journal.authority_id
    original_job = context.get_research_job(created['job_id'])
    restore = 'nexus_prereq_restore_' + uuid.uuid4().hex[:10]
    server = postgres_server[2]
    dump = invoke(['exec', server, 'pg_dump', '-U', 'postgres', '--schema=nexus_workers', pg[1].rsplit('/', 1)[1]])
    with psycopg.connect(postgres_server[0], autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(restore)))
    try:
        invoke(['exec', '-i', server, 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-d', restore], input=dump)
        restored = PgResearchContext(pg[0].rsplit('/', 1)[0] + '/' + restore)
        recovered = ResearchDatasetRetention(restored).load(created['job_id'], owner_id='alice')
        assert recovered['bars'] == bars
        assert recovered['content_hash'] == retained['content_hash']
        assert recovered['snapshot_id'] == retained['snapshot_id']
        assert restored.journal.authority_id == original_authority
        assert restored.get_research_job(created['job_id']) == original_job
        assert restored.get_validation_run(validation['run_id']) == context.get_validation_run(validation['run_id'])
        assert len(restored.validation_runs(filters={'owner_id': 'alice'})) == 1
        assert context.journal.authority_id == original_authority
        assert context.get_research_job(created['job_id']) == original_job
    finally:
        with psycopg.connect(postgres_server[0], autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(restore)))
