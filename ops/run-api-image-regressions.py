#!/usr/bin/env python3
"""Fixed native API test matrix in a local immutable image; no application server."""
import argparse
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    'test_auth.py', 'test_auth_flows.py', 'test_auth_endpoints.py', 'test_auth_accounts.py',
    'test_jwt_auth.py', 'test_oauth.py', 'test_supabase_auth_boundary.py', 'test_phase0_security.py',
    'test_security_headers.py', 'test_strategy_intelligence_api.py', 'test_active_evidence_api.py',
    'test_shadow_comparison_api.py', 'test_forward_qualification_api.py', 'test_integrity_operations_api.py',
    'test_atomic_paper_ledger.py', 'test_account_persistence.py', 'test_paper_evidence_outbox.py',
    'test_native_paper_evidence.py', 'test_paper_evidence_reader.py', 'test_paper_reconciliation.py',
    'test_simulation_account_restart.py', 'test_risk_engine_parity.py', 'test_strategy_simulator_parity.py',
    'test_smc_pro_parity_audit.py', 'test_smc_pro_parity_export.py', 'test_strategy_qualification.py',
    'test_historical_integrity.py', 'test_research_jobs.py', 'test_research_agent.py', 'test_shadow_router.py',
    'test_operational_spool.py', 'test_staging_prerequisites.py',
    'test_acceptance_read_only.py', 'test_reviewed_acceptance_delta.py',
)
CORE_FILES = ('test_indicators.py', 'test_risk_sizing.py', 'test_risk_limits.py',
              'test_portfolio_engine.py', 'test_backtester.py')


def validate_image(image):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image): raise ValueError('LOCAL_IMMUTABLE_IMAGE_REQUIRED')
    if (not os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock').startswith('unix://')
            or os.environ.get('DOCKER_CONTEXT', 'default') != 'default'):
        raise ValueError('LOCAL_IMAGE_DOCKER_CONTEXT_REQUIRED')


def extract_tool(archive, destination):
    # The mount root must also be traversable by the immutable image's nonroot
    # user. Cloud umasks can turn mkdir(mode=0755) into 0700.
    Path(destination).chmod(0o755)
    with zipfile.ZipFile(archive) as wheel:
        rows = wheel.infolist()
        if len(rows) > 5000 or sum(row.file_size for row in rows) > 64 * 1024**2:
            raise ValueError('VALIDATION_TOOL_RESOURCE_LIMIT')
        for row in rows:
            p = PurePosixPath(row.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in row.filename or stat.S_ISLNK(row.external_attr >> 16):
                raise ValueError('VALIDATION_TOOL_PATH_INVALID')
            target = Path(destination) / p
            if row.is_dir(): target.mkdir(parents=True, exist_ok=True); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream: stream.write(wheel.read(row))
            target.chmod(0o644)
        for p in Path(destination).rglob('*'):
            if p.is_dir(): p.chmod(0o755)


def execute(image, output):
    validate_image(image); out = Path(output).absolute()
    if out.exists() or out.is_symlink() or ROOT == out or ROOT in out.parents or any(p.is_symlink() for p in out.parents):
        raise ValueError('NEW_NATIVE_TEST_OUTPUT_REQUIRED')
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    spec = importlib.util.spec_from_file_location('verified_api_inputs', ROOT / 'ops/prepare-api-alpine-inputs.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    pins = module.parse_lock((ROOT / 'ops/ci-test-requirements.lock').read_text())
    if set(pins) != {'pytest', 'pluggy', 'iniconfig', 'pygments'}: raise ValueError('ALLOWLISTED_TEST_TOOL_LOCK_REQUIRED')
    with tempfile.TemporaryDirectory(prefix='nexus-native-api-tools-') as temporary:
        temp = Path(temporary); wheels = temp / 'wheels'; wheels.mkdir()
        tools = temp / 'tools'; tools.mkdir(mode=0o755)
        manifests = []
        for name, (version, hashes) in sorted(pins.items()):
            row = module.download_wheel(name, version, hashes, wheels)
            if not row['filename'].endswith('-py3-none-any.whl'): raise ValueError('PURE_VALIDATION_TOOLS_REQUIRED')
            extract_tool(wheels / row['filename'], tools); manifests.append(row)
        (out / 'validation-tools.json').write_text(json.dumps(manifests, indent=2))
        results = out / 'results'; results.mkdir(mode=0o777); results.chmod(0o777)
        paths = ['automation-hub/tests/' + name for name in FILES] + ['tests/' + name for name in CORE_FILES]
        command = ['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--read-only',
            '--memory', '1536m', '--cpus', '2', '--pids-limit', '128', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true', '--tmpfs', '/tmp:rw,size=128m,mode=1777',
            '--mount', 'type=bind,src=' + str(tools) + ',dst=/validation-tools,readonly',
            '--mount', 'type=bind,src=' + str(results) + ',dst=/evidence', '--workdir', '/app',
            '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'OPENBLAS_NUM_THREADS=1',
            '-e', 'PYTHONPATH=/validation-tools:/app/automation-hub:/app', '-e', 'HUB_DATA_DIR=/tmp/nexus-native-test',
            '--entrypoint', 'python', image, '-B', '-m', 'pytest', '-q',
            '-o', 'cache_dir=/tmp/pytest-cache', '--basetemp=/tmp/pytest', '--junitxml=/evidence/tests.xml', *paths]
        with (out / 'tests.log').open('w') as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=600)
        (out / 'execution.json').write_text(json.dumps({'image': image, 'exit_code': result.returncode,
            'files': paths, 'scope': 'NATIVE_DISPOSABLE_TEST_FIXTURES', 'image_modified': False,
            'services_started': False, 'production_access': False}, indent=2))
        return result.returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args(); raise SystemExit(execute(args.image, args.output))
