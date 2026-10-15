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


@pytest.mark.parametrize('artifact_type', ['filesystem', 'repository'])
@pytest.mark.parametrize('outcome', ['failure', 'skipped', 'cancelled', ''])
def test_unsuccessful_scanner_never_passes(tmp_path, outcome, artifact_type):
    data = clean_report()
    data['ArtifactType'] = artifact_type
    result, _ = run_guard(tmp_path, data, outcome)
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


@pytest.mark.parametrize('artifact_type', ['filesystem', 'repository'])
@pytest.mark.parametrize('with_results', [False, True])
def test_native_git_and_plain_directory_metadata(tmp_path, artifact_type, with_results):
    # Trivy 0.69.3 local/fs.go switches fs scans of Git roots to repository.
    data = clean_report()
    data['ArtifactType'] = artifact_type
    if artifact_type == 'repository':
        data['Metadata'] = {'Commit': '28acdbccc4f9b737c683d94e15bb16a09e50aa85'}
    if with_results:
        data['Results'] = []
    result, output = run_guard(tmp_path, data)
    assert result.returncode == 0, result.stderr
    assert output['secret_count_all_severities'] == 0


@pytest.mark.parametrize('field,value', [('SchemaVersion', None), ('SchemaVersion', '2'), ('SchemaVersion', 2.0), ('SchemaVersion', True), ('SchemaVersion', 3), ('ArtifactType', None), ('ArtifactType', ''), ('ArtifactType', 'unknown'), ('ArtifactType', []), ('ArtifactType', 2)])
def test_invalid_schema_or_artifact_metadata(tmp_path, field, value):
    data = clean_report()
    data[field] = value
    result, _ = run_guard(tmp_path, data)
    assert result.returncode != 0


@pytest.mark.parametrize('severity', ['UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'])
def test_git_checkout_secrets_still_fail(tmp_path, severity):
    data = clean_report()
    data['ArtifactType'] = 'repository'
    data['Results'] = [{'Target': 'fixture.txt', 'Secrets': [{'RuleID': 'fixture', 'Severity': severity, 'Match': 'REPOSITORY_MATCH_SENTINEL'}]}]
    result, output = run_guard(tmp_path, data)
    assert result.returncode != 0
    assert output['secret_count_all_severities'] == 1
    assert 'REPOSITORY_MATCH_SENTINEL' not in json.dumps(output)


@pytest.mark.parametrize('field', ['SchemaVersion', 'ArtifactType'])
def test_required_schema_metadata_cannot_be_missing(tmp_path, field):
    data = clean_report()
    del data[field]
    result, _ = run_guard(tmp_path, data)
    assert result.returncode != 0


def browser_provenance_code():
    step = WORKFLOW.read_text().split('      - name: Verify separate browser harness provenance and unchanged RC3 inputs', 1)[1]
    return textwrap.dedent(step.split("          python3 - <<'PY'\n", 1)[1].split('\n          PY', 1)[0])


@pytest.mark.parametrize('tamper', [None, 'controller-identity', 'source-identity', 'config', 'spec', 'application'])
def test_browser_harness_provenance_is_fail_closed(tmp_path, tamper):
    import shutil

    def git(directory, *args):
        return subprocess.check_output(['git', '-C', str(directory), *args], text=True).strip()

    def commit(directory):
        git(directory, 'add', '.')
        git(directory, '-c', 'user.name=CI fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'isolated fixture')
        return git(directory, 'rev-parse', 'HEAD')

    source = tmp_path / 'source'
    controller = tmp_path / 'validation-controller'
    for directory in (source, controller):
        directory.mkdir()
        git(directory, 'init', '-q')
    app = source / 'automation-hub-dashboard/src/App.tsx'
    app.parent.mkdir(parents=True)
    app.write_text('unchanged application fixture\n')
    source_commit = commit(source)
    harness = controller / '.github/rc3-browser'
    harness.parent.mkdir()
    shutil.copytree(WORKFLOW.parents[1] / 'rc3-browser', harness)
    controller_commit = commit(controller)
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    code = browser_provenance_code()
    assert "'28acdbccc4f9b737c683d94e15bb16a09e50aa85'" in code
    # Only substitute the synthetic fixture's source identity; validation logic is unchanged.
    code = code.replace('28acdbccc4f9b737c683d94e15bb16a09e50aa85', source_commit)
    if tamper == 'config':
        (harness / 'playwright.config.cjs').write_text('unreviewed config')
    if tamper == 'spec':
        (harness / 'clipboard.spec.cjs').write_text('unreviewed test')
    if tamper == 'application':
        app.write_text('changed application')
    if tamper == 'source-identity':
        app.write_text('different committed application')
        commit(source)
    env = {**os.environ, 'GITHUB_WORKSPACE': str(tmp_path), 'GITHUB_WORKFLOW_SHA': '0' * 40 if tamper == 'controller-identity' else controller_commit, 'EVIDENCE_DIR': str(evidence)}
    result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is (tamper is None), result.stderr
    assert (evidence / 'browser-harness.json').exists() is (tamper is None)
    if tamper is None:
        report = json.loads((evidence / 'browser-harness.json').read_text())
        assert report['application_source_modified'] is False
        assert report['controller_commit'] == controller_commit
        assert report['tested_source_commit'] == source_commit


def test_browser_wrapper_preserves_rc3_assertions_and_security():
    text = WORKFLOW.read_text()
    config = (WORKFLOW.parents[1] / 'rc3-browser/playwright.config.cjs').read_text()
    assert 'ref: ${{ github.workflow_sha }}' in text
    assert 'sparse-checkout: .github/rc3-browser' in text
    assert 'path: validation-controller' in text
    assert 'persist-credentials: false' in text
    assert '--config "$GITHUB_WORKSPACE/validation-controller/.github/rc3-browser/playwright.config.cjs"' in text
    assert "permissions: ['clipboard-read', 'clipboard-write']" in config
    assert "original.use.baseURL !== 'http://localhost:4173'" in config
    assert "testDir: path.join(dashboard, 'e2e')" in config
    assert '...original' in config
    assert 'reuseExistingServer: false' in config
    assert 'timeout:' not in config
    assert 'workers:' not in config
    assert '.catch(' not in config
    assert 'addInitScript' not in config


def test_browser_gate_requires_original_suite_and_new_regressions():
    text = WORKFLOW.read_text()
    assert "('browser-tests.xml', 156)" in text
    assert "for tag in ('skipped', 'failure', 'error')" in text
