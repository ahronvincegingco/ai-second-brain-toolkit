"""Explicit maintainer operation: refresh wheel hashes without changing package pins.

Reads official PyPI release metadata over HTTPS. Never installs or executes packages.
Review the resulting diff before committing. Existing hashes remain trusted; new
hashes cover wheels only. No source builds are enabled by the installer.
"""
import argparse
import json
from pathlib import Path
import re
from urllib.request import urlopen


def refresh(path):
    lines = path.read_text(encoding='utf-8').splitlines()
    pins = []
    for line in lines:
        if not line or line.startswith(('#', ' ', '\t')):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^ ;]+)(?:.*)', line.rstrip(' \\'))
        if not match:
            raise ValueError(f'Unsupported requirement: {line}')
        name, version = match.groups()
        with urlopen(f'https://pypi.org/pypi/{name}/{version}/json', timeout=30) as response:
            metadata = json.load(response)
        hashes = {item['digests']['sha256'] for item in metadata['urls']
                  if item['packagetype'] == 'bdist_wheel' and not item['yanked']}
        if not hashes or not all(re.fullmatch(r'[a-f0-9]{64}', value) for value in hashes):
            raise ValueError(f'No valid wheels for {name}=={version}')
        # Preserve the previously reviewed artifacts as well.
        start = lines.index(line)
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i] and not lines[i].startswith((' ', '\t'))), len(lines))
        hashes.update(re.findall(r'--hash=sha256:([a-f0-9]{64})', '\n'.join(lines[start:end])))
        marker = '; sys_platform == "win32"' if name.lower() == 'pywin32' else ''
        pins.append(f'{name}=={version}{marker} \\\n' + ' \\\n'.join(f'    --hash=sha256:{value}' for value in sorted(hashes)))
    header = ('# Exact existing pins; wheel hashes from official PyPI release JSON.\n'
              '# Regenerate explicitly: python scripts/refresh_lock_hashes.py <lockfile>\n'
              '# Install with --require-hashes --only-binary=:all:; pywin32 is Windows-only.\n')
    path.write_text(header + '\n'.join(pins) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lockfile', type=Path)
    refresh(parser.parse_args().lockfile)
