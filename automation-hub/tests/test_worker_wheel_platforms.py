"""Bounded worker wheel inputs; no network or dependency installation."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def wheels():
    path = Path(__file__).resolve().parents[2] / 'ops/prepare-shadow-worker-wheels.py'
    spec = importlib.util.spec_from_file_location('worker_wheels', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PACKAGES = {'websockets': '15.0.1'}
    return module


def asset(platform, payload=b'verified wheel fixture'):
    filename = 'websockets-15.0.1-cp312-cp312-' + platform + '_x86_64.whl'
    return {'filename': filename, 'url': 'https://files.pythonhosted.org/' + filename,
            'size': len(payload), 'digests': {'sha256': hashlib.sha256(payload).hexdigest()}}


def responses(wheels, monkeypatch, assets, payload=b'verified wheel fixture'):
    from io import BytesIO
    def open_url(url, **_):
        return BytesIO(json.dumps({'urls': assets}).encode() if url.endswith('/json') else payload)
    monkeypatch.setattr(wheels, 'urlopen', open_url)


def test_musl_selection_keeps_hashes_and_records_target(wheels, monkeypatch, tmp_path):
    musl = asset('musllinux_1_2')
    responses(wheels, monkeypatch, [asset('manylinux_2_28'), musl])
    manifest = wheels.prepare(tmp_path, platform='musllinux')
    assert [row['filename'] for row in manifest] == [musl['filename']]
    assert manifest[0]['libc'] == 'musl'
    assert manifest[0]['python_abi'] == 'cp312'
    assert manifest[0]['architecture'] == 'x86_64'
    assert '--hash=sha256:' + musl['digests']['sha256'] in (tmp_path / 'requirements-verified.txt').read_text()


def test_default_glibc_selection_is_preserved(wheels, monkeypatch, tmp_path):
    glibc = asset('manylinux_2_28')
    responses(wheels, monkeypatch, [glibc, asset('musllinux_1_2')])
    assert wheels.prepare(tmp_path)[0]['filename'] == glibc['filename']


def test_unknown_platform_rejected_before_network(wheels, monkeypatch, tmp_path):
    monkeypatch.setattr(wheels, 'urlopen', lambda *_args, **_kwargs: pytest.fail('network requested'))
    with pytest.raises(ValueError, match='INVALID_WHEEL_PLATFORM'):
        wheels.prepare(tmp_path, platform='arbitrary-platform')


@pytest.mark.parametrize('filename', [
    'websockets-15.0.1-cp312-cp312-manylinux_2_28_x86_64.whl',
    'websockets-15.0.1-cp311-cp311-musllinux_1_2_x86_64.whl',
    'websockets-15.0.1-cp312-cp312-musllinux_1_2_aarch64.whl',
    'websockets-15.0.1-cp312-cp312-musllinux_1_3_x86_64.whl',
])
def test_incompatible_musl_artifact_fails_closed(wheels, monkeypatch, tmp_path, filename):
    row = asset('musllinux_1_2'); row['filename'] = filename
    responses(wheels, monkeypatch, [row])
    with pytest.raises(ValueError, match='AMBIGUOUS_OR_UNAVAILABLE_PINNED_WHEEL'):
        wheels.prepare(tmp_path, platform='musllinux')


def test_ambiguous_musl_artifacts_are_not_guessed(wheels, monkeypatch, tmp_path):
    responses(wheels, monkeypatch, [asset('musllinux_1_2'), asset('musllinux_1_2')])
    with pytest.raises(ValueError, match='AMBIGUOUS_OR_UNAVAILABLE_PINNED_WHEEL'):
        wheels.prepare(tmp_path, platform='musllinux')


def test_musl_payload_hash_failure_cannot_create_verified_manifest(wheels, monkeypatch, tmp_path):
    responses(wheels, monkeypatch, [asset('musllinux_1_2')], payload=b'corrupt wheel fixture')
    with pytest.raises(ValueError, match='WHEEL_INTEGRITY_FAILED'):
        wheels.prepare(tmp_path, platform='musllinux')
    assert not (tmp_path / 'verified-wheel-manifest.json').exists()


def test_existing_glibc_wheelhouse_cannot_be_relabelled(wheels, monkeypatch, tmp_path):
    old = tmp_path / asset('manylinux_2_28')['filename']; old.write_bytes(b'preserved baseline')
    monkeypatch.setattr(wheels, 'urlopen', lambda *_args, **_kwargs: pytest.fail('network requested'))
    with pytest.raises(ValueError, match='EXISTING_WHEEL_PLATFORM_CONFLICT'):
        wheels.prepare(tmp_path, platform='musllinux')
    assert old.read_bytes() == b'preserved baseline'


def test_existing_selected_wheel_conflict_is_preserved(wheels, monkeypatch, tmp_path):
    selected = asset('musllinux_1_2')
    old = tmp_path / selected['filename']; old.write_bytes(b'preserved conflicting bytes')
    responses(wheels, monkeypatch, [selected])
    with pytest.raises(ValueError, match='EXISTING_WHEEL_CONFLICT'):
        wheels.prepare(tmp_path, platform='musllinux')
    assert old.read_bytes() == b'preserved conflicting bytes'
