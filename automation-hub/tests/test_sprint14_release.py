"""Actual clean-build candidate; no provider or remote deployment operations."""
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[2]
API = 'nexus-api:sprint12-sprint14-validation'
WORKER = 'nexus-workers:sprint12-sprint14-validation'
pytestmark = pytest.mark.skipif(
    os.environ.get('NEXUS_STAGING_TEST') != '1',
    reason='requires explicit disposable local clean-build candidate validation',
)


def image_python(image, code):
    result = subprocess.run(
        ['docker', 'run', '--rm', '--network', 'none', '--read-only',
         '--entrypoint', 'python', image, '-B', '-c', code],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_actual_api_retains_source_driver_bundles_and_excludes_private_inputs():
    proof = image_python(API, """
import hashlib, json, pathlib, sys, psycopg
root = pathlib.Path('/app')
manifest = json.loads((root / 'release-source.json').read_text())
rows = [r for r in manifest['files'] if r['path'].endswith(('.py', '.sql', '.lock', 'package-lock.json'))]
conflicts = [r['path'] for r in rows if hashlib.sha256((root / r['path']).read_bytes()).hexdigest() != r['sha256']]
bundles = {name: (root / 'automation-hub' / name / 'index.html').is_file() for name in ('webui', 'landing')}
private = [str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and (p.name.startswith('.env') or p.suffix in ('.db', '.sqlite', '.sqlite3'))]
print(json.dumps({'python': sys.version, 'driver': psycopg.pq.__impl__, 'source_files': len(rows), 'conflicts': conflicts, 'bundles': bundles, 'private_inputs': private}))
""")
    assert proof['python'].startswith('3.11.') and proof['driver'] == 'binary'
    assert proof['source_files'] > 500 and proof['conflicts'] == []
    assert proof['bundles'] == {'webui': True, 'landing': True}
    assert proof['private_inputs'] == []


def test_actual_worker_preserves_code_and_excludes_execution_entrypoints():
    source = image_python(API, """
import json, pathlib
print(json.dumps(json.loads(pathlib.Path('/app/release-source.json').read_text())['files']))
""")
    proof = image_python(WORKER, """
import hashlib, json, pathlib, psycopg
root = pathlib.Path('/app')
files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for folder in ('services', 'data', 'strategies', 'migrations') for p in (root / 'automation-hub' / folder).rglob('*') if p.is_file() and p.suffix in ('.py', '.sql')}
forbidden = [name for name in ('automation-hub/execution', 'automation-hub/app.py', 'automation-hub/webui', '.env') if (root / name).exists()]
print(json.dumps({'files': files, 'forbidden': forbidden, 'driver': psycopg.pq.__impl__}))
""")
    expected = {r['path']: r['sha256'] for r in source}
    assert len(proof['files']) > 200
    assert all(expected[name] == digest for name, digest in proof['files'].items())
    assert proof['forbidden'] == [] and proof['driver'] == 'binary'


def test_clean_candidate_https_auth_pg_recovery_and_financial_isolation(tmp_path):
    out = tmp_path / 'rehearsal'
    result = subprocess.run(
        [str(ROOT / '.venv/bin/python'), str(ROOT / 'ops/rehearse-staging.py'),
         '--output', str(out), '--api-image', API, '--worker-image', WORKER,
         '--run-disposable', '--exercise-shadow', '--browser-smoke', '--verify-remote-diagnostics'],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((out / 'rehearsal.json').read_text())
    checks = report['checks']
    for name in ('remote_diagnostics_fail_closed', 'financial_state_unchanged_after_recovery', 'postgres_restore_original_authority'):
        assert checks[name] == 'PASS'
    assert checks['research_supervised_crash_recovery']['restart_count'] == 1
    assert checks['shadow_supervised_crash_recovery']['restart_count'] == 1
    assert checks['actual_production_chromium_smoke']['passed_scenarios'] == 2
    assert report['remote_deployment'] is False
