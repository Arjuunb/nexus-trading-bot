"""Disabled PA must have no database/network/runtime dependency."""
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.feature_flags import price_action_lab_enabled
from routers.disabled_price_action import router


@pytest.mark.parametrize('value', [None, '', '0', 'false', 'FALSE', 'off', 'no'])
def test_retired_lab_defaults_disabled(monkeypatch, value):
    monkeypatch.delenv('PRICE_ACTION_LAB_ENABLED', raising=False)
    if value is not None:
        monkeypatch.setenv('PRICE_ACTION_LAB_ENABLED', value)
    assert price_action_lab_enabled() is False


@pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'yes', 'on'])
def test_compatibility_opt_in_is_explicit(monkeypatch, value):
    monkeypatch.setenv('PRICE_ACTION_LAB_ENABLED', value)
    assert price_action_lab_enabled() is True


def test_invalid_flag_fails_closed(monkeypatch):
    monkeypatch.setenv('PRICE_ACTION_LAB_ENABLED', 'tru')
    with pytest.raises(RuntimeError, match='INVALID_PRICE_ACTION_LAB_ENABLED'):
        price_action_lab_enabled()


@pytest.mark.parametrize('method', ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
@pytest.mark.parametrize('path', ['', '/bot-status', '/sessions/start', '/paper/reset', '/research/run'])
def test_all_retired_routes_are_bounded_and_cannot_mutate(method, path):
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.request(method, '/research/price-action' + path)
    assert response.status_code == 410
    assert response.json()['detail']['code'] == 'PRICE_ACTION_LAB_DISABLED'


@pytest.mark.parametrize('script,args', [
    ('validate_price_action_public_feed.py', ['--cache-dir', 'market', '--state-db', 'pa.db', '--output', 'out']),
    ('run_price_action_reference_study.py', ['--cache-dir', 'market', '--research-db', 'research.db', '--output', 'out']),
])
def test_dedicated_cli_jobs_exit_before_creating_data(tmp_path, script, args):
    root = Path(__file__).parents[1]
    environment = dict(os.environ, PRICE_ACTION_LAB_ENABLED='false')
    environment['PYTHONPATH'] = os.pathsep.join([str(root), str(root.parent), environment.get('PYTHONPATH', '')])
    result = subprocess.run([sys.executable, '-B', str(root/'scripts'/script), *args],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert 'PRICE_ACTION_LAB_DISABLED' in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_nightly_backups_skip_retired_pa_but_preserve_other_books_and_old_pa_backups(tmp_path, monkeypatch):
    from services.backup import backup_now
    monkeypatch.setenv('PRICE_ACTION_LAB_ENABLED', 'false')
    monkeypatch.setenv('HUB_PRICE_ACTION_PAPER_DB', str(tmp_path/'price_action_paper.db'))
    monkeypatch.setenv('HUB_PRICE_ACTION_RESEARCH_DB', str(tmp_path/'price_action_research.db'))
    for name in ('price_action_paper.db', 'price_action_research.db', 'smc_strategy_paper.db', 'ledger.db'):
        with sqlite3.connect(tmp_path/name) as db:
            db.execute('CREATE TABLE original(id TEXT PRIMARY KEY)')
            db.execute("INSERT INTO original VALUES ('original-identity')")
    old = tmp_path/'backups'/'20200101T000000Z'
    old.mkdir(parents=True)
    (old/'price_action_paper.db').write_bytes(b'retained-financial-backup')
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    result = backup_now(str(tmp_path), keep=1, now=now)
    assert result['ok']
    assert set(result['files']) == {'smc_strategy_paper.db', 'ledger.db'}
    backup_now(str(tmp_path), keep=1, now=now+timedelta(seconds=1))
    assert (old/'price_action_paper.db').read_bytes() == b'retained-financial-backup'
    assert (tmp_path/'price_action_paper.db').is_file()


def test_retired_backup_filter_rejects_alias_to_smc(tmp_path, monkeypatch):
    from services.backup import backup_now
    monkeypatch.setenv('PRICE_ACTION_LAB_ENABLED', 'false')
    monkeypatch.setenv('HUB_PRICE_ACTION_PAPER_DB', str(tmp_path/'smc_strategy_paper.db'))
    monkeypatch.setenv('HUB_SMC_PAPER_DB', str(tmp_path/'smc_strategy_paper.db'))
    with pytest.raises(RuntimeError, match='PA_DATABASE_ALIASES_PROTECTED_STORE'):
        backup_now(str(tmp_path))


def test_normal_api_boot_defaults_off_without_opening_pa_and_keeps_smc_available(tmp_path):
    root = Path(__file__).parents[1]
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('HUB_', 'SUPABASE_', 'NEXUS_', 'PRICE_ACTION_LAB_ENABLED'))}
    environment.update(HUB_DATA_DIR=str(tmp_path), HUB_ENV='test', HUB_AUTH_MODE='legacy',
        HUB_USERNAME='retirement-owner', HUB_PASSWORD='synthetic-retirement-password',
        HUB_SECRET='synthetic-retirement-session-secret-not-for-real-use',
        HUB_AUTO_ENGINE='0', HUB_USE_LIVE_DATA='0', HUB_ENABLE_EXTERNAL_LIVE='0',
        HUB_RESEARCH_AUTOSTART='0', HUB_MONITOR_AGENT='0', HUB_DAILY_REPORT_HOUR='-1',
        PYTEST_CURRENT_TEST='retirement-normal-boot')
    environment['PYTHONPATH'] = os.pathsep.join([str(root), str(root.parent)])
    code = r'''
from pathlib import Path
from unittest.mock import patch
import json,os,socket,sqlite3,threading
original_connect=sqlite3.connect;original_start=threading.Thread.start
def connect(database,*args,**kwargs):
 if any(name in str(database) for name in ('price_action_paper.db','price_action_research.db')):
  raise AssertionError('Disabled PA database opened during normal API boot')
 return original_connect(database,*args,**kwargs)
def start(thread,*args,**kwargs):
 if 'price-action-paper' in thread.name:raise AssertionError('PA runtime thread started')
 return original_start(thread,*args,**kwargs)
def offline(*a,**k):raise AssertionError('Unexpected external connection')
with patch.object(sqlite3,'connect',connect),patch.object(threading.Thread,'start',start),patch.object(socket.socket,'connect',offline):
 import app
 from fastapi.testclient import TestClient
 assert app.webhook_api.price_action_paper is None
 assert app.webhook_api.price_action_runtime is None
 assert app.webhook_api.price_action_research is None
 assert app.webhook_api.price_action_experiments is None
 assert app.webhook_api.shadow_router.active_sources.paths['pa-lab'] is None
 assert app.webhook_api.smc_paper is not None
 with TestClient(app.app) as client:
  assert client.get('/health').json()['features']['price_action_lab_enabled'] is False
  login=client.post('/auth/login',data={'username':'retirement-owner','password':'synthetic-retirement-password'})
  assert login.status_code==200,login.text
  client.headers['Authorization']='Bearer '+login.json()['token']
  for prefix in ('','/api/v1'):
   for method,path in (('GET','/bot-status'),('POST','/paper/reset'),('POST','/research/run')):
    result=client.request(method,prefix+'/research/price-action'+path,json={})
    assert result.status_code==410,(result.status_code,result.text)
  assert client.get('/research/smc/bot-status').status_code==200
  assert client.get('/api/v2/strategy-intelligence/cohorts').status_code==200
 for name in ('price_action_paper.db','price_action_research.db'):
  assert not (Path(os.environ['HUB_DATA_DIR'])/name).exists()
print(json.dumps({'normal_api_boot_without_pa':True,'authenticated_smc_and_si_available':True}))
'''
    result = subprocess.run([sys.executable, '-B', '-c', code], env=environment,
        text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout+'\n'+result.stderr
