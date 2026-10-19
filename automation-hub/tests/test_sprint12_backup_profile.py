import hashlib
import json
from pathlib import Path
import sqlite3
import pytest
import yaml

from services.staging_backup import backup_sqlite_staging,restore_sqlite_staging
from services.release_source import verify_build_inputs


def test_original_financial_and_metadata_backup_restore_preserves_all_ids(tmp_path):
    source=tmp_path/'disposable';source.mkdir()
    (source/'.nexus-staging-scope.json').write_text(json.dumps({'scope':'DISPOSABLE_LOCAL_STAGING'}))
    before={}
    for name in ('pa.db','smc.db','paper.db','ledger.db','journal.db','decisions.db','spool.sqlite'):
        p=source/name
        with sqlite3.connect(p) as db:
            db.execute('CREATE TABLE original(id TEXT PRIMARY KEY, parent_id TEXT REFERENCES original(id), unknown_funding TEXT)')
            db.execute("INSERT INTO original VALUES ('original-entry',NULL,'UNKNOWN')")
            db.execute("INSERT INTO original VALUES ('partial-exit','original-entry','UNKNOWN')")
            db.execute('PRAGMA user_version=1')
        before[name]=p.read_bytes()
    manifest=backup_sqlite_staging(source,tmp_path/'backup')
    assert all((source/name).read_bytes()==data for name,data in before.items())
    restored=restore_sqlite_staging(tmp_path/'backup',tmp_path/'restore')
    assert restored['restored_databases']==7 and restored['source_authority_changed'] is False
    for row in manifest['originals']:
        assert row['row_counts']=={'original':2} and row['user_version']==1
        with sqlite3.connect(tmp_path/'restore'/row['snapshot']) as db:
            assert db.execute('SELECT unknown_funding FROM original').fetchall()==[('UNKNOWN',),('UNKNOWN',)]


def test_backup_requires_disposable_scope_and_restore_rejects_corruption(tmp_path):
    p=tmp_path/'source';p.mkdir()
    with pytest.raises(ValueError,match='SCOPE_REQUIRED'):backup_sqlite_staging(p,tmp_path/'unscoped')
    (p/'.nexus-staging-scope.json').write_text('{"scope":"DISPOSABLE_LOCAL_STAGING"}')
    with sqlite3.connect(p/'test.db') as db:db.execute('CREATE TABLE records(id TEXT)')
    backup_sqlite_staging(p,tmp_path/'backup')
    (tmp_path/'backup/test.db').write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='HASH_MISMATCH'):restore_sqlite_staging(tmp_path/'backup',tmp_path/'restore')


def test_offline_inputs_cannot_hide_unlisted_private_files_or_changed_wheels(tmp_path):
    p=tmp_path/'python';p.mkdir();wheel=p/'fixture.whl';wheel.write_bytes(b'public fixture')
    manifest={'scope':'PUBLIC_LOCKED_PACKAGES','files':[{'path':'python/fixture.whl','sha256':hashlib.sha256(wheel.read_bytes()).hexdigest()}]}
    (tmp_path/'input-manifest.json').write_text(json.dumps(manifest));assert len(verify_build_inputs(tmp_path))==64
    (tmp_path/'.env').write_text('PRIVATE_SENTINEL')
    with pytest.raises(ValueError,match='UNDECLARED'):verify_build_inputs(tmp_path)
    (tmp_path/'.env').unlink();wheel.write_bytes(b'changed')
    with pytest.raises(ValueError,match='INTEGRITY_FAILED'):verify_build_inputs(tmp_path)


def test_staging_profile_is_isolated_bounded_and_shadow_is_opt_in():
    root=Path(__file__).parents[2];profile=yaml.safe_load((root/'ops/staging.local.compose.yaml').read_text())
    services=profile['services'];assert services['shadow-worker']['profiles']==['shadow']
    for name in ('state-init','metadata-init'):
        assert 'NEXUS_STAGING_WORKER_IMAGE' in services[name]['image']
    assert services['postgres'].get('ports') is None and profile['networks']['metadata']['internal'] is True
    assert services['api'].get('ports') is None
    assert services['ingress']['ports']==['127.0.0.1::8443']
    assert services['api']['networks']==['metadata','ingress']
    assert all(services[n]['networks']==['metadata'] for n in ('postgres','research-worker','shadow-worker'))
    for name in ('research-worker','shadow-worker'):
        s=services[name];assert 'env_file' not in s and s['read_only'] is True
        assert s['cap_drop']==['ALL'] and s['restart']=='on-failure:2'
        assert s['mem_limit'] and s['cpus'] and s['pids_limit']
        assert all('api-state' not in v for v in s['volumes'])
        assert not set(s['environment']) & {'HUB_CONTROL_KEY','HUB_PASSWORD','HUB_EXCHANGE_API_KEY','SUPABASE_KEY'}
    assert services['api']['environment']['HUB_AUTO_ENGINE']=='0'
    assert services['api']['environment']['HUB_ENABLE_EXTERNAL_LIVE']=='0'
    assert all('tradexa-data' not in json.dumps(v) for v in profile['volumes'].values())


def test_actual_rehearsal_financial_probe_reads_original_identifiers(tmp_path):
    import ast,contextlib,io
    root=Path(__file__).parents[2]
    tree=ast.parse((root/'ops/rehearse-staging.py').read_text())
    fn=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='financial')
    code=next(n.value for n in ast.walk(fn) if isinstance(n,ast.Constant) and isinstance(n.value,str) and 'import sqlite3' in n.value)
    p=tmp_path/'fixture.db'
    with sqlite3.connect(p) as db:
        db.execute('CREATE TABLE v2_fills(fill_id TEXT PRIMARY KEY, funding TEXT)')
        db.execute("INSERT INTO v2_fills VALUES ('original-id','UNKNOWN')")
    before=p.read_bytes();output=io.StringIO()
    with contextlib.redirect_stdout(output):exec(compile(code.replace('/var/lib/tradexa',str(tmp_path)),'actual-read-only-probe','exec'),{})
    proof=json.loads(output.getvalue())
    assert set(proof)=={'fixture.db:v2_fills'} and p.read_bytes()==before
