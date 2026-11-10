"""Read-only lexical retrieval from an exact, hash-pinned four-note preview.

No index, model, network client, publication operation, or recursive corpus scan.
This is an application-level allowlist, not an OS sandbox for same-account agents.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

try:
    from . import _compat  # noqa: F401
except (ImportError, ValueError):
    try:
        import _compat  # noqa: F401
    except ImportError:
        pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def local_regular_file(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('Linked paths are not supported')
    if any(part.lower().startswith('onedrive') or part.lower() == '.git' for part in path.parts):
        raise ValueError('Live/synced and Git sources are excluded')
    if not path.is_file():
        raise ValueError('Expected a local regular file')
    return path


def load_notes(manifest_path, expected_manifest_sha256):
    manifest_path = local_regular_file(manifest_path)
    raw = manifest_path.read_bytes()
    if digest(raw) != expected_manifest_sha256:
        raise ValueError('Preview manifest changed; review and repin explicitly')
    manifest = json.loads(raw)
    if manifest.get('live_publication_authorized') is not False or manifest.get('publication') != 'NOT_PUBLISHED':
        raise ValueError('Only the unpublished reviewed preview is supported')
    notes = manifest.get('notes', [])
    if len(notes) != 4 or len({n['id'] for n in notes}) != 4:
        raise ValueError('Expected exactly four distinct selected notes')
    result = []
    for note in notes:
        if note.get('content_approval') != 'owner-approved' or note.get('publication_state') != 'NOT_PUBLISHED':
            raise ValueError('Unapproved or published material is excluded')
        rel = Path(note['destination_relative'])
        if rel.is_absolute() or '..' in rel.parts or rel.suffix != '.md':
            raise ValueError('Invalid selected-note path')
        path = local_regular_file(note['preview_path'])
        required_path = (manifest_path.parent / 'notes' / rel).resolve()
        if path.resolve() != required_path:
            raise ValueError('Note is outside the exact selected preview')
        data = path.read_bytes()
        if len(data) != note['size'] or digest(data) != note['sha256']:
            raise ValueError('Selected note changed; stale retrieval refused')
        text = data.decode('utf-8')
        if 'privacy: "personal"' not in text.split('---', 2)[1] or 'scopes: ["engineering"]' not in text.split('---', 2)[1]:
            raise ValueError('Expected personal engineering notes')
        result.append((note, path, text))
    return result


def search(manifest_path, expected_manifest_sha256, query, limit=4):
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise ValueError('Query must contain 1 to 300 characters')
    if not isinstance(limit, int) or not 1 <= limit <= 4:
        raise ValueError('Limit must be between 1 and 4')
    terms = set(re.findall(r'[\w-]+', query.lower()))
    if not terms:
        raise ValueError('Query needs at least one word')
    matches = []
    # Validate the ENTIRE selected corpus before returning any result.
    for note, path, text in load_notes(manifest_path, expected_manifest_sha256):
        lines = text.splitlines()
        heading = next(i for i, line in enumerate(lines) if line.startswith('# '))
        scored = [(sum(term in line.lower() for term in terms), i)
                  for i, line in enumerate(lines) if i >= heading and line.strip() and not line.startswith('   Source:')]
        score, index = max(scored, key=lambda pair: (pair[0], pair[1] > heading, -pair[1]))
        if score == 0:
            continue
        matches.append({'note_id': note['id'], 'path': str(path), 'sha256': note['sha256'],
                        'content_approval': 'owner-approved', 'publication_state': 'NOT_PUBLISHED',
                        'privacy': 'personal', 'scope': 'engineering', 'line': index + 1,
                        'excerpt': lines[index], 'source_references': re.findall(r'^   Source: (.+)$', text, re.MULTILINE),
                        'score': score + 2 * sum(term in lines[heading].lower() for term in terms)})
    matches.sort(key=lambda item: (-item['score'], item['note_id']))
    return {'query': query, 'mode': 'local-read-only-reviewed-preview',
            'selected_note_count': 4, 'matches': matches[:limit]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--query', required=True)
    parser.add_argument('--limit', type=int, default=4)
    args = parser.parse_args()
    try:
        print(json.dumps(search(args.manifest, args.manifest_sha256, args.query, args.limit), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, StopIteration) as error:
        parser.exit(2, f'Retrieval refused: {error}\n')


if __name__ == '__main__':
    main()
