"""Only synthetic local PostgreSQL fixtures; never reads operator credentials."""
import ctypes.util
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_auth_diagnostic', ROOT / 'ops/diagnose-ledger-native-auth.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)

fixture_spec = importlib.util.spec_from_file_location('native_auth_fixtures', ROOT / 'tests/test_supabase_ledger_to_mac.py')
base = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(base)
server, source, tls_certificates = base.server, base.source, base.tls_certificates


@pytest.fixture
def clean_pg(monkeypatch):
    for key in list(os.environ):
        if key.startswith('PG'):
            monkeypatch.delenv(key)


def fixture_config():
    return dict(host='fixture.invalid', hostaddr='127.0.0.1', port=5432, dbname='postgres',
                user='postgres', password='synthetic:private\\password', sslmode='verify-full',
                sslrootcert='/fixture/ca.crt', connect_timeout=10)


@pytest.mark.parametrize('stop', ['memory', 'file', 'mismatch', 'tls', 'trust'])
def test_comparison_stops_at_failed_gate_without_dump_or_retry(tmp_path, monkeypatch, capsys, stop):
    calls = []
    class Native:
        version = 170011
        def probe(self, config, *, expected_password):
            mode = 'memory' if 'password' in config else 'file'
            calls.append(mode)
            assert expected_password == fixture_config()['password']
            assert config['hostaddr'] == '127.0.0.1' and config['sslmode'] == 'verify-full'
            assert Path(config['passfile']).stat().st_mode & 0o777 == 0o600
            assert ('password' in config) == (mode == 'memory')
            failed = stop == mode
            return dict(authenticated=not failed, consumed_password_matches_input=not (stop == 'mismatch' and mode == 'file'),
                        tls_in_use=stop != 'tls', password_authentication_used=stop != 'trust', failure_category='PASSWORD_AUTHENTICATION_FAILED')
    monkeypatch.setattr(diag.helper, 'verify_native_source', lambda *args, **kwargs: pytest.fail('psql ran after a failed gate'))
    monkeypatch.setattr(diag.helper, 'recover', lambda *args, **kwargs: pytest.fail('Backup is not allowed'))
    with pytest.raises(diag.helper.SourceConnectionBlocked) as caught:
        diag.compare_authentication(fixture_config(), native=Native(), bin_dir=base.PG_BIN, directory=tmp_path)
    assert calls == (['memory'] if stop in ('memory', 'tls', 'trust') else ['memory', 'file'])
    report = diag.helper.failure_report(caught.value)
    assert report['client_authentication_evidence']['python_psycopg_attempted'] is False
    if stop == 'mismatch':
        assert report['client_authentication_evidence']['native_passfile']['consumed_password_matches_input'] is False
    assert fixture_config()['password'] not in json.dumps(report)
    assert fixture_config()['password'] not in capsys.readouterr().out
    assert not (tmp_path / '.pgpass').exists()
    assert not (tmp_path / 'application-ledger.dump').exists()


def test_comparison_success_still_does_not_prove_or_run_backup(tmp_path, monkeypatch):
    calls = []
    class Native:
        version = 170011
        def probe(self, config, *, expected_password):
            calls.append(dict(config))
            return dict(authenticated=True, consumed_password_matches_input=True, tls_in_use=True, password_authentication_used=True)
    def psql(config, **kwargs):
        assert 'password' not in config and Path(config['passfile']).exists()
        assert config['hostaddr'] == calls[0]['hostaddr'] == calls[1]['hostaddr']
        return dict(native_psql_authenticated=True, native_identity_and_read_only_verified=True)
    monkeypatch.setattr(diag.helper, 'verify_native_source', psql)
    monkeypatch.setattr(diag.helper, 'recover', lambda *args, **kwargs: pytest.fail('Backup ran'))
    report = diag.compare_authentication(fixture_config(), native=Native(), bin_dir=base.PG_BIN, directory=tmp_path)
    assert report['status'] == 'AUTHENTICATION_COMPARISON_PASSED_NO_BACKUP'
    assert len(calls) == 2 and 'password' in calls[0] and 'password' not in calls[1]
    assert not (tmp_path / '.pgpass').exists()
    assert not (tmp_path / 'application-ledger.dump').exists()


def test_existing_credential_is_never_overwritten_or_removed(tmp_path):
    path = tmp_path / '.pgpass'
    path.write_text('existing synthetic file')
    class Native:
        version = 170011
    with pytest.raises(FileExistsError):
        diag.compare_authentication(fixture_config(), native=Native(), bin_dir=base.PG_BIN, directory=tmp_path)
    assert path.read_text() == 'existing synthetic file'


def test_native_probe_requires_clean_environment_before_connecting(monkeypatch):
    monkeypatch.setenv('PGPASSWORD', 'synthetic-ambient')
    native = object.__new__(diag.NativePQ)
    with pytest.raises(diag.helper.Blocked, match='CLEAN_POSTGRES_ENVIRONMENT_REQUIRED'):
        native.probe(fixture_config(), expected_password='synthetic-only')


def test_numeric_endpoint_pin_never_re_resolves_supplied_address(monkeypatch):
    monkeypatch.setattr(diag.socket, 'getaddrinfo', lambda *args, **kwargs: pytest.fail('Unexpected DNS'))
    assert diag.pinned_endpoint(fixture_config()) == '127.0.0.1'
    with pytest.raises(ValueError):
        diag.pinned_endpoint(dict(fixture_config(), hostaddr='127.0.0.1,::1'))


def test_cli_rejects_wrong_native_major_before_password_or_connection(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['diag.py', '--diagnose-auth', '--project', 'a'*20,
        '--sslrootcert', str(tmp_path / 'ca.crt'), '--destination', str(tmp_path)])
    monkeypatch.setattr(diag.helper, 'check_tools', lambda *args: None)
    class Native:
        version = 180000
    monkeypatch.setattr(diag, 'NativePQ', lambda *args: Native())
    monkeypatch.setattr(diag.helper, 'read_database_password', lambda *args, **kwargs: pytest.fail('Password requested'))
    assert diag.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report['reason'] == 'NATIVE_POSTGRES_LIBPQ_17_REQUIRED'
    assert report['backup_performed'] is False and report['external_ledger_recovery_proven'] is False


def test_native_tls_memory_file_and_psql_use_same_synthetic_password(source, tls_certificates, tmp_path, clean_pg):
    library = ('/Applications/Postgres.app/Contents/Versions/17/lib/libpq.5.dylib'
               if sys.platform == 'darwin' else ctypes.util.find_library('pq'))
    native = diag.NativePQ(library)
    config = dict(source, host='ledger.fixture.invalid', hostaddr='127.0.0.1', connect_timeout=10,
                  sslmode='verify-full', sslrootcert=str(tls_certificates / 'ca.crt'))
    report = diag.compare_authentication(config, native=native, bin_dir=base.PG_BIN, directory=tmp_path)
    evidence = report['client_authentication_evidence']
    assert evidence['native_memory']['authenticated'] is True
    assert evidence['native_passfile']['authenticated'] is True
    assert evidence['native_passfile']['consumed_password_matches_input'] is True
    assert evidence['native_psql_authenticated'] is True
    assert evidence['python_psycopg_attempted'] is False
    assert not (tmp_path / '.pgpass').exists()
    assert not (tmp_path / 'application-ledger.dump').exists()


@pytest.mark.parametrize('failure', ['password', 'ca'])
def test_native_real_failed_connection_never_reaches_file_probe(source, tls_certificates, tmp_path, clean_pg, failure):
    library = ('/Applications/Postgres.app/Contents/Versions/17/lib/libpq.5.dylib'
               if sys.platform == 'darwin' else ctypes.util.find_library('pq'))
    native = diag.NativePQ(library)
    config = dict(source, host='ledger.fixture.invalid', hostaddr='127.0.0.1', connect_timeout=10,
                  sslmode='verify-full', sslrootcert=str(tls_certificates / ('untrusted.crt' if failure == 'ca' else 'ca.crt')))
    if failure == 'password':
        config['password'] = 'synthetic-wrong-password'
    with pytest.raises(diag.helper.SourceConnectionBlocked) as caught:
        diag.compare_authentication(config, native=native, bin_dir=base.PG_BIN, directory=tmp_path)
    report = diag.helper.failure_report(caught.value)
    evidence = report['client_authentication_evidence']
    assert evidence['native_memory_attempted'] is True and evidence['native_passfile_attempted'] is False
    assert evidence['native_psql_attempted'] is False
    assert report['stage'] == 'NATIVE_MEMORY_AUTHENTICATION_ONLY'
    assert report['reason'].endswith('PASSWORD_AUTHENTICATION_FAILED' if failure == 'password' else 'TLS_CERTIFICATE_VERIFICATION_FAILED')
    assert not (tmp_path / '.pgpass').exists()
