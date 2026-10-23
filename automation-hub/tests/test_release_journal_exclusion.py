"""Release inputs cannot carry generated journal history; originals stay intact."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from services.release_source import bundle_source

ROOT = Path(__file__).resolve().parents[2]
NAMES = ('strategy_v2_research_ledger.jsonl', 'strategy_v2_verdict_ledger.jsonl',
         'strategy_v3_research_ledger.jsonl')
HEAD = 'a' * 40
SOURCE = 'b' * 64


def release():
    spec = importlib.util.spec_from_file_location('journal_release', ROOT / 'ops/github-release.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('name', NAMES)
def test_bundle_excludes_journal_before_read_and_preserves_other_jsonl(tmp_path, monkeypatch, name):
    root = tmp_path / 'source'; root.mkdir()
    journal = root / 'automation-hub/data' / name; journal.parent.mkdir(parents=True)
    original = b'{"fixture": "generated history; not performance evidence"}\n'
    journal.write_bytes(original); (root / 'code.py').write_text('VALUE=1\n')
    (root / 'attestation.jsonl').write_text('{"fixture": "immutable attestation"}\n')
    before = Path.read_bytes
    def guarded(path):
        if path == journal: pytest.fail('excluded journal contents were read')
        return before(path)
    monkeypatch.setattr(Path, 'read_bytes', guarded)
    result = bundle_source(root, tmp_path / 'out',
        ['code.py', 'attestation.jsonl', str(journal.relative_to(root))], head=HEAD, dirty_paths={})
    assert {r['path'] for r in result['files']} == {'code.py', 'attestation.jsonl'}
    assert not (tmp_path / 'out/context/automation-hub/data' / name).exists()
    assert before(journal) == original


@pytest.mark.parametrize('name', NAMES)
def test_tracked_generated_journal_is_excluded_without_weakening_private_input_gate(tmp_path, name):
    root = tmp_path / 'source'; root.mkdir(); (root / 'code.py').write_text('VALUE=1\n')
    path = 'automation-hub/data/' + name
    # Missing original is intentional: exclusion must happen before even opening it.
    result = release().freeze_source(root, tmp_path / 'frozen', ['code.py', path],
        head=HEAD, dirty={}, tracked={'code.py', path})
    assert [r['path'] for r in result['files']] == ['code.py']
    policy = json.loads((tmp_path / 'frozen/source-policy.json').read_text())
    assert path in policy['excluded_without_reading']
    assert policy['source_exclusion_policy'] == 'nexus_source_exclusion.v2'
    with pytest.raises(ValueError, match='PRIVATE|STATE'):
        release().freeze_source(root, tmp_path / 'unsafe', ['state/orders.json'],
            head=HEAD, dirty={}, tracked={'state/orders.json'})


@pytest.mark.parametrize('name', NAMES)
def test_accidental_renesting_cannot_bypass_packet_journal_exclusion(tmp_path, name):
    root = tmp_path / 'packet'; root.mkdir(); path = root / 'nested' / name
    path.parent.mkdir(); path.write_text('{"fixture": true}\n')
    with pytest.raises(ValueError, match='PRIVATE|JOURNAL'):
        release().seal_packet(root, commit=HEAD, source_sha=SOURCE)
    assert path.is_file()


@pytest.mark.parametrize('name', NAMES)
def test_obsolete_journal_bearing_source_cannot_enter_image_verification(monkeypatch, name):
    module = release()
    monkeypatch.setattr(module, 'run', lambda *a, **k: pytest.fail('Docker invoked before source rejection'))
    source = {'head': HEAD, 'source_sha256': SOURCE,
              'files': [{'path': 'automation-hub/data/' + name, 'sha256': 'c' * 64}]}
    with pytest.raises(ValueError, match='JOURNAL'):
        module.verify_local_images(source, 'unused-api', 'unused-worker')


@pytest.mark.parametrize('name', NAMES)
def test_existing_forged_packet_hashes_do_not_authorize_journal_payload(tmp_path, name):
    root = tmp_path / 'packet'; root.mkdir(); payload = b'{"fixture": true}\n'
    (root / name).write_bytes(payload)
    (root / 'packet-integrity.json').write_text(json.dumps({'commit': HEAD, 'source_sha256': SOURCE,
        'files': [{'path': name, 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}]}))
    with pytest.raises(ValueError, match='PRIVATE|JOURNAL'):
        release().verify_packet(root, commit=HEAD, source_sha=SOURCE)


@pytest.mark.parametrize('name', NAMES)
def test_docker_context_and_git_ignore_explicitly_cover_generated_journal_names(name):
    assert '**/' + name in (ROOT / '.dockerignore').read_text().splitlines()
    assert name in (ROOT / '.gitignore').read_text().splitlines()


def test_image_journal_guard_accepts_absence_and_rejects_unknown_or_present():
    module = release()
    assert module.validate_image_journal_exclusion([]) == {'generated_journals_absent': True}
    for bad in (None, ['automation-hub/data/' + NAMES[0]]):
        with pytest.raises(ValueError, match='JOURNAL'):
            module.validate_image_journal_exclusion(bad)


@pytest.mark.parametrize('name', NAMES)
def test_reviewed_name_cannot_bypass_tracked_private_directory_rejection(tmp_path, name):
    root = tmp_path / 'source'; root.mkdir()
    with pytest.raises(ValueError, match='PRIVATE|STATE'):
        release().freeze_source(root, tmp_path / 'private', ['.ssh/' + name],
            head=HEAD, dirty={}, tracked={'.ssh/' + name})
