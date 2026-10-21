import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


def helper():
    path = Path(__file__).with_name('vps-recovery-capacity.py')
    spec = importlib.util.spec_from_file_location('capacity', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(url, key='never-print-this-key'):
    return {'Config': {'Env': ['SUPABASE_URL='+url, 'SUPABASE_KEY='+key]}}


def test_provider_comparison_checks_host_without_revealing_values():
    module = helper()
    result = module.provider_comparison(row('https://private.example'), [row('https://PRIVATE.example/')])
    assert result['provider_hostname_overlaps_production'] is True
    assert result['external_provider_queried'] is False
    assert 'private.example' not in json.dumps(result)
    assert 'never-print-this-key' not in json.dumps(result)
    assert module.provider_comparison(row('https://private.example'), [])['status'] == 'UNPROVEN'


def test_prior_recovery_sizes_are_read_only_and_reject_symlinks(tmp_path):
    module = helper()
    directory = tmp_path / 'task6-sqlite-recovery-20261021T000000Z-012345abcdef'
    directory.mkdir()
    file = directory / 'private-archive.gz'
    file.write_bytes(b'keep-private')
    before = file.read_bytes()
    result = module.private_recovery_sizes(tmp_path)
    assert result[0]['logical_bytes'] == len(before)
    assert 'keep-private' not in json.dumps(result)
    assert file.read_bytes() == before
    (directory/'link').symlink_to(file)
    with pytest.raises(RuntimeError, match='SYMLINK'):
        module.private_recovery_sizes(tmp_path)


def test_owned_scratch_inspection_preserves_files_and_reports_unowned(tmp_path):
    module = helper()
    directory = tmp_path / ('.task6-sqlite-recovery-'+'a'*32)
    directory.mkdir()
    raw = directory/'owned-snapshot.sqlite'
    raw.write_bytes(b'keep-financial-snapshot')
    code = module.OWNED_SCRATCH_CODE.replace("Path('/var/lib/tradexa')", 'Path('+repr(str(tmp_path))+')')
    result = subprocess.run([sys.executable, '-B', '-c', code], check=True, capture_output=True, text=True)
    report = json.loads(result.stdout)
    assert report[0]['ownership_marker_matches'] is False
    assert raw.read_bytes() == b'keep-financial-snapshot'
    assert 'keep-financial-snapshot' not in result.stdout


def test_inventory_dependency_is_checksum_pinned(tmp_path, monkeypatch):
    module = helper()
    module.load_inventory()
    (tmp_path/'vps-financial-inventory.py').write_text("raise AssertionError('must not execute altered code')")
    monkeypatch.setattr(module, '__file__', str(tmp_path/'vps-recovery-capacity.py'))
    with pytest.raises(RuntimeError, match='PINNED_INVENTORY_REQUIRED'):
        module.load_inventory()
