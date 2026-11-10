"""Stage an explicit current-context summary locally. No model or transcript reader."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import unicodedata
import uuid

from capture_outcomes import privacy_check
from migrate import digest, json_bytes, private_root, render, validate_candidate, write_new
from selected_memory import checked_path

ROLES = {'planner', 'implementer', 'reviewer', 'mixed', 'unknown'}
KINDS = {'decision', 'work', 'test', 'lesson', 'blocker', 'next-action', 'review'}
STATUSES = {'reported-completed', 'reported-passed', 'reported-failed', 'proposed', 'blocked', 'unknown'}
SUPPORT = {'visible-tool-output', 'visible-user-message', 'artifact', 'inference', 'unsupported'}

ILLEGAL_SCALAR_CHARS = {'\r', '\n', '\t', '\x00', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029', '\u202e'}
RESERVED_METADATA_KEYS = {
    'id', 'note_id', 'title', 'tags', 'kind', 'status', 'sha256', 'source_sha256',
    'schema_version', 'created', 'updated', 'privacy', 'scopes', 'sources',
    'external_processing', 'chat_capture', 'candidate', 'proposal'
}


def sanitize_unicode(s, name='STRING'):
    require(isinstance(s, str), f'INVALID_{name}')
    try:
        s.encode('utf-8')
    except (UnicodeEncodeError, UnicodeError):
        raise ValueError(f'INVALID_SURROGATE_{name}')
    return unicodedata.normalize('NFC', s)


def require(value, code):
    if not value:
        raise ValueError(code)


def inspect_strings(value):
    if isinstance(value, str):
        privacy_check(value)
    elif isinstance(value, list):
        for child in value:
            inspect_strings(child)
    elif isinstance(value, dict):
        for key, child in value.items():
            privacy_check(key)
            inspect_strings(child)


def validate_packet(value):
    require(isinstance(value, dict) and set(value) == {'schema', 'role', 'coverage', 'items', 'artifacts'}, 'INVALID_PACKET')
    require(value['schema'] == 'brain-save-context-v1' and value['role'] in ROLES, 'INVALID_IDENTITY')
    require(value['coverage'] in {'visible-context', 'compacted-context'}, 'INVALID_COVERAGE')
    require(isinstance(value['items'], list) and len(value['items']) <= 8, 'INVALID_ITEMS')
    require(isinstance(value['artifacts'], list) and len(value['artifacts']) <= 8, 'INVALID_ARTIFACTS')
    for item in value['items']:
        require(isinstance(item, dict) and set(item) == {'kind', 'text', 'status', 'actor', 'support', 'references'}, 'INVALID_ITEM')
        require(item['kind'] in KINDS and item['status'] in STATUSES and item['support'] in SUPPORT, 'INVALID_CLASSIFICATION')
        require(item['actor'] in {'this-session', 'other-session', 'unknown'}, 'INVALID_ACTOR')
        require(isinstance(item['text'], str) and 1 <= len(item['text'].strip()) <= 1200, 'INVALID_TEXT')
        require(isinstance(item['references'], list) and 1 <= len(item['references']) <= 4
                and all(isinstance(x, str) and 1 <= len(x) <= 400 for x in item['references']), 'SOURCE_REFERENCE_REQUIRED')
        if item['status'].startswith('reported-'):
            require(item['support'] in {'visible-tool-output', 'artifact', 'visible-user-message'}, 'RESULT_NEEDS_SUPPORT')
        if item['kind'] == 'next-action':
            require(item['status'] == 'proposed', 'NEXT_ACTION_NOT_COMPLETED')
        if item['kind'] == 'test':
            require(item['status'] in {'reported-passed', 'reported-failed', 'proposed', 'unknown'}, 'INVALID_TEST_STATUS')
        if value['role'] in {'planner', 'reviewer'} and item['kind'] == 'work' and item['actor'] == 'this-session':
            require(item['status'] != 'reported-completed', 'ROLE_CANNOT_CLAIM_IMPLEMENTER_WORK')
    inspect_strings(value)
    require(len({json.dumps(x, sort_keys=True) for x in value['items']}) == len(value['items']), 'DUPLICATE_ITEMS')
    return value


def artifact_records(project, paths):
    records = []
    for name in paths:
        require(isinstance(name, str), 'INVALID_ARTIFACT_PATH')
        path = PurePosixPath(name)
        require(not path.is_absolute() and str(path) == name and path.parts and path.suffix.lower() in {'.py', '.md', '.txt', '.json', '.toml', '.yaml', '.yml', '.js', '.ts', '.tsx', '.html', '.css'}, 'INVALID_ARTIFACT_PATH')
        require(not any(x in {'.', '..', '.git', '.codex', '.obsidian'} or x.lower().startswith('.env')
                        or x.lower() in {'auth.json', 'credentials.json', 'secrets.json'} for x in path.parts)
                and not any(x in name for x in '\\:*?"<>|'), 'ARTIFACT_NOT_AUTHORIZED')
        actual = checked_path(project.joinpath(*path.parts))
        require(actual.resolve().is_relative_to(project), 'ARTIFACT_OUTSIDE_PROJECT')
        require(actual.stat().st_size <= 2 * 1024 * 1024, 'ARTIFACT_TOO_LARGE')
        records.append({'path': name, 'sha256': digest(actual.read_bytes()),
                        'verification': 'file-bytes-only; not authorship, execution or semantic correctness'})
    require(len({x['path'].casefold() for x in records}) == len(records), 'DUPLICATE_ARTIFACT')
    return records


def make_note(envelope, sha, timestamp):
    packet = envelope['context_summary']
    source = 'archive/' + sha + '.brain-save-context.json'
    identity = envelope['identity']
    return {'schema_version': '1.0', 'id': 'brain-save-' + sha[:32],
            'title': 'Session memory proposal (' + packet['role'] + ')', 'kind': 'session',
            'review_state': 'candidate', 'created': timestamp, 'updated': timestamp,
            'as_of': None, 'privacy': 'personal', 'scopes': ['engineering'],
            'sources': [source], 'supersedes': [], 'external_processing': False,
            'interactive_capture': {'method': 'current-context-summary', 'project': identity['project_name'],
                                    'project_id': identity['project_id'], 'session_reference': identity['session_reference'],
                                    'session_reference_kind': identity['session_reference_kind'],
                                    'role': packet['role'], 'coverage': packet['coverage'],
                                    'full_transcript': False, 'artifacts': envelope['artifacts']},
            'body': 'Explicit current-session context summary, not a full transcript or CLI completion receipt. '
                    'Claims below are model-derived reports or interpretations, including descriptions of visible tool results; '
                    'the adapter does not independently attest conversation quotes or test execution. '
                    'Only the listed artifact hashes were checked locally, without proving authorship or implementation success.',
            'claims': [{'text': item['kind'] + ' [' + item['status'] + '; actor=' + item['actor'] +
                        '; support=' + item['support'] + ']: ' + item['text'] +
                        '\nSupporting references (session-supplied): ' + '; '.join(item['references']),
                        'evidence': 'inferred' if item['support'] == 'inference' else 'summary-derived',
                        'locator': source + '#/context_summary/items/' + str(i)}
                       for i, item in enumerate(packet['items'])]}


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError('SAVE_BUSY_RETRY_ONCE') from None
    os.close(fd)
    try:
        yield
    finally:
        path.unlink()


def stage(root, project, value, environment=None):
    environment = os.environ if environment is None else environment
    root = private_root(root)
    checked_path(root, directory=True)
    project = checked_path(Path(project).absolute(), directory=True).resolve()
    project = next((parent for parent in (project, *project.parents) if (parent / '.git').exists()), project)
    require(not any(x.casefold().startswith('onedrive') or x in {'.codex', '.obsidian'} for x in project.parts), 'PROJECT_NOT_AUTHORIZED')
    packet = validate_packet(value)
    # These host variables are opportunistic identifiers, not a documented transcript API.
    session = environment.get('CODEX_THREAD_ID') or environment.get('CODEX_SESSION_ID')
    if session:
        require(isinstance(session, str) and 1 <= len(session) <= 120 and all(x.isalnum() or x in '_-' for x in session), 'INVALID_HOST_SESSION_ID')
        kind = 'host-environment-unattested'
    else:
        session, kind = 'unknown-' + str(uuid.uuid4()), 'invocation-id-not-a-session-id'
    privacy_check(session)
    privacy_check(project.name)
    identity = {'project_id': digest(str(project).casefold().encode()), 'project_name': project.name,
                'project_root': str(project), 'session_reference': session, 'session_reference_kind': kind}
    envelope = {'schema': 'brain-save-archive-v1', 'identity': identity, 'context_summary': packet,
                'artifacts': artifact_records(project, packet['artifacts'])}
    data = json_bytes(envelope)
    sha = digest(data)
    scope = identity['project_id'][:16] + '/' + digest((session + ':' + packet['role']).encode())[:24]
    folder = root / 'staging/brain-save' / scope / sha
    # Check existing namespace before creating a directory or lock.
    for part in (folder, *folder.parents):
        if part.exists():
            require(not part.is_symlink() and not getattr(part, 'is_junction', lambda: False)(), 'REDIRECTED_OUTPUT')
    receipt_path = folder / 'receipt.json'
    archive = root / 'archive' / (sha + '.brain-save-context.json')
    candidate_path = folder / 'candidate.json'
    relative = 'logs/brain-save/' + scope + '/' + sha + '.md'
    markdown = root / 'test-vault' / relative
    with locked(folder / '.lock'):
        if receipt_path.exists():
            receipt = json.loads(checked_path(receipt_path).read_bytes())
            require(checked_path(archive).read_bytes() == data, 'CACHED_SOURCE_MISMATCH')
            expected = {'schema': 'brain-save-receipt-v1', 'source_sha256': sha,
                        'source': str(archive.relative_to(root)), 'scope': scope,
                        'status': 'STAGED' if packet['items'] else 'NO_MEANINGFUL_OUTCOMES',
                        'proposals': 1 if packet['items'] else 0}
            if packet['items']:
                old = json.loads(checked_path(candidate_path).read_bytes())
                timestamp = old['created']
                require(datetime.fromisoformat(timestamp).utcoffset() == timezone.utc.utcoffset(None), 'CACHED_TIMESTAMP_INVALID')
                reconstructed = make_note(envelope, sha, timestamp)
                require(checked_path(candidate_path).read_bytes() == json_bytes(reconstructed), 'CACHED_CANDIDATE_MISMATCH')
                checked_path(markdown)  # Never recreate a deleted proposal on replay.
                render(reconstructed, root, relative)
                expected.update(candidate=str(candidate_path.relative_to(root)), candidate_sha256=digest(candidate_path.read_bytes()),
                                proposal='test-vault/' + relative, proposal_sha256=digest(markdown.read_bytes()))
            require(receipt == expected, 'CACHED_RECEIPT_MISMATCH')
            return {**receipt, 'status': 'DUPLICATE_SOURCE', 'proposals': 0, 'private_root': str(root)}
        require(not candidate_path.exists() and not markdown.exists() and not archive.exists(), 'INCOMPLETE_PRIOR_SAVE')
        write_new(archive, data)
        receipt = {'schema': 'brain-save-receipt-v1', 'source_sha256': sha,
                   'source': str(archive.relative_to(root)), 'scope': scope,
                   'status': 'STAGED' if packet['items'] else 'NO_MEANINGFUL_OUTCOMES',
                   'proposals': 1 if packet['items'] else 0}
        if packet['items']:
            note = make_note(envelope, sha, datetime.now(timezone.utc).isoformat())
            validate_candidate(note)
            write_new(candidate_path, json_bytes(note))
            render(note, root, relative)
            receipt.update(candidate=str(candidate_path.relative_to(root)), candidate_sha256=digest(candidate_path.read_bytes()),
                           proposal='test-vault/' + relative, proposal_sha256=digest(markdown.read_bytes()))
        write_new(receipt_path, json_bytes(receipt))
        return {**receipt, 'private_root': str(root)}


def _make_chat_note(envelope, sha, timestamp):
    summary = envelope['chat_summary']
    source = 'archive/' + sha + '.brain-chat-context.json'
    identity = envelope['identity']
    claim_text = summary['title'] + ': ' + (summary['content'][:1100] if len(summary['content']) > 1100 else summary['content'])
    note = {
        'schema_version': '1.0',
        'id': 'brain-save-' + sha[:32],
        'title': summary['title'],
        'kind': summary.get('kind', 'session'),
        'review_state': 'candidate',
        'created': timestamp,
        'updated': timestamp,
        'as_of': None,
        'privacy': 'personal',
        'scopes': ['engineering'],
        'sources': [source],
        'supersedes': [],
        'external_processing': False,
        'chat_capture': {
            'method': 'chat-mcp-explicit-save',
            'client': identity['client'],
            'surface': identity['surface'],
            'tags': summary['tags'],
            'metadata': summary['metadata'],
        },
        'body': summary['content'],
        'claims': [{
            'text': claim_text,
            'evidence': 'user-stated',
            'locator': source + '#/chat_summary/content',
        }],
    }
    if summary['tags']:
        note['tags'] = summary['tags']
    return note


def cleanup_staged_chat_memory(root, receipt):
    if not isinstance(receipt, dict):
        return
    root = Path(root)
    proposal = receipt.get('proposal')
    if proposal:
        p_path = root / proposal
        if p_path.exists():
            try: p_path.unlink()
            except OSError: pass
    candidate = receipt.get('candidate')
    if candidate:
        c_path = root / candidate
        if c_path.exists():
            try: c_path.unlink()
            except OSError: pass
    source = receipt.get('source')
    if source:
        s_path = root / source
        if s_path.exists():
            try: s_path.unlink()
            except OSError: pass
    scope = receipt.get('scope')
    sha = receipt.get('source_sha256')
    if scope and sha:
        folder = root / 'staging/brain-save' / scope / sha
        if folder.exists():
            for f in folder.glob('*'):
                try: f.unlink()
                except OSError: pass
            try: folder.rmdir()
            except OSError: pass


def stage_chat_memory(root, title, text=None, summary=None, tags=None, metadata=None, *, kind='session'):
    root = private_root(root)
    checked_path(root, directory=True)

    require(isinstance(title, str), 'INVALID_TITLE')
    title = sanitize_unicode(title.strip(), 'TITLE')
    require(1 <= len(title) <= 120, 'INVALID_TITLE_LENGTH')
    stem = title.split('.')[0].strip().upper()
    require(re.fullmatch(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]|CONIN\$|CONOUT\$|CLOCK\$)$', stem) is None, 'RESERVED_DEVICE_NAME')
    require(not any(c in title for c in '\\/:*?"<>|\x00' + ''.join(ILLEGAL_SCALAR_CHARS)) and '..' not in title and not title.endswith('.') and title != '.', 'INVALID_TITLE_CHARACTERS')
    privacy_check(title)

    if text is not None:
        require(isinstance(text, str), 'INVALID_TEXT')
        privacy_check(text)
    if summary is not None:
        require(isinstance(summary, str), 'INVALID_SUMMARY')
        privacy_check(summary)

    has_text = isinstance(text, str) and bool(text.strip())
    has_summary = isinstance(summary, str) and bool(summary.strip())
    if has_text:
        content = text.strip()
    elif has_summary:
        content = summary.strip()
    else:
        raise ValueError('INVALID_CONTENT')

    content = sanitize_unicode(content, 'CONTENT')
    require(1 <= len(content) <= 16384, 'INVALID_CONTENT_LENGTH')
    require(len(content.encode('utf-8')) <= 16384, 'CONTENT_TOO_LARGE')

    tags = tags if tags is not None else []
    require(isinstance(tags, list) and len(tags) <= 16, 'INVALID_TAGS')
    clean_tags = []
    for tag in tags:
        require(isinstance(tag, str), 'INVALID_TAG')
        tag = sanitize_unicode(tag.strip(), 'TAG')
        require(1 <= len(tag) <= 40, 'INVALID_TAG_LENGTH')
        require(re.fullmatch(r'[A-Za-z0-9_-]+', tag) is not None, 'INVALID_TAG_CHARACTERS')
        clean_tags.append(tag)
    require(len({t.casefold() for t in clean_tags}) == len(clean_tags), 'DUPLICATE_TAGS')

    metadata = metadata if metadata is not None else {}
    require(isinstance(metadata, dict) and len(metadata) <= 16, 'INVALID_METADATA')
    clean_metadata = {}
    seen_raw_keys = set()
    for k, v in metadata.items():
        require(isinstance(k, str) and isinstance(v, str), 'INVALID_METADATA_ENTRY')
        require(k not in seen_raw_keys, 'DUPLICATE_METADATA_KEY')
        seen_raw_keys.add(k)
        k_clean = sanitize_unicode(k.strip(), 'METADATA_KEY')
        v_clean = sanitize_unicode(v.strip(), 'METADATA_VALUE')
        require(1 <= len(k_clean) <= 40 and 1 <= len(v_clean) <= 200, 'INVALID_METADATA_LENGTH')
        require(re.fullmatch(r'[A-Za-z0-9_-]+', k_clean) is not None, 'INVALID_METADATA_KEY')
        require(k_clean.lower() not in RESERVED_METADATA_KEYS, 'RESERVED_METADATA_KEY')
        require(not any(c in v_clean for c in ILLEGAL_SCALAR_CHARS), 'INVALID_METADATA_VALUE')
        clean_metadata[k_clean] = v_clean
    require(len({k.casefold() for k in clean_metadata}) == len(clean_metadata), 'DUPLICATE_METADATA_KEY')

    require(isinstance(kind, str) and kind in ('session', 'decision', 'lesson', 'project', 'reference', 'plan'), 'INVALID_KIND')

    identity = {
        'client': 'chat-mcp',
        'surface': 'regular-chat',
        'invocation_kind': 'chat-mcp-create-only',
    }
    chat_summary = {
        'title': title,
        'content': content,
        'tags': clean_tags,
        'metadata': clean_metadata,
        'kind': kind,
    }
    envelope = {
        'schema': 'brain-chat-save-archive-v1',
        'identity': identity,
        'chat_summary': chat_summary,
    }
    data = json_bytes(envelope)
    privacy_check(data.decode('utf-8', errors='replace'))
    sha = digest(data)
    scope = 'chat/' + digest(title.casefold().encode('utf-8'))[:16]
    folder = root / 'staging/brain-save' / scope / sha

    for part in (folder, *folder.parents):
        if part.exists():
            require(not part.is_symlink() and not getattr(part, 'is_junction', lambda: False)(), 'REDIRECTED_OUTPUT')

    receipt_path = folder / 'receipt.json'
    archive = root / 'archive' / (sha + '.brain-chat-context.json')
    candidate_path = folder / 'candidate.json'
    relative = 'logs/brain-save/' + scope + '/' + sha + '.md'
    markdown = root / 'test-vault' / relative

    with locked(folder / '.lock'):
        if receipt_path.exists():
            receipt = json.loads(checked_path(receipt_path).read_bytes())
            require(checked_path(archive).read_bytes() == data, 'CACHED_SOURCE_MISMATCH')
            expected = {
                'schema': 'brain-chat-save-receipt-v1',
                'source_sha256': sha,
                'source': str(archive.relative_to(root)),
                'scope': scope,
                'status': 'STAGED',
                'proposals': 1,
            }
            old = json.loads(checked_path(candidate_path).read_bytes())
            timestamp = old['created']
            require(datetime.fromisoformat(timestamp).utcoffset() == timezone.utc.utcoffset(None), 'CACHED_TIMESTAMP_INVALID')
            reconstructed = _make_chat_note(envelope, sha, timestamp)
            require(checked_path(candidate_path).read_bytes() == json_bytes(reconstructed), 'CACHED_CANDIDATE_MISMATCH')
            checked_path(markdown)
            render(reconstructed, root, relative)
            expected.update(
                candidate=str(candidate_path.relative_to(root)),
                candidate_sha256=digest(candidate_path.read_bytes()),
                proposal='test-vault/' + relative,
                proposal_sha256=digest(markdown.read_bytes()),
                note_id=reconstructed['id'],
                title=title,
            )
            require(receipt == expected, 'CACHED_RECEIPT_MISMATCH')
            return {**receipt, 'status': 'DUPLICATE_SOURCE', 'proposals': 0, 'private_root': str(root)}

        require(not candidate_path.exists() and not markdown.exists() and not archive.exists(), 'INCOMPLETE_PRIOR_SAVE')
        write_new(archive, data)
        timestamp = datetime.now(timezone.utc).isoformat()
        note = _make_chat_note(envelope, sha, timestamp)
        validate_candidate(note)
        write_new(candidate_path, json_bytes(note))
        render(note, root, relative)
        receipt = {
            'schema': 'brain-chat-save-receipt-v1',
            'source_sha256': sha,
            'source': str(archive.relative_to(root)),
            'scope': scope,
            'status': 'STAGED',
            'proposals': 1,
            'candidate': str(candidate_path.relative_to(root)),
            'candidate_sha256': digest(candidate_path.read_bytes()),
            'proposal': 'test-vault/' + relative,
            'proposal_sha256': digest(markdown.read_bytes()),
            'note_id': note['id'],
            'title': title,
        }
        write_new(receipt_path, json_bytes(receipt))
        return {**receipt, 'private_root': str(root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--policy')
    parser.add_argument('--policy-sha256')
    args = parser.parse_args()
    if (args.policy is None) != (args.policy_sha256 is None):
        parser.error('--policy and --policy-sha256 must be supplied together')
    try:
        raw = sys.stdin.buffer.read(32001)
        require(len(raw) <= 32000, 'INPUT_TOO_LARGE')
        text = raw.decode('utf-8-sig')
        privacy_check(text)
        value = json.loads(text)
        result = stage(args.root, Path.cwd(), value)
        publication = None
        if args.policy is not None:
            try:
                # CLI-only lazy import: publisher reuses unchanged staging primitives.
                from publish_memory import publish_staged
                publication = publish_staged(args.root, result, args.policy, args.policy_sha256)
            except (ValueError, OSError, KeyError, TypeError, AttributeError, ImportError, RuntimeError) as error:
                uncertain = isinstance(error, (OSError, RuntimeError))
                publication = {'schema': 'brain-publication-result-v1',
                               'status': 'PARTIAL' if uncertain else 'REFUSED',
                               'transaction_id': None,
                               'counts': {'saved': 0, 'updated': 0, 'linked': 0, 'skipped': 0, 'quarantined': 0},
                               'error_code': 'PUBLICATION_STATE_UNCERTAIN' if uncertain else 'PUBLICATION_REFUSED',
                               'sync_status': 'UNVERIFIED'}
            result = {**result, 'publication': publication}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if publication is None or publication['status'] in ('SAVED', 'NO_CHANGE', 'DISABLED') else 2
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        # Never echo source content, paths from exceptions, or credential-bearing input.
        code = str(error) if isinstance(error, ValueError) and str(error).replace('_', '').isalnum() else 'SAVE_REFUSED_IO_OR_FORMAT'
        print(json.dumps({'status': 'REFUSED', 'error_code': code, 'proposals': 0}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
