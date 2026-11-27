# Save from the current Codex conversation

In an existing local Codex conversation, send:

```text
$brain-save
```

Codex extracts up to eight essential decisions, work/test reports, lessons, blockers,
review observations and next actions from its available context, then invokes the
local adapter once. No separate Codex/model session, handoff copy or transcript
export is involved. With no publication settings, the reply gives STAGED, DUPLICATE_SOURCE,
NO_MEANINGFUL_OUTCOMES or a safe refusal code and, when applicable, the proposal path.

On the approved local host, create-only publication and brain-recall were installed
and activated on 2026-11-02. A harmless canonical save, replay, publication-settings
disable and blind fresh Codex CLI recall passed. Use `$brain-recall` with a narrow
question to retrieve enrolled knowledge. The standing Codex/OpenAI grant covers only
notes enrolled by this publisher, including its citation metadata. See
[release status](brain-memory-release.md). Existing-note updates, automatic contextual
linking remain pending. The exact pilot's OneDrive cloud bytes were later verified;
B also observed native Backed up and synced and exact-pilot local availability.
Ongoing health remains a separate observation, not a publisher guarantee.

## Native managed recall on the local host

The separately configured read-only `second_brain_managed` MCP now works in this
local app and a fresh Codex CLI. Installed `$brain-recall` prefers those native
search/read tools; command fallback applies only when they are absent, never after
an MCP refusal/error. Start context reads with five lines; both the 40-line and
8000-character ceilings still apply. Cite returned note ID, hash, lines and source
metadata. The existing approved enrolled corpus is shared by these local clients.
The five reviewed historical notes were separately imported and enrolled under
`second_brain_history`; use that explicit server for this pilot. The installed
brain-recall route keeps its existing session-memory corpus, without aggregation.
Native historical recall now passes in the local app after supported app-server
configuration reload, and in the separately recorded fresh CLI test. This
connection adds no MCP save tool or app-side save proof.
See [client evidence and configuration limits](client-compatibility.md#managed-mcp-connection-and-official-obsidian-cli).

## Install once

The user-scoped installation applies across projects without editing project
settings or global config. This machine's existing `~/.agents/skills` junction
points to `~/.codex/skills`; these are the same installation, not two copies.
The source package is in `skills/brain-save` so it does not create a duplicate
project-scoped skill with the same name.

```powershell
$tools = Join-Path $env:USERPROFILE 'Documents\AI-Second-Brain-Tools'
$skill = Join-Path $env:USERPROFILE '.agents\skills\brain-save'
if (Test-Path -LiteralPath $skill) { throw 'Already installed; inspect before replacing' }
Copy-Item -LiteralPath (Join-Path $tools 'skills\brain-save') -Destination $skill -Recurse
$settings = @{
    python = 'C:\Python313\python.exe'
    adapter = Join-Path $tools 'scripts\save_interactive_memory.py'
    private_root = Join-Path $env:USERPROFILE 'Documents\AI-Second-Brain-Private'
}
[IO.File]::WriteAllText((Join-Path $skill 'local.json'), ($settings | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
```

The original staging-only installation was performed and tested during implementation;
the current host has since received the separately approved update. `local.json`
contains machine-specific paths and stays outside Git. The private root must
already have the existing `.private-staging.json` marker denying live writes and
external exports. No package installation or new database is required.

The skill has `allow_implicit_invocation: false`: it runs only on explicit mention.
Codex normally detects skill changes; if an existing session does not list it, use
`/skills` or explicitly mention the installed SKILL.md path once. Some clients need
a reload/restart for discovery. This does not authorize reading session histories.

## Roles, evidence and parallel sessions

- Infer planner/implementer/reviewer/mixed/unknown from the current task; do not
  relabel another session's implementation as this reviewer's work. Reviewers may
  report tests they actually ran and completed review observations.
- Only visible context is summarized. Compacted/truncated context is labeled;
  neither mode claims full transcript fidelity. Tool/message references are kept
  when available, with readable context locators otherwise. Never fabricate IDs.
- Host-provided CODEX_THREAD_ID/CODEX_SESSION_ID are used opportunistically, not
  treated as an authenticated public API. Their match to the actual thread was
  verified in the disposable test. Without them, the adapter creates an explicitly
  labeled invocation ID; it does not invent a real thread ID, and cross-invocation
  deduplication cannot be promised in that fallback.
- Project, session and role scope the immutable saves. Parallel roles/sessions
  have separate paths; same-scope identical source returns DUPLICATE_SOURCE with
  zero new proposals. Changed context/artifact bytes create a new candidate.
- Conversation claims remain summary-derived or inferred, including model reports
  of visible commands. The adapter independently hashes only selected safe project
  artifacts; this proves current bytes, not authorship, execution or correctness.
  It does not manufacture a CLI completion receipt for an interactive summary.

The adapter reuses existing privacy filtering, frozen candidate validation and
Markdown rendering. Suspect inputs fail before archival/rendering, including quoted
JSON credential assignments. Do not include raw dialogue, secrets, personal contact
details, personal absolute paths or unrelated information. Filtering is heuristic;
it does not make raw personal context safe by itself.

Every `reported-*` status requires `visible-tool-output`, `artifact`, or
`visible-user-message` support, including a completed decision. If a planning claim
is available only through a compacted summary, preserve it as `unknown` with
`inference` support; keep unexecuted next actions `proposed`. Do not upgrade its
support label merely to make the packet pass. The
[packet evidence/status gate](../skills/brain-save/references/packet.md#evidence-status-gate)
contains the exact combinations and an uncertainty-preserving example.

`RESULT_NEEDS_SUPPORT` is a classification refusal before staging writes.
`SAVE_REFUSED_IO_OR_FORMAT` is a separate generic error and does not establish a
filesystem cause or the absence of partial staging artifacts. Correcting a packet
does not resolve a host's managed output-folder restriction. Diagnose the current
host separately; stop on refusal and retain the existing policy, pins and retry
boundaries. No save is redirected to a substitute root to make it succeed.

## Staging, review and disable

Staging artifacts stay under the configured private root:

```text
archive/<hash>.brain-save-context.json
staging/brain-save/<project>/<session-role>/<hash>/candidate.json
staging/brain-save/<project>/<session-role>/<hash>/receipt.json
test-vault/logs/brain-save/<project>/<session-role>/<hash>.md
```

The archive is the exact normalized summary packet and artifact hashes, **not the
conversation transcript**. Candidates keep `review_state: candidate`, personal
privacy, engineering scope and `external_processing: false`. Without paired publication
settings, no publisher is invoked. No indexer, capture watcher or connector is invoked.
Missing/tampered cached evidence is refused,
not repaired silently. Existing renderer boilerplate still says demonstration;
that generic label does not turn real session summaries into synthetic evidence.

For staging-only use, review the returned Markdown and references. Accept/reject outside
the immutable records. Publication requires a separately approved standing policy;
ordinary captures within that policy do not require per-note approval. To stop, do not invoke
the skill. For removal, move only the installed brain-save folder out of the skill
search path after saving needed settings; staged proposals remain private.

## Optional source publication and recall

The adapter accepts paired `--policy` and `--policy-sha256` host arguments. The skill
reads the matching optional `publication_policy` and `publication_policy_sha256`
settings; it cannot create a policy or repin approval. No flags preserve the existing
staging result. Configured mode retains that result and adds nested `publication`
status/counts, including refusals and partial outcomes. Only verified publications
count as saved. Canonical note metadata remains personal/external-processing false,
with policy validation distinguished from human review. Cloud sync is UNVERIFIED.

The create-only publisher writes only approved new paths, keeps private journals and
hash-bound managed manifests, and refuses changed enrolled notes. It does not update
existing prose, delete notes, compile the history archive or maintain project hubs.
The current implementation caps the corpus at 256 notes and each note at 128000 bytes;
capacity refusal requires a later scope decision, not silent expansion.

`skills/brain-recall` invokes `scripts/recall_managed_memory.py` with a fixed named-client
grant. Search returns at most four matches; read returns at most 40 lines and 8000
total characters. Begin with small relevant context windows; the 40-line ceiling
does not guarantee that a request fits. On any refusal, stop without automatic
smaller-window retries. Only enrolled
notes within the separately approved scope are eligible. Publication does not authorize
external-model reads; the standing grant records that exception explicitly. Exact
host settings and interfaces are in [the shared contract](brain-memory-contract.md).

## Host permissions and desktop

The current session needs a local execution tool and permission to write the
configured staging folder. A strict Planning/read-only session may need a narrowly
scoped write approval, or may be unable to stage until the host permits it. The
skill never bypasses that boundary, enables shell tools, or starts another model.
The former no-tool pilot profiles deliberately disable capabilities needed here.
OneDrive project roots/internal Codex directories and credential/history artifacts
are refused by this adapter. Normal local project folders are supported.

In the Codex desktop local project workflow, use its skill selector/explicit skill
mention when available. ChatGPT-style composers may use `@` selection instead of
CLI `$` syntax. The desktop terminal runs the same CLI invocation. CLI same-thread
behaviour was tested; this change does not claim a Windows desktop UI interaction
test, automatic desktop-session capture, or cloud-only execution support.

## Future hooks — assessed, not enabled

Official local Stop/SessionEnd hooks could later append small local event records.
Stop is turn-oriented; SessionEnd is advisory and may fire only after close/archive
or idle, not immediately on chat switching. Hook transcript_path may be null and
its file format is unstable. A hook alone cannot recover model reasoning or turn
an incomplete transcript into verified memory. A future adapter would need separate
authorization, lightweight bounded writes, privacy handling and deduplication;
none was installed here. Do not run a long model extraction from SessionEnd.

Sources: [official skills](https://learn.chatgpt.com/docs/build-skills),
[official hooks](https://learn.chatgpt.com/docs/hooks).

## Verification

```powershell
& .\.venv-graphify\Scripts\python.exe -B -m unittest discover -s tests -v
```

The original implementation run completed 96 tests: 95 passed, one native Windows symlink
case skipped for unavailable creation privilege. A real disposable CLI conversation
ran a synthetic review in turn one, then received only `$brain-save` on turn two
of the same thread. Its generated proposal preserved reviewer identity, an actual
failed test result and a proposed/unimplemented fix. No fixture files changed.
Private transcripts/evidence, machine paths and local settings are outside Git.

On 2026-11-02 the combined source suite ran 162 tests: 161 passed and the same native
Windows symlink case skipped. A real fresh stdio MCP process recalled two successive
synthetic publications with unchanged grant bytes. That protocol/source proof alone
does not establish installed-client or cloud behavior.
The separately recorded live pilot below adds narrower host evidence.

On 2026-11-02 the approved local installation published one harmless fictional pilot
note. Exact replay returned NO_CHANGE with zero new saves. Temporarily removing the
paired publication settings produced the staging-only result; exact active settings
were restored. A fresh Codex CLI 0.166.0 session discovered brain-recall and executed
its installed reader, returning the hidden phrase with the correct note ID/line/hash.
The phrase was absent from that session's prompt and probe project. Two earlier
isolated attempts were refused before the reader ran; supplying the existing native
Windows sandbox selection per invocation resolved the probe. Global Codex settings
were not changed. The exact pilot was subsequently found through the authenticated
OneDrive website and downloaded. Its 2095 bytes and SHA-256 match the local canonical
file; the website's active account matches the configured local Personal account.
That verifies this pilot at that time, not continuous sync; see the release status.
