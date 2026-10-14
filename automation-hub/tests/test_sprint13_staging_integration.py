"""Default Sprint 13 delta, or an explicitly selected current local candidate."""
import json
import os
from pathlib import Path
import subprocess
import pytest


@pytest.mark.skipif(os.environ.get('NEXUS_STAGING_TEST')!='1',reason='requires explicit disposable local candidate image rehearsal')
def test_actual_candidate_auth_readiness_recovery_and_original_financial_isolation(tmp_path):
    root=Path(__file__).parents[2];out=tmp_path/'rehearsal'
    r=subprocess.run([str(root/'.venv/bin/python'),str(root/'ops/rehearse-staging.py'),'--output',str(out),
        '--api-image',os.environ.get('NEXUS_STAGING_TEST_API_IMAGE','nexus-api:sprint12-sprint13-validation'),
        '--worker-image',os.environ.get('NEXUS_STAGING_TEST_WORKER_IMAGE','nexus-workers:sprint12-sprint13-validation'),
        '--run-disposable','--exercise-shadow','--browser-smoke','--verify-remote-diagnostics'],capture_output=True,text=True,timeout=300)
    assert r.returncode==0,r.stderr
    report=json.loads((out/'rehearsal.json').read_text())
    assert report['checks']['remote_diagnostics_fail_closed']=='PASS'
    assert report['checks']['financial_state_unchanged_after_recovery']=='PASS'
    assert report['checks']['postgres_restore_original_authority']=='PASS'
    assert report['checks']['research_supervised_crash_recovery']['restart_count']==1
    assert report['checks']['shadow_supervised_crash_recovery']['restart_count']==1
    assert report['checks']['actual_production_chromium_smoke']['passed_scenarios']==2
    assert report['remote_deployment'] is False
