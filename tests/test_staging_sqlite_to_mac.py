"""Synthetic-only safety and recovery checks; no VPS/network access."""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

SPEC = importlib.util.spec_from_file_location('mac_backup', Path(__file__).parents[1] / 'ops/backup-staging-sqlite-to-mac.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def containers():
    return [{'Id': 'stage-id', 'Name': '/stage-api-1', 'Image': 'sha256:synthetic',
             'State': {'Status': 'running'}, 'HostConfig': {'Memory': 123},
             'Config': {'Labels': {'com.docker.compose.project': 'nexus-remote-stage-synthetic',
                                   'com.docker.compose.service': 'api'}},
             'Mounts': [{'Source': '/synthetic/state', 'Destination': '/var/lib/tradexa',
                         'Type': 'volume', 'Name': 'synthetic-state'}]}]


def select(rows, project='nexus-remote-stage-synthetic', image='sha256:synthetic'):
    return M.select_state(rows, 'stage-api-1', project, image)


def test_select_rejects_production_wrong_image_and_ambiguous_api():
    select(containers())
    for rows, project, image in [(containers(), 'production', 'sha256:synthetic'),
                                 (containers(), 'nexus-remote-stage-synthetic', 'sha256:other'),
                                 (containers() * 2, 'nexus-remote-stage-synthetic', 'sha256:synthetic')]:
        with pytest.raises(M.Blocked):
            select(rows, project, image)


def test_cross_project_stopped_consumer_is_rejected():
    rows = containers()
    other = copy.deepcopy(rows[0])
    other['Id'] = 'stopped-replay'; other['State']['Status'] = 'exited'
    other['Config']['Labels']['com.docker.compose.project'] = 'production'
    rows.append(other)
    with pytest.raises(M.Blocked, match='SHARED_WITH_ANOTHER_PROJECT'):
        select(rows)


def test_source_symlinks_and_hardlinks_are_rejected(tmp_path):
    source = tmp_path / 'source.db'; source.write_bytes(b'synthetic')
    link = tmp_path / 'link.db'; link.symlink_to(source)
    with pytest.raises(M.Blocked, match='SYMLINK'):
        M.file_identity(link)
    hard = tmp_path / 'hard.db'; os.link(source, hard)
    with pytest.raises(M.Blocked, match='UNSHARED'):
        M.file_identity(source)


def test_sidecar_alias_and_pending_rollback_journal_rejected(tmp_path):
    source = tmp_path / 'source.db'; source.write_bytes(b'synthetic')
    wal = Path(str(source) + '-wal'); wal.symlink_to(source)
    with pytest.raises(M.Blocked, match='SIDECAR_SYMLINK'):
        M.check_sidecars(source)
    wal.unlink()
    Path(str(source) + '-journal').write_bytes(b'not-assumed-safe')
    with pytest.raises(M.Blocked, match='ROLLBACK_JOURNAL'):
        M.check_sidecars(source)


@pytest.mark.parametrize('condition', ['free', 'wal', 'scratch', 'deadline', 'identity'])
def test_origin_budget_stops_for_each_bound(tmp_path, monkeypatch, condition):
    source = tmp_path / 'source.db'; source.write_bytes(b'synthetic')
    scratch = tmp_path / 'scratch'; scratch.mkdir()
    identity = M.file_identity(source)
    monkeypatch.setattr(M, 'free_bytes', lambda _: M.RESERVE + 1000000)
    monkeypatch.setattr(M, 'wal_size', lambda _: 0)
    monkeypatch.setattr(M, 'scratch_bytes', lambda _: 0)
    started = time.monotonic()
    M.budget_guard(source, identity, 0, scratch, started)
    if condition == 'free': monkeypatch.setattr(M, 'free_bytes', lambda _: M.RESERVE - 1)
    if condition == 'wal': monkeypatch.setattr(M, 'wal_size', lambda _: M.WAL_GROWTH_CAP + 1)
    if condition == 'scratch': monkeypatch.setattr(M, 'scratch_bytes', lambda _: M.SCRATCH_CAP + 1)
    if condition == 'deadline': started -= M.ORIGIN_SECONDS + 1
    if condition == 'identity': identity = dict(identity, inode=identity['inode'] + 1)
    with pytest.raises(M.Blocked):
        M.budget_guard(source, identity, 0, scratch, started)


def test_origin_can_never_receive_replica_role(tmp_path):
    with pytest.raises(M.Blocked, match='ONLY_ORIGIN_ROLE_ALLOWED'):
        M.origin(str(tmp_path / 'does-not-exist.json'), ['--replica', 'source.db', 'target.db'])


def test_mismatched_official_package_is_rejected():
    with pytest.raises(M.Blocked, match='OFFICIAL_SQLITE_ZIP_HASH_MISMATCH'):
        M.tools_from_zip(b'not-official-bytes', M.MAC_ZIP)


def test_remote_commands_cannot_inject_a_host_or_use_an_unknown_key(tmp_path):
    key = tmp_path / 'synthetic-key'; key.write_text('not-a-real-key')
    for host in ['-oProxyCommand=evil', 'user@host;touch /tmp/evil', 'user@$(id)']:
        with pytest.raises(M.Blocked): M.ssh_base(host, key)
    args = M.ssh_base('synthetic@example.invalid', key)
    assert 'StrictHostKeyChecking=yes' in args and 'BatchMode=yes' in args


def test_stop_process_reaps_only_its_process_group():
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    M.stop_process(process)
    assert process.poll() is not None


def test_bootstrap_does_not_execute_cli_or_accept_an_unpinned_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(M.Path, 'home', lambda: tmp_path)
    with pytest.raises(M.Blocked, match='LINUX_TOOL_HASH_MISMATCH'):
        M.bootstrap({'script': 'c3ludGhldGlj', 'linux_tool': 'YmFk', 'plan': {}})
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def native_tools():
    base = os.environ.get('NEXUS_SYNTHETIC_SQLITE_TOOLS')
    if not base: pytest.skip('Explicit verified native tools needed for local WAL integration')
    root = Path(base)
    prefix = 'linux' if platform.system() == 'Linux' else 'mac'
    for name, key in [('sqlite3_rsync', prefix + '_rsync'), ('sqlite3', prefix + '_sqlite')]:
        assert M.digest(root / name) == M.TOOL_SHA256[key]
    return root


def fixture_db(path):
    db = sqlite3.connect(path)
    db.executescript("""PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;
      CREATE TABLE account(id INTEGER PRIMARY KEY, owner TEXT);
      CREATE TABLE fills(id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER REFERENCES account(id), price REAL, evidence BLOB);
      CREATE INDEX fills_account ON fills(account_id);
      CREATE TABLE journal(id INTEGER, event TEXT);
      CREATE TRIGGER fill_receipt AFTER INSERT ON fills BEGIN INSERT INTO journal VALUES(new.id,'fill'); END;
      INSERT INTO account VALUES(7,'synthetic');""")
    db.executemany('INSERT INTO fills(account_id,price,evidence) VALUES(?,?,?)', [(7, i / 100, bytes([i % 255]) * 64) for i in range(12000)])
    db.commit()
    return db


def test_native_live_wal_snapshot_and_independent_restore(native_tools, tmp_path):
    source = tmp_path / 'source.db'; db = fixture_db(source)
    wal = Path(str(source) + '-wal'); assert wal.stat().st_size > 0
    hashes = {p: M.digest(p) for p in (source, wal)}
    version = db.execute('PRAGMA data_version').fetchone()
    schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name').fetchall()
    snapshot = tmp_path / 'snapshot.db'
    subprocess.run([str(native_tools / 'sqlite3_rsync'), str(source), str(snapshot)], check=True, timeout=30)
    restored = tmp_path / "restore folder with spaces" / 'restored.db'; restored.parent.mkdir()
    result = M.verify_and_restore(native_tools / 'sqlite3', snapshot, restored)
    assert result['full_integrity_and_foreign_keys_passed'] and result['separate_restore_passed']
    for p in (snapshot, restored):
        with sqlite3.connect(p) as copy_db:
            assert copy_db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name').fetchall() == schema
            for name in ('account', 'fills', 'journal', 'sqlite_sequence'):
                assert copy_db.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall() == db.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall()
    assert {p: M.digest(p) for p in (source, wal)} == hashes
    assert db.execute('PRAGMA data_version').fetchone() == version
    db.close()


def test_foreign_key_violation_and_existing_restore_stop(native_tools, tmp_path):
    snapshot = tmp_path / 'bad.db'
    with sqlite3.connect(snapshot) as db:
        db.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(pid REFERENCES parent(id)); INSERT INTO child VALUES(999);')
    with pytest.raises(M.Blocked, match='FOREIGN_KEY_CHECK_FAILED'):
        M.verify_and_restore(native_tools / 'sqlite3', snapshot, tmp_path / 'new.db')
    existing = tmp_path / 'existing.db'; existing.write_bytes(b'preserve')
    with pytest.raises(M.Blocked, match='NEW_DISTINCT_RESTORE_REQUIRED'):
        M.verify_and_restore(native_tools / 'sqlite3', snapshot, existing)
    assert existing.read_bytes() == b'preserve'


def test_changed_container_configuration_cannot_be_accepted(monkeypatch):
    rows = containers()
    plan = {'identities': M.identities(rows)}
    monkeypatch.setattr(M, 'run', lambda _: b'stage-id\n')
    monkeypatch.setattr(M, 'docker', lambda _: rows)
    M.unchanged(plan)
    rows[0]['HostConfig']['Memory'] += 1
    with pytest.raises(M.Blocked, match='CONTAINER_STATE_OR_CONFIGURATION_CHANGED'):
        M.unchanged(plan)


@pytest.mark.skipif(platform.system() != 'Linux', reason='Linux origin resource/IO controls require native Linux')
def test_complete_guarded_origin_protocol_without_network(native_tools, tmp_path):
    import json
    source = tmp_path / 'source.db'; db = fixture_db(source)
    guard_dir = tmp_path / 'guard'; guard_dir.mkdir()
    shutil.copyfile(native_tools / 'sqlite3_rsync', guard_dir / 'sqlite3_rsync')
    (guard_dir / 'sqlite3_rsync').chmod(0o700)
    M.write_json(guard_dir / 'request.json', {'files': [{'name': source.name, 'source': str(source),
                  'exists': True, 'identity': M.file_identity(source)}]})
    # The wire is a local process bridge, never SSH. Docker identity checking is
    # exercised separately; only that external boundary is substituted here.
    harness = tmp_path / 'origin-fixture.py'
    harness.write_text('import sys,importlib.util\n'
                       's=importlib.util.spec_from_file_location("guard",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n'
                       'm.unchanged=lambda p:None\n'
                       'm.origin(sys.argv[2],sys.argv[3:])\n')
    bridge = tmp_path / 'local-wire'
    bridge.write_text('#!' + sys.executable + '\nimport os,sys\n'
                      'assert sys.argv[1:4]==["-e","none","synthetic@fixture.invalid"]\n'
                      'os.execv(sys.executable,[sys.executable,' + repr(str(harness)) + ','
                      + repr(str(SPEC.origin)) + ',' + repr(str(guard_dir / 'request.json')) + ']+sys.argv[5:])\n')
    bridge.chmod(0o700)
    target = tmp_path / source.name / 'source.db'; target.parent.mkdir()
    subprocess.run([str(native_tools / 'sqlite3_rsync'), 'synthetic@fixture.invalid:' + str(source),
                    str(target), '--ssh', str(bridge), '--exe', '/synthetic/guard'], check=True, timeout=30)
    done = json.loads((guard_dir / 'completed-source.db.json').read_text())
    assert done['source_identity'] == M.file_identity(source)
    assert done['vps_free_bytes'] >= M.RESERVE
    assert M.check_snapshot(native_tools / 'sqlite3', target) == M.check_snapshot(native_tools / 'sqlite3', source)
    db.close()
