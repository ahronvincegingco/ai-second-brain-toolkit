"""Policy-bound local publication. Disabled unless separately configured/approved."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import uuid

from migrate import digest, json_bytes, private_root as staging_root, validate_candidate
from save_interactive_memory import make_note, require, validate_packet
from selected_memory import checked_path


def read_bytes(path, limit=128000):
    path = checked_path(path)
    require(path.stat().st_size <= limit, 'PUBLICATION_STATE_TOO_LARGE')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, 'PUBLICATION_STATE_TOO_LARGE')
    return data


def read_json(path, limit=128000):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    return json.loads(read_bytes(path, limit), object_pairs_hook=unique)


def prepare_parent(path):
    for part in (path, *path.parents):
        require(not part.is_symlink() and not getattr(part, 'is_junction', lambda: False)(), 'REDIRECTED_PUBLICATION_PATH')
    path.parent.mkdir(parents=True, exist_ok=True)
    checked_path(path.parent, directory=True)


def write_immutable(path, data):
    """Flushed same-filesystem install; existing destination is never clobbered."""
    path = Path(path)
    prepare_parent(path)
    if path.exists():
        require(read_bytes(path, max(len(data), 1)) == data, 'PUBLICATION_STATE_CONFLICT')
        return False
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    try:
        if os.name == 'nt':
            os.rename(temporary, path)  # Windows rename refuses an existing target.
        else:
            os.link(temporary, path)  # POSIX rename would overwrite; link is no-clobber.
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def replace_pointer(path, data):
    prepare_parent(path)
    if path.exists():
        checked_path(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def publication_lock(root, vault):
    path = root / 'locks' / ('publish-' + digest(str(vault).casefold().encode()) + '.lock')
    prepare_parent(path)
    token = uuid.uuid4().hex.encode()
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        yield False
        return
    try:
        os.write(descriptor, token)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        yield True
    finally:
        if path.exists() and read_bytes(path, 64) == token:
            path.unlink()  # Only this invocation's private lock, never stale locks.


_DEVICE_PATTERN = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]|CONIN\$|CONOUT\$|CLOCK\$)(\..*)?$', re.I)


def safe_relative(value):
    require(isinstance(value, str) and value and not any(c in value for c in '\\:*?"<>|'),
            'INVALID_MANAGED_PATH')
    path = PurePosixPath(value)
    require(not path.is_absolute() and str(path) == value and path.suffix == '.md'
            and all(part not in ('', '.', '..') and part.casefold() not in ('.git', '.obsidian')
                    and _DEVICE_PATTERN.fullmatch(part.strip()) is None
                    for part in value.split('/')), 'INVALID_MANAGED_PATH')
    return path


def manifest_state(directory, policy, pin):
    pointer = directory / 'latest.json'
    if not pointer.exists():
        require(not pointer.is_symlink(), 'REDIRECTED_MANIFEST_POINTER')
        return None, {'generation': 0, 'notes': []}
    raw = read_bytes(pointer, 8000)
    value = json.loads(raw)
    name = value['manifest']
    require(isinstance(name, str) and re.fullmatch(r'[a-f0-9]{64}\.manifest\.json', name),
            'INVALID_MANIFEST_REFERENCE')
    data = read_bytes(directory / name, 2 * 1024 * 1024)
    require(digest(data) == value['sha256'], 'MANIFEST_CHANGED')
    manifest = json.loads(data)
    require(manifest['schema'] == 'brain-managed-manifest-v1'
            and manifest['policy_id'] == policy['policy_id'] and manifest['policy_sha256'] == pin
            and type(manifest['generation']) is int and manifest['generation'] >= 1
            and isinstance(manifest['notes'], list)
            and len(manifest['notes']) <= policy['limits']['max_managed_notes'], 'INVALID_MANIFEST')
    ids, paths = set(), set()
    for note in manifest['notes']:
        safe_relative(note['path'])
        require(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', note['id']) and note['id'] not in ids
                and note['path'].casefold() not in paths
                and re.fullmatch(r'[a-f0-9]{64}', note['sha256'])
                and type(note['size']) is int and 0 < note['size'] <= 128000, 'INVALID_MANAGED_NOTE')
        ids.add(note['id'])
        paths.add(note['path'].casefold())
    return raw, manifest


def render_note(candidate, policy_id, pin):
    note = deepcopy(candidate)
    note.update(review_state='policy-validated', title='Session knowledge (' +
                candidate['interactive_capture']['role'] + ')',
                publication_validation={'policy_id': policy_id, 'policy_sha256': pin})
    metadata = {key: value for key, value in note.items() if key not in ('body', 'claims')}
    lines = ['---', *[key + ': ' + json.dumps(value, ensure_ascii=False)
                     for key, value in metadata.items()], '---', '', '# ' + note['title'], '',
             'Automatic policy validation; not human review or independent verification.', '',
             note['body'], '', '## Evidence', '']
    for claim in note['claims']:
        lines += ['- ' + claim['text'], '  Evidence: ' + claim['evidence'] +
                  '; source: ' + claim['locator'], '']
    return ('\n'.join(lines).rstrip() + '\n').encode('utf-8')


def commit_new(root, policy, policy_path, pin, source_sha, candidate, relative):
    safe_relative(relative)
    vault = checked_path(Path(policy['vault_root']), directory=True).resolve()
    target = vault / relative
    data = render_note(candidate, policy['policy_id'], pin)
    require(len(data) <= policy['limits']['max_note_bytes'], 'NOTE_TOO_LARGE')
    entry = {'id': candidate['id'], 'path': relative, 'sha256': digest(data), 'size': len(data),
             'privacy': candidate['privacy'], 'external_processing': False,
             'source_references': candidate['sources']}
    transaction = digest(json_bytes({'source_sha256': source_sha,
                                    'candidate_sha256': digest(json_bytes(candidate)), 'policy_sha256': pin}))
    base = root / 'publications' / policy['policy_id']
    transaction_root = base / 'transactions' / transaction
    journal_path, receipt_path = transaction_root / 'journal.json', transaction_root / 'receipt.json'
    journal = {'schema': 'brain-publication-journal-v1', 'transaction_id': transaction,
               'source_sha256': source_sha, 'policy_sha256': pin,
               'note': entry, 'preimage': None, 'content_utf8': data.decode('utf-8')}
    with publication_lock(root, vault) as acquired:
        if not acquired:
            return result('BUSY', transaction, error='PUBLICATION_BUSY_RETRY_ONCE')
        saved = skipped = 0
        mutation_attempted = False
        initially_missing = not target.exists()
        try:
            directory = base / 'managed'
            pointer_before, manifest = manifest_state(directory, policy, pin)
            matches = [note for note in manifest['notes']
                       if note['id'] == entry['id'] or note['path'].casefold() == relative.casefold()]
            require(not matches or matches == [entry], 'MANAGED_NOTE_ID_CONFLICT')
            if matches:
                require(target.exists() and read_bytes(target) == data, 'ENROLLED_NOTE_CHANGED_OR_DELETED')
            else:
                require(len(manifest['notes']) < policy['limits']['max_managed_notes'], 'MANAGED_CAPACITY_REACHED')
            owned = journal_path.exists()
            if owned:
                require(read_json(journal_path, 512000) == journal, 'JOURNAL_CHANGED')
            if receipt_path.exists():
                receipt = read_json(receipt_path)
                require(owned and receipt['schema'] == 'brain-publication-receipt-v1'
                        and receipt['transaction_id'] == transaction and receipt['note'] == entry
                        and receipt['source_sha256'] == source_sha and receipt['policy_sha256'] == pin,
                        'COMPLETED_RECEIPT_CHANGED')
                require(read_bytes(target) == data and entry in manifest['notes'], 'PUBLISHED_NOTE_CHANGED')
                return result('NO_CHANGE', transaction, skipped=1)
            require(initially_missing or owned, 'UNMANAGED_DESTINATION_EXISTS')
            write_immutable(journal_path, json_bytes(journal))
            require(digest(read_bytes(policy_path, 32000)) == pin, 'PUBLICATION_POLICY_CHANGED')
            if target.exists():
                require(read_bytes(target) == data, 'OWNER_EDIT_AFTER_PARTIAL_WRITE')
                skipped = 1
            else:
                mutation_attempted = True
                installed = write_immutable(target, data)
                saved, skipped = (1, 0) if installed else (0, 1)
            require(read_bytes(target) == data, 'COMMITTED_NOTE_MISMATCH')
            if not matches:
                updated = {'schema': 'brain-managed-manifest-v1', 'policy_id': policy['policy_id'],
                           'policy_sha256': pin, 'generation': manifest['generation'] + 1,
                           'transaction_id': transaction, 'notes': [*manifest['notes'], entry]}
                manifest_data = json_bytes(updated)
                name = digest(manifest_data) + '.manifest.json'
                write_immutable(directory / name, manifest_data)
                current = read_bytes(directory / 'latest.json', 8000) if (directory / 'latest.json').exists() else None
                require(current == pointer_before and digest(read_bytes(policy_path, 32000)) == pin,
                        'PUBLICATION_STATE_CHANGED_DURING_COMMIT')
                mutation_attempted = True
                replace_pointer(directory / 'latest.json', json_bytes(
                    {'manifest': name, 'sha256': digest(manifest_data)}))
            require(read_bytes(target) == data, 'COMMITTED_NOTE_MISMATCH')
            outcome = result('SAVED', transaction, saved=saved, skipped=skipped)
            receipt = {'schema': 'brain-publication-receipt-v1', 'transaction_id': transaction,
                       'source_sha256': source_sha, 'policy_sha256': pin, 'note': entry, 'result': outcome}
            write_immutable(receipt_path, json_bytes(receipt))
            return outcome
        except FileExistsError:
            # Failed no-clobber installation never proves this invocation wrote bytes.
            if saved:
                return result('PARTIAL', transaction, saved=saved, error='PUBLICATION_INCOMPLETE')
            return result('QUARANTINED', transaction, quarantined=1, error='CONCURRENT_DESTINATION_EXISTS')
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
            if saved:
                return result('PARTIAL', transaction, saved=saved, error='PUBLICATION_INCOMPLETE')
            if mutation_attempted and isinstance(error, OSError):
                return result('PARTIAL', transaction, error='PUBLICATION_STATE_UNCERTAIN')
            return result('QUARANTINED', transaction, skipped=skipped, quarantined=1,
                          error='PUBLICATION_CONFLICT_OR_INCOMPLETE_STATE')


def result(status, transaction_id=None, saved=0, skipped=0, quarantined=0, error=None):
    return {'schema': 'brain-publication-result-v1', 'status': status,
            'transaction_id': transaction_id,
            'counts': {'saved': saved, 'updated': 0, 'linked': 0,
                       'skipped': skipped, 'quarantined': quarantined},
            'error_code': error, 'sync_status': 'UNVERIFIED'}


def load_policy(root, path, pin):
    path = checked_path(Path(path).absolute()).resolve()
    require(path.is_relative_to(root), 'POLICY_OUTSIDE_PRIVATE_ROOT')
    raw = read_bytes(path, 32000)
    require(digest(raw) == pin, 'PUBLICATION_POLICY_CHANGED')
    policy = json.loads(raw)
    require(policy.get('schema') == 'brain-publication-policy-v1'
            and type(policy.get('enabled')) is bool, 'INVALID_PUBLICATION_POLICY')
    if not policy['enabled']:
        return policy
    owner = policy.get('owner_authorization', {})
    require(owner.get('state') == 'approved' and isinstance(owner.get('reference'), str)
            and owner['reference'].strip(), 'PUBLICATION_NOT_AUTHORIZED')
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', policy.get('policy_id', '')), 'INVALID_POLICY_ID')
    vault = checked_path(Path(policy['vault_root']), directory=True).resolve()
    require(not vault.is_relative_to(root) and not root.is_relative_to(vault), 'PUBLICATION_ROOT_OVERLAP')
    require(not any((parent / '.git').exists() for parent in (vault, *vault.parents)),
            'REPOSITORY_VAULT_REFUSED')
    limits = policy['limits']
    require(type(limits['max_managed_notes']) is int and 1 <= limits['max_managed_notes'] <= 256
            and type(limits['max_note_bytes']) is int and 1 <= limits['max_note_bytes'] <= 128000,
            'INVALID_PUBLICATION_LIMITS')
    require(not policy['write_scope']['updates'] and not policy['write_scope']['bootstrap'],
            'PUBLICATION_OPERATION_NOT_IMPLEMENTED')
    prefixes = policy['write_scope']['create_prefixes']
    require(isinstance(prefixes, list) and len(prefixes) <= 16, 'INVALID_CREATE_SCOPE')
    for prefix in prefixes:
        require(isinstance(prefix, str) and prefix.endswith('/') and prefix != '/', 'INVALID_CREATE_SCOPE')
        safe_relative(prefix + 'placeholder.md')
    return policy


def staged_candidate(root, supplied):
    sha, scope = supplied.get('source_sha256', ''), supplied.get('scope', '')
    require(re.fullmatch(r'[a-f0-9]{64}', sha) and re.fullmatch(r'[a-f0-9]{16}/[a-f0-9]{24}', scope),
            'INVALID_STAGE_REFERENCE')
    folder = root / 'staging/brain-save' / scope / sha
    stored = read_json(folder / 'receipt.json', 16000)
    require(stored['schema'] == 'brain-save-receipt-v1' and stored['source_sha256'] == sha
            and stored['scope'] == scope and stored['source'].replace('\\', '/') ==
            'archive/' + sha + '.brain-save-context.json',
            'INVALID_STAGE_RECEIPT')
    archive = read_bytes(root / stored['source'], 65536)
    require(digest(archive) == sha, 'STAGED_SOURCE_CHANGED')
    envelope = json.loads(archive)
    require(envelope['schema'] == 'brain-save-archive-v1', 'INVALID_STAGED_SOURCE')
    packet = validate_packet(envelope['context_summary'])
    identity = envelope['identity']
    require(re.fullmatch(r'[a-f0-9]{64}', identity['project_id']), 'INVALID_PROJECT_ID')
    expected_scope = identity['project_id'][:16] + '/' + digest(
        (identity['session_reference'] + ':' + packet['role']).encode())[:24]
    require(scope == expected_scope, 'STAGED_SCOPE_MISMATCH')
    if stored['proposals'] == 0:
        require(stored['status'] == 'NO_MEANINGFUL_OUTCOMES' and not packet['items'], 'INVALID_EMPTY_RECEIPT')
        return sha, None
    require(stored['proposals'] == 1 and stored['status'] == 'STAGED', 'INVALID_STAGE_RECEIPT')
    expected_candidate = str((folder / 'candidate.json').relative_to(root)).replace('\\', '/')
    proposal = 'test-vault/logs/brain-save/' + scope + '/' + sha + '.md'
    require(stored['candidate'].replace('\\', '/') == expected_candidate and stored['proposal'] == proposal,
            'INVALID_STAGE_PATH')
    raw = read_bytes(root / expected_candidate, 65536)
    require(digest(raw) == stored['candidate_sha256']
            and digest(read_bytes(root / proposal)) == stored['proposal_sha256'], 'STAGED_CANDIDATE_CHANGED')
    candidate = json.loads(raw)
    validate_candidate(candidate)
    require(candidate['privacy'] != 'restricted', 'SENSITIVE_CANDIDATE_QUARANTINED')
    datetime.fromisoformat(candidate['created'])
    require(raw == json_bytes(make_note(envelope, sha, candidate['created'])),
            'STAGED_CANDIDATE_RECONSTRUCTION_FAILED')
    return sha, candidate


def publish_staged(private_root, staged_receipt, policy_path, policy_sha256, *,
                   wiki_sessions=None, index_preimage_sha256=None, log_preimage_sha256=None):
    root = staging_root(private_root)
    policy = load_policy(root, policy_path, policy_sha256)
    if not policy['enabled']:
        return result('DISABLED')
    sha, candidate = staged_candidate(root, staged_receipt)
    if candidate is None:
        return result('NO_CHANGE')
    relative = 'logs/brain-save/' + candidate['interactive_capture']['project_id'][:16] + '/' + sha + '.md'
    require(any(relative.startswith(prefix) for prefix in policy['write_scope']['create_prefixes']),
            'PUBLICATION_PATH_NOT_AUTHORIZED')
    outcome = commit_new(root, policy, Path(policy_path).absolute(), policy_sha256, sha, candidate, relative)
    if wiki_sessions is not None and outcome['status'] in ('SAVED', 'NO_CHANGE'):
        from wiki_memory import integrate_cross_session_knowledge
        vault = checked_path(Path(policy['vault_root']), directory=True).resolve()
        directory = root / 'publications' / policy['policy_id'] / 'managed'
        _, manifest = manifest_state(directory, policy, policy_sha256)
        wiki_res = integrate_cross_session_knowledge(
            private_dir=root,
            vault_dir=vault,
            manifest_notes=manifest['notes'],
            local_read_scope=policy.get('local_read_scope', {'prefixes': [], 'notes': []}),
            sessions=wiki_sessions,
            index_preimage_sha256=index_preimage_sha256,
            log_preimage_sha256=log_preimage_sha256,
            allow_personal=(candidate.get('privacy') == 'personal'),
            model_output_policy=policy.get('model_output'),
        )
        w_counts = wiki_res['model_output']['counts']
        for key in ('saved', 'updated', 'linked', 'skipped', 'quarantined'):
            outcome['counts'][key] = outcome['counts'].get(key, 0) + w_counts.get(key, 0)
    return outcome
