"""The staging API must not start unrelated legacy paper lab workers."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('configured,expected', [(None, True), ('0', False), ('false', False), ('1', True)])
def test_actual_api_construction_obeys_legacy_lab_autostart(tmp_path, configured, expected):
    code = '''
import json,threading
started=[]
threading.Thread.start=lambda thread: started.append(thread.name)
import webhook_api
print('LEGACY_WORKER_RESULT='+json.dumps({'threads':started,
    'enabled':webhook_api.settings.legacy_lab_autostart}))
'''
    env = {key: value for key, value in os.environ.items() if key in {'PATH', 'SYSTEMROOT', 'LANG'}}
    env.update(PYTHONPATH=str(ROOT/'automation-hub'), HUB_DATA_DIR=str(tmp_path),
        HUB_AUTO_ENGINE='0', HUB_USE_LIVE_DATA='0', HUB_RESEARCH_AUTOSTART='0',
        HUB_MONITOR_AGENT='0', HUB_DAILY_REPORT_HOUR='-1', HUB_ENABLE_EXTERNAL_LIVE='0',
        PYTEST_CURRENT_TEST='isolated-staging-construction', PYTHONDONTWRITEBYTECODE='1')
    if configured is not None:
        env['HUB_LEGACY_LAB_AUTOSTART'] = configured
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT/'automation-hub',
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    value = json.loads(next(line.split('=', 1)[1] for line in result.stdout.splitlines()
                           if line.startswith('LEGACY_WORKER_RESULT=')))
    assert value['enabled'] is expected
    assert ('price-action-paper-runtime' in value['threads']) is expected
    assert ('smc-paper-runtime' in value['threads']) is expected


def test_staging_profile_disables_legacy_labs_without_touching_production():
    config = yaml.safe_load((ROOT/'ops/staging.local.compose.yaml').read_text())
    assert str(config['services']['api']['environment']['HUB_LEGACY_LAB_AUTOSTART']) == '0'
