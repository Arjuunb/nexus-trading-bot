"""Execute the committed sanitizer, including fail-closed negative controls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / '.github/workflows/rc3-release-validation.yml'


def run_guard(tmp_path, report, outcome='success'):
    text = WORKFLOW.read_text()
    step = text.split('      - name: Retain sanitized secret-scan evidence', 1)[1]
    code = textwrap.dedent(step.split("          python3 - <<'PY'\n", 1)[1].split('\n          PY', 1)[0])
    (tmp_path / 'rc3-3.12').mkdir()
    if report is not None:
        (tmp_path / 'rc3-secret-raw-3.12.json').write_text(json.dumps(report))
    result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'RUNNER_TEMP': str(tmp_path), 'PYTHON_VERSION': '3.12', 'SECRET_SCAN_OUTCOME': outcome}, capture_output=True, text=True)
    output = tmp_path / 'rc3-3.12/source-secrets.json'
    return result, json.loads(output.read_text()) if output.exists() else None


def clean_report():
    # Actual Trivy 0.69.3 clean fs report omits Results (omitempty).
    return {'SchemaVersion': 2, 'Trivy': {'Version': '0.69.3'}, 'ReportID': 'fixture-report', 'CreatedAt': '2026-10-15T13:27:22Z', 'ArtifactName': '/source', 'ArtifactType': 'filesystem'}


@pytest.mark.parametrize('results', ['omitted', [], [{'Target': 'file.py', 'Class': 'secret'}]])
def test_clean_native_formats(tmp_path, results):
    data = clean_report()
    if results != 'omitted':
        data['Results'] = results
    result, output = run_guard(tmp_path, data)
    assert result.returncode == 0, result.stderr
    assert output['secret_count_all_severities'] == 0


@pytest.mark.parametrize('field,value', [('SchemaVersion', 1), ('ArtifactType', 'container_image'), ('Results', None), ('Results', {}), ('Results', [None]), ('Results', [{'Secrets': None}]), ('Results', [{'Secrets': {}}]), ('Results', [{'Secrets': [None]}]), ('Trivy', {}), ('ReportID', ''), ('CreatedAt', ''), ('ArtifactName', '')])
def test_malformed_evidence_fails(tmp_path, field, value):
    data = clean_report()
    data[field] = value
    result, _ = run_guard(tmp_path, data)
    assert result.returncode != 0


@pytest.mark.parametrize('outcome', ['failure', 'skipped', 'cancelled', ''])
def test_unsuccessful_scanner_never_passes(tmp_path, outcome):
    result, _ = run_guard(tmp_path, clean_report(), outcome)
    assert result.returncode != 0


def test_missing_evidence_fails(tmp_path):
    result, _ = run_guard(tmp_path, None)
    assert result.returncode != 0


@pytest.mark.parametrize('severity', ['UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'])
def test_every_severity_blocks_without_leaking_matches(tmp_path, severity):
    data = clean_report()
    data['Results'] = [{'Target': 'fixture.txt', 'Secrets': [{'RuleID': 'fixture', 'Severity': severity, 'StartLine': 1, 'EndLine': 2, 'Match': 'SENSITIVE_MATCH_SENTINEL', 'Code': {'Lines': ['SENSITIVE_CODE_SENTINEL']}}]}]
    result, output = run_guard(tmp_path, data)
    assert result.returncode != 0
    assert output['secret_count_all_severities'] == 1
    assert 'SENSITIVE_' not in json.dumps(output)
    assert 'SENSITIVE_' not in result.stdout + result.stderr


def test_security_and_identity_controls_remain():
    text = WORKFLOW.read_text()
    assert 'severity: UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL' in text
    assert "exit-code: '1'" in text
    assert 'scanners: secret' in text
    assert "ref: '28acdbccc4f9b737c683d94e15bb16a09e50aa85'" in text
    assert '3ba76e44d9181ee1a8910667be645bfa477a7b3e4055c30538a29030e83caa4d' in text
    assert 'SECRET_SCAN_OUTCOME: ${{ steps.source-secret-scan.outcome }}' in text


@pytest.mark.parametrize('report', [[], 'not-a-report', 2])
def test_non_object_report_fails(tmp_path, report):
    result, _ = run_guard(tmp_path, report)
    assert result.returncode != 0


def test_truncated_json_fails(tmp_path):
    (tmp_path / 'rc3-3.12').mkdir()
    (tmp_path / 'rc3-secret-raw-3.12.json').write_text('{"SchemaVersion":')
    text = WORKFLOW.read_text().split('      - name: Retain sanitized secret-scan evidence', 1)[1]
    code = textwrap.dedent(text.split("          python3 - <<'PY'\n", 1)[1].split('\n          PY', 1)[0])
    result = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'RUNNER_TEMP': str(tmp_path), 'PYTHON_VERSION': '3.12', 'SECRET_SCAN_OUTCOME': 'success'}, capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / 'rc3-3.12/source-secrets.json').exists()
