"""Synthetic target inventory; never connects to a VPS or real provider."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location('pa_audit', Path(__file__).parents[1]/'ops/audit-price-action-retirement.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def container(project, service):
    return {'Id': project+'-synthetic', 'Name': '/'+project+'-'+service+'-1',
        'Image': 'sha256:synthetic', 'Config': {'Labels': {
            'com.docker.compose.project': project, 'com.docker.compose.service': service},
            'Env': ['SUPABASE_KEY=must-never-be-printed']},
        'State': {'Status': 'running', 'StartedAt': 'synthetic-start'},
        'Mounts': [{'Source': '/synthetic/'+project, 'Destination': '/var/lib/tradexa',
                    'Name': project+'_state', 'RW': True}]}


def test_inventory_is_readonly_and_never_discloses_configuration_secrets():
    rows = [container(*item) for item in M.PROJECTS.values()]
    calls = []
    def command(args):
        calls.append(args)
        if args[0] == 'ps': return 'one two'
        if args[0] == 'inspect': return json.dumps(rows)
        if args[0] == 'exec': return json.dumps({'databases': [], 'health': {'features': {'price_action_lab_enabled': False}}})
        if args[0] == 'stats': return json.dumps({'CPUPerc': '2.00%', 'MemUsage': '100MiB', 'BlockIO': '1MB / 2MB'})
        raise AssertionError('Unexpected operation')
    report = M.collect(command)
    assert report['container_identities_and_mounts_unchanged']
    assert report['files_deleted'] is False
    assert report['services_changed'] is False
    assert {call[0] for call in calls} == {'ps', 'inspect', 'exec', 'stats'}
    assert 'must-never-be-printed' not in json.dumps(report)
    assert all(row['production_data_deletion_authorized'] is False for row in report['environments'])


def test_ambiguous_or_stopped_api_cannot_be_selected():
    row = container(*M.PROJECTS['staging'])
    with pytest.raises(RuntimeError, match='ONE_RUNNING_EXISTING_API_REQUIRED'):
        M.select_api([row, deepcopy(row)], *M.PROJECTS['staging'])
    row['State']['Status'] = 'exited'
    with pytest.raises(RuntimeError, match='ONE_RUNNING_EXISTING_API_REQUIRED'):
        M.select_api([row], *M.PROJECTS['staging'])


def test_stopped_replay_is_retained_in_inventory_but_never_selected_or_executed():
    rows = [container(*item) for item in M.PROJECTS.values()]
    replay = deepcopy(rows[1])
    replay.update(Id='stopped-replay-synthetic', Name='/pa-replay-2025')
    replay['State'].update(Status='exited', StartedAt='synthetic-old-start')
    rows.append(replay)
    calls = []

    def command(args):
        calls.append(args)
        if args[0] == 'ps': return 'one two three'
        if args[0] == 'inspect': return json.dumps(rows)
        if args[0] == 'exec': return json.dumps({'databases': []})
        if args[0] == 'stats': return json.dumps({'CPUPerc': '0.00%'})
        raise AssertionError('Unexpected operation')

    assert M.select_api(rows, *M.PROJECTS['production']) is rows[1]
    report = M.collect(command)
    production = next(r for r in report['environments'] if r['scope'] == 'production')
    assert production['container_id'] == rows[1]['Id']
    assert '/pa-replay-2025' in production['containers_mounting_same_state']
    assert production['matching_api_containers'] == [
        {'container': r['Name'], 'container_id': r['Id'], 'image': r['Image'],
         'status': r['State']['Status']} for r in (rows[1], replay)]
    assert all(replay['Id'] not in args for args in calls if args[0] in ('exec', 'stats'))
    assert report['container_identities_and_mounts_unchanged'] is True
    assert report['services_changed'] is False
    assert report['files_deleted'] is False
    assert 'must-never-be-printed' not in json.dumps(report)


def test_real_child_reads_schema_ownership_and_sizes_without_financial_values(tmp_path):
    paper = tmp_path/'price_action_paper.db'
    with sqlite3.connect(paper) as db:
        db.execute('CREATE TABLE v2_account(id INTEGER PRIMARY KEY, account_type TEXT, balance TEXT)')
        db.execute("INSERT INTO v2_account VALUES (1,'PA_LAB','PRIVATE-FINANCIAL-VALUE')")
        db.execute('CREATE TABLE pa_sessions(id TEXT PRIMARY KEY)')
    code = M.INSPECT_CODE.replace("root=Path('/var/lib/tradexa')", 'root=Path('+repr(str(tmp_path))+')')
    process = tmp_path/'proc'/'123'/'fd'
    process.mkdir(parents=True)
    (process/'4').symlink_to(paper)
    code = code.replace("Path('/proc')", 'Path('+repr(str(tmp_path/'proc'))+')')
    prefix = "import urllib.request\ndef offline(*a,**k): raise OSError('synthetic network boundary')\nurllib.request.urlopen=offline\n"
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('HUB_', 'SUPABASE_'))}
    result = subprocess.run([sys.executable, '-B', '-c', prefix+code], env=environment,
        text=True, capture_output=True, timeout=15, check=True)
    report = json.loads(result.stdout)
    assert 'PRIVATE-FINANCIAL-VALUE' not in result.stdout
    assert report['databases'][0]['isolated_account_type'] == 'PA_LAB'
    assert report['databases'][0]['bytes'] == paper.stat().st_size
    assert report['databases'][1]['status'] == 'MISSING'
    assert report['financial_values_printed'] is False
    assert report['application_imported'] is False
    assert report['open_pa_file_descriptors'] == [{'pid': 123, 'fd': '4', 'path': str(paper)}]
    assert not (tmp_path/'price_action_research.db').exists()
