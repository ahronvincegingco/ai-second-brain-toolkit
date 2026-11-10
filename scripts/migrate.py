"""Offline intake and disposable draft retrieval. No publisher, network, or AI calls."""
import argparse
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
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


def stamp(value):
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    # Preserve supplied dates rather than substituting capture time.
    return str(value)


def safe_id(value):
    value = str(value)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,119}', value):
        raise ValueError('Invalid identifier')
    return value


def private_root(value):
    p = Path(value).absolute()
    for item in [p, *p.parents]:
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise ValueError('Linked output ancestors are refused')
    p = p.resolve()
    if any(x.lower().startswith('onedrive') or x.lower() == '.git' for x in p.parts):
        raise ValueError('OneDrive/Git output refused')
    if any((parent / '.git').exists() for parent in [p, *p.parents]):
        raise ValueError('Private output must be outside a repository')
    marker = json.loads((p / '.private-staging.json').read_text(encoding='utf-8'))
    if marker.get('live_writes') is not False or marker.get('external_exports') is not False:
        raise ValueError('Not an offline private staging workspace')
    return p


def write_new(path, data):
    """Idempotent exact replay; changed bytes never silently replace a record."""
    for ancestor in [path.absolute(), *path.absolute().parents]:
        if ancestor.is_symlink() or getattr(ancestor, 'is_junction', lambda: False)():
            raise ValueError('Linked output path refused')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('Existing record differs; create an explicit revision')
        return
    with path.open('xb') as out:
        out.write(data)


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def message_text(item):
    if isinstance(item.get('text'), str):
        return item['text']
    content = item.get('content', {})
    if isinstance(content, list):
        return '\n'.join(x.get('text', '') for x in content if isinstance(x, dict))
    return '\n'.join(x for x in content.get('parts', []) if isinstance(x, str))


def normalize(data, source_pointer=''):
    """Accept read_thread captures or a ChatGPT conversations.json export.

    Export branch identities are retained. Tool/reasoning items are not treated as
    dialogue evidence. Attachments are locators only; no attachment bytes fetched.
    """
    if isinstance(data, list):
        for index, conversation in enumerate(data):
            yield from normalize(conversation, source_pointer + '/' + str(index))
        return
    if 'thread' in data and 'turns' in data:
        t = data['thread']
        messages = []
        for turn in reversed(data['turns']):
            for item in turn.get('items', []):
                if item.get('type') not in ('userMessage', 'agentMessage'):
                    continue
                text = message_text(item)
                messages.append({'id': item.get('id'), 'turn_id': turn.get('id'),
                    'role': 'user' if item['type'] == 'userMessage' else 'assistant',
                    'date': stamp(turn.get('startedAt')), 'text': text,
                    'truncated': bool(item.get('truncated', False)),
                    'unavailable': bool(re.fullmatch(r'\s*::?chatgpt-content-reference\{[^\n]+\}\s*', text))})
        yield {'id': safe_id(t['id']), 'title': t.get('title'),
            'origin': t.get('kind', 'unknown'), 'original_date': stamp(t.get('createdAt')),
            'updated': stamp(t.get('updatedAt')), 'messages': messages,
            'link': ('https://chatgpt.com/c/' + t['id']) if t.get('kind') == 'chatgpt' else None,
            'coverage': 'partial-page' if data.get('page', {}).get('hasMore') else 'returned-history',
            'next_cursor': data.get('page', {}).get('nextCursor'),
            'attachments': data.get('attachments', [])}
        return
    if 'mapping' in data:
        sid = safe_id(data.get('id') or data.get('conversation_id'))
        current = data.get('current_node')
        ancestry = set()
        cursor = current
        while cursor is not None:
            if cursor in ancestry:
                raise ValueError('Cyclic export branch')
            ancestry.add(cursor)
            cursor = data['mapping'].get(cursor, {}).get('parent')
        messages = []
        attachments = []
        for node_id, node in data['mapping'].items():
            m = node.get('message')
            if not m:
                continue
            # JSON pointers address archived source bytes, never filesystem targets.
            escaped = node_id.replace('~', '~0').replace('/', '~1')
            base = '#' + source_pointer + '/mapping/' + escaped + '/message'
            content = m.get('content')
            content_type = content.get('content_type', 'text') if isinstance(content, dict) else 'unknown'
            parts = content.get('parts', []) if isinstance(content, dict) else []
            structured = [{'source_locator': base + '/content/parts/' + str(index),
                           'content_type': part.get('content_type', 'unknown') if isinstance(part, dict) else 'unknown',
                           'status': 'not-loaded'}
                          for index, part in enumerate(parts) if not isinstance(part, str)]
            raw_metadata = m.get('metadata')
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            metadata_reference = ({'source_locator': base + '/metadata', 'status': 'not-loaded'}
                                  if raw_metadata is not None and not isinstance(raw_metadata, dict) else None)
            supplied = metadata.get('attachments', [])
            entries = enumerate(supplied) if isinstance(supplied, list) else [(None, supplied)]
            for index, attachment in entries:
                attachments.append({'node_id': node_id, 'message_id': m.get('id', node_id),
                    'source_locator': base + '/metadata/attachments' + ('' if index is None else '/' + str(index)),
                    'metadata': deepcopy(attachment), 'status': 'not-loaded'})
            unloaded = bool(structured or supplied or not isinstance(supplied, list)
                            or metadata_reference or content_type not in ('text', 'code'))
            messages.append({'id': m.get('id', node_id), 'node_id': node_id,
                'parent': node.get('parent'), 'on_current_branch': node_id in ancestry,
                'role': m.get('author', {}).get('role', 'unknown'),
                'date': stamp(m.get('create_time')), 'text': message_text(m),
                'unavailable': False,
                'structured_parts': structured,
                'metadata_reference': metadata_reference,
                'content_reference': {'source_locator': base + '/content',
                                      'content_type': content_type, 'status': 'not-loaded'} if unloaded else None,
                'content_coverage': 'text-with-unloaded-content' if unloaded else 'text-only'})
        yield {'id': sid, 'title': data.get('title'), 'origin': 'chatgpt-export',
            'original_date': stamp(data.get('create_time')), 'updated': stamp(data.get('update_time')),
            'link': 'https://chatgpt.com/c/' + sid, 'coverage': 'export-nodes-not-flattened',
            'messages': messages, 'attachments': attachments, 'current_node': current,
            'source_locator': '#' + source_pointer}
        return
    raise ValueError('Unsupported source format; supply explicit role-labelled capture')


def intake(source, workspace):
    root = private_root(workspace)
    original = Path(source).read_bytes()
    source_hash = digest(original)
    records = list(normalize(json.loads(original)))
    # Content addressed source archives retain exact supplied bytes and revisions.
    write_new(root / 'archive' / (source_hash + '.json'), original)
    for record in records:
        record['capture_date'] = datetime.now(timezone.utc).isoformat()
        record['normalization_version'] = '1.2'
        record['source_sha256'] = source_hash
        record['privacy'] = 'personal'
        record['external_processing'] = False
        target = root / 'normalized' / record['id'] / (source_hash + '.v1_2.json')
        if not target.exists():
            write_new(target, json_bytes(record))
    return {'source_sha256': source_hash, 'sessions': len(records), 'published': 0}


REQUIRED = {'schema_version', 'id', 'kind', 'review_state', 'created', 'updated',
            'as_of', 'privacy', 'scopes', 'sources', 'supersedes', 'claims'}


def validate_candidate(note):
    if not REQUIRED <= note.keys():
        raise ValueError('Missing frozen metadata')
    safe_id(note['id'])
    if note['review_state'] != 'candidate' or note['privacy'] not in ('personal', 'restricted', 'shareable'):
        raise ValueError('Drafts must remain candidates with a privacy label')
    if note.get('external_processing') is not False or not note['sources']:
        raise ValueError('External processing must be denied; provenance required')
    if note['kind'] not in ('profile', 'project', 'decision', 'plan', 'lesson', 'reference', 'session'):
        raise ValueError('Unknown frozen note kind')
    for claim in note['claims']:
        if claim.get('evidence') not in ('observed', 'user-stated', 'primary-source-supported',
                                       'summary-derived', 'inferred', 'disputed', 'unknown'):
            raise ValueError('Claim evidence classification required')
        if not claim.get('locator') or not claim.get('text'):
            raise ValueError('Claim locator and text required')
    # This is a staging contract, not a replacement for the blocked manager schema.


def render(note, workspace, relative):
    root = private_root(workspace)
    validate_candidate(note)
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or rel.suffix != '.md' or ':' in relative:
        raise ValueError('Invalid disposable note path')
    vault = root / 'test-vault'
    target = vault / rel
    for part in [target, *target.parents]:
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('Linked draft target refused')
        if part == root:
            break
    if not target.resolve().is_relative_to(vault.resolve()):
        raise ValueError('Draft escape')
    # JSON scalar/list encodings are a YAML-compatible subset; no YAML parser here.
    metadata = {k: v for k, v in note.items() if k not in ('claims', 'body')}
    clean_title = re.sub(r'[\r\n\t\x00-\x1f]', ' ', str(note.get('title', note['id']))).strip()
    lines = ['---', *[f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in metadata.items()],
             '---', '', '> [!warning] Candidate only',
             '> Disposable demonstration; no owner content approval or live publication.', '',
             '# ' + (clean_title or note['id']), '', note.get('body', ''), '', '## Evidence', '']
    for i, claim in enumerate(note['claims'], 1):
        lines += [f"{i}. **{claim['evidence']}** — {claim['text']}", f"   Source: {claim['locator']}", '']
    write_new(target, ('\n'.join(lines).rstrip() + '\n').encode('utf-8'))
    return str(target)


def build_index(workspace, notes):
    root = private_root(workspace)
    entries = []
    for relative in notes:
        path = root / 'test-vault' / relative
        if not path.resolve().is_relative_to((root / 'test-vault').resolve()) or path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Index source outside disposable vault')
        body = path.read_text(encoding='utf-8')
        if 'review_state: "candidate"' not in body or 'privacy: "restricted"' in body:
            raise ValueError('Only selected nonrestricted demonstration candidates allowed')
        entries.append((relative, digest(path.read_bytes()), body))
    out = root / 'indexes'
    if out.is_symlink() or getattr(out, 'is_junction', lambda: False)() or (out / 'demo.sqlite').is_symlink():
        raise ValueError('Linked index output refused')
    out.mkdir(exist_ok=True)
    with closing(sqlite3.connect(out / 'demo.sqlite')) as db, db:
        db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(path UNINDEXED, sha UNINDEXED, body)')
        db.execute('DELETE FROM notes')
        db.executemany('INSERT INTO notes VALUES (?,?,?)', entries)
    return len(entries)


def search(workspace, query):
    root = private_root(workspace)
    index = root / 'indexes' / 'demo.sqlite'
    with closing(sqlite3.connect(index.as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT path,sha,body FROM notes WHERE notes MATCH ? LIMIT 5', (query,)).fetchall()
    results = []
    for path, sha, body in rows:
        actual = root / 'test-vault' / path
        if not actual.resolve().is_relative_to((root / 'test-vault').resolve()) or digest(actual.read_bytes()) != sha:
            raise ValueError('Index stale or source escaped; rebuild before use')
        results.append({'path': path, 'sha256': sha, 'review_state': 'candidate', 'body': body})
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace', required=True)
    sub = p.add_subparsers(dest='operation', required=True)
    sub.add_parser('intake').add_argument('source')
    draft = sub.add_parser('draft')
    draft.add_argument('candidate_json')
    draft.add_argument('relative_note')
    idx = sub.add_parser('index-demo')
    idx.add_argument('--allow-candidates-for-demo', action='store_true', required=True)
    idx.add_argument('notes', nargs='+')
    sub.add_parser('search-demo').add_argument('query')
    a = p.parse_args()
    if a.operation == 'intake':
        result = intake(a.source, a.workspace)
    elif a.operation == 'draft':
        result = render(json.loads(Path(a.candidate_json).read_text(encoding='utf-8')), a.workspace, a.relative_note)
    elif a.operation == 'index-demo':
        result = {'indexed_demo_candidates': build_index(a.workspace, a.notes), 'published': 0}
    else:
        result = search(a.workspace, a.query)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
