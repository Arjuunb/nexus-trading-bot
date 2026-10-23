"""Prepare a bounded, checksum-verified local build context from public PyPI.

No credentials, registry override, TLS bypass or arbitrary package selection.
The local worker image targets Linux amd64 / CPython 3.12 only.
"""
import hashlib
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlparse
from urllib.request import urlopen

PACKAGES={'setuptools':'80.10.2','wheel':'0.46.2','packaging':'25.0','websockets':'15.0.1',
    'psycopg':'3.3.6','psycopg-binary':'3.3.6','typing-extensions':'4.16.0',
    'requests':'2.34.2','certifi':'2026.10.19','charset-normalizer':'3.4.7','idna':'3.18','urllib3':'2.8.0'}


def prepare(directory, *, platform='manylinux'):
    if platform not in {'manylinux', 'musllinux'}:
        raise ValueError('INVALID_WHEEL_PLATFORM')
    target=Path(directory);target.mkdir(parents=True,exist_ok=True)
    incompatible = 'manylinux' if platform == 'musllinux' else 'musllinux'
    if any(incompatible in path.name for path in target.glob('*.whl')):
        raise ValueError('EXISTING_WHEEL_PLATFORM_CONFLICT')
    manifest=[]
    for name,version in PACKAGES.items():
        with urlopen(f'https://pypi.org/pypi/{name}/{version}/json',timeout=30) as response:
            metadata=json.loads(response.read(2*1024*1024))
        candidates=[r for r in metadata['urls'] if r['filename'].endswith('.whl') and (
            r['filename'].endswith('-py3-none-any.whl') if name not in {'websockets','psycopg-binary'} else
            (r['filename'].endswith('-cp312-cp312-musllinux_1_2_x86_64.whl') if platform == 'musllinux' else
             'cp312-cp312' in r['filename'] and 'manylinux' in r['filename'] and r['filename'].endswith('_x86_64.whl')))]
        if len(candidates)!=1: raise ValueError('AMBIGUOUS_OR_UNAVAILABLE_PINNED_WHEEL:'+name)
        wheel=candidates[0];url=urlparse(wheel['url'])
        if Path(wheel['filename']).name!=wheel['filename']: raise ValueError('INVALID_WHEEL_FILENAME')
        if url.scheme!='https' or url.hostname!='files.pythonhosted.org': raise ValueError('UNEXPECTED_WHEEL_ORIGIN')
        if not 0<wheel['size']<8*1024*1024: raise ValueError('WHEEL_SIZE_LIMIT')
        with urlopen(wheel['url'],timeout=30) as response: payload=response.read(8*1024*1024+1)
        actual=hashlib.sha256(payload).hexdigest()
        if len(payload)!=wheel['size'] or actual!=wheel['digests']['sha256']: raise ValueError('WHEEL_INTEGRITY_FAILED')
        path=target/wheel['filename']
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=actual: raise ValueError('EXISTING_WHEEL_CONFLICT')
        path.write_bytes(payload)
        manifest.append({'name':name,'version':version,'filename':wheel['filename'],'sha256':actual,'bytes':len(payload),'origin':'PUBLIC_PYPI_HTTPS',
                         'libc':'musl' if platform == 'musllinux' else 'glibc',
                         'python_abi':'cp312','architecture':'x86_64',
                         'wheel_platform':wheel['filename'][:-4].rsplit('-',1)[-1]})
    (target/'verified-wheel-manifest.json').write_text(json.dumps(manifest,indent=2))
    (target/'requirements-verified.txt').write_text(''.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n" for r in manifest))
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Prepare fixed-version verified CPython 3.12 worker wheels')
    parser.add_argument('directory')
    parser.add_argument('--platform',choices=('manylinux','musllinux'),default='manylinux')
    args=parser.parse_args()
    print(json.dumps(prepare(args.directory,platform=args.platform),indent=2))
