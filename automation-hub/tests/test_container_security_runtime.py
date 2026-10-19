"""Actual isolated candidate images; no remote registry or trading execution."""
import json
import os
import subprocess

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get('NEXUS_SECURITY_IMAGE_TEST') != '1',
    reason='requires explicitly selected locally rebuilt security images',
)


@pytest.mark.parametrize('role,python_minor', [('api', '3.11.'), ('worker', '3.12.')])
def test_actual_security_candidate_has_patched_direct_and_vendored_tools(role, python_minor):
    image = os.environ['NEXUS_SECURITY_' + role.upper() + '_IMAGE']
    probe = """
import email,importlib.metadata as m,json,pathlib,sys,psycopg
import setuptools, wheel
vendor=pathlib.Path(setuptools.__file__).parent/'_vendor'
versions={}
for p in vendor.glob('*.dist-info/METADATA'):
    message=email.message_from_bytes(p.read_bytes())
    versions[message['Name']]=message['Version']
print(json.dumps({'python':sys.version,'setuptools':m.version('setuptools'),
 'wheel':m.version('wheel'),'vendor':versions,'postgres_driver':psycopg.pq.__impl__}))
"""
    result = subprocess.run(
        ['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--read-only',
         '--memory', '256m', '--pids-limit', '32', '--entrypoint', 'python', image, '-B', '-c', probe],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof['python'].startswith(python_minor)
    assert proof['setuptools'] == '80.10.2'
    assert proof['wheel'] == '0.46.2'
    assert proof['vendor']['jaraco.context'] == '6.1.0'
    assert proof['vendor']['wheel'] == '0.46.3'
    assert proof['postgres_driver'] == 'binary'


@pytest.mark.parametrize('role', ['api', 'worker'])
def test_actual_security_candidate_dependency_graph_is_consistent(role):
    image = os.environ['NEXUS_SECURITY_' + role.upper() + '_IMAGE']
    result = subprocess.run(
        ['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--read-only',
         '--memory', '256m', '--pids-limit', '32', '--entrypoint', 'python', image, '-B', '-m', 'pip', 'check'],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'No broken requirements found.' in result.stdout
