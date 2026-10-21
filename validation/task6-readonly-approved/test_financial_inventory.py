import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


def helper():
    path = Path(__file__).with_name('vps-financial-inventory.py')
    spec = importlib.util.spec_from_file_location('inventory', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_metadata_inventory_does_not_modify_rows_and_keeps_missing_files_missing(tmp_path):
    module = helper()
    path = tmp_path / 'account.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE secret_finances(value TEXT)')
        db.execute("INSERT INTO secret_finances VALUES ('never-print-this-financial-value')")
    before = path.read_bytes()
    code = module.CHILD.replace("Path('/var/lib/tradexa')", 'Path(' + repr(str(tmp_path)) + ')')
    result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert 'never-print-this-financial-value' not in result.stdout
    assert report['files'][0]['status'] == 'READ_ONLY_METADATA'
    assert report['financial_recovery_proven'] is False
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['account.db']


def test_unknown_images_block_before_executing_inside_container(monkeypatch):
    module = helper()
    monkeypatch.setattr(module, 'fingerprint', lambda _project: [])
    monkeypatch.setattr(module, 'inspect', lambda _name: {'Image':'unrecognized'})
    def forbidden(*_args, **_kwargs):
        raise AssertionError('must not run any container command')
    monkeypatch.setattr(module, 'docker', forbidden)
    import pytest
    with pytest.raises(RuntimeError, match='EXPECTED_EXISTING'):
        module.main()
