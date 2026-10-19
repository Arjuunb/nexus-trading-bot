#!/usr/bin/env python3
"""Fixed-lock public CPython 3.11 musl inputs; never credentials or application state."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'automation-hub'))
from services.release_source import verify_build_inputs


def parse_lock(text):
    packages = {}; current = None
    for line in text.splitlines():
        line = line.strip().removesuffix('\\').strip()
        if not line or line.startswith('#'): continue
        pin = re.fullmatch(r'([A-Za-z0-9_-]+)==([A-Za-z0-9.+_-]+)', line)
        digest = re.fullmatch(r'--hash=sha256:([a-f0-9]{64})', line)
        if pin:
            current = pin[1].lower().replace('_', '-')
            if current in packages: raise ValueError('DUPLICATE_LOCK_PACKAGE')
            packages[current] = (pin[2], set())
        elif digest and current:
            packages[current][1].add(digest[1])
        else: raise ValueError('EXACT_HASH_LOCK_REQUIRED')
    if not packages or len(packages) > 128 or any(not hashes for _, hashes in packages.values()):
        raise ValueError('EXACT_HASH_LOCK_REQUIRED')
    return packages


def wheel_rank(filename):
    if not filename.endswith('.whl') or Path(filename).name != filename: return None
    try: py, abi, platform = filename[:-4].rsplit('-', 3)[-3:]
    except ValueError: return None
    if platform == 'any' and abi == 'none' and any(p in {'py3', 'py311'} for p in py.split('.')):
        return (0, 0, 0)
    ranks = []
    for tag in platform.split('.'):
        if tag not in {'musllinux_1_1_x86_64', 'musllinux_1_2_x86_64'}: continue
        level = 2 if '1_2' in tag else 1
        if py == abi == 'cp311': ranks.append((3, 11, level))
        if abi == 'abi3' and re.fullmatch(r'cp3\d+', py) and 2 <= int(py[3:]) <= 11:
            ranks.append((2, int(py[3:]), level))
    return max(ranks) if ranks else None


def select_wheel(rows, approved_hashes):
    candidates = [(wheel_rank(row['filename']), row) for row in rows if not row.get('yanked')]
    candidates = [(rank, row) for rank, row in candidates if rank is not None]
    if not candidates: raise ValueError('COMPATIBLE_MUSL_WHEEL_UNAVAILABLE')
    best = max(rank for rank, _ in candidates)
    chosen = [row for rank, row in candidates if rank == best]
    if len(chosen) != 1: raise ValueError('AMBIGUOUS_MUSL_WHEEL')
    row = chosen[0]; parsed = urlsplit(row['url'])
    if parsed.scheme != 'https' or parsed.hostname != 'files.pythonhosted.org' or parsed.username or parsed.password:
        raise ValueError('UNEXPECTED_WHEEL_ORIGIN')
    if row['digests']['sha256'] not in approved_hashes: raise ValueError('WHEEL_NOT_IN_LOCK_HASHES')
    if not 0 < row['size'] < 64 * 1024**2: raise ValueError('WHEEL_SIZE_LIMIT')
    return row


def download_wheel(name, version, hashes, directory):
    with urlopen(f'https://pypi.org/pypi/{name}/{version}/json', timeout=30) as response:
        raw = response.read(8 * 1024**2 + 1)
    if len(raw) > 8 * 1024**2: raise ValueError('PUBLIC_METADATA_LIMIT')
    row = select_wheel(json.loads(raw)['urls'], hashes)
    with urlopen(row['url'], timeout=60) as response: payload = response.read(64 * 1024**2 + 1)
    actual = hashlib.sha256(payload).hexdigest()
    if len(payload) != row['size'] or actual != row['digests']['sha256']:
        raise ValueError('WHEEL_INTEGRITY_FAILED')
    with (Path(directory) / row['filename']).open('xb') as output: output.write(payload)
    return {'name': name, 'version': version, 'filename': row['filename'], 'sha256': actual,
            'bytes': len(payload), 'origin': 'PUBLIC_PYPI_HTTPS', 'hash_in_lock': True}


def prepare(output, *, npm_cache=None):
    out = Path(output).absolute()
    if out.exists() or out.is_symlink() or ROOT == out or ROOT in out.parents or any(p.is_symlink() for p in out.parents):
        raise ValueError('NEW_INPUT_DIRECTORY_OUTSIDE_CHECKOUT_REQUIRED')
    packages = parse_lock((ROOT / 'ops/api-requirements.lock').read_text())
    out.mkdir(mode=0o700, parents=True, exist_ok=False); python = out / 'python'; python.mkdir()
    records = [download_wheel(name, version, hashes, python) for name, (version, hashes) in sorted(packages.items())]
    total = sum(row['bytes'] for row in records)
    if npm_cache is not None:
        cache = Path(npm_cache).resolve() / '_cacache'
        if not cache.is_dir() or Path(npm_cache).is_symlink(): raise ValueError('PUBLIC_NPM_CACHE_REQUIRED')
        rows = list(cache.rglob('*'))
        if len(rows) > 20000 or any(p.is_symlink() for p in rows): raise ValueError('PUBLIC_CACHE_PATH_LIMIT')
        total += sum(p.stat().st_size for p in rows if p.is_file())
        if total > 512 * 1024**2: raise ValueError('PUBLIC_BUILD_INPUT_SIZE_LIMIT')
        shutil.copytree(cache, out / 'npm-cache/_cacache')
    manifest = {'scope': 'PUBLIC_LOCKED_PACKAGES', 'files': [
        {'path': str(p.relative_to(out)), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted(out.rglob('*')) if p.is_file()]}
    (out / 'input-manifest.json').write_text(json.dumps(manifest, indent=2))
    identity = verify_build_inputs(out)
    return {'schema_version': 'nexus_api_musl_inputs.v1', 'python': '3.11', 'platform': 'musllinux_x86_64',
            'dependency_lock_sha256': hashlib.sha256((ROOT / 'ops/api-requirements.lock').read_bytes()).hexdigest(),
            'input_manifest_sha256': identity, 'packages': records, 'credentials_included': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('output'); p.add_argument('--npm-cache')
    args = p.parse_args(); print(json.dumps(prepare(args.output, npm_cache=args.npm_cache), indent=2))
