#!/usr/bin/env python3
"""Owner's Mac entry point. Run only after reviewing the printed scope.

Uses the owner's existing SSH identity. Never asks for a database password.
The server helper makes a fresh staging backup before changing its one flag.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

HOST = 'ubuntu@57.129.155.230'
ROOT = Path(__file__).resolve().parent
SERVER_SHA = 'a45400d11a4446384a5aca1ef721cf3baca06640665b6e2d01a729738d22e65b'
BACKUP_SHA = '81f07017470558ba6b83787903aebca0a33198fc203d2244ed40c7249f7a5436'
LEDGER_STATUS = 'RETAINED_EXTERNAL_LEDGER_BACKUP_AND_NEW_LOCAL_RESTORE_VERIFIED'


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(4 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()


def ledger_gate(receipt_path):
    receipt_path = Path(receipt_path)
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise RuntimeError('VERIFIED_LEDGER_RECEIPT_REQUIRED')
    receipt = json.loads(receipt_path.read_text())
    required = ('external_application_ledger_recovery_proven',
        'visible_column_order_and_all_recorded_definitions_match',
        'object_ownership_and_grants_match', 'all_selected_table_rows_match_recorded_snapshot',
        'retained_input_files_unchanged', 'local_restore_server_stopped')
    if receipt.get('status') != LEDGER_STATUS or not all(receipt.get(k) is True for k in required):
        raise RuntimeError('LEDGER_RESTORE_DID_NOT_PASS')
    bundle = Path(receipt['original_bundle'])
    seals = receipt.get('input_sha256') or {}
    expected_names = {'application-ledger.dump', 'source-schema.json', 'source-fingerprints.json',
                      'restored-schema.json', 'blocked-receipt.json'}
    if set(seals) != expected_names or bundle.is_symlink():
        raise RuntimeError('RETAINED_LEDGER_INPUTS_REQUIRED')
    for name, digest in seals.items():
        p = bundle / name
        if p.is_symlink() or not p.is_file() or sha(p) != digest:
            raise RuntimeError('RETAINED_LEDGER_INPUT_CHANGED')
    bootstrap = receipt_path.parent / 'restore-bootstrap.json'
    if not bootstrap.is_file() or bootstrap.is_symlink():
        raise RuntimeError('RESTORE_BOOTSTRAP_RECORD_REQUIRED')
    # Only the non-secret gate result goes to the same owner's private VPS folder.
    return {'status': LEDGER_STATUS, 'external_application_ledger_recovery_proven': True,
            'scope': receipt['scope'], 'snapshot_scope': receipt['snapshot_scope'],
            'full_supabase_platform_recovery_proven': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', action='store_true', required=True)
    parser.add_argument('--ledger-receipt', type=Path,
        default=Path.home() / 'Nexus-Backups/external-ledger-verified-5kl1w794/recovery-receipt.json')
    args = parser.parse_args()
    os.umask(0o077)
    proof = ledger_gate(args.ledger_receipt)
    server = ROOT / 'start-task6-staging-acceptance.py'
    backup = ROOT / 'task6-verified-staging-backup.sh'
    for path, expected in ((server, SERVER_SHA), (backup, BACKUP_SHA)):
        if path.is_symlink() or sha(path) != expected:
            raise RuntimeError('OPERATION_HELPER_HASH_MISMATCH')
    key = Path.home() / '.ssh/id_ed25519'
    if not key.is_file():
        raise RuntimeError('EXISTING_OWNER_SSH_IDENTITY_REQUIRED')
    options = ['-i', str(key), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3']
    print('Starting only the existing staging API in research-only acceptance mode; production is excluded.', flush=True)
    r = subprocess.run(['ssh', *options, HOST,
        'umask 077; mktemp -d /home/ubuntu/nexus-task6-acceptance-XXXXXXXX'],
        capture_output=True, text=True, timeout=30, check=True)
    directory = r.stdout.strip()
    if not re.fullmatch(r'/home/ubuntu/nexus-task6-acceptance-[A-Za-z0-9]{8}', directory):
        raise RuntimeError('UNEXPECTED_REMOTE_PRIVATE_DIRECTORY')
    with tempfile.TemporaryDirectory(prefix='nexus-start-') as temporary:
        proof_file = Path(temporary) / 'ledger-gate.json'
        proof_file.write_text(json.dumps(proof))
        for source, name in ((server, 'start.py'), (backup, 'staging-backup.sh'), (proof_file, 'ledger-gate.json')):
            subprocess.run(['scp', '-q', *options, str(source), HOST + ':' + directory + '/' + name],
                           timeout=60, check=True)
    remote = ('chmod 600 ' + directory + '/* && '
        "printf '%s  %s\\n' '" + SERVER_SHA + "' '" + directory + "/start.py' | sha256sum -c - && "
        'sudo -n python3 -I ' + directory + '/start.py --start --run-directory ' + directory)
    # Attach the owner's terminal output. No password is sent in argv or environment.
    result = subprocess.run(['ssh', *options, HOST, remote], check=False)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({'status': 'BLOCKED', 'reason': str(error) if type(error) is RuntimeError else type(error).__name__,
                          'task6_complete': False, 'no_automatic_retry': True}))
        raise SystemExit(1)
