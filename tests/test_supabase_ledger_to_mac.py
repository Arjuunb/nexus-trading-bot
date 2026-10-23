"""Real PostgreSQL 17 backup/restore checks; never connects to an external provider."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket as sockets
import ssl
import subprocess
import sys
import tempfile
import uuid
from types import SimpleNamespace

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql

SCRIPT = Path(__file__).resolve().parents[1] / "ops/backup-supabase-ledger-to-mac.py"
spec = importlib.util.spec_from_file_location("ledger_backup", SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
PG_BIN = Path(os.environ.get("NEXUS_TEST_PG17_BIN", "/Applications/Postgres.app/Contents/Versions/17/bin"))


@pytest.fixture(scope="module")
def server(tmp_path_factory, tls_certificates):
    if not (PG_BIN / "initdb").is_file():
        pytest.skip("Native PostgreSQL 17 binaries not available")
    directory = tmp_path_factory.mktemp("ledger-source")
    socket = Path(tempfile.mkdtemp(prefix="nxsrc-", dir="/tmp"))
    cluster = directory / "cluster"
    helper.check_tools(PG_BIN)
    helper.command([PG_BIN / "initdb", "-D", cluster, "-U", "postgres", "--locale=C", "-E", "UTF8",
                    "--auth-local=trust", "--auth-host=scram-sha-256"], directory=directory, name="init")
    with sockets.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    (tls_certificates / 'server.key').chmod(0o600)
    options = "-c listen_addresses=127.0.0.1 -c port=" + str(port) + " -c unix_socket_directories='" + str(socket) + "'"
    options += " -c ssl=on -c ssl_cert_file='" + str(tls_certificates / 'server.crt') + "'"
    options += " -c ssl_key_file='" + str(tls_certificates / 'server.key') + "'"
    helper.command([PG_BIN / "pg_ctl", "-D", cluster, "-l", directory / "postgres.log", "-w", "-t", "30",
                    "-o", options, "start"], directory=directory, name="start")
    config = {"host": str(socket), "port": port, "dbname": "postgres", "user": "postgres", "password": "fixture-only"}
    try:
        with psycopg.connect(**config, autocommit=True) as c:
            c.execute("ALTER ROLE postgres PASSWORD 'fixture-only'")
            c.execute("CREATE ROLE authenticated NOLOGIN")
        yield config
    finally:
        helper.command([PG_BIN / "pg_ctl", "-D", cluster, "-m", "fast", "-w", "stop"], directory=directory, name="stop")
        socket.rmdir()


@pytest.fixture
def source(server):
    name = "source_" + uuid.uuid4().hex
    with psycopg.connect(**server, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    config = dict(server, dbname=name)
    with psycopg.connect(**config, autocommit=True) as c:
        c.execute("CREATE SCHEMA auth; CREATE SCHEMA extensions")
        c.execute("CREATE EXTENSION pgcrypto SCHEMA extensions")
        c.execute("CREATE TYPE public.trade_side AS ENUM ('BUY','SELL')")
        c.execute("CREATE TABLE auth.users(id uuid PRIMARY KEY, email text UNIQUE)")
        c.execute("INSERT INTO auth.users VALUES('00000000-0000-0000-0000-000000000001','fixture@example.invalid')")
        c.execute("CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT '00000000-0000-0000-0000-000000000001'::uuid $$")
        for table in helper.CORE_TABLES:
            c.execute(sql.SQL("CREATE TABLE public.{} (id text PRIMARY KEY, amount numeric(18,8), payload jsonb, ts timestamptz)").format(sql.Identifier(table)))
            c.execute(sql.SQL("INSERT INTO public.{} VALUES ('row-1',123.45678901,%s,'2026-10-23 13:00:00+00')").format(sql.Identifier(table)),
                      ('{"text":"Unicode नेपाल\\nline\\twith tabs","nested":[1,true,null]}',))
        c.execute("ALTER TABLE public.positions ADD COLUMN owner uuid REFERENCES auth.users(id)")
        c.execute("UPDATE public.positions SET owner='00000000-0000-0000-0000-000000000001'")
        c.execute("ALTER TABLE public.positions ENABLE ROW LEVEL SECURITY")
        c.execute("CREATE POLICY owned_positions ON public.positions FOR SELECT TO authenticated USING (owner=auth.uid())")
        c.execute("GRANT USAGE ON SCHEMA auth TO authenticated")
        c.execute("GRANT SELECT ON public.positions TO authenticated")
        c.execute("CREATE INDEX position_amount ON public.positions(amount)")
        c.execute("CREATE FUNCTION public.keep_record() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
        c.execute("CREATE TRIGGER keep_record BEFORE UPDATE ON public.positions FOR EACH ROW EXECUTE FUNCTION public.keep_record()")
        c.execute("CREATE TABLE public.serial_history(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, side public.trade_side)")
        c.execute("INSERT INTO public.serial_history(side) VALUES ('BUY')")
    yield config
    # Private test fixture data only; originals operated by the helper are never dropped.
    with psycopg.connect(**server, autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def run_recovery(source, tmp_path, **kwargs):
    path = tmp_path / "recovery"
    path.mkdir(mode=0o700)
    try:
        return helper.recover(source, bin_dir=PG_BIN, directory=path, require_tls=False, **kwargs), path
    except helper.Blocked as error:
        if str(error) == 'RESTORED_SCHEMA_DEFINITION_MISMATCH':
            before=json.loads((path/'source-schema.json').read_text())
            after=json.loads((path/'restored-schema.json').read_text())
            # Only synthetic test definitions. The operator helper never prints these.
            print(json.dumps({k:{'before':before[k],'after':after[k]} for k in before if before[k]!=after[k]},indent=2))
        raise


def test_real_snapshot_restores_every_row_and_definition_during_concurrent_write(source, tmp_path):
    def concurrent_external_write(exporter):
        exporter.execute("SAVEPOINT write_probe")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            exporter.execute("INSERT INTO public.positions(id) VALUES('forbidden')")
        exporter.execute("ROLLBACK TO SAVEPOINT write_probe")
        with psycopg.connect(**source, autocommit=True) as c:
            c.execute("INSERT INTO public.positions(id,amount) VALUES('late-arrival',9.5)")
    report, directory = run_recovery(source, tmp_path, snapshot_hook=concurrent_external_write)
    assert report["tables_verified"] == 7
    assert report["rows_verified"] == 7
    assert report["all_selected_table_rows_match_same_exported_snapshot"] is True
    assert report["schema_columns_constraints_indexes_triggers_functions_policies_match"] is True
    assert report["object_ownership_and_grants_match"] is True
    assert report["original_role_login_attributes_and_memberships_recovered"] is False
    assert not (directory / ".pgpass").exists()
    assert not (directory / "detached-postgres-17/postmaster.pid").exists()
    with psycopg.connect(**source) as c:
        assert c.execute("SELECT count(*) FROM public.positions").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM public.positions WHERE id='forbidden'").fetchone()[0] == 0


def test_tls_cannot_be_silently_downgraded(source, tmp_path):
    directory = tmp_path / "recovery"
    directory.mkdir()
    with pytest.raises(helper.Blocked, match="VERIFIED_TLS_REQUIRED"):
        helper.recover(source, bin_dir=PG_BIN, directory=directory)
    assert not (directory / ".pgpass").exists()
    assert not (directory / "application-ledger.dump").exists()


def test_uncovered_foreign_key_schema_stops_before_dump(source, tmp_path):
    with psycopg.connect(**source, autocommit=True) as c:
        c.execute("CREATE SCHEMA extra; CREATE TABLE extra.refs(id int PRIMARY KEY)")
        c.execute("ALTER TABLE public.positions ADD COLUMN extra_id int REFERENCES extra.refs(id)")
    with pytest.raises(helper.Blocked, match="ADDITIONAL_FOREIGN_KEY_SCHEMA_BACKUP_REQUIRED"):
        run_recovery(source, tmp_path)
    assert not (tmp_path / "recovery/application-ledger.dump").exists()


def test_wrong_ledger_cannot_be_accepted(source, tmp_path):
    with psycopg.connect(**source, autocommit=True) as c:
        c.execute("DROP TABLE public.alerts")
    with pytest.raises(helper.Blocked, match="EXPECTED_FINANCIAL_TABLES_MISSING"):
        run_recovery(source, tmp_path)


def test_fingerprint_mismatch_retains_archive_and_stops_detached_server(source, tmp_path, monkeypatch):
    original = helper.fingerprint
    calls = 0
    def corrupt_target(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 2:
            result[0]["sha256"] = "0" * 64
        return result
    monkeypatch.setattr(helper, "fingerprint", corrupt_target)
    with pytest.raises(helper.Blocked, match="RESTORED_FINANCIAL_ROW_FINGERPRINT_MISMATCH"):
        run_recovery(source, tmp_path)
    directory = tmp_path / "recovery"
    assert (directory / "application-ledger.dump").stat().st_size > 0
    assert not (directory / ".pgpass").exists()
    assert not (directory / "detached-postgres-17/postmaster.pid").exists()
    with psycopg.connect(**source) as c:
        assert c.execute("SELECT count(*) FROM public.positions").fetchone()[0] == 1


def test_insufficient_space_never_connects_or_copies(source, tmp_path, monkeypatch):
    monkeypatch.setattr(helper.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(100,90,10))
    with pytest.raises(helper.Blocked, match="MAC_RECOVERY_SPACE_REQUIRED"):
        run_recovery(source, tmp_path)
    assert not (tmp_path / "recovery/.pgpass").exists()


def test_new_write_grant_fails_even_with_unchanged_financial_rows(source, tmp_path, monkeypatch):
    original = helper.command
    def grant_unexpected_write(args, **kwargs):
        original(args, **kwargs)
        if kwargs.get("name") == "restore":
            dsn = next(str(a).split("=",1)[1] for a in args if str(a).startswith("--dbname="))
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute("GRANT UPDATE ON public.positions TO authenticated")
    monkeypatch.setattr(helper, "command", grant_unexpected_write)
    with pytest.raises(helper.Blocked, match="RESTORED_SCHEMA_DEFINITION_MISMATCH"):
        run_recovery(source, tmp_path)
    assert (tmp_path / "recovery/application-ledger.dump").is_file()
    assert not (tmp_path / "recovery/detached-postgres-17/postmaster.pid").exists()


def test_unvalidated_financial_constraint_requires_review(source, tmp_path):
    with psycopg.connect(**source, autocommit=True) as c:
        c.execute("ALTER TABLE public.positions ADD CONSTRAINT amount_positive CHECK(amount>0) NOT VALID")
    with pytest.raises(helper.Blocked, match="SOURCE_UNVALIDATED_CONSTRAINT_REVIEW_REQUIRED"):
        run_recovery(source, tmp_path)
    assert not (tmp_path / "recovery/application-ledger.dump").exists()


def test_password_escaping_does_not_change_special_characters():
    config = dict(host="example.invalid", port=5432, dbname="postgres", user="postgres.ref", password="a:b\\c'$")
    assert helper.passfile_line(config).endswith(":a\\:b\\\\c'$\n")
    config["password"] = "first\nsecond"
    with pytest.raises(helper.Blocked, match="INVALID_CREDENTIAL_CHARACTER"):
        helper.passfile_line(config)


def test_private_metadata_is_not_world_readable_or_overwritten(tmp_path):
    path = tmp_path / "metadata.json"
    helper.private_json(path, {"schema": "fixture"})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        helper.private_json(path, {"schema": "replaced"})


@pytest.mark.parametrize(("message", "state", "expected"), [
    ('FATAL: Tenant or user not found', '28P01', 'POOLER_TENANT_OR_USER_NOT_FOUND'),
    ('FATAL: (ENOTFOUND) tenant/user postgres.fixture not found', None, 'POOLER_TENANT_OR_USER_NOT_FOUND'),
    ('SSL error: certificate verify failed', None, 'TLS_CERTIFICATE_VERIFICATION_FAILED'),
    ('server certificate for "other.invalid" does not match host name "fixture.invalid"', None,
     'TLS_HOSTNAME_VERIFICATION_FAILED'),
    ('FATAL: password authentication failed for user "postgres.fixture"', None, 'PASSWORD_AUTHENTICATION_FAILED'),
    ('authentication rejected', '28P01', 'PASSWORD_AUTHENTICATION_FAILED'),
    ('FATAL: Circuit breaker open: Too many authentication errors', None, 'PROVIDER_CONNECTION_BLOCKED'),
    ('FATAL: MaxClientsInSessionMode: max clients reached', None, 'PROVIDER_CONNECTION_LIMIT'),
    ('connections exhausted', '53300', 'PROVIDER_CONNECTION_LIMIT'),
    ('could not translate host name "fixture.invalid" to address: nodename nor servname provided', None,
     'HOSTNAME_RESOLUTION_FAILED'),
    ('connection to server at "fixture.invalid" failed: Connection refused', None, 'CONNECTION_REFUSED'),
    ('connection timeout expired', None, 'CONNECTION_TIMEOUT'),
    ('FATAL: unsupported startup parameter: options', None, 'STARTUP_PARAMETER_REJECTED'),
    ('unfamiliar provider failure', None, 'UNCLASSIFIED_DRIVER_ERROR'),
])
def test_connection_diagnostics_are_categorized_without_disclosing_driver_text(message, state, expected):
    class DriverError(Exception):
        sqlstate = state
    secret = 'fixture-password-never-disclose'
    error = DriverError(message + ' postgresql://private-user:' + secret + '@private-host/private-db')
    category, sqlstate = helper.connection_diagnostic(error)
    assert (category, sqlstate) == (expected, state)
    report = helper.failure_report(helper.SourceConnectionBlocked(category, sqlstate))
    serialized = json.dumps(report)
    assert report['stage'] == 'CONNECT_EXTERNAL_LEDGER_READ_ONLY'
    assert report['external_ledger_recovery_proven'] is False
    assert secret not in serialized
    assert 'private-user' not in serialized and 'private-host' not in serialized
    assert report['automatic_retries'] == 0


def test_untrusted_sqlstate_and_unknown_error_details_are_never_printed():
    class DriverError(Exception):
        sqlstate = 'fixture-password-in-untrusted-field'
    error = DriverError('postgresql://private-user:fixture-secret@private-host/private-db')
    assert helper.connection_diagnostic(error) == ('UNCLASSIFIED_DRIVER_ERROR', None)
    serialized = json.dumps(helper.failure_report(error))
    assert 'fixture-secret' not in serialized and 'private-host' not in serialized


def test_failed_connection_attempt_is_not_retried_or_downgraded_and_removes_passfile(tmp_path, monkeypatch, capsys):
    directory = tmp_path / 'recovery'
    directory.mkdir(mode=0o700)
    calls = []
    sockets = []
    original_mkdtemp = helper.tempfile.mkdtemp
    def track_socket(*args, **kwargs):
        path = original_mkdtemp(*args, **kwargs)
        sockets.append(Path(path))
        return path
    def fail_connect(**kwargs):
        calls.append(kwargs)
        assert 'password' not in kwargs
        assert kwargs['passfile'] == str(directory / '.pgpass')
        assert kwargs['sslmode'] == 'verify-full'
        assert kwargs['sslrootcert'] == '/etc/ssl/cert.pem'
        assert 'default_transaction_read_only=on' in kwargs['options']
        assert kwargs['connect_timeout'] == 10
        assert (directory / '.pgpass').stat().st_mode & 0o777 == 0o600
        raise psycopg.OperationalError('SSL error: certificate verify failed; password=fixture-secret')
    monkeypatch.setattr(helper.tempfile, 'mkdtemp', track_socket)
    monkeypatch.setattr(helper, 'check_tools', lambda *args: None)
    monkeypatch.setattr(helper, 'verify_native_source', lambda *args, **kwargs:
                        {'native_psql_authenticated': True, 'native_identity_and_read_only_verified': True})
    monkeypatch.setattr(helper.shutil, 'disk_usage', lambda path: shutil._ntuple_diskusage(20*helper.RESERVE, 0, 20*helper.RESERVE))
    monkeypatch.setattr(psycopg, 'connect', fail_connect)
    config = dict(host='fixture.invalid', port=5432, dbname='postgres', user='postgres.fixture',
                  password='fixture-secret', sslmode='verify-full', sslrootcert='/etc/ssl/cert.pem', connect_timeout=10)
    with pytest.raises(helper.SourceConnectionBlocked, match='TLS_CERTIFICATE_VERIFICATION_FAILED') as caught:
        helper.recover(config, bin_dir=PG_BIN, directory=directory)
    assert len(calls) == 1
    assert not (directory / '.pgpass').exists()
    assert not (directory / 'application-ledger.dump').exists()
    assert sockets and all(not path.exists() for path in sockets)
    assert 'fixture-secret' not in capsys.readouterr().out
    report = helper.failure_report(caught.value)
    assert report['client_authentication_evidence']['native_psql_authenticated'] is True
    assert report['client_authentication_evidence']['same_private_passfile_for_all_clients'] is True
    assert 'fixture-secret' not in json.dumps(report)


@pytest.fixture(scope='module')
def tls_certificates(tmp_path_factory):
    """Short-lived synthetic CA/server certificates; never uses provider credentials."""
    directory = tmp_path_factory.mktemp('ledger-tls-fixture')
    config = directory / 'cert.cnf'
    config.write_text('''[req]
distinguished_name=dn
prompt=no
[dn]
CN=Fixture CA
[ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
[server]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:ledger.fixture.invalid
''')
    def openssl(*args):
        subprocess.run(['openssl', *map(str, args)], check=True, capture_output=True, timeout=30)
    template = config.read_text()
    for name in ('ca', 'untrusted'):
        config.write_text(template.replace('CN=Fixture CA', 'CN=Fixture ' + name))
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                '-config', config, '-subj', '/CN=Fixture ' + name, '-extensions', 'ca', '-keyout', directory / (name + '.key'),
                '-out', directory / (name + '.crt'))
    config.write_text(template.replace('CN=Fixture CA', 'CN=ledger.fixture.invalid'))
    openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-config', config, '-subj', '/CN=ledger.fixture.invalid',
            '-keyout', directory / 'server.key', '-out', directory / 'server.csr')
    openssl('x509', '-req', '-in', directory / 'server.csr', '-days', '2',
            '-CA', directory / 'ca.crt', '-CAkey', directory / 'ca.key', '-CAcreateserial',
            '-extfile', config, '-extensions', 'server', '-out', directory / 'server.crt')
    return directory


def tls_handshake(ca_data, certificate_directory, hostname):
    """Real TLS chain and hostname verification in memory, without a database login."""
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.load_verify_locations(cadata=ca_data.decode('ascii'))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_directory / 'server.crt', certificate_directory / 'server.key')
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    for _ in range(20):
        try:
            client.do_handshake()
            return client.version()
        except ssl.SSLWantReadError:
            pass
        server_in.write(client_out.read())
        try:
            server.do_handshake()
        except ssl.SSLWantReadError:
            pass
        client_in.write(server_out.read())
    raise AssertionError('Synthetic TLS handshake did not complete')


def test_explicit_ca_verifies_chain_but_does_not_accept_wrong_ca_or_hostname(tls_certificates):
    ca_data = helper.load_root_certificate(tls_certificates / 'ca.crt')
    assert tls_handshake(ca_data, tls_certificates, 'ledger.fixture.invalid').startswith('TLSv1.')
    untrusted = helper.load_root_certificate(tls_certificates / 'untrusted.crt')
    with pytest.raises(ssl.SSLCertVerificationError):
        tls_handshake(untrusted, tls_certificates, 'ledger.fixture.invalid')
    with pytest.raises(ssl.SSLCertVerificationError):
        tls_handshake(ca_data, tls_certificates, 'other.fixture.invalid')


def test_ca_validation_rejects_non_ca_certificates_and_private_keys(tls_certificates, tmp_path):
    with pytest.raises(helper.Blocked, match='PROVIDER_CA_CERTIFICATE_REQUIRED'):
        helper.load_root_certificate(tls_certificates / 'server.crt')
    bundle = tmp_path / 'wrong-bundle.pem'
    bundle.write_bytes((tls_certificates / 'ca.crt').read_bytes() + (tls_certificates / 'ca.key').read_bytes())
    with pytest.raises(helper.Blocked, match='CA_CERTIFICATE_MUST_NOT_CONTAIN_A_PRIVATE_KEY'):
        helper.load_root_certificate(bundle)
    bundle.write_text('not a certificate')
    with pytest.raises(helper.Blocked, match='INVALID_PROVIDER_CA_CERTIFICATE'):
        helper.load_root_certificate(bundle)


def test_cli_requires_ca_before_password_and_never_uses_global_pg_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['backup.py', '--backup', '--project', 'a' * 20,
        '--pooler-host', 'aws-0-fixture.pooler.supabase.com', '--destination', str(tmp_path),
        '--sslrootcert', str(tmp_path / 'missing.crt')])
    monkeypatch.setattr(helper, 'check_tools', lambda *args: None)
    monkeypatch.setattr(helper.getpass, 'getpass', lambda *args: pytest.fail('Password requested before CA validation'))
    monkeypatch.setattr(helper.shutil, 'disk_usage', lambda path: shutil._ntuple_diskusage(20*helper.RESERVE, 0, 20*helper.RESERVE))
    assert helper.main() == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'PROVIDER_CA_CERTIFICATE_FILE_REQUIRED'


@pytest.mark.parametrize('direct', [False, True])
@pytest.mark.parametrize('password_input', ['terminal', 'mac-dialog'])
def test_cli_uses_one_fixed_ca_copy_and_verify_full_for_recovery(tls_certificates, tmp_path, monkeypatch, capsys, direct, password_input):
    path = tls_certificates / 'ca.crt'
    original_ca = path.read_bytes()
    mode_args = ['--direct'] if direct else ['--pooler-host', 'aws-0-fixture.pooler.supabase.com']
    monkeypatch.setattr(sys, 'argv', ['backup.py', '--backup', '--project', 'a' * 20,
        '--destination', str(tmp_path), '--sslrootcert', str(path), '--password-input', password_input] + mode_args)
    monkeypatch.setenv('PGSSLMODE', 'disable')
    monkeypatch.setenv('PGSSLROOTCERT', '/untrusted/ambient.crt')
    monkeypatch.setattr(helper, 'check_tools', lambda *args: None)
    def read_password(method, *, project):
        assert method == password_input and project == 'a' * 20
        return 'fixture-only'
    monkeypatch.setattr(helper, 'read_database_password', read_password)
    monkeypatch.setattr(helper.shutil, 'disk_usage', lambda path: shutil._ntuple_diskusage(20*helper.RESERVE, 0, 20*helper.RESERVE))
    def recover(config, *, directory, bin_dir):
        assert config['host'] == ('db.' + 'a' * 20 + '.supabase.co' if direct else 'aws-0-fixture.pooler.supabase.com')
        assert config['user'] == ('postgres' if direct else 'postgres.' + 'a' * 20)
        assert config['port'] == 5432 and config['dbname'] == 'postgres'
        assert config['sslmode'] == 'verify-full'
        saved_ca = Path(config['sslrootcert'])
        assert saved_ca.parent == directory
        assert saved_ca.read_bytes() == original_ca
        assert saved_ca.stat().st_mode & 0o777 == 0o600
        assert 'PGSSLMODE' not in os.environ and 'PGSSLROOTCERT' not in os.environ
        return {'status': 'SYNTHETIC_RECOVERY_CHECK_ONLY'}
    monkeypatch.setattr(helper, 'recover', recover)
    assert helper.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report['source_tls_mode'] == 'verify-full'
    assert report['source_connection_method'] == ('DIRECT' if direct else 'SESSION_POOLER')
    assert report['password_input_method'] == password_input
    assert report['trusted_ca_sha256'] == helper.hashlib.sha256(original_ca).hexdigest()
    assert 'fixture-only' not in json.dumps(report)


@pytest.mark.parametrize('mode_args', [[], ['--direct', '--pooler-host', 'aws-0-fixture.pooler.supabase.com']])
def test_cli_requires_exactly_one_explicit_connection_mode(tmp_path, monkeypatch, mode_args):
    monkeypatch.setattr(sys, 'argv', ['backup.py', '--backup', '--project', 'a' * 20,
        '--sslrootcert', str(tmp_path / 'ca.crt')] + mode_args)
    monkeypatch.setattr(helper.getpass, 'getpass', lambda *args: pytest.fail('Unexpected password prompt'))
    with pytest.raises(SystemExit) as caught:
        helper.main()
    assert caught.value.code == 2


def test_direct_mode_rejects_invalid_project_before_password_or_connection(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['backup.py', '--backup', '--project', 'other.invalid/host',
        '--direct', '--sslrootcert', str(tmp_path / 'ca.crt')])
    monkeypatch.setattr(helper.getpass, 'getpass', lambda *args: pytest.fail('Unexpected password prompt'))
    monkeypatch.setattr(psycopg, 'connect', lambda **kwargs: pytest.fail('Unexpected source connection'))
    assert helper.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report['reason'] == 'SUPABASE_PROJECT_REF_REQUIRED'
    assert report['source_connection_method'] == 'DIRECT'


def test_real_pg_dump_and_financial_restore_use_explicit_ca_and_verify_full(source, tls_certificates, tmp_path):
    directory = tmp_path / 'recovery'
    directory.mkdir(mode=0o700)
    ca_data = helper.load_root_certificate(tls_certificates / 'ca.crt')
    ca_path = helper.preserve_root_certificate(directory, ca_data)
    config = dict(source, host='ledger.fixture.invalid', hostaddr='127.0.0.1',
                  sslmode='verify-full', sslrootcert=str(ca_path))
    report = helper.recover(config, bin_dir=PG_BIN, directory=directory)
    assert report['external_application_ledger_recovery_proven'] is True
    assert report['all_selected_table_rows_match_same_exported_snapshot'] is True
    assert ca_path.read_bytes() == ca_data
    assert not (directory / '.pgpass').exists()
    assert not (directory / 'detached-postgres-17/postmaster.pid').exists()


def test_native_libpq_wrong_ca_cannot_copy_or_fall_back_to_unverified_tls(source, tls_certificates, tmp_path):
    directory = tmp_path / 'recovery'
    directory.mkdir(mode=0o700)
    config = dict(source, host='ledger.fixture.invalid', hostaddr='127.0.0.1',
                  sslmode='verify-full', sslrootcert=str(tls_certificates / 'untrusted.crt'))
    with pytest.raises(helper.SourceConnectionBlocked, match='TLS_CERTIFICATE_VERIFICATION_FAILED'):
        helper.recover(config, bin_dir=PG_BIN, directory=directory)
    assert not (directory / 'application-ledger.dump').exists()
    assert not (directory / '.pgpass').exists()


def test_native_probe_uses_private_passfile_without_secret_in_argv_or_env(tmp_path, monkeypatch):
    from types import SimpleNamespace
    secret = 'synthetic-password-never-disclose'
    path = tmp_path / '.pgpass'
    config = dict(host='fixture.invalid', port=5432, dbname='postgres', user='postgres',
                  passfile=str(path), sslmode='verify-full', sslrootcert='/fixture-ca.crt', connect_timeout=10)
    monkeypatch.setenv('PGPASSWORD', secret)
    calls = []
    def execute(args, **kwargs):
        calls.append(args)
        assert secret not in json.dumps(args) and secret not in json.dumps(kwargs['env'])
        assert 'PGPASSWORD' not in kwargs['env']
        assert '-X' in args and '--no-password' in args
        dsn = next(a.split('=', 1)[1] for a in args if a.startswith('--dbname='))
        connection = psycopg.conninfo.conninfo_to_dict(dsn)
        assert connection['passfile'] == str(path)
        assert 'password' not in connection
        assert connection['sslmode'] == 'verify-full'
        assert connection['options'] == helper.SOURCE_OPTIONS
        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(role='postgres', database='postgres', read_only='on')), stderr='')
    monkeypatch.setattr(helper.subprocess, 'run', execute)
    assert helper.verify_native_source(config, bin_dir=PG_BIN)['native_psql_authenticated'] is True
    assert len(calls) == 1


@pytest.mark.parametrize('response', [
    'not json',
    json.dumps(dict(role='other', database='postgres', read_only='on')),
    json.dumps(dict(role='postgres', database='other', read_only='on')),
    json.dumps(dict(role='postgres', database='postgres', read_only='off')),
])
def test_native_probe_rejects_wrong_identity_or_writable_session(response, tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(helper.subprocess, 'run', lambda *args, **kwargs:
        SimpleNamespace(returncode=0, stdout=response, stderr=''))
    config = dict(host='fixture.invalid', port=5432, dbname='postgres', user='postgres', passfile=str(tmp_path / '.pgpass'))
    with pytest.raises(helper.SourceConnectionBlocked, match='NATIVE_SOURCE_IDENTITY_OR_READ_ONLY_MISMATCH'):
        helper.verify_native_source(config, bin_dir=PG_BIN)


def test_native_auth_failure_stops_before_python_and_removes_passfile(tmp_path, monkeypatch):
    from types import SimpleNamespace
    directory = tmp_path / 'recovery'
    directory.mkdir(mode=0o700)
    secret = 'synthetic-password-never-disclose'
    monkeypatch.setattr(helper, 'check_tools', lambda *args: {'psql': '17.11'})
    monkeypatch.setattr(helper.shutil, 'disk_usage', lambda path: shutil._ntuple_diskusage(20*helper.RESERVE, 0, 20*helper.RESERVE))
    monkeypatch.setattr(helper.subprocess, 'run', lambda *args, **kwargs:
        SimpleNamespace(returncode=2, stdout='', stderr='FATAL: password authentication failed; ' + secret))
    monkeypatch.setattr(psycopg, 'connect', lambda *args, **kwargs: pytest.fail('Python attempted after native authentication failed'))
    config = dict(host='fixture.invalid', port=5432, dbname='postgres', user='postgres', password=secret)
    with pytest.raises(helper.SourceConnectionBlocked) as caught:
        helper.recover(config, bin_dir=PG_BIN, directory=directory)
    report = helper.failure_report(caught.value)
    assert report['reason'].endswith('PASSWORD_AUTHENTICATION_FAILED')
    assert report['stage'] == 'VERIFY_NATIVE_PSQL_READ_ONLY'
    assert report['client_authentication_evidence']['native_psql_authenticated'] is False
    assert report['client_authentication_evidence']['native_psql_version'] == '17.11'
    assert secret not in json.dumps(report)
    assert not (directory / '.pgpass').exists()
    assert not (directory / 'application-ledger.dump').exists()


@pytest.mark.parametrize('password_input', ['existing', 'mac-dialog'])
def test_real_tls_special_password_works_for_native_python_and_pg_dump(source, tls_certificates, tmp_path, monkeypatch, password_input):
    password = "synthetic:back\\slash '$%& पद"
    if password_input == 'mac-dialog':
        native_run = helper.subprocess.run
        dialog_calls = []
        def dialog_or_native(args, **kwargs):
            if args[0] == '/usr/bin/osascript':
                dialog_calls.append(True)
                return SimpleNamespace(returncode=0, stdout=password + '\n', stderr='')
            return native_run(args, **kwargs)
        monkeypatch.setattr(helper, 'sys', SimpleNamespace(platform='darwin'))
        monkeypatch.setattr(helper.subprocess, 'run', dialog_or_native)
        assert helper.read_database_password('mac-dialog', project='fixture') == password
        assert dialog_calls == [True, True]
    with psycopg.connect(**source, autocommit=True) as connection:
        connection.execute(sql.SQL('ALTER ROLE postgres PASSWORD {}').format(sql.Literal(password)))
    original = helper.command
    dump_checks = []
    def check_dump(args, **kwargs):
        if kwargs.get('name') == 'dump':
            dsn = next(str(a).split('=', 1)[1] for a in args if str(a).startswith('--dbname='))
            parameters = psycopg.conninfo.conninfo_to_dict(dsn)
            assert 'password' not in parameters and password not in dsn
            assert Path(parameters['passfile']).is_file()
            assert parameters['passfile'] == kwargs['env']['PGPASSFILE']
            assert Path(parameters['passfile']).stat().st_mode & 0o777 == 0o600
            dump_checks.append(True)
        return original(args, **kwargs)
    monkeypatch.setattr(helper, 'command', check_dump)
    directory = tmp_path / 'recovery'
    directory.mkdir(mode=0o700)
    config = dict(source, password=password, host='ledger.fixture.invalid', hostaddr='127.0.0.1',
                  sslmode='verify-full', sslrootcert=str(tls_certificates / 'ca.crt'))
    try:
        report = helper.recover(config, bin_dir=PG_BIN, directory=directory)
    finally:
        with psycopg.connect(**source, autocommit=True) as connection:
            connection.execute(sql.SQL('ALTER ROLE postgres PASSWORD {}').format(sql.Literal(source['password'])))
    evidence = report['client_authentication_evidence']
    assert evidence['native_psql_authenticated'] is True
    assert evidence['python_psycopg_authenticated'] is True
    assert evidence['same_private_passfile_for_all_clients'] is True
    assert report['external_application_ledger_recovery_proven'] is True
    assert dump_checks == [True]
    assert password not in json.dumps(report)
    assert not (directory / '.pgpass').exists()


def test_mac_dialog_confirms_exact_password_without_logging_or_secret_arguments(monkeypatch, capsys):
    secret = "  synthetic:back\\slash '$%& नेपाल  "
    calls = []
    monkeypatch.setattr(helper, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setenv('PGPASSWORD', 'unrelated-fixture-secret')
    monkeypatch.setenv('AEDebugSends', '1')
    monkeypatch.setenv('AEDebugReceives', '1')
    monkeypatch.setattr(helper.getpass, 'getpass', lambda *args: pytest.fail('Terminal input was used'))
    def native_dialog(args, **kwargs):
        assert args[:5] == ['/usr/bin/osascript', '-s', 'he', '-e', helper.MAC_PASSWORD_SCRIPT]
        assert 'with hidden answer' in args[4]
        assert secret not in str(args) and secret not in str(kwargs)
        assert kwargs['capture_output'] is True and kwargs['encoding'] == 'utf-8'
        assert kwargs['stdin'] == subprocess.DEVNULL and kwargs['timeout'] == 180
        assert not any(k.startswith(('PG', 'AEDebug')) for k in kwargs['env'])
        calls.append(args[-1])
        return SimpleNamespace(returncode=0, stdout=secret + '\n', stderr='')
    monkeypatch.setattr(helper.subprocess, 'run', native_dialog)
    assert helper.read_database_password('mac-dialog', project='fixture-project') == secret
    assert len(calls) == 2 and calls[0].startswith('Enter') and calls[1].startswith('Confirm')
    assert 'fixture-project' in calls[0]
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


@pytest.mark.parametrize(('responses', 'reason'), [
    (['\n'], 'DATABASE_PASSWORD_REQUIRED'),
    (['without-output-newline'], 'MAC_PASSWORD_DIALOG_INVALID_OUTPUT'),
    (['line\nbreak\n'], 'INVALID_CREDENTIAL_CHARACTER'),
    (['carriage\rreturn\n'], 'INVALID_CREDENTIAL_CHARACTER'),
    (['nul\x00value\n'], 'INVALID_CREDENTIAL_CHARACTER'),
    (['first\n', 'different\n'], 'DATABASE_PASSWORD_CONFIRMATION_MISMATCH'),
])
def test_mac_dialog_invalid_input_blocks_without_retry_or_secret_disclosure(monkeypatch, capsys, responses, reason):
    monkeypatch.setattr(helper, 'sys', SimpleNamespace(platform='darwin'))
    calls = []
    def dialog(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(returncode=0, stdout=responses[len(calls)-1], stderr='fixture-private-detail')
    monkeypatch.setattr(helper.subprocess, 'run', dialog)
    with pytest.raises(helper.Blocked) as caught:
        helper.read_database_password('mac-dialog', project='fixture')
    report = helper.failure_report(caught.value)
    assert report['reason'] == reason and report['deployment_performed'] is False
    assert report['external_ledger_recovery_proven'] is False
    assert len(calls) == len(responses)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''
    assert 'fixture-private-detail' not in json.dumps(report)


def test_mac_dialog_cancel_stops_without_fallback_and_redacts_process_output(monkeypatch, capsys):
    secret = 'synthetic-private-output'
    monkeypatch.setattr(helper, 'sys', SimpleNamespace(platform='darwin'))
    calls = []
    def cancel(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(returncode=1, stdout=secret, stderr=secret + ' User canceled. (-128)')
    monkeypatch.setattr(helper.subprocess, 'run', cancel)
    monkeypatch.setattr(helper.getpass, 'getpass', lambda *args: pytest.fail('Unexpected fallback'))
    with pytest.raises(helper.Blocked, match='MAC_PASSWORD_DIALOG_CANCELLED_OR_FAILED') as caught:
        helper.read_database_password('mac-dialog', project='fixture')
    assert calls == [True]
    assert secret not in json.dumps(helper.failure_report(caught.value))
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


@pytest.mark.parametrize('error', [OSError('fixture-secret'),
    subprocess.TimeoutExpired('fixture-secret', 180, output='fixture-secret'),
    UnicodeError('fixture-secret')])
def test_mac_dialog_process_failure_is_fixed_and_never_retried(monkeypatch, capsys, error):
    monkeypatch.setattr(helper, 'sys', SimpleNamespace(platform='darwin'))
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(helper.subprocess, 'run', fail)
    with pytest.raises(helper.Blocked, match='MAC_PASSWORD_DIALOG_UNAVAILABLE') as caught:
        helper.read_database_password('mac-dialog', project='fixture')
    assert calls == [True]
    assert 'fixture-secret' not in json.dumps(helper.failure_report(caught.value))
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


def test_mac_dialog_requires_macos_before_starting_any_process(monkeypatch):
    monkeypatch.setattr(helper, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(helper.subprocess, 'run', lambda *args, **kwargs: pytest.fail('Process started'))
    with pytest.raises(helper.Blocked, match='MAC_PASSWORD_DIALOG_REQUIRES_MACOS'):
        helper.read_database_password('mac-dialog', project='fixture')


def test_terminal_password_input_stays_unchanged(monkeypatch, capsys):
    secret = '  synthetic:back\\slash  '
    monkeypatch.setattr(helper.getpass, 'getpass', lambda *args: secret)
    assert helper.read_database_password('terminal', project='fixture') == secret
    assert capsys.readouterr().out == ''


def test_cli_mac_dialog_cancellation_cannot_start_backup(tls_certificates, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['backup.py', '--backup', '--project', 'a' * 20,
        '--direct', '--password-input', 'mac-dialog', '--sslrootcert', str(tls_certificates / 'ca.crt'),
        '--destination', str(tmp_path)])
    monkeypatch.setattr(helper, 'check_tools', lambda *args: None)
    monkeypatch.setattr(helper.shutil, 'disk_usage', lambda path: shutil._ntuple_diskusage(20*helper.RESERVE, 0, 20*helper.RESERVE))
    def cancel(method, *, project):
        assert method == 'mac-dialog'
        raise helper.Blocked('MAC_PASSWORD_DIALOG_CANCELLED_OR_FAILED')
    monkeypatch.setattr(helper, 'read_database_password', cancel)
    monkeypatch.setattr(helper, 'recover', lambda *args, **kwargs: pytest.fail('Recovery started after cancellation'))
    assert helper.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report['reason'] == 'MAC_PASSWORD_DIALOG_CANCELLED_OR_FAILED'
    assert report['password_input_method'] == 'mac-dialog'
    assert report['external_ledger_recovery_proven'] is False
    assert not list(tmp_path.glob('external-ledger-*'))


def test_real_native_wrong_password_stops_before_python_or_dump(source, tls_certificates, tmp_path):
    directory = tmp_path / 'recovery'
    directory.mkdir(mode=0o700)
    config = dict(source, password='synthetic-wrong-password', host='ledger.fixture.invalid', hostaddr='127.0.0.1',
                  sslmode='verify-full', sslrootcert=str(tls_certificates / 'ca.crt'))
    with pytest.raises(helper.SourceConnectionBlocked) as caught:
        helper.recover(config, bin_dir=PG_BIN, directory=directory)
    report = helper.failure_report(caught.value)
    assert report['stage'] == 'VERIFY_NATIVE_PSQL_READ_ONLY'
    assert report['reason'].endswith('PASSWORD_AUTHENTICATION_FAILED')
    assert report['client_authentication_evidence']['native_psql_authenticated'] is False
    assert not (directory / 'application-ledger.dump').exists()
    assert not (directory / '.pgpass').exists()
