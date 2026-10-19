"""Opt-in actual Python 3.11 / Node 20 image, isolated HTTPS/PG/workers."""
import json
import os
from pathlib import Path
import subprocess
import pytest


@pytest.mark.skipif(os.environ.get('NEXUS_STAGING_TEST')!='1',reason='requires approved built Sprint 12 images and disposable local Docker staging')
def test_actual_production_image_staging_auth_recovery_and_financial_isolation(tmp_path):
    root=Path(__file__).parents[2];output=tmp_path/'rehearsal'
    r=subprocess.run([str(root/'.venv/bin/python'),str(root/'ops/rehearse-staging.py'),
        '--output',str(output),'--run-disposable','--exercise-shadow','--browser-smoke'],
        capture_output=True,text=True,timeout=270)
    assert r.returncode==0,r.stderr
    proof=json.loads((output/'rehearsal.json').read_text())
    assert proof['status']=='VERIFIED_DISPOSABLE_LOCAL' and proof['execution_authorization'] is False
    assert proof['checks']['financial_state_unchanged_after_recovery']=='PASS'
    assert proof['checks']['actual_runtime_role_restricted']=='PASS'
    assert proof['checks']['actual_production_chromium_smoke']['passed_scenarios']==2
    assert proof['checks']['research_supervised_crash_recovery']['restart_count']==1
    assert proof['checks']['shadow_supervised_crash_recovery']['restart_count']==1
    assert proof['checks']['postgres_restore_original_authority']=='PASS'
    assert proof['readiness']['checks']['FEED_READY']['status']=='BLOCKED'
    assert proof['readiness']['checks']['FINANCIAL_EVIDENCE_READY']['status']=='UNKNOWN'
