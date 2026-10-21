"""Real application boot over synthetic existing books; no VPS claim."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]


def child_python():
    executable = Path(sys.executable)
    base = executable.parents[3]
    loader = base / 'lib/ld-musl-x86_64.so.1'
    if loader.is_file():
        return [str(loader), '--library-path', ':'.join(str(base/p) for p in
            ('usr/local/lib', 'lib', 'usr/lib')), str(executable)]
    return [str(executable)]


def probe(directory):
    from contextlib import ExitStack
    import hashlib
    import socket
    import sqlite3
    import threading
    from unittest.mock import patch

    for key in list(os.environ):
        if key.startswith(('HUB_', 'SUPABASE_', 'NEXUS_')) or key == 'PYTEST_CURRENT_TEST':
            os.environ.pop(key, None)
    os.environ.update(HUB_DATA_DIR=str(directory), HUB_ENV='test', HUB_AUTH_MODE='legacy',
        HUB_USERNAME='acceptance-owner', HUB_PASSWORD='synthetic-acceptance-password',
        HUB_SECRET='synthetic-acceptance-session-secret-with-no-real-use',
        HUB_AUTO_ENGINE='1', HUB_USE_LIVE_DATA='1', HUB_ENABLE_EXTERNAL_LIVE='1',
        HUB_RESEARCH_AUTOSTART='1', NEXUS_SHADOW_SUPERVISED='1',
        HUB_MONITOR_AGENT='1', HUB_DAILY_REPORT_HOUR='8', NEXUS_ACCEPTANCE_READ_ONLY='0')
    from config import settings
    from data.account_store import AccountStore
    from data.ledger import SqliteLedger
    from execution.paper_broker_v2 import PaperBrokerV2
    from services.price_action_lab import PriceActionPaperAccount
    from services.smc_strategy_lab import SMCPaperAccount
    from services.trading_instances import InstanceStore, TradingInstance
    from services.tenancy import OWNER_TENANT

    primary = SqliteLedger(settings.ledger_path)
    instances = InstanceStore(primary)
    instances.create(TradingInstance(id='acceptance-owned', owner_id=OWNER_TENANT,
        symbol='BTCUSDT', strategy_key='brain', strategy_label='Retained fixture',
        strategy_version='frozen-fixture-1', timeframe='5m', risk_per_trade_pct=.005,
        capital_allocation=1200, state='running', desired_running=True, config_revision=7))
    primary._c.close()
    account = AccountStore(settings.account_db)
    account.seed_if_empty(1234.0)
    account.update_snapshot(current_equity=1210, available_balance=1200, realized_pnl=-24)
    account._c.close()
    broker = PaperBrokerV2(settings.paper_broker_v2_db, starting_balance=2345)
    broker._c.close()
    pa = PriceActionPaperAccount(settings.price_action_paper_db, starting_balance=3456, evidence_owner=OWNER_TENANT)
    for db in (pa._db, pa.broker._c, pa.journal._db):
        db.close()
    smc = SMCPaperAccount(settings.smc_paper_db, starting_balance=4567, evidence_owner=OWNER_TENANT)
    for db in (smc._db, smc.broker._c):
        db.close()
    financial = [settings.ledger_path, settings.account_db, settings.paper_broker_v2_db,
        settings.price_action_paper_db, settings.smc_paper_db]

    def fingerprints():
        result = []
        for path in financial:
            with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True) as db:
                db.execute('BEGIN')
                schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
                rows = {}
                for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                    rows[table] = sorted(db.execute('SELECT * FROM "'+table.replace('"','""')+'"').fetchall(), key=repr)
                result.append(hashlib.sha256(json.dumps([schema, rows], sort_keys=True, default=str).encode()).hexdigest())
        return result

    before = fingerprints()
    os.environ['NEXUS_ACCEPTANCE_READ_ONLY'] = '1'
    def forbidden(*_, **__):
        raise AssertionError('Acceptance attempted an outbound connection or execution thread')
    with ExitStack() as guards:
        guards.enter_context(patch.object(socket.socket, 'connect', forbidden))
        guards.enter_context(patch.object(socket, 'create_connection', forbidden))
        with patch.object(threading.Thread, 'start', forbidden):
            import app
        from fastapi.testclient import TestClient
        with TestClient(app.app) as client:
            health = client.get('/health')
            assert health.status_code == 200, health.text
            assert health.json()['acceptance_read_only'] is True
            assert client.get('/api/v2/strategy-intelligence/cohorts').status_code == 401
            login = client.post('/auth/login', data={'username': 'acceptance-owner', 'password': 'synthetic-acceptance-password'})
            assert login.status_code == 200, login.text
            client.headers['Authorization'] = 'Bearer ' + login.json()['token']
            cohorts = client.get('/api/v2/strategy-intelligence/cohorts')
            assert cohorts.status_code == 200, cohorts.text
            bundled = app._LANDING_READY and app._WEBUI_READY
            if bundled:
                dashboard = client.get('/app/strategy-intelligence')
                assert dashboard.status_code == 200, dashboard.text
            for path in ('/webhook', '/api/v1/webhook', '/instances/acceptance-owned/start',
                '/paper-v2/orders', '/api/v2/strategy-intelligence/shadow/registrations'):
                response = client.post(path, json={})
                assert response.status_code == 423, (path, response.status_code, response.text)
            assert not app.webhook_api.price_action_runtime.stream.running
            assert app.webhook_api.smc_runtime._thread is None
            assert app.webhook_api.price_action_runtime._supervisor_thread is None
            assert not app.webhook_api.engine.status().get('running')
        assert fingerprints() == before, 'Boot/HTTP/shutdown changed financial rows or saved rules'
    print(json.dumps({'status': 'LOCAL_ACCEPTANCE_BOOT_HTTP_SHUTDOWN_PASSED',
        'financial_files_unchanged': len(financial), 'bundled_dashboard_checked': bundled,
        'new_execution_threads': 0, 'external_connections': 0, 'vps_acceptance': False}))


class FullAcceptanceBoot(unittest.TestCase):
    def test_real_boot_preserves_saved_intent_and_finances_with_autostart_flags_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment['PYTHONPATH'] = os.pathsep.join([str(ROOT), str(ROOT.parent), environment.get('PYTHONPATH', '')])
            result = subprocess.run([*child_python(), str(Path(__file__).resolve()), '--boot-probe', directory],
                env=environment, text=True, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
            proof = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(proof['financial_files_unchanged'], 5)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--boot-probe':
        probe(Path(sys.argv[2]))
    else:
        unittest.main()
