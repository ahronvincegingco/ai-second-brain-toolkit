"""Hash-pinned, allowlisted Markdown reads. No index, network, or write operations.

The grant file is configured by the owner, never supplied by a model tool argument.
It is an application boundary, not an OS sandbox against same-account processes.
"""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote

try:
    from . import _compat  # noqa: F401
except (ImportError, ValueError):
    try:
        import _compat  # noqa: F401
    except ImportError:
        pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked_path(path, directory=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Absolute configured paths required')
    for part in [path, *path.parents]:
        st = part.lstat()
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)() or getattr(st, 'st_reparse_tag', 0) & 0x20000000:
            raise ValueError('Redirected paths refused')
    if directory:
        if not path.is_dir():
            raise ValueError('Expected corpus directory')
    elif not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError('Expected non-hardlinked regular file')
    return path


def load_selected(manifest_path, manifest_sha256, client):
    raw = checked_path(manifest_path).read_bytes()
    if digest(raw) != manifest_sha256:
        raise ValueError('Manifest changed; explicit review and repin required')
    grant = json.loads(raw)
    if grant.get('schema') != 'selected-memory-readonly-v1':
        raise ValueError('Unsupported grant schema')
    if client not in ('codex-openai', 'claude-anthropic') or grant.get('client') != client:
        raise ValueError('Client not authorized')
    authorization = grant.get('owner_authorization', {})
    if authorization.get('state') != 'approved' or not authorization.get('reference'):
        raise ValueError('Explicit owner authorization required')
    kind = grant.get('source_kind')
    root = checked_path(grant['root'], directory=True)
    if kind == 'synthetic':
        if 'test-vault' not in [part.casefold() for part in root.parts]:
            raise ValueError('Synthetic corpus must be in the disposable test vault')
    elif kind == 'canonical':
        if authorization.get('personal_content_external_model_approved') is not True:
            raise ValueError('Separate personal-content external-model approval required')
    else:
        raise ValueError('Unsupported corpus kind')
    notes = grant.get('notes', [])
    if not 1 <= len(notes) <= 16:
        raise ValueError('Select 1 to 16 exact notes')
    seen_ids, seen_paths, result = set(), set(), []
    # Validate every selected byte before returning any excerpt. No recursive scan.
    for note in notes:
        note_id, relative = note['id'], note['path']
        rel = PurePosixPath(relative)
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', note_id) or note_id in seen_ids
                or rel.is_absolute() or rel.suffix != '.md' or str(rel) != relative
                or any(part in ('..', '.', '.git', '.obsidian') for part in rel.parts)
                or any(c in relative for c in '\\:*?"<>|')
                or relative.casefold() in seen_paths):
            raise ValueError('Invalid or duplicate selected-note identity/path')
        seen_ids.add(note_id)
        seen_paths.add(relative.casefold())
        path = checked_path(root.joinpath(*rel.parts))
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Selected note escapes configured corpus')
        data = path.read_bytes()
        if len(data) != note['size'] or digest(data) != note['sha256']:
            raise ValueError('Selected note changed; stale retrieval refused')
        if len(data) > 128000:
            raise ValueError('Selected note too large')
        text = data.decode('utf-8')
        if kind == 'synthetic' and (not text.startswith('---\n')
                                    or 'privacy: "synthetic"' not in text.split('---', 2)[1]):
            raise ValueError('Synthetic-only configuration refuses personal material')
        lines = text.splitlines()
        result.append((note, path, lines))
    return grant, result


def citation(grant, note, path):
    return {'note_id': note['id'], 'path': note['path'], 'relative_path': note['path'],
            'sha256': note['sha256'], 'source_kind': grant['source_kind'],
            'client': grant['client'], 'source_references': note.get('source_references', []),
            'obsidian_uri': 'obsidian://open?vault=' + quote(Path(grant['root']).name, safe='')
                            + '&file=' + quote(note['path'], safe='')}


def search_selected(manifest_path, manifest_sha256, client, query, limit=4):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
        raise ValueError('Query must contain 1 to 300 characters')
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError('Limit must be 1 to 4')
    terms = set(re.findall(r'[\w-]+', query.casefold()))
    if not terms:
        raise ValueError('Query needs a word')
    grant, notes = load_selected(manifest_path, manifest_sha256, client)
    matches = []
    for note, path, lines in notes:
        heading = next((i for i, line in enumerate(lines) if line.startswith('# ')), len(lines))
        scored = [(sum(term in line.casefold() for term in terms), i)
                  for i, line in enumerate(lines) if i >= heading and line.strip()]
        if not scored:
            continue
        score, index = max(scored, key=lambda pair: (pair[0], -pair[1]))
        if score:
            if len(lines[index]) > 4000:
                raise ValueError('Matching line too large; narrow the source note')
            matches.append({**citation(grant, note, path), 'line': index + 1,
                            'excerpt': lines[index], 'score': score})
    matches.sort(key=lambda item: (-item['score'], item['note_id']))
    return {'mode': 'allowlisted-read-only-markdown', 'query': query,
            'selected_note_count': len(notes), 'matches': matches[:limit]}


def read_selected(manifest_path, manifest_sha256, client, note_id, start_line=1, line_count=20):
    if type(start_line) is not int or start_line < 1 or type(line_count) is not int or not 1 <= line_count <= 40:
        raise ValueError('Read requires positive start and 1 to 40 lines')
    grant, notes = load_selected(manifest_path, manifest_sha256, client)
    for note, path, lines in notes:
        if note['id'] == note_id:
            if start_line > len(lines):
                raise ValueError('Start line outside note')
            selected = lines[start_line - 1:start_line - 1 + line_count]
            if sum(len(line) for line in selected) > 8000:
                raise ValueError('Requested excerpt too large; request fewer lines')
            return {**citation(grant, note, path), 'start_line': start_line,
                    'end_line': start_line + len(selected) - 1,
                    'lines': [{'line': start_line + i, 'text': text} for i, text in enumerate(selected)]}
    raise ValueError('Note ID not authorized')
