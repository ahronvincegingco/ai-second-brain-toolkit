"""Explicit, pinned historical import validation; no CLI or host configuration."""
import hashlib
import json
import os
from pathlib import Path
import re

from publish_memory import (load_policy, publication_lock, read_bytes, replace_pointer,
                            write_immutable)
from selected_memory import checked_path

_ID = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_PART = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
_DEVICE = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z', re.I)


def _require(condition):
    if not condition:
        raise ValueError('INVALID')


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        _require(key not in obj)
        obj[key] = value
    return obj


def _json(data):
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=lambda _: _require(False))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _path(value, markdown=False):
    _require(isinstance(value, str) and 0 < len(value) <= 512 and '\\' not in value)
    parts = value.split('/')
    _require(all(_PART.fullmatch(p) and not p.endswith('.') and not _DEVICE.fullmatch(p)
                 and p.casefold() not in ('.git', '.obsidian') for p in parts))
    if markdown:
        _require(value.endswith('.md'))
    return parts


def _pin(root, path, expected, limit):
    path = Path(path)
    _require(path.is_absolute() and '..' not in path.parts and path.is_relative_to(root)
             and path == path.resolve() and path.resolve().is_relative_to(root) and isinstance(expected, str)
             and _HASH.fullmatch(expected))
    data = read_bytes(path, limit)
    _require(_sha(data) == expected)
    return data


def _private(path):
    root = checked_path(Path(path), directory=True)
    _require(root == root.resolve() and not any(p.name.lower().startswith('onedrive') or p.name.lower() == '.git'
                                                for p in (root, *root.parents)))
    _require(not any(os.path.lexists(p / '.git') for p in (root, *root.parents)))
    marker = _json(read_bytes(root / '.private-staging.json', 4096))
    _require(isinstance(marker, dict) and marker.get('live_writes') is False
             and marker.get('external_exports') is False)
    return root


def _frontmatter(data, note):
    text = data.decode('utf-8')
    lines = text.splitlines()
    _require(lines and lines[0] == '---' and '---' in lines[1:])
    end = lines.index('---', 1)
    metadata = {}
    for line in lines[1:end]:
        match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_-]*):\s*(.+)', line)
        _require(match is not None and match[1] not in metadata)
        metadata[match[1]] = _json(match[2])
    _require(metadata.get('id') == note['id'] and metadata.get('privacy') == note['privacy']
             and metadata.get('external_processing') is False and metadata.get('sources') == note['source_references'])
    return '\n'.join(lines[end + 1:])


def _without_inline_code(line):
    output, cursor = [], 0
    while cursor < len(line):
        if line[cursor] != '`':
            output.append(line[cursor]); cursor += 1
            continue
        end = cursor
        while end < len(line) and line[end] == '`':
            end += 1
        width = end - cursor
        closing = None
        for match in re.finditer(r'`+', line[end:]):
            if len(match.group()) == width:
                closing = end + match.end()
                break
        _require(closing is not None)
        cursor = closing
    return ''.join(output)


def _links(bodies):
    lookup = {path[:-3].casefold(): body for path, body in bodies.items()}
    headings = {}
    cleaned = {}
    for path, body in bodies.items():
        # Deliberately conservative: reject unclosed fences and unsupported links.
        visible, fence = [], None
        for line in body.splitlines():
            m = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
            if m and (fence is None or (m[1][0] == fence[0] and len(m[1]) >= fence[1] and not m[2].strip())):
                if fence is None:
                    fence = (m[1][0], len(m[1]))
                else:
                    fence = None
                continue
            if fence is None:
                visible.append(_without_inline_code(line))
        _require(fence is None)
        cleaned[path] = '\n'.join(visible)
        headings[path[:-3].casefold()] = {m.group(1).strip() for line in visible
                                         if (m := re.match(r'^#{1,6}\s+(.+?)\s*#*\s*$', line))}
    for path, text in cleaned.items():
        _require(not re.search(r'!\[|<\s*/?\s*[A-Za-z]|\[[^\]]+\]\s*\[[^\]]*\]|^ {0,3}\[[^\]]+\]:',
                               text, re.I | re.M))
        for match in re.finditer(r'(?<!!)\[[^\]]+\]\(([^)]+)\)', text):
            _require(re.match(r'^https?://', match[1]) is not None)
        for target in re.findall(r'\[\[([^\]]+)\]\]', text):
            _require(target.count('|') <= 1)
            target = target.split('|', 1)[0]
            _require(target.count('#') <= 1 and '^' not in target)
            name, _, heading = target.partition('#')
            _require('#' not in target or bool(heading))
            key = (name[:-3] if name.endswith('.md') else name).casefold() if name else path[:-3].casefold()
            _require((not name or '/' in name) and key in lookup and (not heading or heading in headings[key]))
        remaining = re.sub(r'\[\[[^\[\]]+\]\]', '', text)
        remaining = re.sub(r'\[[^\]]+\]\(https?://[^)]+\)', '', remaining)
        _require('[[' not in remaining and ']]' not in remaining and not re.search(r'\[[^\]]+\]\(', remaining))


def _load(private_root, manifest_path, manifest_sha256, policy_path, policy_sha256,
          authorization_path, authorization_sha256):
    root = _private(private_root)
    policy_raw = _pin(root, policy_path, policy_sha256, 32000)
    policy_obj = _json(policy_raw)
    _require(isinstance(policy_obj, dict) and policy_obj.get('schema') == 'brain-publication-policy-v1'
             and type(policy_obj.get('enabled')) is bool)
    if not policy_obj['enabled']:
        return None
    scope = policy_obj.get('write_scope')
    _require(isinstance(scope, dict) and set(scope) == {'updates', 'bootstrap', 'create_prefixes'}
             and (scope['updates'] is False or (type(scope['updates']) is list and not scope['updates']))
             and (scope['bootstrap'] is False or (type(scope['bootstrap']) is list and not scope['bootstrap'])))
    policy = load_policy(root, policy_path, policy_sha256)
    _require(not _DEVICE.fullmatch(policy['policy_id']))
    vault = checked_path(Path(policy['vault_root']), directory=True)
    _require(vault == vault.resolve() and vault != root and not vault.is_relative_to(root)
             and not root.is_relative_to(vault))
    manifest = _json(_pin(root, manifest_path, manifest_sha256, 128000))
    auth = _json(_pin(root, authorization_path, authorization_sha256, 128000))
    _require(isinstance(manifest, dict) and set(manifest) == {'schema', 'batch_id', 'client', 'review_evidence', 'notes'}
             and manifest['schema'] == 'brain-historical-import-v1' and _ID.fullmatch(manifest['batch_id'])
             and manifest['client'] == 'codex-openai')
    _require(isinstance(auth, dict) and set(auth) == {'schema', 'state', 'reference', 'purpose', 'client', 'batch_id',
                                                       'manifest_sha256', 'policy_id', 'vault_root'}
             and auth['schema'] == 'brain-historical-import-authorization-v1' and auth['state'] == 'approved'
             and all(isinstance(auth[k], str) and auth[k].strip() for k in ('reference', 'purpose'))
             and auth['reference'] == policy['owner_authorization']['reference'] and auth['client'] == manifest['client']
             and auth['batch_id'] == manifest['batch_id'] and auth['manifest_sha256'] == manifest_sha256
             and auth['policy_id'] == policy['policy_id'] and auth['vault_root'] == str(vault))
    _require(policy.get('historical_import') == {'batch_id': manifest['batch_id'], 'client': manifest['client'],
                                               'manifest_sha256': manifest_sha256, 'authorization_sha256': authorization_sha256})
    evidence, notes = manifest['review_evidence'], manifest['notes']
    _require(isinstance(evidence, list) and 1 <= len(evidence) <= 16 and isinstance(notes, list) and 1 <= len(notes) <= 16)
    evidence_paths = set()
    for item in evidence:
        _require(isinstance(item, dict) and set(item) == {'path', 'size', 'sha256'} and type(item['size']) is int
                 and 0 < item['size'] <= 128000)
        parts = _path(item['path'])
        _require(item['path'].casefold() not in evidence_paths)
        evidence_paths.add(item['path'].casefold())
        data = read_bytes(root.joinpath(*parts), 128000)
        _require(len(data) == item['size'] and _sha(data) == item['sha256'])
    bodies, ids, paths, sources = {}, set(), set(), set()
    for note in notes:
        _require(isinstance(note, dict) and set(note) == {'id', 'source', 'path', 'sha256', 'size', 'privacy',
                                                           'external_processing', 'source_references'})
        _require(isinstance(note['id'], str) and _ID.fullmatch(note['id']) and not _DEVICE.fullmatch(note['id'])
                 and note['id'].casefold() not in ids
                 and type(note['size']) is int and 0 < note['size'] <= policy['limits']['max_note_bytes']
                 and note['privacy'] in ('personal', 'synthetic') and note['external_processing'] is False)
        parts = _path(note['path'], True)
        _require(len(parts) > 1 and parts[0].casefold() != 'system' and note['path'].casefold() not in paths
                 and any(note['path'].startswith(p) for p in policy['write_scope']['create_prefixes']))
        refs = note['source_references']
        _require(isinstance(refs, list) and 1 <= len(refs) <= 8 and all(isinstance(x, str) and x.strip() and len(x) <= 512 for x in refs)
                 and sum(map(len, refs)) <= 2048)
        _require(note['source'].casefold() not in sources)
        sources.add(note['source'].casefold())
        data = read_bytes(root.joinpath(*_path(note['source'], True)), 128000)
        _require(len(data) == note['size'] and _sha(data) == note['sha256'])
        bodies[note['path']] = _frontmatter(data, note)
        ids.add(note['id'].casefold()); paths.add(note['path'].casefold())
    _links(bodies)
    return {'root': root, 'vault': vault, 'policy': policy, 'manifest': manifest}


def validate_historical(private_root, manifest_path, manifest_sha256, policy_path, policy_sha256,
                        authorization_path, authorization_sha256):
    try:
        data = _load(private_root, manifest_path, manifest_sha256, policy_path, policy_sha256,
                     authorization_path, authorization_sha256)
        if data is None:
            return {'status': 'DISABLED', 'count': 0, 'note_ids': []}
        ids = [n['id'] for n in data['manifest']['notes']]
        return {'status': 'VALID', 'count': len(ids), 'note_ids': ids}
    except (ValueError, OSError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, OverflowError):
        return {'status': 'REFUSED', 'count': 0, 'note_ids': []}


def _encoded(value, limit=128000):
    data = (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode()
    _require(len(data) <= limit)
    return data


def _present(path):
    for part in (path, *path.parents):
        if os.path.lexists(part):
            st = part.lstat()
            _require(not part.is_symlink() and not getattr(part, 'is_junction', lambda: False)() and not getattr(st, 'st_reparse_tag', 0) & 0x20000000)
    return os.path.lexists(path)


def _state(directory, policy, pin):
    pointer_path = directory / 'latest.json'
    if not _present(pointer_path):
        return None, {'schema': 'brain-managed-manifest-v1', 'policy_id': policy['policy_id'],
                      'policy_sha256': pin, 'generation': 0, 'notes': []}, None
    pointer_bytes = read_bytes(pointer_path, 4096)
    pointer = _json(pointer_bytes)
    _require(isinstance(pointer, dict) and set(pointer) == {'manifest', 'sha256'}
             and isinstance(pointer['manifest'], str) and re.fullmatch(r'[0-9a-f]{64}\.manifest\.json', pointer['manifest'])
             and isinstance(pointer['sha256'], str) and _HASH.fullmatch(pointer['sha256']))
    raw = read_bytes(directory / pointer['manifest'], 1048576)
    _require(_sha(raw) == pointer['sha256'])
    state = _json(raw)
    _require(isinstance(state, dict) and set(state) == {'schema', 'policy_id', 'policy_sha256', 'generation', 'transaction_id', 'notes'}
             and state['schema'] == 'brain-managed-manifest-v1' and state['policy_id'] == policy['policy_id']
             and state['policy_sha256'] == pin and type(state['generation']) is int and state['generation'] >= 1
             and isinstance(state['transaction_id'], str) and isinstance(state['notes'], list)
             and len(state['notes']) <= policy['limits']['max_managed_notes'])
    ids, paths = set(), set()
    for note in state['notes']:
        _require(isinstance(note, dict) and set(note) == {'id', 'path', 'sha256', 'size', 'privacy', 'external_processing', 'source_references'}
                 and isinstance(note['id'], str) and _ID.fullmatch(note['id']) and note['id'] not in ids
                 and isinstance(note['sha256'], str) and _HASH.fullmatch(note['sha256'])
                 and type(note['size']) is int and 0 < note['size'] <= policy['limits']['max_note_bytes']
                 and note['privacy'] in ('synthetic', 'personal') and note['external_processing'] is False)
        _path(note['path'], True)
        _require(note['path'].casefold() not in paths)
        refs = note['source_references']
        _require(isinstance(refs, list) and 1 <= len(refs) <= 8 and all(isinstance(x, str) and x.strip() and len(x) <= 512 for x in refs)
                 and sum(map(len, refs)) <= 2048)
        ids.add(note['id']); paths.add(note['path'].casefold())
    return pointer_bytes, state, pointer['sha256']


def _outcome(status, tx=None, saved=0, recovered=0, unchanged=0, error=None, outcomes=None):
    outcomes = outcomes or []
    return {'status': status, 'transaction_id': tx, 'counts': {'saved': saved, 'recovered': recovered,
            'unchanged': unchanged, 'not_attempted': sum(n['status'] == 'NOT_ATTEMPTED' for n in outcomes),
            'updated': 0, 'linked': 0}, 'outcomes': outcomes,
            'error_code': error, 'sync_status': 'UNVERIFIED'}


class _Collision(ValueError):
    pass


def publish_historical(private_root, manifest_path, manifest_sha256, policy_path, policy_sha256,
                       authorization_path, authorization_sha256):
    args = (private_root, manifest_path, manifest_sha256, policy_path, policy_sha256,
            authorization_path, authorization_sha256)
    tx = None
    saved = recovered = 0
    outcomes = []
    mutated = False
    try:
        loaded = _load(*args)
        if loaded is None:
            return _outcome('DISABLED')
        root, vault, policy, manifest = (loaded[k] for k in ('root', 'vault', 'policy', 'manifest'))
        entries = [{k: n[k] for k in ('id', 'path', 'sha256', 'size', 'privacy', 'external_processing', 'source_references')}
                   for n in manifest['notes']]
        outcomes = [{'id': n['id'], 'status': 'NOT_ATTEMPTED'} for n in entries]
        binding = {'schema': 'brain-historical-intent-v1', 'root': str(root), 'vault': str(vault),
                   'manifest_sha256': manifest_sha256, 'policy_sha256': policy_sha256,
                   'authorization_sha256': authorization_sha256, 'batch_id': manifest['batch_id'], 'entries': entries}
        tx = _sha(_encoded(binding))
        base = root / 'publications' / policy['policy_id']
        transaction = base / 'historical' / tx
        intent = transaction / 'intent.json'
        receipt_path = transaction / 'receipt.json'
        commit_path = transaction / 'commit.json'
        directory = base / 'managed'
        with publication_lock(root, vault) as acquired:
            if not acquired:
                return _outcome('BUSY', tx, error='BUSY')
            _require(_load(*args) is not None)
            pointer, state, state_hash = _state(directory, policy, policy_sha256)
            retry = _present(intent)
            if retry:
                intent_record = _json(read_bytes(intent, 128000))
                _require(isinstance(intent_record, dict) and set(intent_record) == {
                    'binding', 'initial_pointer', 'initial_pointer_sha256', 'initial_manifest_sha256', 'initial_generation'}
                    and intent_record['binding'] == binding and type(intent_record['initial_generation']) is int
                    and intent_record['initial_generation'] >= 0
                    and (intent_record['initial_pointer_sha256'] is None or
                         isinstance(intent_record['initial_pointer_sha256'], str) and _HASH.fullmatch(intent_record['initial_pointer_sha256']))
                    and (intent_record['initial_manifest_sha256'] is None or
                         isinstance(intent_record['initial_manifest_sha256'], str) and _HASH.fullmatch(intent_record['initial_manifest_sha256'])))
            else:
                for entry in entries:
                    if _present(vault.joinpath(*_path(entry['path'], True))):
                        raise _Collision()
                _require(not any(n['id'] in {e['id'] for e in entries} or
                                 n['path'].casefold() in {e['path'].casefold() for e in entries} for n in state['notes']))
                _require(len(state['notes']) + len(entries) <= policy['limits']['max_managed_notes'])
                intent_record = {'binding': binding, 'initial_pointer': pointer.decode('utf-8') if pointer is not None else None,
                                 'initial_pointer_sha256': _sha(pointer) if pointer is not None else None,
                                 'initial_manifest_sha256': state_hash, 'initial_generation': state['generation']}
                mutated = True
                _require(write_immutable(intent, _encoded(intent_record)))
            commit = None
            if _present(commit_path):
                commit = _json(read_bytes(commit_path, 128000))
                initial_pointer = intent_record['initial_pointer']
                if initial_pointer is None:
                    _require(intent_record['initial_pointer_sha256'] is None
                             and intent_record['initial_manifest_sha256'] is None and intent_record['initial_generation'] == 0)
                    initial_notes = []
                else:
                    _require(isinstance(initial_pointer, str) and _sha(initial_pointer.encode()) == intent_record['initial_pointer_sha256'])
                    initial_ref = _json(initial_pointer)
                    _require(isinstance(initial_ref, dict) and set(initial_ref) == {'manifest', 'sha256'}
                             and isinstance(initial_ref['manifest'], str)
                             and re.fullmatch(r'[0-9a-f]{64}\.manifest\.json', initial_ref['manifest'])
                             and initial_ref['sha256'] == intent_record['initial_manifest_sha256'])
                    initial_raw = read_bytes(directory / initial_ref['manifest'], 1048576)
                    _require(_sha(initial_raw) == intent_record['initial_manifest_sha256'])
                    initial_state = _json(initial_raw)
                    _require(isinstance(initial_state, dict) and isinstance(initial_state.get('notes'), list)
                             and initial_state.get('generation') == intent_record['initial_generation'])
                    initial_notes = initial_state['notes']
                _require(isinstance(commit, dict) and set(commit) == {
                    'schema', 'transaction_id', 'previous_pointer_sha256', 'pointer_sha256',
                    'manifest_sha256', 'generation', 'entries'}
                    and commit['schema'] == 'brain-historical-commit-v1' and commit['transaction_id'] == tx
                    and type(commit['generation']) is int
                    and commit['entries'] == entries and pointer is not None
                    and commit['previous_pointer_sha256'] == intent_record['initial_pointer_sha256']
                    and _sha(pointer) == commit['pointer_sha256'] and state_hash == commit['manifest_sha256']
                    and state['generation'] == commit['generation'] and state.get('transaction_id') == tx
                    and state['generation'] == intent_record['initial_generation'] + 1
                    and state['notes'] == [*initial_notes, *entries])
            else:
                _require((pointer.decode('utf-8') if pointer is not None else None) == intent_record['initial_pointer']
                         and (_sha(pointer) if pointer is not None else None) == intent_record['initial_pointer_sha256']
                         and state_hash == intent_record['initial_manifest_sha256']
                         and state['generation'] == intent_record['initial_generation'])
            if _present(receipt_path):
                receipt = _json(read_bytes(receipt_path, 128000))
                _require(commit is not None and isinstance(receipt, dict)
                         and type(receipt.get('generation')) is int and all(e in state['notes'] for e in entries)
                         and receipt == {'transaction_id': tx, 'manifest_sha256': state_hash,
                                     'generation': state['generation'], 'entries': entries})
                for entry in entries:
                    marker = transaction / (entry['id'] + '.installed.json')
                    attempt = transaction / (entry['id'] + '.attempt.json')
                    collision = transaction / (entry['id'] + '.collision.json')
                    _require(not _present(collision) and read_bytes(attempt, 128000) == _encoded(entry)
                             and read_bytes(marker, 128000) == _encoded(entry)
                             and read_bytes(vault.joinpath(*_path(entry['path'], True))) ==
                             read_bytes(root.joinpath(*_path(next(n['source'] for n in manifest['notes'] if n['id'] == entry['id']), True))))
                return _outcome('NO_CHANGE', tx, unchanged=len(entries),
                                outcomes=[{'id': e['id'], 'status': 'UNCHANGED'} for e in entries])
            if not (state.get('transaction_id') == tx and all(e in state['notes'] for e in entries)):
                _require(not any(n['id'] in {e['id'] for e in entries} or n['path'].casefold() in
                                 {e['path'].casefold() for e in entries} for n in state['notes']))
                _require(len(state['notes']) + len(entries) <= policy['limits']['max_managed_notes'])
            # Refuse a conflicting later member before making any new target writes.
            for entry in entries:
                target = vault.joinpath(*_path(entry['path'], True))
                attempt = transaction / (entry['id'] + '.attempt.json')
                installed = transaction / (entry['id'] + '.installed.json')
                collision = transaction / (entry['id'] + '.collision.json')
                _require(not _present(collision))
                if _present(attempt):
                    _require(read_bytes(attempt, 128000) == _encoded(entry) and _present(installed)
                             and read_bytes(installed, 128000) == _encoded(entry) and _present(target))
                    target_data = read_bytes(target, 128000)
                    _require(len(target_data) == entry['size'] and _sha(target_data) == entry['sha256'])
                else:
                    _require(not _present(installed) and not _present(target))
            for index, (note, entry) in enumerate(zip(manifest['notes'], entries)):
                target = vault.joinpath(*_path(entry['path'], True))
                attempt = transaction / (entry['id'] + '.attempt.json')
                installed = transaction / (entry['id'] + '.installed.json')
                collision = transaction / (entry['id'] + '.collision.json')
                _require(not _present(collision))
                if _present(attempt):
                    _require(read_bytes(attempt, 128000) == _encoded(entry) and _present(installed)
                             and read_bytes(installed, 128000) == _encoded(entry) and _present(target))
                    payload = read_bytes(root.joinpath(*_path(note['source'], True)), 128000)
                    _require(read_bytes(target, 128000) == payload)
                    recovered += 1
                    outcomes[index]['status'] = 'RECOVERED_PRESENT'
                    continue
                _require(not _present(installed) and not _present(target))
                _require(_load(*args) is not None)
                mutated = True
                _require(write_immutable(attempt, _encoded(entry)))
                outcomes[index]['status'] = 'ATTEMPTED_UNCERTAIN'
                payload = read_bytes(root.joinpath(*_path(note['source'], True)), 128000)
                _require(_sha(payload) == entry['sha256'] and len(payload) == entry['size'])
                try:
                    success = write_immutable(target, payload)
                except FileExistsError:
                    success = False
                if not success:
                    outcomes[index]['status'] = 'COLLISION'
                    write_immutable(collision, _encoded(entry))
                    return _outcome('PARTIAL', tx, saved, recovered, error='COLLISION', outcomes=outcomes)
                saved += 1
                outcomes[index]['status'] = 'CREATED'
                _require(read_bytes(target, 128000) == payload)
                _require(write_immutable(installed, _encoded(entry)))
            for entry in entries:
                target = vault.joinpath(*_path(entry['path'], True))
                data = read_bytes(target, 128000)
                _require(len(data) == entry['size'] and _sha(data) == entry['sha256'])
            _require(_load(*args) is not None)
            current_pointer, current_state, current_hash = _state(directory, policy, policy_sha256)
            _require(current_pointer == pointer)
            if commit is not None and current_state.get('transaction_id') == tx and all(e in current_state['notes'] for e in entries):
                committed = current_state; committed_hash = current_hash
            else:
                _require(not any(n['id'] in {e['id'] for e in entries} or n['path'].casefold() in
                                 {e['path'].casefold() for e in entries} for n in current_state['notes']))
                _require(len(current_state['notes']) + len(entries) <= policy['limits']['max_managed_notes'])
                committed = {'schema': 'brain-managed-manifest-v1', 'policy_id': policy['policy_id'],
                             'policy_sha256': policy_sha256, 'generation': current_state['generation'] + 1,
                             'transaction_id': tx, 'notes': [*current_state['notes'], *entries]}
                raw = _encoded(committed, 1048576)
                committed_hash = _sha(raw)
                name = committed_hash + '.manifest.json'
                mutated = True
                write_immutable(directory / name, raw)
                _require(_load(*args) is not None and _state(directory, policy, policy_sha256)[0] == pointer)
                new_pointer = _encoded({'manifest': name, 'sha256': committed_hash})
                commit = {'schema': 'brain-historical-commit-v1', 'transaction_id': tx,
                          'previous_pointer_sha256': _sha(pointer) if pointer is not None else None,
                          'pointer_sha256': _sha(new_pointer), 'manifest_sha256': committed_hash,
                          'generation': committed['generation'], 'entries': entries}
                _require(write_immutable(commit_path, _encoded(commit)))
                _require(_load(*args) is not None and _state(directory, policy, policy_sha256)[0] == pointer)
                replace_pointer(directory / 'latest.json', new_pointer)
                mutated = True
            for entry in entries:
                data = read_bytes(vault.joinpath(*_path(entry['path'], True)), 128000)
                _require(len(data) == entry['size'] and _sha(data) == entry['sha256'])
            _require(_load(*args) is not None)
            _, verified, verified_hash = _state(directory, policy, policy_sha256)
            _require(verified_hash == committed_hash and verified == committed)
            receipt = {'transaction_id': tx, 'manifest_sha256': committed_hash,
                       'generation': committed['generation'], 'entries': entries}
            mutated = True
            _require(write_immutable(receipt_path, _encoded(receipt)))
            return _outcome('COMPLETED', tx, saved, recovered, outcomes=outcomes)
    except _Collision:
        return _outcome('PARTIAL' if mutated or saved else 'QUARANTINED', tx, saved, recovered,
                        error='COLLISION', outcomes=outcomes)
    except (ValueError, OSError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, OverflowError):
        return _outcome('PARTIAL' if mutated or saved else 'REFUSED', tx, saved, recovered,
                        error='HISTORICAL_IMPORT_REFUSED', outcomes=outcomes)
