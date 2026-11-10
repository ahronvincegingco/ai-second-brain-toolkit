"""Read-only snapshot of a supplied vault, restore only into private disposable staging."""
import argparse
import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from migrate import private_root, write_new, json_bytes


def inventory(vault):
    files, directories = {}, []
    for current, dirs, names in os.walk(vault, followlinks=False):
        for name in dirs + names:
            p = Path(current) / name
            st = p.lstat()
            tag = getattr(st, 'st_reparse_tag', 0)
            # No namespace-surrogate links; cloud placeholders may require hydration.
            if p.is_symlink() or getattr(p, 'is_junction', lambda: False)() or tag & 0x20000000:
                raise ValueError('Unsupported linked vault entry')
        directories += [(Path(current) / name).relative_to(vault).as_posix() for name in dirs]
        for name in names:
            p = Path(current) / name
            content = p.read_bytes()
            files[p.relative_to(vault).as_posix()] = {'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}
    return {'files': files, 'directories': sorted(directories)}


def snapshot(vault, workspace):
    root = private_root(workspace)
    vault = Path(vault).resolve()
    if root.is_relative_to(vault) or vault.is_relative_to(root):
        raise ValueError('Backup and source must be separate')
    before = inventory(vault)
    target = root / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target.mkdir(parents=True, exist_ok=False)
    archive = target / 'vault.zip'
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as z:
        for rel in before['directories']:
            z.writestr(rel + '/', b'')
        for rel in sorted(before['files']):
            z.write(vault / rel, rel)
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError('Archive CRC failure')
        actual = {i.filename: {'sha256': hashlib.sha256(z.read(i)).hexdigest(), 'size': i.file_size}
                  for i in z.infolist() if not i.is_dir()}
        if actual != before['files']:
            raise ValueError('Archive bytes differ')
        restored = target / 'restore-verification'
        restored.mkdir()
        # These names came from our own file inventory, not an untrusted archive.
        z.extractall(restored)
    if inventory(restored) != before or inventory(vault) != before:
        raise ValueError('Restore mismatch or source changed during snapshot')
    write_new(target / 'manifest.json', json_bytes(before))
    result = {'archive': str(archive), 'manifest': str(target / 'manifest.json'),
        'restore_directory': str(restored), 'files': len(before['files']),
        'directories': len(before['directories']),
        'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'archive_crc': 'PASS', 'restored_bytes': 'PASS', 'source_unchanged': True,
        'independent_verification': 'pending separate verifier',
        'limits': 'Same disk, no disaster recovery or continuous OneDrive/offline-pinning proof'}
    write_new(target / 'result.json', json_bytes(result))
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vault', required=True)
    p.add_argument('--workspace', required=True)
    a = p.parse_args()
    print(json.dumps(snapshot(a.vault, a.workspace), indent=2))
