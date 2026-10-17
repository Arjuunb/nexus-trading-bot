"""Synthetic SQL policy/gateway rehearsal; no provider, SSH, release or production access."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time
import traceback

COMMIT = 'f8778ed42039d415f3829e08a05225976fe0d0a5'
MIGRATION_SHA = 'f254393c9d9562ea9d1f468f59c5044914b1281cb041950c586f305328db4cec'
GATEWAY_SHA = 'fce20490330ed57befad016892a549f3c7aa169c9c1aec7f5c1ffd7156a8e4b4'
GUARDED_SHA = '93f8152f918391f6aea58518be19a363785f97e521f53fa1a72f6379b8300c03'
PG = 'ghcr.io/arjuunb/nexus-trading-bot-postgres@sha256:0c184f826eac85c55b6abd3ad520470cc5556870570e287997109e860dcce8f9'
PG_CONFIG = 'sha256:9223b83d7d0ed373a991c449034e0d1f054f9cc920369463fa7360a911f9a17c'
NGINX = 'nginx@sha256:959ae6b898852db82e5b29b929388e59914f42be0a8f4d44c89a4c1f53b49208'
NGINX_CONFIG = 'sha256:0d831f5435ede3eeaba5768270c9e6f542d58306f69bb61f148e5c2374b57c56'
A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
TABLE = 'public.tradexa_customer_workspaces'
ROOT = Path(os.environ['GITHUB_WORKSPACE'])
SOURCE = ROOT / 'source'
TEMP = Path(os.environ['RUNNER_TEMP'])
OUT = TEMP / 'rc3-workspace-rehearsal-evidence'
OUT.mkdir(exist_ok=True)
NAME = 'rc3-workspace-rehearsal-' + os.environ['GITHUB_RUN_ID']
CASES = []
REPORT = {'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'RUNNING',
          'source_commit': COMMIT, 'migration_sha256': MIGRATION_SHA, 'gateway_sha256': GATEWAY_SHA,
          'guarded_migration_sha256': GUARDED_SHA,
          'controller_commit': os.environ['GITHUB_SHA'], 'run_id': os.environ['GITHUB_RUN_ID'],
          'synthetic_data_only': True, 'real_provider_called': False,
          'VPS_access': False, 'publication': False, 'cases': CASES}


def save():
    (OUT / 'result.json').write_text(json.dumps(REPORT, indent=2) + '\n')


def run(args, *, body=None, check=True, timeout=120):
    proc = subprocess.run(args, input=body, text=True, capture_output=True, timeout=timeout)
    if check and proc.returncode:
        raise RuntimeError('COMMAND_FAILED ' + Path(args[0]).name + ': ' + proc.stderr[-3000:])
    return proc


def sql(body, *, role=None, uid=None, check=True):
    prefix = '\\set VERBOSITY verbose\n'
    if role:
        assert role in ('authenticated', 'anon') and uid in (None, A, B)
        prefix += 'begin; set local role ' + role + '; '
        if uid:
            prefix += 'set local "request.jwt.claim.sub" = \'' + uid + '\'; '
        body = prefix + body + '; commit;'
    else:
        body = prefix + body
    return run(['docker', 'exec', '-i', NAME, 'psql', '-X', '-qAt', '-U', 'postgres',
                '-d', 'postgres', '-v', 'ON_ERROR_STOP=1'], body=body, check=check, timeout=30)


def case(name, body, expected=None, *, role='authenticated', uid=A, error=None):
    r = sql(body, role=role, uid=uid, check=False)
    actual = r.stdout.strip()
    if error:
        passed = r.returncode != 0 and error in r.stderr
    else:
        passed = r.returncode == 0 and (expected is None or actual == expected)
    CASES.append({'name': name, 'passed': passed, 'expected_SQLSTATE': error,
                  'exit_code': r.returncode, 'actual': actual,
                  'error': r.stderr[-2000:] if not passed else None})
    save()
    assert passed, 'REHEARSAL_CASE_FAILED: ' + name


def verify_image(reference, expected_config):
    run(['docker', 'pull', '--platform', 'linux/amd64', reference], timeout=180)
    image = json.loads(run(['docker', 'image', 'inspect', reference, '--format', '{{json .}}']).stdout)
    assert image['Id'] == expected_config and image['Os'] == 'linux' and image['Architecture'] == 'amd64'
    assert any(v.split('@')[-1] == reference.split('@')[-1] for v in image['RepoDigests'])
    return {'reference': reference, 'config_id': expected_config, 'repo_digests': image['RepoDigests']}


def main():
    assert os.environ['RUNNER_ENVIRONMENT'] == 'github-hosted'
    assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
    assert not os.environ.get('DOCKER_HOST') and not os.environ.get('DOCKER_CONTEXT')
    assert run(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD']).stdout.strip() == COMMIT
    migration = SOURCE / 'supabase/migrations/0006_customer_workspaces.sql'
    gateway = SOURCE / 'ops/staging.customer-workspace.nginx.conf'
    guarded = Path(__file__).resolve().with_name('0006_customer_workspaces.guarded.sql')
    assert hashlib.sha256(migration.read_bytes()).hexdigest() == MIGRATION_SHA
    assert hashlib.sha256(gateway.read_bytes()).hexdigest() == GATEWAY_SHA
    assert hashlib.sha256(guarded.read_bytes()).hexdigest() == GUARDED_SHA
    source_body = migration.read_text().split('begin;\n',1)[1].rsplit('commit;',1)[0]
    assert guarded.read_text().split('-- BEGIN exact source migration body; only original transaction markers removed.\n',1)[1].split('-- END exact source migration body.',1)[0] == source_body
    REPORT['postgres_image'] = verify_image(PG, PG_CONFIG)
    REPORT['nginx_image'] = verify_image(NGINX, NGINX_CONFIG)
    # No host networking, published ports, host state mounts or named volumes.
    assert not run(['docker', 'ps', '-aq', '--filter', 'name=^/' + NAME + '$']).stdout.strip()
    run(['docker', 'run', '-d', '--pull=never', '--name', NAME, '--network', 'none',
         '--memory', '512m', '--cpus', '1', '--pids-limit', '128',
         '--label', 'nexus.purpose=synthetic-workspace-rehearsal',
         '--tmpfs', '/var/lib/postgresql/data:rw,nosuid,nodev,size=128m',
         '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', PG])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = run(['docker', 'exec', NAME, 'pg_isready', '-U', 'postgres'], check=False)
        log = run(['docker', 'logs', NAME], check=False)
        if ready.returncode == 0 and 'PostgreSQL init process complete; ready for start up.' in log.stdout + log.stderr:
            break
        time.sleep(1)
    else:
        raise RuntimeError('SYNTHETIC_POSTGRES_NOT_READY')
    sql('''create role anon nologin nosuperuser nobypassrls;
create role authenticated nologin nosuperuser nobypassrls;
create schema auth;
create table auth.users (id uuid primary key);
create function auth.uid() returns uuid language sql stable as
$$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
grant usage on schema public, auth to anon, authenticated;
create table public.legacy_sentinel (name text primary key, value text);
insert into public.legacy_sentinel values ('preserve', 'unchanged');
insert into auth.users values (''' + "'" + A + "'), ('" + B + "');")
    case('guard_rejects_missing_expected_profile_schema', guarded.read_text(), role=None, uid=None, error='P0001')
    sql('create table public.tradexa_profiles (id uuid primary key references auth.users(id), full_name text);')
    sql(guarded.read_text())
    case('guarded_batch_refuses_reapplication', guarded.read_text(), role=None, uid=None, error='P0001')
    case('row_security_enabled_and_forced', "select relrowsecurity::text || ',' || relforcerowsecurity::text from pg_class where oid='public.tradexa_customer_workspaces'::regclass", 'true,true', role=None, uid=None)
    case('exact_three_authenticated_policies', "select string_agg(cmd, ',' order by cmd) from pg_policies where schemaname='public' and tablename='tradexa_customer_workspaces' and roles='{authenticated}'::name[]", 'INSERT,SELECT,UPDATE', role=None, uid=None)
    case('authenticated_no_bypass_or_superuser', "select rolsuper::text || ',' || rolbypassrls::text from pg_roles where rolname='authenticated'", 'false,false', role=None, uid=None)
    case('A_initially_empty', 'select count(*) from ' + TABLE, '0')
    case('A_insert_own', f'''insert into {TABLE} values ('{A}', 'settings-center', '{{"marker":"A"}}');''')
    case('B_insert_own', f'''insert into {TABLE} values ('{B}', 'settings-center', '{{"marker":"B"}}');''', uid=B)
    case('A_reads_only_A', f"select string_agg(data->>'marker', ',') from {TABLE}", 'A')
    case('B_reads_only_B', f"select string_agg(data->>'marker', ',') from {TABLE}", 'B', uid=B)
    case('A_cross_read_empty', f"select count(*) from {TABLE} where user_id='{B}'", '0')
    case('B_cross_read_empty', f"select count(*) from {TABLE} where user_id='{A}'", '0', uid=B)
    case('A_cross_insert_denied', f"insert into {TABLE} values ('{B}', 'settings-center', '{{}}')", error='42501')
    case('B_cross_insert_denied', f"insert into {TABLE} values ('{A}', 'settings-center', '{{}}')", uid=B, error='42501')
    case('A_cross_update_invisible', f"with changed as (update {TABLE} set data='{{}}' where user_id='{B}' returning 1) select count(*) from changed", '0')
    case('B_cross_update_invisible', f"with changed as (update {TABLE} set data='{{}}' where user_id='{A}' returning 1) select count(*) from changed", '0', uid=B)
    case('A_cannot_change_owner', f"update {TABLE} set user_id='{B}' where user_id='{A}'", error='42501')
    case('B_cannot_change_owner', f"update {TABLE} set user_id='{A}' where user_id='{B}'", uid=B, error='42501')
    case('A_own_upsert_acknowledges_value', f'''insert into {TABLE} values ('{A}', 'settings-center', '{{"marker":"A2"}}') on conflict (user_id,namespace) do update set data=excluded.data returning data->>'marker' ''', 'A2')
    case('B_own_upsert_acknowledges_value', f'''insert into {TABLE} values ('{B}', 'settings-center', '{{"marker":"B2"}}') on conflict (user_id,namespace) do update set data=excluded.data returning data->>'marker' ''', 'B2', uid=B)
    case('A_reload_saved_value', f"select data->>'marker' from {TABLE}", 'A2')
    case('B_reload_saved_value', f"select data->>'marker' from {TABLE}", 'B2', uid=B)
    case('anonymous_select_denied', 'select count(*) from ' + TABLE, role='anon', uid=None, error='42501')
    case('anonymous_insert_denied', f"insert into {TABLE} values ('{A}', 'settings-center', '{{}}')", role='anon', uid=None, error='42501')
    case('authenticated_missing_identity_reads_zero', 'select count(*) from ' + TABLE, '0', uid=None)
    case('authenticated_missing_identity_write_denied', f"insert into {TABLE} values ('{A}', 'settings-center', '{{}}')", uid=None, error='42501')
    case('authenticated_delete_denied', 'delete from ' + TABLE, error='42501')
    case('authenticated_truncate_denied', 'truncate ' + TABLE, error='42501')
    case('unsupported_namespace_rejected', f"update {TABLE} set namespace='financial'", error='23514')
    case('array_payload_rejected', f"update {TABLE} set data='[]'::jsonb", error='23514')
    case('scalar_payload_rejected', f"update {TABLE} set data='1'::jsonb", error='23514')
    case('null_payload_rejected', f"update {TABLE} set data=null", error='23502')
    case('oversized_payload_rejected', f"update {TABLE} set data=jsonb_build_object('x',repeat('x',200001))", error='23514')
    case('denied_operations_preserve_A_value', f"select data->>'marker' from {TABLE}", 'A2')
    case('denied_operations_preserve_B_value', f"select data->>'marker' from {TABLE}", 'B2', uid=B)
    case('legacy_sentinel_preserved', "select value from public.legacy_sentinel where name='preserve'", 'unchanged', role=None, uid=None)
    case('exact_migration_is_not_silently_reapplied', migration.read_text(), role=None, uid=None, error='42P07')
    case('failed_reapply_preserves_rows', 'select count(*) from ' + TABLE, '2', role=None, uid=None)
    # Syntax-check the exact private gateway using generated, disposable TLS.
    tls = TEMP / ('rc3-rehearsal-tls-' + os.environ['GITHUB_RUN_ID'])
    tls.mkdir(mode=0o700)
    run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(tls / 'tls.key'),
         '-out', str(tls / 'tls.crt'), '-days', '1', '-subj', '/CN=localhost',
         '-addext', 'subjectAltName=DNS:localhost'])
    r = run(['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--read-only',
             '--user', str(os.getuid()) + ':' + str(os.getgid()), '--cap-drop', 'ALL',
             '--security-opt', 'no-new-privileges:true', '--memory', '64m', '--cpus', '.25', '--pids-limit', '32',
             '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m', '--add-host', 'api:127.0.0.1',
             '--mount', 'type=bind,src=' + str(gateway) + ',dst=/etc/nginx/nginx.conf,readonly',
             '--mount', 'type=bind,src=' + str(tls) + ',dst=/run/fixture,readonly',
             '--entrypoint', 'nginx', NGINX, '-t'])
    REPORT['gateway_syntax'] = {'passed': True, 'stdout': r.stdout, 'stderr': r.stderr}
    REPORT['status'] = 'SYNTHETIC_SQL_RLS_AND_GATEWAY_REHEARSAL_PASSED'
    REPORT['case_count'] = len(CASES)
    REPORT['actual_supabase_RLS_acceptance'] = 'NOT_PERFORMED; CLAIMS_FUNCTION_AND_AUTH_USERS_ARE_SYNTHETIC'
    REPORT['actual_application_or_browser_acceptance'] = 'NOT_PERFORMED'


try:
    save()
    main()
except BaseException:
    REPORT['status'] = 'REHEARSAL_FAILED'
    REPORT['error'] = traceback.format_exc()[-6000:]
    raise
finally:
    cleanup = run(['docker', 'inspect', NAME], check=False)
    if cleanup.returncode == 0:
        found = json.loads(cleanup.stdout)[0]
        assert found['Config']['Labels'].get('nexus.purpose') == 'synthetic-workspace-rehearsal'
        removed = run(['docker', 'rm', '-f', NAME], check=False)
        REPORT['ephemeral_postgres_removed'] = removed.returncode == 0
        if removed.returncode:
            REPORT['status'] = 'REHEARSAL_CLEANUP_FAILED'
    else:
        REPORT['ephemeral_postgres_removed'] = 'NOT_CREATED'
    REPORT['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
