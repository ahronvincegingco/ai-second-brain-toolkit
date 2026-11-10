"""Connected project knowledge, conservative wikilinks, cross-session deduplication,
and recoverable managed index/log maintenance (T07).

No network, watcher, background daemon, or external model calls.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

from migrate import json_bytes, private_root
from publish_memory import publication_lock, read_bytes, replace_pointer, write_immutable
from selected_memory import checked_path

_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_SLUG_RE = re.compile(r'[^a-z0-9]+')
_PART_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
_DEVICE_RE = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z', re.I)
_NOTE_KINDS = {'project', 'lesson', 'session', 'topic'}
_CLAIM_KINDS = {'decision', 'work', 'test', 'lesson', 'blocker', 'next-action', 'review'}


class WikiMemoryError(ValueError):
    """Safe error raised when a wiki memory operation violates a contract or boundary."""

    def __init__(self, error_code):
        super().__init__(error_code)
        self.error_code = error_code


def _require(condition, error_code='WIKI_MEMORY_INVALID'):
    if not condition:
        raise WikiMemoryError(error_code)


def _sha256_hex(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def slugify_identifier(value, max_len=40):
    """Normalize a human label into a deterministic lowercase slug."""
    _require(isinstance(value, str) and bool(value.strip()), 'INVALID_IDENTIFIER_TEXT')
    slug = _SLUG_RE.sub('-', value.strip().casefold()).strip('-')
    _require(bool(slug), 'INVALID_IDENTIFIER_TEXT')
    return slug[:max_len].rstrip('-')


def normalize_claim_text(text):
    """Collapse whitespace and casefold claim prose for deterministic comparison."""
    _require(isinstance(text, str) and bool(text.strip()), 'INVALID_CLAIM_TEXT')
    return ' '.join(text.strip().split()).casefold()


def deterministic_note_id(kind, scope_slug, topic_slug=None):
    """Compute a deterministic note identifier from kind, scope, and optional topic."""
    _require(isinstance(kind, str) and kind in _NOTE_KINDS, 'INVALID_NOTE_KIND')
    norm_scope = slugify_identifier(scope_slug, max_len=24)
    if topic_slug is not None:
        norm_topic = slugify_identifier(topic_slug, max_len=24)
        seed = f'{kind}:{norm_scope}:{norm_topic}'
        prefix = f'{kind}-{norm_scope}-{norm_topic}'
    else:
        seed = f'{kind}:{norm_scope}'
        prefix = f'{kind}-{norm_scope}'
    suffix = _sha256_hex(seed)[:12]
    note_id = f'{prefix[:60].rstrip("-")}-{suffix}'
    _require(_ID_RE.fullmatch(note_id) is not None, 'INVALID_NOTE_ID')
    return note_id


def deterministic_claim_id(subject_key, text):
    """Compute a deterministic claim identifier from subject key and normalized text."""
    norm_subject = slugify_identifier(subject_key, max_len=40)
    norm_text = normalize_claim_text(text)
    digest_part = _sha256_hex(f'{norm_subject}\x00{norm_text}')[:16]
    return f'claim-{digest_part}'


def validate_wiki_relpath(value, markdown=True):
    """Validate a vault-relative POSIX path, rejecting traversal, prefixes, and bad names."""
    _require(isinstance(value, str) and 0 < len(value) <= 512, 'INVALID_WIKI_PATH')
    _require(
        not any(c in value for c in '\\:*?"<>|\x00')
        and not any(ord(c) < 32 for c in value)
        and not value.startswith('/'),
        'INVALID_WIKI_PATH',
    )
    path = PurePosixPath(value)
    parts = value.split('/')
    _require(
        not path.is_absolute()
        and str(path) == value
        and all(
            part not in ('', '.', '..')
            and _PART_RE.fullmatch(part) is not None
            and not part.endswith((' ', '.'))
            and not _DEVICE_RE.fullmatch(part)
            and part.casefold() not in ('.git', '.obsidian')
            for part in parts
        ),
        'INVALID_WIKI_PATH',
    )
    if markdown:
        _require(path.suffix == '.md', 'INVALID_WIKI_PATH')
    return value


def _check_no_reparse_or_escape(vault_root, relpath):
    """Verify a vault-relative target resides inside vault_root with no symlinks/junctions."""
    try:
        vault = checked_path(Path(vault_root), directory=True)
    except ValueError as exc:
        raise WikiMemoryError('REDIRECTED_VAULT_ROOT') from exc
    _require(vault == vault.resolve(), 'REDIRECTED_VAULT_ROOT')
    parts = validate_wiki_relpath(relpath, markdown=True).split('/')
    target = vault.joinpath(*parts)
    for ancestor in (target, *target.parents):
        if os.path.lexists(ancestor):
            st = ancestor.lstat()
            if (
                ancestor.is_symlink()
                or getattr(ancestor, 'is_junction', lambda: False)()
                or getattr(st, 'st_reparse_tag', 0) & 0x20000000
            ):
                raise WikiMemoryError('REDIRECTED_WIKI_PATH')
        if ancestor == vault:
            break
    try:
        resolved = target.resolve()
        _require(resolved.is_relative_to(vault), 'WIKI_PATH_OUTSIDE_ROOT')
    except OSError as exc:
        raise WikiMemoryError('WIKI_PATH_RESOLUTION_ERROR') from exc
    if os.path.lexists(target):
        try:
            checked_path(target, directory=False)
        except ValueError as exc:
            raise WikiMemoryError('REDIRECTED_WIKI_PATH') from exc
    return target


def route_claim(claim, project_slug, project_id, source_sha256=None):
    """Route an eligible claim into managed project, lesson, and session note structures."""
    _require(isinstance(claim, dict), 'INVALID_CLAIM')
    kind = claim.get('kind')
    _require(kind in _CLAIM_KINDS, 'INVALID_CLAIM_KIND')
    text = claim.get('text', '')
    _require(isinstance(text, str) and bool(text.strip()), 'INVALID_CLAIM_TEXT')
    _require(isinstance(project_id, str) and re.fullmatch(r'[0-9a-f]{64}', project_id) is not None, 'INVALID_PROJECT_ID')
    norm_project = slugify_identifier(project_slug, max_len=32)
    topic_raw = claim.get('topic') or claim.get('subject') or kind
    norm_topic = slugify_identifier(topic_raw, max_len=32)

    if kind == 'lesson':
        target_kind = 'lesson'
        relpath = f'permanent/engineering/{norm_topic}.md'
        note_id = deterministic_note_id('lesson', 'engineering', norm_topic)
    else:
        target_kind = 'project'
        relpath = f'projects/{norm_project}/{norm_topic}.md'
        note_id = deterministic_note_id('project', norm_project, norm_topic)

    validate_wiki_relpath(relpath, markdown=True)
    hub_path = f'projects/{norm_project}/index.md'
    validate_wiki_relpath(hub_path, markdown=True)

    session_path = None
    if source_sha256 is not None:
        _require(
            isinstance(source_sha256, str) and re.fullmatch(r'[0-9a-f]{64}', source_sha256) is not None,
            'INVALID_SOURCE_HASH',
        )
        session_path = f'logs/brain-save/{project_id[:16]}/{source_sha256}.md'
        validate_wiki_relpath(session_path, markdown=True)

    return {
        'target_kind': target_kind,
        'note_id': note_id,
        'claim_id': deterministic_claim_id(norm_topic, text),
        'subject_key': norm_topic,
        'relative_path': relpath,
        'session_hub_path': hub_path,
        'session_log_path': session_path,
    }


def _safe_wikilink_token(value, allow_slash=False):
    _require(isinstance(value, str) and bool(value.strip()), 'INVALID_WIKILINK_TOKEN')
    forbidden = set('\\:*?"<>|\x00[]#^')
    if not allow_slash:
        forbidden.add('/')
    _require(
        not any(c in value for c in forbidden) and not any(ord(c) < 32 for c in value),
        'INVALID_WIKILINK_TOKEN',
    )
    return value.strip()


def format_path_wikilink(relative_path, heading=None, label=None):
    """Format a validated path-qualified Obsidian wikilink."""
    validated = validate_wiki_relpath(relative_path, markdown=True)
    target_no_ext = validated[:-3]
    _require('/' in target_no_ext, 'UNQUALIFIED_WIKILINK_TARGET')
    anchor = ''
    if heading is not None:
        clean_heading = _safe_wikilink_token(heading, allow_slash=False)
        anchor = f'#{clean_heading}'
    if label is not None:
        clean_label = _safe_wikilink_token(label, allow_slash=True)
        return f'[[{target_no_ext}{anchor}|{clean_label}]]'
    return f'[[{target_no_ext}{anchor}]]'


def resolve_wikilinks(candidates, allowed_notes, vault_root=None):
    """Conservatively resolve link queries against an approved enrolled subset.

    Only unambiguous matches within allowed_notes produce wikilinks; ambiguous or
    missing matches are skipped without fabricating links.
    """
    _require(isinstance(candidates, list) and isinstance(allowed_notes, list), 'INVALID_LINK_INPUT')
    validated_notes = []
    seen_ids = set()
    seen_paths = set()
    for note in allowed_notes:
        _require(isinstance(note, dict), 'INVALID_ALLOWED_NOTE')
        if note.get('denied') is True or note.get('privacy') == 'restricted':
            continue
        note_id = note.get('id')
        relpath = note.get('path')
        _require(isinstance(note_id, str) and _ID_RE.fullmatch(note_id) is not None, 'INVALID_ALLOWED_NOTE')
        validate_wiki_relpath(relpath, markdown=True)
        _require(note_id not in seen_ids and relpath.casefold() not in seen_paths, 'DUPLICATE_ALLOWED_NOTE')
        if vault_root is not None:
            _check_no_reparse_or_escape(vault_root, relpath)
        seen_ids.add(note_id)
        seen_paths.add(relpath.casefold())
        validated_notes.append(note)

    linked = []
    skipped = []
    for item in candidates:
        _require(isinstance(item, dict) and isinstance(item.get('query'), str), 'INVALID_LINK_CANDIDATE')
        raw_query = item['query'].strip()
        heading = item.get('heading')
        label = item.get('label')
        if not raw_query:
            skipped.append({'query': raw_query, 'reason': 'EMPTY_QUERY'})
            continue

        q_fold = raw_query.casefold()
        q_no_ext = q_fold[:-3] if q_fold.endswith('.md') else q_fold
        q_slug = _SLUG_RE.sub('-', q_fold).strip('-')

        matches = []
        for note in validated_notes:
            rel = note['path']
            rel_no_ext = rel[:-3].casefold()
            title = (note.get('title') or '').strip()
            title_fold = title.casefold()
            title_slug = _SLUG_RE.sub('-', title_fold).strip('-') if title_fold else ''
            stem_slug = PurePosixPath(rel).stem.casefold()
            aliases = [a.strip().casefold() for a in note.get('aliases', []) if isinstance(a, str)]

            if (
                q_fold == note['id'].casefold()
                or q_no_ext == rel_no_ext
                or (title_fold and q_fold == title_fold)
                or (title_slug and q_slug and q_slug == title_slug)
                or (q_slug and q_slug == stem_slug)
                or q_fold in aliases
            ):
                matches.append(note)

        if not matches:
            entry = {'query': raw_query, 'reason': 'NO_MATCH'}
            if heading:
                entry['heading'] = heading
            skipped.append(entry)
            continue

        if len(matches) > 1:
            entry = {'query': raw_query, 'reason': 'AMBIGUOUS_MATCH'}
            if heading:
                entry['heading'] = heading
            skipped.append(entry)
            continue

        target = matches[0]
        resolved_heading = None
        if heading is not None:
            note_headings = {
                h.strip().casefold(): h.strip()
                for h in target.get('headings', [])
                if isinstance(h, str) and h.strip()
            }
            if heading.strip().casefold() not in note_headings:
                skipped.append({'query': raw_query, 'heading': heading, 'reason': 'MISSING_HEADING'})
                continue
            resolved_heading = note_headings[heading.strip().casefold()]

        wikilink = format_path_wikilink(target['path'], heading=resolved_heading, label=label)
        linked.append({
            'query': raw_query,
            'target_id': target['id'],
            'wikilink': wikilink,
        })

    return {'linked': linked, 'skipped': skipped}


def _provenance_entry(claim):
    for field in ('session_reference', 'role', 'locator', 'date', 'status', 'evidence'):
        _require(isinstance(claim.get(field), str) and bool(claim[field].strip()), 'INVALID_CLAIM_PROVENANCE')
    return {
        'session_reference': claim['session_reference'].strip(),
        'role': claim['role'].strip(),
        'locator': claim['locator'].strip(),
        'date': claim['date'].strip(),
        'status': claim['status'].strip(),
        'evidence': claim['evidence'].strip(),
    }


def merge_session_claims(existing_claims, incoming_claims):
    """Merge incoming session claims into existing claims with cross-session deduplication.

    - Identical normalized claims on the same subject avoid duplicate prose while
      retaining every distinct session/role/source provenance record.
    - Explicit superseding claims preserve earlier dated claims as historical ('superseded').
    - Unresolved contradictory claims on an active subject are quarantined without
      erasing or overwriting earlier claims.
    """
    _require(isinstance(existing_claims, list) and isinstance(incoming_claims, list), 'INVALID_CLAIM_LIST')
    claims = [deepcopy(c) for c in existing_claims]
    quarantined = []

    by_id = {c['claim_id']: c for c in claims}

    for raw in incoming_claims:
        _require(isinstance(raw, dict), 'INVALID_CLAIM')
        privacy = raw.get('privacy', 'personal')
        _require(privacy in ('personal', 'synthetic'), 'INVALID_CLAIM_PRIVACY')
        subject_raw = raw.get('subject_key') or raw.get('topic') or raw.get('kind')
        norm_subject = slugify_identifier(subject_raw, max_len=40)
        kind = raw.get('kind')
        _require(kind in _CLAIM_KINDS, 'INVALID_CLAIM_KIND')
        text = raw.get('text', '')
        _require(isinstance(text, str) and bool(text.strip()), 'INVALID_CLAIM_TEXT')
        clean_text = ' '.join(text.strip().split())
        norm_text = normalize_claim_text(clean_text)
        cid = deterministic_claim_id(norm_subject, clean_text)
        prov = _provenance_entry(raw)
        supersedes = list(raw.get('supersedes') or [])
        _require(all(isinstance(x, str) and bool(x.strip()) for x in supersedes), 'INVALID_SUPERSEDES')

        if cid in by_id:
            target = by_id[cid]
            _require(target.get('privacy') == privacy, 'PRIVACY_BOUNDARY_MISMATCH')
            if prov not in target['provenance']:
                target['provenance'].append(prov)
                target['updated'] = prov['date']
            continue

        active_same_subject = [
            c for c in claims
            if c['subject_key'] == norm_subject and c.get('lifecycle', 'active') == 'active'
        ]
        for existing_item in claims:
            if existing_item['subject_key'] == norm_subject:
                _require(existing_item.get('privacy') == privacy, 'PRIVACY_BOUNDARY_MISMATCH')

        if active_same_subject:
            active_ids = {c['claim_id'] for c in active_same_subject}
            if supersedes and active_ids.issubset(set(supersedes)):
                for old_claim in active_same_subject:
                    old_claim['lifecycle'] = 'superseded'
                    sup_by = old_claim.setdefault('superseded_by', [])
                    if cid not in sup_by:
                        sup_by.append(cid)
            else:
                quarantined.append({
                    'claim_id': cid,
                    'subject_key': norm_subject,
                    'kind': kind,
                    'text': clean_text,
                    'provenance': [prov],
                    'conflicting_claim_ids': sorted(active_ids),
                    'reason': 'CONTRADICTORY_CLAIM_QUARANTINED',
                })
                continue

        new_entry = {
            'claim_id': cid,
            'subject_key': norm_subject,
            'kind': kind,
            'text': clean_text,
            'normalized_text': norm_text,
            'privacy': privacy,
            'lifecycle': 'active',
            'created': prov['date'],
            'updated': prov['date'],
            'supersedes': supersedes,
            'superseded_by': [],
            'provenance': [prov],
            'links': list(raw.get('links') or []),
        }
        claims.append(new_entry)
        by_id[cid] = new_entry

    return {'claims': claims, 'quarantined': quarantined}


def render_wiki_knowledge_note(note_id, title, kind, privacy, claims, links=(), quarantined=()):
    """Render a deterministic Markdown knowledge note preserving active and historical claims."""
    _require(isinstance(note_id, str) and _ID_RE.fullmatch(note_id) is not None, 'INVALID_NOTE_ID')
    _require(isinstance(title, str) and bool(title.strip()), 'INVALID_NOTE_TITLE')
    _require(kind in _NOTE_KINDS, 'INVALID_NOTE_KIND')
    _require(privacy in ('personal', 'synthetic'), 'INVALID_CLAIM_PRIVACY')
    _require(isinstance(claims, list) and len(claims) >= 1, 'EMPTY_NOTE_CLAIMS')

    all_sources = []
    for claim in claims:
        _require(claim.get('privacy') == privacy, 'PRIVACY_BOUNDARY_MISMATCH')
        for prov in claim.get('provenance', []):
            loc = prov['locator']
            if loc not in all_sources:
                all_sources.append(loc)

    created = min(c['created'] for c in claims)
    updated = max(c['updated'] for c in claims)
    metadata = {
        'schema_version': '1.0',
        'id': note_id,
        'title': title.strip(),
        'kind': kind,
        'review_state': 'policy-validated',
        'created': created,
        'updated': updated,
        'privacy': privacy,
        'external_processing': False,
        'sources': all_sources,
    }
    lines = [
        '---',
        *[f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in metadata.items()],
        '---',
        '',
        f'# {title.strip()}',
        '',
        'Automatic policy validation; not human review or independent verification.',
        '',
        '## Active Knowledge',
        '',
    ]
    active_claims = [c for c in claims if c.get('lifecycle', 'active') == 'active']
    historical_claims = [c for c in claims if c.get('lifecycle', 'active') != 'active']

    for claim in active_claims:
        lines.append(f"- **[{claim['claim_id']}]** ({claim['kind']}): {claim['text']}")
        if claim.get('supersedes'):
            lines.append(f"  Supersedes: {', '.join(claim['supersedes'])}")
        if claim.get('links'):
            lines.append(f"  Related: {', '.join(claim['links'])}")
        for prov in claim['provenance']:
            lines.append(
                f"  - Provenance: {prov['session_reference']} (role={prov['role']}; "
                f"date={prov['date']}; status={prov['status']}; evidence={prov['evidence']}; "
                f"source={prov['locator']})"
            )
        lines.append('')

    if historical_claims:
        lines.extend(['## Historical & Superseded Claims', ''])
        for claim in historical_claims:
            sup_by = ', '.join(claim.get('superseded_by', []))
            lines.append(
                f"- **[{claim['claim_id']}]** ({claim['lifecycle']}"
                f"{'; superseded by ' + sup_by if sup_by else ''}): {claim['text']}"
            )
            for prov in claim['provenance']:
                lines.append(
                    f"  - Provenance: {prov['session_reference']} (role={prov['role']}; "
                    f"date={prov['date']}; status={prov['status']}; evidence={prov['evidence']}; "
                    f"source={prov['locator']})"
                )
            lines.append('')

    if links:
        lines.extend(['## Connected Notes', ''])
        for wl in links:
            lines.append(f'- {wl}')
        lines.append('')

    if quarantined:
        lines.extend(['## Unresolved Contradictions (Quarantined)', ''])
        for q in quarantined:
            lines.append(
                f"- **[{q['claim_id']}]** ({q['reason']}; conflicts with "
                f"{', '.join(q['conflicting_claim_ids'])})"
            )
        lines.append('')

    return ('\n'.join(lines).rstrip() + '\n').encode('utf-8')


_INDEX_ROUTE_DEFAULTS = (
    ('projects/', 'Project context, decisions and current status', 'Add permitted project hubs when available'),
    ('permanent/', 'Reusable lessons with evidence', 'Add permitted topic hubs when available'),
    ('references/', 'Selected source summaries and citations', 'Add permitted references when available'),
    ('logs/', 'Dated session summaries', 'Add permitted summaries when available'),
)


def render_managed_index_routes(routes_by_folder):
    """Render the managed Knowledge routes table matching templates/vault/index.md."""
    _require(isinstance(routes_by_folder, dict), 'INVALID_INDEX_ROUTES')
    allowed_folders = {folder for folder, _, _ in _INDEX_ROUTE_DEFAULTS}
    _require(set(routes_by_folder).issubset(allowed_folders), 'INVALID_INDEX_FOLDER')

    lines = [
        '| Existing folder | Purpose | Navigation |',
        '| --- | --- | --- |',
    ]
    for folder, purpose, placeholder in _INDEX_ROUTE_DEFAULTS:
        items = routes_by_folder.get(folder) or []
        _require(isinstance(items, list) and len(items) <= 32, 'INVALID_INDEX_ROUTES')
        links = []
        for entry in items:
            _require(isinstance(entry, dict), 'INVALID_INDEX_ROUTE_ENTRY')
            if entry.get('denied') is True or entry.get('privacy') == 'restricted':
                continue
            path = entry.get('path')
            validate_wiki_relpath(path, markdown=True)
            _require(path.startswith(folder), 'INDEX_ROUTE_FOLDER_MISMATCH')
            label = entry.get('label') or entry.get('title')
            heading = entry.get('heading')
            links.append(format_path_wikilink(path, heading=heading, label=label))
        nav_cell = ', '.join(links) if links else placeholder
        lines.append(f'| `{folder}` | {purpose} | {nav_cell} |')
    return '\n'.join(lines) + '\n'


def render_managed_log_entries(entries):
    """Render verified chronological wiki log lines matching templates/vault/log.md."""
    _require(isinstance(entries, list) and 1 <= len(entries) <= 128, 'INVALID_LOG_ENTRIES')
    lines = []
    for item in entries:
        _require(isinstance(item, dict), 'INVALID_LOG_ENTRY')
        ts = item.get('timestamp')
        _require(isinstance(ts, str) and bool(ts.strip()), 'INVALID_LOG_TIMESTAMP')
        try:
            parsed_ts = datetime.fromisoformat(ts)
        except ValueError as exc:
            raise WikiMemoryError('INVALID_LOG_TIMESTAMP') from exc
        _require(parsed_ts.utcoffset() is not None, 'LOG_TIMESTAMP_MISSING_TZ')

        op = item.get('operation')
        _require(
            op in ('SAVED', 'UPDATED', 'LINKED', 'STAGED', 'BLOCKED', 'QUARANTINED', 'RECOVERED'),
            'INVALID_LOG_OPERATION',
        )
        note_links = item.get('note_links')
        _require(isinstance(note_links, list) and 1 <= len(note_links) <= 16, 'INVALID_LOG_LINKS')
        for wl in note_links:
            _require(
                isinstance(wl, str)
                and wl.startswith('[[')
                and wl.endswith(']]')
                and '/' in wl
                and '\n' not in wl,
                'INVALID_LOG_WIKILINK',
            )
        locator = item.get('locator')
        _require(
            isinstance(locator, str)
            and bool(locator.strip())
            and '\n' not in locator
            and '..' not in locator
            and '\\' not in locator,
            'INVALID_LOG_LOCATOR',
        )
        verification = item.get('verification')
        _require(
            isinstance(verification, str)
            and re.fullmatch(r'[A-Z0-9_-]{2,40}', verification) is not None,
            'INVALID_LOG_VERIFICATION',
        )
        links_str = ', '.join(note_links)
        lines.append(
            f'- `{ts}` | **{op}** | {links_str} | receipt: `{locator.strip()}` | verification: `{verification}`'
        )
    return '\n'.join(lines) + '\n'


def _replace_managed_block(document_text, section, new_section_body):
    _require(isinstance(section, str) and _ID_RE.fullmatch(section) is not None, 'INVALID_SECTION_ID')
    _require(isinstance(new_section_body, str) and '<!--' not in new_section_body and '-->' not in new_section_body,
             'INVALID_SECTION_BODY')
    start_marker = f'<!-- brain-managed:start section="{section}" -->\n'
    end_marker = f'<!-- brain-managed:end section="{section}" -->'

    start_count = document_text.count(start_marker)
    end_count = document_text.count(end_marker)
    _require(start_count == 1 and end_count == 1, 'MALFORMED_MANAGED_MARKERS')

    start_idx = document_text.index(start_marker)
    inner_start = start_idx + len(start_marker)
    end_idx = document_text.index(end_marker)
    _require(inner_start <= end_idx, 'MALFORMED_MANAGED_MARKERS')

    inner_existing = document_text[inner_start:end_idx]
    _require('<!-- brain-managed:' not in inner_existing, 'MALFORMED_MANAGED_MARKERS')

    body_normalized = new_section_body if new_section_body.endswith('\n') else (new_section_body + '\n')
    prefix = document_text[:inner_start]
    suffix = document_text[end_idx:]
    updated = prefix + body_normalized + suffix
    _require(updated[:inner_start] == prefix and updated[len(updated) - len(suffix):] == suffix,
             'UNMANAGED_BYTES_ALTERED')
    return updated


def _validate_roots(private_dir, vault_dir):
    try:
        priv = private_root(private_dir)
        checked_path(priv, directory=True)
        vault = checked_path(Path(vault_dir), directory=True)
    except ValueError as exc:
        raise WikiMemoryError('INVALID_ROOT_PATH') from exc
    _require(priv == priv.resolve() and vault == vault.resolve(), 'ROOT_ALIAS_REFUSED')
    _require(
        priv != vault and not vault.is_relative_to(priv) and not priv.is_relative_to(vault),
        'OVERLAPPING_ROOTS',
    )
    return priv, vault


def update_managed_section(
    private_dir,
    vault_dir,
    relative_path,
    section,
    expected_preimage_sha256,
    new_section_body,
):
    """Safely update only the explicit managed section of an enrolled vault file.

    Preserves all unmanaged bytes outside the section markers, writes a private
    write-ahead journal before mutation, refuses stale hashes or malformed markers,
    and supports idempotent recovery if interrupted.
    """
    priv, vault = _validate_roots(private_dir, vault_dir)
    validate_wiki_relpath(relative_path, markdown=True)
    _require(
        isinstance(expected_preimage_sha256, str)
        and re.fullmatch(r'[0-9a-f]{64}', expected_preimage_sha256) is not None,
        'INVALID_PREIMAGE_HASH',
    )
    target = _check_no_reparse_or_escape(vault, relative_path)
    _require(target.exists(), 'MANAGED_TARGET_MISSING')

    tx_seed = json_bytes({
        'vault': str(vault),
        'relative_path': relative_path,
        'section': section,
        'expected_preimage_sha256': expected_preimage_sha256,
        'new_section_body_sha256': _sha256_hex(new_section_body),
    })
    transaction_id = _sha256_hex(tx_seed)
    tx_dir = priv / 'wiki-updates' / transaction_id
    journal_path = tx_dir / 'journal.json'
    receipt_path = tx_dir / 'receipt.json'

    with publication_lock(priv, vault) as acquired:
        _require(acquired, 'PUBLICATION_LOCK_BUSY')
        target = _check_no_reparse_or_escape(vault, relative_path)
        current_bytes = read_bytes(target, 128000)
        current_sha = _sha256_hex(current_bytes)

        if journal_path.exists():
            journal = json.loads(read_bytes(journal_path, 512000))
            _require(
                journal.get('schema') == 'brain-wiki-section-journal-v1'
                and journal.get('transaction_id') == transaction_id
                and journal.get('relative_path') == relative_path
                and journal.get('section') == section
                and journal.get('preimage_sha256') == expected_preimage_sha256,
                'JOURNAL_INTENT_DRIFT',
            )
            intended_bytes = journal['intended_utf8'].encode('utf-8')
            intended_sha = journal['intended_sha256']
            _require(_sha256_hex(intended_bytes) == intended_sha, 'JOURNAL_CORRUPTED')

            if receipt_path.exists():
                _require(current_sha == intended_sha, 'LATER_EDIT_CONFLICT')
                return {
                    'status': 'NO_CHANGE',
                    'transaction_id': transaction_id,
                    'preimage_sha256': expected_preimage_sha256,
                    'committed_sha256': intended_sha,
                }

            if current_sha == expected_preimage_sha256:
                try:
                    replace_pointer(target, intended_bytes)
                except OSError as exc:
                    raise WikiMemoryError('MANAGED_UPDATE_INTERRUPTED') from exc
                verified = read_bytes(target, 128000)
                _require(_sha256_hex(verified) == intended_sha, 'COMMITTED_BYTES_MISMATCH')
                receipt = {
                    'schema': 'brain-wiki-section-receipt-v1',
                    'transaction_id': transaction_id,
                    'relative_path': relative_path,
                    'section': section,
                    'preimage_sha256': expected_preimage_sha256,
                    'committed_sha256': intended_sha,
                    'status': 'RECOVERED',
                }
                write_immutable(receipt_path, json_bytes(receipt))
                return {
                    'status': 'RECOVERED',
                    'recovery_stage': 'APPLIED_FROM_PREIMAGE',
                    'transaction_id': transaction_id,
                    'preimage_sha256': expected_preimage_sha256,
                    'committed_sha256': intended_sha,
                }

            if current_sha == intended_sha:
                receipt = {
                    'schema': 'brain-wiki-section-receipt-v1',
                    'transaction_id': transaction_id,
                    'relative_path': relative_path,
                    'section': section,
                    'preimage_sha256': expected_preimage_sha256,
                    'committed_sha256': intended_sha,
                    'status': 'RECOVERED',
                }
                write_immutable(receipt_path, json_bytes(receipt))
                return {
                    'status': 'RECOVERED',
                    'recovery_stage': 'FINALIZED_RECEIPT',
                    'transaction_id': transaction_id,
                    'preimage_sha256': expected_preimage_sha256,
                    'committed_sha256': intended_sha,
                }

            raise WikiMemoryError('LATER_EDIT_CONFLICT')

        _require(current_sha == expected_preimage_sha256, 'STALE_PREIMAGE_HASH')
        current_text = current_bytes.decode('utf-8')
        intended_text = _replace_managed_block(current_text, section, new_section_body)
        intended_bytes = intended_text.encode('utf-8')
        _require(len(intended_bytes) <= 128000, 'MANAGED_NOTE_TOO_LARGE')
        intended_sha = _sha256_hex(intended_bytes)

        journal = {
            'schema': 'brain-wiki-section-journal-v1',
            'transaction_id': transaction_id,
            'relative_path': relative_path,
            'section': section,
            'preimage_sha256': expected_preimage_sha256,
            'preimage_utf8': current_text,
            'intended_sha256': intended_sha,
            'intended_utf8': intended_text,
        }
        write_immutable(journal_path, json_bytes(journal))
        try:
            replace_pointer(target, intended_bytes)
        except OSError as exc:
            raise WikiMemoryError('MANAGED_UPDATE_INTERRUPTED') from exc

        verified = read_bytes(target, 128000)
        _require(_sha256_hex(verified) == intended_sha, 'COMMITTED_BYTES_MISMATCH')
        receipt = {
            'schema': 'brain-wiki-section-receipt-v1',
            'transaction_id': transaction_id,
            'relative_path': relative_path,
            'section': section,
            'preimage_sha256': expected_preimage_sha256,
            'committed_sha256': intended_sha,
            'status': 'UPDATED',
        }
        write_immutable(receipt_path, json_bytes(receipt))
        return {
            'status': 'UPDATED',
            'transaction_id': transaction_id,
            'preimage_sha256': expected_preimage_sha256,
            'committed_sha256': intended_sha,
        }


def restore_managed_preimage(private_dir, vault_dir, transaction_id):
    """Conditionally restore a managed file's preimage only if no later edits occurred."""
    priv, vault = _validate_roots(private_dir, vault_dir)
    _require(
        isinstance(transaction_id, str) and re.fullmatch(r'[0-9a-f]{64}', transaction_id) is not None,
        'INVALID_TRANSACTION_ID',
    )
    journal_path = priv / 'wiki-updates' / transaction_id / 'journal.json'
    _require(journal_path.exists(), 'JOURNAL_MISSING')
    with publication_lock(priv, vault) as acquired:
        _require(acquired, 'PUBLICATION_LOCK_BUSY')
        journal = json.loads(read_bytes(journal_path, 512000))
        _require(
            journal.get('schema') == 'brain-wiki-section-journal-v1'
            and journal.get('transaction_id') == transaction_id,
            'INVALID_JOURNAL',
        )
        relpath = journal['relative_path']
        target = _check_no_reparse_or_escape(vault, relpath)
        current_bytes = read_bytes(target, 128000)
        current_sha = _sha256_hex(current_bytes)
        if current_sha == journal['preimage_sha256']:
            return {'status': 'NO_CHANGE', 'restored_sha256': current_sha}
        _require(current_sha == journal['intended_sha256'], 'LATER_EDIT_CONFLICT')
        preimage_bytes = journal['preimage_utf8'].encode('utf-8')
        _require(_sha256_hex(preimage_bytes) == journal['preimage_sha256'], 'JOURNAL_CORRUPTED')
        replace_pointer(target, preimage_bytes)
        verified = read_bytes(target, 128000)
        _require(_sha256_hex(verified) == journal['preimage_sha256'], 'RESTORE_MISMATCH')
        return {'status': 'RESTORED', 'restored_sha256': journal['preimage_sha256']}


def _parse_note_frontmatter_and_headings(data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise WikiMemoryError('INVALID_NOTE_ENCODING') from exc
    lines = text.splitlines()
    _require(len(lines) >= 3 and lines[0] == '---' and '---' in lines[1:], 'INVALID_NOTE_FRONTMATTER')
    end_idx = lines.index('---', 1)
    frontmatter = {}
    for line in lines[1:end_idx]:
        if not line.strip():
            continue
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_-]*):\s*(.+)\s*$', line)
        _require(match is not None and match.group(1) not in frontmatter, 'INVALID_NOTE_FRONTMATTER')
        key, raw_val = match.group(1), match.group(2)
        try:
            frontmatter[key] = json.loads(raw_val)
        except json.JSONDecodeError:
            frontmatter[key] = raw_val.strip('"\'')

    headings = []
    fence = None
    for line in lines[end_idx + 1:]:
        f_match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if f_match:
            marker, info = f_match.group(1), f_match.group(2)
            if fence is None:
                fence = (marker[0], len(marker))
                continue
            if marker[0] == fence[0] and len(marker) >= fence[1] and not info.strip():
                fence = None
                continue
        if fence is None:
            h_match = re.match(r'^#{1,6}\s+(.+?)\s*#*\s*$', line)
            if h_match:
                headings.append(h_match.group(1).strip())
    return frontmatter, headings


def load_local_read_corpus(vault_dir, manifest_notes, local_read_scope, allow_personal=False):
    """Load metadata and headings ONLY for enrolled notes inside approved local_read_scope.

    Unselected, restricted, or out-of-scope personal notes are filtered out before
    any file open/read occurs, preventing denied-note canary access or leakage.
    """
    try:
        vault = checked_path(Path(vault_dir), directory=True)
    except ValueError as exc:
        raise WikiMemoryError('INVALID_VAULT_ROOT') from exc
    _require(vault == vault.resolve(), 'REDIRECTED_VAULT_ROOT')
    _require(isinstance(manifest_notes, list) and isinstance(local_read_scope, dict), 'INVALID_READ_SCOPE')

    prefixes = local_read_scope.get('prefixes', [])
    scope_ids = local_read_scope.get('notes', [])
    _require(isinstance(prefixes, list) and isinstance(scope_ids, list), 'INVALID_READ_SCOPE')
    for prefix in prefixes:
        _require(isinstance(prefix, str) and prefix.endswith('/') and prefix != '/', 'INVALID_READ_SCOPE')
        validate_wiki_relpath(prefix + 'placeholder.md', markdown=True)
    for sid in scope_ids:
        _require(isinstance(sid, str) and _ID_RE.fullmatch(sid) is not None, 'INVALID_READ_SCOPE')

    scope_id_set = set(scope_ids)
    seen_ids = set()
    seen_paths = set()
    corpus = []

    for entry in manifest_notes:
        _require(isinstance(entry, dict), 'INVALID_MANIFEST_NOTE')
        note_id = entry.get('id')
        relpath = entry.get('path')
        _require(isinstance(note_id, str) and _ID_RE.fullmatch(note_id) is not None, 'INVALID_MANIFEST_NOTE')
        validate_wiki_relpath(relpath, markdown=True)
        _require(note_id not in seen_ids and relpath.casefold() not in seen_paths, 'DUPLICATE_MANIFEST_NOTE')
        seen_ids.add(note_id)
        seen_paths.add(relpath.casefold())

        in_scope = (note_id in scope_id_set) or any(relpath.startswith(p) for p in prefixes)
        if not in_scope:
            continue

        privacy = entry.get('privacy')
        if privacy == 'restricted':
            continue
        _require(privacy in ('personal', 'synthetic'), 'INVALID_NOTE_PRIVACY')
        if privacy == 'personal' and not allow_personal:
            continue

        target = _check_no_reparse_or_escape(vault, relpath)
        data = read_bytes(target, 128000)
        _require(
            len(data) == entry.get('size') and _sha256_hex(data) == entry.get('sha256'),
            'ENROLLED_NOTE_DRIFT',
        )
        fm, headings = _parse_note_frontmatter_and_headings(data)
        _require(fm.get('privacy') == privacy and fm.get('privacy') != 'restricted', 'FRONTMATTER_PRIVACY_MISMATCH')
        title = fm.get('title')
        _require(isinstance(title, str) and bool(title.strip()), 'INVALID_NOTE_TITLE')
        corpus.append({
            'id': note_id,
            'path': relpath,
            'title': title.strip(),
            'headings': headings,
            'privacy': privacy,
            'external_processing': bool(fm.get('external_processing', False)),
        })

    return corpus


def sanitize_model_output(operation_result, model_output_policy=None):
    """Produce a bounded model-facing result that strips note bodies, queries, and denied titles/paths."""
    _require(isinstance(operation_result, dict), 'INVALID_OPERATION_RESULT')
    policy = model_output_policy if isinstance(model_output_policy, dict) else {'paths': False, 'titles': False}
    raw_counts = operation_result.get('counts') or {}
    counts = {
        key: int(raw_counts.get(key, 0))
        for key in ('saved', 'updated', 'linked', 'skipped', 'quarantined')
    }
    error_code = operation_result.get('error_code')
    if error_code is not None:
        _require(
            isinstance(error_code, str) and re.fullmatch(r'[A-Z0-9_]{2,64}', error_code) is not None,
            'UNSAFE_ERROR_CODE',
        )
    output = {
        'schema': 'brain-publication-result-v1',
        'status': operation_result.get('status', 'SAVED'),
        'transaction_id': operation_result.get('transaction_id'),
        'counts': counts,
        'error_code': error_code,
        'sync_status': 'UNVERIFIED',
    }
    if policy.get('paths') is True and policy.get('titles') is True:
        # Even when paths/titles are enabled in policy, never include notes with external_processing=False
        safe_links = [
            {'target_id': item['target_id'], 'wikilink': item['wikilink']}
            for item in operation_result.get('linked', [])
            if isinstance(item, dict) and item.get('external_processing') is True
        ]
        output['permitted_links'] = safe_links
    return output


def integrate_cross_session_knowledge(
    private_dir,
    vault_dir,
    manifest_notes,
    local_read_scope,
    sessions,
    index_preimage_sha256=None,
    log_preimage_sha256=None,
    allow_personal=False,
    model_output_policy=None,
):
    """Route, deduplicate, link, and commit cross-session knowledge notes and navigation."""
    priv, vault = _validate_roots(private_dir, vault_dir)
    _require(isinstance(sessions, list) and len(sessions) >= 1, 'INVALID_SESSIONS')
    corpus = load_local_read_corpus(
        vault_dir=vault,
        manifest_notes=manifest_notes,
        local_read_scope=local_read_scope,
        allow_personal=allow_personal,
    )

    notes_by_path = {}
    unique_linked = {}
    skipped_links_count = 0
    quarantined_count = 0
    latest_timestamp = None

    for sess in sessions:
        _require(isinstance(sess, dict), 'INVALID_SESSION_ENTRY')
        project_slug = sess.get('project_slug')
        project_id = sess.get('project_id')
        session_ref = sess.get('session_reference')
        role = sess.get('role')
        date = sess.get('date')
        privacy = sess.get('privacy', 'personal')
        _require(privacy in ('personal', 'synthetic'), 'INVALID_CLAIM_PRIVACY')
        if privacy == 'personal' and not allow_personal:
            raise WikiMemoryError('PERSONAL_SCOPE_NOT_AUTHORIZED')
        latest_timestamp = date
        for raw_claim in sess.get('claims') or []:
            routed = route_claim(raw_claim, project_slug=project_slug, project_id=project_id)
            relpath = routed['relative_path']
            link_queries = raw_claim.get('link_queries') or []
            resolved = resolve_wikilinks(link_queries, allowed_notes=corpus, vault_root=vault)
            claim_links = []
            for item in resolved['linked']:
                unique_linked[item['wikilink']] = item
                if item['wikilink'] not in claim_links:
                    claim_links.append(item['wikilink'])
            skipped_links_count += len(resolved['skipped'])

            bucket = notes_by_path.setdefault(
                relpath,
                {
                    'note_id': routed['note_id'],
                    'title': (raw_claim.get('topic') or routed['subject_key']).strip(),
                    'kind': routed['target_kind'],
                    'privacy': privacy,
                    'claims': [],
                    'quarantined': [],
                    'links': [],
                },
            )
            for wl in claim_links:
                if wl not in bucket['links']:
                    bucket['links'].append(wl)

            enriched_claim = {
                'subject_key': routed['subject_key'],
                'kind': raw_claim['kind'],
                'text': raw_claim['text'],
                'status': raw_claim['status'],
                'evidence': raw_claim['evidence'],
                'date': date,
                'session_reference': session_ref,
                'role': role,
                'locator': raw_claim['locator'],
                'privacy': privacy,
                'supersedes': raw_claim.get('supersedes') or [],
                'links': claim_links,
            }
            merged = merge_session_claims(bucket['claims'], [enriched_claim])
            bucket['claims'] = merged['claims']
            bucket['quarantined'].extend(merged['quarantined'])
            quarantined_count += len(merged['quarantined'])

    enrolled_by_path = {
        entry['path'].casefold(): entry
        for entry in manifest_notes
        if isinstance(entry, dict) and isinstance(entry.get('path'), str)
    }
    saved_count = 0
    updated_count = 0
    routes_by_folder = {'projects/': [], 'permanent/': [], 'references/': [], 'logs/': []}
    changed_note_links = []

    for relpath, bucket in sorted(notes_by_path.items()):
        target = _check_no_reparse_or_escape(vault, relpath)
        rendered_bytes = render_wiki_knowledge_note(
            note_id=bucket['note_id'],
            title=bucket['title'],
            kind=bucket['kind'],
            privacy=bucket['privacy'],
            claims=bucket['claims'],
            links=bucket['links'],
            quarantined=bucket['quarantined'],
        )
        _require(len(rendered_bytes) <= 128000, 'MANAGED_NOTE_TOO_LARGE')
        if target.exists():
            enrolled = enrolled_by_path.get(relpath.casefold())
            _require(enrolled is not None, 'UNMANAGED_TARGET_COLLISION')
            existing_bytes = read_bytes(target, 128000)
            _require(
                len(existing_bytes) == enrolled.get('size')
                and _sha256_hex(existing_bytes) == enrolled.get('sha256'),
                'UNMANAGED_TARGET_COLLISION',
            )
            replace_pointer(target, rendered_bytes)
            updated_count += 1
        else:
            installed = write_immutable(target, rendered_bytes)
            _require(installed, 'NOTE_INSTALL_CONFLICT')
            saved_count += 1

        verified = read_bytes(target, 128000)
        _require(verified == rendered_bytes, 'COMMITTED_BYTES_MISMATCH')
        wl = format_path_wikilink(relpath, label=bucket['title'])
        changed_note_links.append(wl)
        for folder in routes_by_folder:
            if relpath.startswith(folder):
                routes_by_folder[folder].append({
                    'path': relpath,
                    'title': bucket['title'],
                    'privacy': bucket['privacy'],
                })

    if index_preimage_sha256 is not None:
        index_body = render_managed_index_routes(routes_by_folder)
        update_managed_section(
            private_dir=priv,
            vault_dir=vault,
            relative_path='index.md',
            section='index-routes',
            expected_preimage_sha256=index_preimage_sha256,
            new_section_body=index_body,
        )
        updated_count += 1

    if log_preimage_sha256 is not None and changed_note_links:
        log_body = render_managed_log_entries([
            {
                'timestamp': latest_timestamp,
                'operation': 'SAVED' if saved_count else 'UPDATED',
                'note_links': changed_note_links,
                'locator': 'wiki-updates/batch-receipt.json',
                'verification': 'LOCAL_BYTES_VERIFIED',
            }
        ])
        update_managed_section(
            private_dir=priv,
            vault_dir=vault,
            relative_path='log.md',
            section='change-log',
            expected_preimage_sha256=log_preimage_sha256,
            new_section_body=log_body,
        )
        updated_count += 1

    tx_id = _sha256_hex(json_bytes({
        'vault': str(vault),
        'notes': sorted(notes_by_path.keys()),
        'saved': saved_count,
        'updated': updated_count,
    }))
    raw_result = {
        'status': 'SAVED' if (saved_count or updated_count) else 'NO_CHANGE',
        'transaction_id': tx_id,
        'counts': {
            'saved': saved_count,
            'updated': updated_count,
            'linked': len(unique_linked),
            'skipped': skipped_links_count,
            'quarantined': quarantined_count,
        },
        'linked': list(unique_linked.values()),
    }
    return {
        'model_output': sanitize_model_output(raw_result, model_output_policy=model_output_policy),
    }

