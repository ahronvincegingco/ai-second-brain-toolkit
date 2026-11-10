"""Source-only bootstrap primitive; no CLI or authorization grant.

File flushes and an immutable intent aid retry; directory durability after power
loss is not established. Same-account writers can race path checks and are not
isolated by this cooperative lock. A process crash cannot return partial results;
inspect the private intent and target bytes before a same-intent retry.
"""
import hashlib
import json
import os
from pathlib import Path

from publish_memory import publication_lock, read_bytes, write_immutable
from selected_memory import checked_path
from migrate import private_root

TARGETS = ('AGENTS.md', 'CLAUDE.md', 'index.md', 'log.md', 'system/brain.md')


class BootstrapRefused(ValueError):
    """The requested operation did not satisfy the bootstrap contract."""

    def __init__(self, error_code, created=(), recovered=()):
        super().__init__(error_code)
        self.error_code = error_code
        self.created = list(created)
        self.recovered = list(recovered)


def _check_path(path):
    for part in (path, *path.parents):
        if os.path.lexists(part):
            info = part.lstat()
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)() or getattr(info, 'st_reparse_tag', 0) & 0x20000000:
                raise BootstrapRefused('REDIRECTED_PATH')


def _read(path):
    _check_path(path)
    return read_bytes(path, 128000)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def bootstrap_vault(source_dir, vault_dir, journal_dir, expected_hashes):
    """Install a fixed, hash-pinned payload; retry only an identical private intent.

    Returns paths created this invocation and matching paths observed on recovery.
    The caller supplies authorization; this function does not enable any policy.
    """
    progress = {'created': [], 'recovered': []}
    try:
        return _bootstrap(source_dir, vault_dir, journal_dir, expected_hashes, progress)
    except BootstrapRefused:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise BootstrapRefused('BOOTSTRAP_REFUSED', **progress) from exc


def _bootstrap(source_dir, vault_dir, journal_dir, expected_hashes, progress):
    roots = [checked_path(Path(p), directory=True) for p in (source_dir, vault_dir, journal_dir)]
    source, vault, private = roots
    if any(p != p.resolve() for p in roots):
        raise BootstrapRefused('ROOT_ALIAS')
    checked_path(private / '.private-staging.json')
    if private_root(private) != private:
        raise BootstrapRefused('PRIVATE_ROOT_ALIAS')
    if any(a == b or a in b.parents or b in a.parents
           for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise BootstrapRefused('OVERLAPPING_ROOTS')
    if not isinstance(expected_hashes, dict) or set(expected_hashes) != set(TARGETS):
        raise BootstrapRefused('INVALID_HASH_SET')
    payload = {name: _read(source / name) for name in TARGETS}
    if any(not isinstance(expected_hashes[name], str) or
           _hash(payload[name]) != expected_hashes[name] for name in TARGETS):
        raise BootstrapRefused('SOURCE_DRIFT')
    identity = {'schema': 'brain-bootstrap-intent-v1', 'source': str(source),
                'vault': str(vault), 'private': str(private),
                'files': {name: {'sha256': expected_hashes[name], 'size': len(payload[name])}
                          for name in TARGETS}}
    intent = (json.dumps(identity, sort_keys=True, separators=(',', ':')) + '\n').encode()
    intent_path = private / 'bootstrap-intents' / 'intent.json'
    with publication_lock(private, vault) as acquired:
        if not acquired:
            raise BootstrapRefused('LOCK_BUSY')
        _check_path(intent_path)
        retry = os.path.lexists(intent_path)
        if retry:
            if _read(intent_path) != intent:
                raise BootstrapRefused('INTENT_DRIFT')
        for name in TARGETS:
            target = vault / name
            _check_path(target)
            if os.path.lexists(target):
                if not retry or _read(target) != payload[name]:
                    raise BootstrapRefused('TARGET_COLLISION')
        if not retry and not write_immutable(intent_path, intent):
            raise BootstrapRefused('INTENT_COLLISION')
        try:
            for name in TARGETS:
                target = vault / name
                _check_path(target)
                if _read(source / name) != payload[name]:
                    raise BootstrapRefused('SOURCE_DRIFT')
                if os.path.lexists(target):
                    if not retry or _read(target) != payload[name]:
                        raise BootstrapRefused('TARGET_COLLISION')
                    progress['recovered'].append(name)
                elif write_immutable(target, payload[name]):
                    progress['created'].append(name)
                else:
                    raise BootstrapRefused('TARGET_COLLISION')
            if any(_read(source / name) != payload[name] or
                   _read(vault / name) != payload[name] for name in TARGETS):
                raise BootstrapRefused('FINAL_DRIFT')
        except (BootstrapRefused, OSError, ValueError) as exc:
            # Matching bytes observed after an uncertain failure do not prove authorship.
            for name in TARGETS:
                if name in progress['created'] or name in progress['recovered']:
                    continue
                try:
                    if _read(vault / name) == payload[name]:
                        progress['recovered'].append(name)
                except (OSError, ValueError):
                    pass
            code = exc.error_code if isinstance(exc, BootstrapRefused) else 'WRITE_FAILED'
            raise BootstrapRefused(code, **progress) from exc
        return progress
