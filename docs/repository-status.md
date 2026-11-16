# Components and readiness

macOS contribution: `scripts/install.sh` delegates to the shared Python installer;
MCP and optional Graphify locks retain existing versions with portable wheel hashes
and a Windows-only `pywin32` marker. CI is configured for Windows/macOS and Python
3.11–3.13. See [setup and verification limits](macos-installation.md). This source
change does not activate any historical live installation or personal-data grant.

The current uncommitted source includes create-only publication and managed recall,
alongside private export intake and wiki templates. It does not enable a model or
automatically publish canonical memories. A separately approved local create-only
installation is now active; see [the current milestone](brain-memory-release.md).

| Paths | Status | Evidence / remaining work |
|---|---|---|
| `scripts/selected_memory.py` | Operational synthetic retrieval | Fixed manifest/client/path/hash checks, bounded reads and source citations. Canonical mode is fixture-tested; live access needs separate consent. |
| `scripts/selected_memory_mcp.py` | Operational local stdio | Search/read only; deterministic protocol test. Earlier two fresh Codex CLI sessions succeeded. No fresh model sessions run for repository preparation. |
| `scripts/read_memory.py`, `memory_mcp.py` | Legacy operational preview | Exactly four historical unpublished notes; retained for compatibility. Private transport remains disabled for model sessions. |
| `scripts/migrate.py` | Experimental local importer and draft workflow | Synthetic parsing/replay/provenance/branch/draft/FTS tests. No account-completeness or blocked-publisher acceptance claim. |
| `scripts/import_export.py` | Local source intake implemented | Bounded nested ZIP selection and private source/cache reconciliation tested; supplied export normalized locally. Attachments are references, not recovered content; wiki coverage is separate. |
| `scripts/save_interactive_memory.py`, `skills/brain-save/` | Approved local install active | Optional paired pinned-policy flags preserve staging fallback and truthful nested publication receipts; old settings backed up privately. |
| `scripts/publish_memory.py` | Create-only local pilot PASS | No-clobber writes, journal, replay, races, limits and recovery tested; one harmless live new note verified. Existing updates/linking remain pending. |
| `scripts/managed_memory.py`, `managed_memory_mcp.py` | Managed read-only source pilot GO | Bounded named-client search/read from enrolled, hash-verified notes; fixed grant sees later committed generations. Real synthetic stdio and validation-error redaction tested. |
| `scripts/recall_managed_memory.py`, `skills/brain-recall/` | Approved local install and fresh CLI recall PASS | Reuses managed reader and fixed host grant. Fresh Codex session retrieved the hidden pilot phrase/citation; no vault scan or raw-source access. Other clients remain unverified. |
| `scripts/capture_codex.py` | Explicit local snapshot helper | Stable supplied rollout bytes, dialogue extraction and evidence archive tested. Not automatic capture or a running session hook. |
| `scripts/run_codex_capture.py`, `capture_outcomes.py` | Opt-in CLI capture MVP | Supported JSON/schema interface; synthetic fresh-session extraction, repeats, replay, failure, unauthorized paths and disable exercised. Proposals only. Native/desktop hooks not installed or tested. See automatic-capture-mvp.md. |
| `scripts/backup.py` | Operational local snapshot helper | Synthetic bytes, hidden files, directories and restore checked. Source-path/root hardening and cloud independence are not generally certified. |
| `scripts/install_toolkit.py`, `scripts/install.ps1` | Operational installer and packaging validator | Python >= 3.11 check, OneDrive Obsidian vault discovery/validation, MCP dependency validation against `requirements-mcp.lock`, local private directory structure and default templates initialization (no-clobber), and comprehensive self-diagnostics / dry-run mode. Tested by `tests/test_installer.py`. |
| `scripts/source_report.py` (local, ignored) | Account-specific legacy helper; excluded | Contains historical assumptions and private taxonomy. Preserved locally, not part of the proposed repository. |
| `config.example.json`, `examples/` | Inert templates | No credentials or real grant. Legacy config.example.json is illustrative; the new publication-policy example describes the publisher's parsed schema and remains disabled. |
| `requirements-mcp.lock` | Existing pinned MCP dependency subset | Windows/CPython 3.13 closure derived from the existing Graphify lock. No package changes. |
| `docs/requirements-graphify.lock` | Optional pinned Graphify environment | Existing 0.9.92 demo pin, not a latest-version or vulnerability audit. |
| `.agents/skills/` | Vendored Obsidian format references | Markdown, Bases, Canvas. Pinned upstream and MIT notice retained. Skill discovery depends on host support. |
| `.codex/skills/graphify/` | Vendored experimental code skill | Upstream automation suggestions do not override AGENTS.md. No hook or watcher is enabled. |
| `tests/` | Portable synthetic regression suite | Core tests use stdlib. MCP protocol test runs when the optional dependency is installed; otherwise explicitly skips. |
| `docs/` | Maintainer guides and historical demo notes | Historical results are dated; private event logs and machine-specific profiles remain outside Git. |

The blocked custom Memory Manager remains blocked. Automatic background session
capture, managed updates/contextual linking, attachment recovery, complete-history
wiki coverage and authenticated cross-client retrieval remain incomplete.
Read-only live retrieval and personal/background capture require separate scope and
approval; publishing this toolkit does not authorize either milestone.

Combined source regression on 2026-11-02: 162 tests run, 161 passed, one existing native
Windows symlink skip. Fresh independent review issued scoped GO for controlled
create-only source use subject to owner activation; it does not establish full
production readiness, installed clients, cloud sync or OS isolation.

## Layout retained

```text
.agents/skills/         Obsidian format references
.codex/skills/graphify/  Upstream manual code-intelligence skill
scripts/                Local Python tools and read-only MCP transport
tests/                  Synthetic tests, no personal fixture content
examples/               Placeholder configuration examples
docs/                   Contracts, status, provenance and procedures
THIRD-PARTY-LICENSES/    Preserved upstream license/notice texts
requirements-mcp.lock   Optional MCP dependency pin
config.example.json     Inert policy illustration
AGENTS.md               Agent scope and operating boundaries
README.md               Entry point
THIRD_PARTY.md           Attribution and pinned source roles
```

Private workspaces, notes, exports, source inventories, backups, grants, runtime
environments, local settings and test evidence are not repository contents.
