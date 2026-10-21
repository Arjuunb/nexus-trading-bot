"""Read-only capacity and provider-binding check; never changes or removes state."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

INVENTORY_SHA = '474b99e467d43f869394c05f30ffebff15cb9bbb8318266159c74e46909791b5'
RESERVE = 8 * 1024**3
ARCHIVE_CAP = 4 * 1024**3


def load_inventory():
    path = Path(__file__).with_name('vps-financial-inventory.py')
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != INVENTORY_SHA:
        raise RuntimeError('PINNED_INVENTORY_REQUIRED')
    spec = importlib.util.spec_from_file_location('pinned_inventory', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment(row):
    return dict(item.split('=', 1) for item in row['Config'].get('Env', []) if '=' in item)


def provider_comparison(stage, production):
    current = environment(stage)
    configured = bool(current.get('SUPABASE_URL') and current.get('SUPABASE_KEY'))
    observed = [environment(row) for row in production]
    observed = [row for row in observed if row.get('SUPABASE_URL')]
    def host(value):
        target = urlsplit(value)
        if target.scheme not in ('https', 'http') or not target.hostname:
            raise RuntimeError('PROVIDER_BINDING_NOT_COMPARABLE')
        return target.hostname.lower()
    overlap = configured and any(host(row['SUPABASE_URL']) == host(current['SUPABASE_URL'])
                                 for row in observed)
    return {'external_primary_configured': configured,
            'production_provider_bindings_observed': bool(observed),
            'provider_hostname_overlaps_production': bool(overlap),
            'status': 'OVERLAP' if overlap else ('NO_OBSERVED_OVERLAP' if observed else 'UNPROVEN'),
            'external_provider_queried': False, 'external_recovery_proven': False}


def private_recovery_sizes(base):
    """Stat only recognized prior recovery directories, with hard traversal bounds."""
    results = []
    for directory in sorted(base.glob('task6-sqlite-recovery-*')):
        if len(results) >= 64:
            raise RuntimeError('RECOVERY_DIRECTORY_COUNT_BOUND')
        if directory.is_symlink() or not re.fullmatch(r'task6-sqlite-recovery-\d{8}T\d{6}Z-[a-f0-9]{12}', directory.name):
            raise RuntimeError('RECOVERY_DIRECTORY_REQUIRES_REVIEW')
        total = allocated = count = 0
        for root, dirs, files in os.walk(directory, followlinks=False):
            for name in [*dirs, *files]:
                path = Path(root) / name
                info = path.lstat()
                count += 1
                if count > 4096 or stat.S_ISLNK(info.st_mode):
                    raise RuntimeError('RECOVERY_TRAVERSAL_BOUND_OR_SYMLINK')
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    allocated += info.st_blocks * 512
        results.append({'directory': directory.name, 'files_and_directories': count,
                        'logical_bytes': total, 'allocated_bytes': allocated})
    return results


OWNED_SCRATCH_CODE = r'''
import json,re
from pathlib import Path
root=Path('/var/lib/tradexa'); rows=[]
for p in sorted(root.glob('.task6-sqlite-recovery-*')):
 if len(rows)>=64:raise RuntimeError('RECOVERY_DIRECTORY_COUNT_BOUND')
 if p.is_symlink() or not re.fullmatch(r'\.task6-sqlite-recovery-[a-f0-9]{32}',p.name):
  raise RuntimeError('RECOVERY_DIRECTORY_REQUIRES_REVIEW')
 marker=p/'ownership.json';raw=p/'owned-snapshot.sqlite'
 owned=False
 if marker.is_file() and not marker.is_symlink() and marker.stat().st_size<=4096:
  m=json.loads(marker.read_text());owned=m=={'token':p.name.rsplit('-',1)[1],'purpose':'TASK6_OWNED_TEMPORARIES_ONLY'}
 row={'directory':p.name,'ownership_marker_matches':owned,'scratch_exists':raw.is_file()}
 if raw.is_symlink():raise RuntimeError('SCRATCH_SYMLINK_REQUIRES_REVIEW')
 if raw.is_file():row.update(scratch_bytes=raw.stat().st_size,scratch_allocated_bytes=raw.stat().st_blocks*512)
 rows.append(row)
print(json.dumps(rows))
'''


def main():
    inventory = load_inventory()
    before = {p: inventory.fingerprint(p) for p in (inventory.STAGE, 'nexus-trading-bot')}
    stream = io.StringIO()
    with redirect_stdout(stream):
        inventory.main()
    current = json.loads(stream.getvalue())
    api = inventory.inspect(inventory.API)
    ids = inventory.docker(['ps', '-aq', '--filter', 'label=com.docker.compose.project=nexus-trading-bot']).split()
    if not ids:
        raise RuntimeError('PRODUCTION_IDENTITIES_REQUIRED_FOR_COMPARISON')
    production = json.loads(inventory.docker(['inspect', *ids]))
    if any(m['Source'] == p['Source'] for m in api['Mounts'] for row in production for p in row['Mounts']):
        raise RuntimeError('STAGING_PRODUCTION_SHARED_MOUNT')
    provider = provider_comparison(api, production)
    scratch = json.loads(inventory.docker(['exec', inventory.API, 'python', '-B', '-c', OWNED_SCRATCH_CODE]))
    directories = private_recovery_sizes(inventory.BASE)
    if before != {p: inventory.fingerprint(p) for p in before}:
        raise RuntimeError('CONTAINER_IDENTITIES_OR_MOUNTS_CHANGED')
    if any(row['status'] not in ('READ_ONLY_METADATA', 'MISSING') for row in current['files']):
        raise RuntimeError('DATABASE_CAPACITY_NOT_VERIFIED')
    largest = max(row.get('page_size', 0) * row.get('page_count', 0) for row in current['files'])
    required = largest + ARCHIVE_CAP + RESERVE
    print(json.dumps({'status': 'READ_ONLY_RECOVERY_CAPACITY_ONLY',
        'free_disk_bytes': current['free_disk_bytes'], 'largest_selected_database_bytes': largest,
        'unchanged_reserve_bytes': RESERVE, 'unchanged_archive_cap_bytes': ARCHIVE_CAP,
        'minimum_sequential_space_bytes': required,
        'additional_bytes_required': max(0, required-current['free_disk_bytes']),
        'existing_private_host_recoveries': directories, 'existing_container_scratch': scratch,
        'provider_binding_comparison': provider,
        'production_and_staging_identities_unchanged': True,
        'provider_credentials_printed': False, 'files_removed': False,
        'services_updated': False, 'financial_recovery_proven': False, 'task6_complete': False}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        reason = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        print(json.dumps({'status': 'BLOCKED', 'reason': reason,
                          'services_updated': False, 'files_removed': False, 'task6_complete': False}))
        raise SystemExit(1)
