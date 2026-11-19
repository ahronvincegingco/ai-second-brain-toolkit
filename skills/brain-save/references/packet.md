# Adapter input

JSON, maximum 32 KB. Exactly these top-level fields:

```json
{
  "schema": "brain-save-context-v1",
  "role": "reviewer",
  "coverage": "visible-context",
  "items": [{
    "kind": "test",
    "text": "The focused test command reported one failure; no fix was applied.",
    "status": "reported-failed",
    "actor": "this-session",
    "support": "visible-tool-output",
    "references": ["visible python unittest command: exit 1"]
  }],
  "artifacts": ["tests/test_example.py"]
}
```

- role: planner, implementer, reviewer, mixed, unknown.
- coverage: visible-context, compacted-context; full transcript is never asserted.
- kind: decision, work, test, lesson, blocker, next-action, review.
- status: reported-completed, reported-passed, reported-failed, proposed, blocked, unknown.
- actor: this-session, other-session, unknown.
- support: visible-tool-output, visible-user-message, artifact, inference, unsupported.
- references: 1–4 strings per item, up to 400 characters; text at most 1,200 characters.
- artifacts: 0–8 safe relative project text/source paths. No credentials, .git/.codex, external paths or transcript files. Leave empty if unnecessary.

Use proposed for next actions and unexecuted tests. Reported results require visible support. Planners/reviewers cannot claim completed implementation work by this-session; use decision/review or attribute other-session correctly. A reviewer may report tests actually run in this reviewer conversation. All supplied conversation claims remain summary-derived (or inferred); local file hashes are independently checked separately. If roles change substantially, use mixed and preserve actor distinctions. Source references are annotations, not a promise that a whole transcript was archived or independently authenticated.

## Evidence/status gate

Check each item before the single adapter invocation. This rule applies to every
kind, including decisions; a `mixed` role does not bypass it.

| Status | Support allowed by the validator | Meaning |
| --- | --- | --- |
| `reported-completed`, `reported-passed`, `reported-failed` | `visible-tool-output`, `artifact`, `visible-user-message` | Cite the actual available support; the adapter does not independently authenticate it. |
| `unknown` | Any declared support value, including `inference` | Preserve a claim whose outcome or historical evidence is uncertain. |
| `proposed` | Any declared support value, including `inference` | An intended or unexecuted action; mandatory for `next-action` items. |
| `blocked` | Any declared support value, including `inference` | A blocker, with its evidence and uncertainty retained. |

`reported-completed` with `support: inference` is invalid even when references
point to a compacted checkpoint. A reference describes provenance; it does not
make an inferred result directly observed. Likewise, a written handoff alone does
not independently verify the implementation or test outcomes it reports.

For a planning claim available only through an inherited summary, a valid item is:

```json
{
  "kind": "decision",
  "text": "An inherited checkpoint describes a provisional navigation preference; confirm it before implementation.",
  "status": "unknown",
  "actor": "other-session",
  "support": "inference",
  "references": ["supplied compacted planning checkpoint"]
}
```

Use `actor: unknown` when the actor is not established. Keep `coverage:
compacted-context` when appropriate; that label neither upgrades the support nor
requires downgrading genuinely visible evidence. Do not change `support` to a
visible category unless that support is actually available. Never mark an
unexecuted test as passed or failed to satisfy a schema.

### Distinguish refusal causes

- `RESULT_NEEDS_SUPPORT` identifies an invalid status/support pair. Validation
  occurs before staging writes. Stop and report it; do not automatically rewrite
  the packet and retry. A later owner-authorized invocation can use an accurately
  classified packet.
- `SAVE_REFUSED_IO_OR_FORMAT` is a generic caught I/O or format error, not proof
  of a permission failure. A zero-proposal refusal does not prove that no partial
  private staging artifacts were written. Preserve the receipt and diagnose only
  within the authorized paths; do not redirect a real save, repin approval, or
  weaken validation. Testing validation alone does not test write permissions.
- Only `SAVE_BUSY_RETRY_ONCE` or nested publication `BUSY` permits the one
  identical automatic retry described by the skill. Synthetic diagnostic tests
  are separate from retrying a real save.

## Optional host publication settings

The five-field packet stays unchanged. Policy paths, hashes, destinations, grants and
manifest pointers must never be packet fields or model-chosen arguments.

An independently approved host configuration may add `publication_policy` and
`publication_policy_sha256` to the installed skill's existing `local.json`. Preserve
`python`, `adapter`, `private_root` and other local settings. This source package does
not install the update, edit local settings or grant canonical writes.

When both optional values are absent, use the existing staging command unchanged.
If exactly one is supplied, report incomplete settings; do not invent a pin or
silently omit the configured publication request. With both present, append
`--policy $settings.publication_policy --policy-sha256 $settings.publication_policy_sha256`
to the same adapter invocation. The adapter imports the publisher only after staging.

The output keeps the actual staging receipt at top level and adds `publication` only
when requested. Duplicate-stage receipts still allow a previously unpublished proposal
to be published. Publication success/NO_CHANGE/DISABLED exit 0; REFUSED, BUSY, PARTIAL
or QUARANTINED exit 2. Staging failure keeps the original safe-error behavior. On a
publication failure after staging, retain stdout's staging receipt and nested result;
do not replace it with a claim that staging failed or nothing was written.

Publication counts represent confirmed work. PARTIAL with PUBLICATION_STATE_UNCERTAIN
and zero confirmed saves does not establish zero actual writes. Do not retry conflicts
or change policy to force success. Publication is local with sync UNVERIFIED; recall
requires its separate client/provider grant. Canonical operations currently support
only the publisher's implemented scope, not automatic updates or linking by implication.
