"""Original fixtures are preserved. Failure artifacts never become complete."""
from pathlib import Path
import json
import sqlite3
import pytest
from services.staging_recovery import seal_recovery_bundle, verify_recovery_bundle


def test_backup_references_survive_recovery_and_changed_archive_is_blocked(tmp_path):
    source=tmp_path/'source';source.mkdir()
    (source/'metadata.dump').write_bytes(b'PG_FIXTURE_ONLY')
    (source/'dataset.json').write_text('{"data":"immutable fixture"}')
    before={p.name:p.read_bytes() for p in source.iterdir()}
    result=seal_recovery_bundle(source,tmp_path/'bundle',files=['metadata.dump','dataset.json'],authority='disposable:fixture')
    assert result['status']=='COMPLETE' and result['off_host_durability'] is False
    assert verify_recovery_bundle(tmp_path/'bundle')['status']=='INTEGRITY_VERIFIED'
    assert before=={p.name:p.read_bytes() for p in source.iterdir()}
    (tmp_path/'bundle/dataset.json').write_text('changed')
    with pytest.raises(ValueError,match='INTEGRITY'):verify_recovery_bundle(tmp_path/'bundle')


def test_interrupted_copy_never_commits_complete_manifest(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir();(source/'one').write_text('one');(source/'two').write_text('two')
    import services.staging_recovery as service
    copy=service.shutil.copyfile
    def interrupted(a,b):
        if Path(a).name=='two':raise OSError('fixture interruption')
        return copy(a,b)
    monkeypatch.setattr(service.shutil,'copyfile',interrupted)
    with pytest.raises(OSError):seal_recovery_bundle(source,tmp_path/'bundle',files=['one','two'],authority='disposable:fixture')
    assert not (tmp_path/'bundle').exists()
    assert not list(tmp_path.glob('*.partial/backup-manifest.json'))


@pytest.mark.parametrize('name',['../private','/absolute','.env','tls.key','paper.db'])
def test_recovery_bundle_cannot_accidentally_archive_private_or_financial_inputs(tmp_path,name):
    with pytest.raises(ValueError,match='PATH|PRIVATE|FINANCIAL'):
        seal_recovery_bundle(tmp_path,tmp_path/'bundle',files=[name],authority='disposable:fixture')


def test_duplicate_cutover_identity_is_reported_before_import(tmp_path):
    from services.metadata_cutover import frozen_export
    paths=[]
    for n in range(2):
        p=tmp_path/f'source{n}.sqlite';paths.append(p)
        with sqlite3.connect(p) as db:
            db.execute('CREATE TABLE operational_sources(owner_id TEXT,source_id TEXT,scope_json TEXT,created_at TEXT,PRIMARY KEY(owner_id,source_id))')
            db.execute('INSERT INTO operational_sources VALUES(?,?,?,?)',('alice','same','{}','2026-10-13'))
    x=frozen_export(paths,tmp_path/'snapshots')
    assert x['status']=='BLOCKED'
    assert any(b['code']=='DUPLICATE_METADATA_IDENTITY' for b in x['blockers'])
