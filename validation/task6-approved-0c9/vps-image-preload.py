"""Verify Task 6 run 39813894000; optionally cache images without deployment.

Download the existing, SHA-pinned loader as preload-engine.py alongside this
file. The loader runs isolated source probes without network, state mounts,
credentials or application imports. It never replaces running services.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path

ENGINE_SHA256 = 'e86f615966ed1c7cb467337c93024d49f008d41516755ab2775336220b9cf9fa'
POLICY = {
    'commit': '0c9ff8b1f5c29f07fb11b0cd14136457e24251e4',
    'source_sha256': '5eb5f4429238db93d3467ef48fec97286d6f09a4d0740826c2887a8b41d449c8',
    'source_files': 1522,
    'zip_sha256': 'd3a970b68f6036d24a8c074445a6d1271eecf8c7c69b570b4618548a524acc25',
    'zip_bytes': 176461808,
    'images': {
        'api': {
            'config_id': 'sha256:dc32782877bd8d09acbcc6fd9f13e25af58016fe0ee1b5f074c5332bacbb5bb8',
            'oci_manifest_digest': 'sha256:570fe0b80db6243abac82fb680292023e24c63451e6da406b5f0d146ad01efea',
            'source_files': 1522,
        },
        'worker': {
            'config_id': 'sha256:d27d8803c8126736a85fcbabe3dede2be3aca4aed00079e89480652b06c5e555',
            'oci_manifest_digest': 'sha256:213e5ed1454d6799fd5de587e5bc7abd08980ed3bdeabb9dbae626bac1791d3b',
            'source_files': 322,
        },
    },
}


def load_engine(path=None):
    path = Path(path) if path is not None else Path(__file__).with_name('preload-engine.py')
    if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != ENGINE_SHA256:
        raise RuntimeError('PINNED_PRELOAD_ENGINE_REQUIRED')
    spec = importlib.util.spec_from_file_location('task6_pinned_preload_engine', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packet', type=Path)
    parser.add_argument('--load', action='store_true', help='Load only the image cache and run isolated probes.')
    args = parser.parse_args(argv)
    engine = load_engine()
    if args.load:
        report = engine.preload(args.packet, policy=POLICY)
    else:
        verified = engine.verify_packet(args.packet, policy=POLICY)
        report = {
            'status': 'APPROVED_PACKET_VERIFIED_NO_IMAGES_LOADED',
            'commit': POLICY['commit'], 'source_sha256': POLICY['source_sha256'],
            'zip_sha256': POLICY['zip_sha256'],
            'sealed_files_verified': verified['sealed_files_verified'],
            'exports': verified['exports'], 'ci_security_reports': verified['scans'],
            'services_updated': False, 'application_started': False,
            'deployment_performed': False, 'new_paper_execution': False,
            'task6_complete': False,
        }
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({
            'status': 'BLOCKED', 'error_type': type(error).__name__,
            'reason': str(error) if type(error).__name__ == 'Blocked' or type(error) is RuntimeError else None,
            'detail': getattr(error, 'detail', None),
            'receipt_directory': getattr(error, 'receipt_directory', None),
            'services_updated': False, 'deployment_performed': False, 'task6_complete': False,
        }), flush=True)
        raise SystemExit(1)
