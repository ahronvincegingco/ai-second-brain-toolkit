# 🧠 Second Brain Toolkit

**Private, fail-closed personal memory engine for Obsidian & AI coding agents.**

Memory enables AI agents to remember your architecture decisions, project milestones, and coding style across sessions. This toolkit connects your local Obsidian vault to any MCP-compatible AI agent over standard input/output (stdio IPC)—with zero cloud databases, zero subscription fees, and strict cryptographic privacy.

<p align="center">
  <img src="assets/banner.svg" alt="Second Brain" width="100%" />
</p>

```
  CAPTURE           STAGE             GATE             VAULT            RECALL
┌─────────┐      ┌───────────┐    ┌───────────┐    ┌───────────┐      ┌─────────┐
│ AI Chat │ ───> │ Candidate │ ─> │  SHA-256  │ ─> │ Canonical │ ───> │ Any AI  │
│ Desktop │      │  Staging  │    │  Policy   │    │ Vault .md │      │  Agent  │
└─────────┘      └───────────┘    └───────────┘    └───────────┘      └─────────┘
 /brain-save      fail-closed      no-clobber        Obsidian        /brain-recall
```

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-F5A623?style=flat-square&logo=open-source-initiative&logoColor=white" alt="License: MIT" /></a>
  <a href="https://github.com/Heiron8/ai-second-brain-toolkit/actions"><img src="https://img.shields.io/badge/Tests-454%20Passing-10B981?style=flat-square&logo=github-actions&logoColor=white" alt="Tests" /></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/Protocol-MCP%202.3-8B5CF6?style=flat-square&logo=anthropic&logoColor=white" alt="MCP" /></a>
  <a href="SECURITY.md"><img src="https://img.shields.io/badge/Privacy-100%25%20Local-6366F1?style=flat-square" alt="Privacy" /></a>
  <img src="https://img.shields.io/badge/Cost-%240%20%2F%20Free-059669?style=flat-square" alt="Cost" />
</p>

<p align="center">
  <b>Zero cloud subscriptions</b> • <b>Strict local stdio IPC</b> • <b>Single-source Markdown truth</b> • <b>No-clobber staging</b>
</p>

---

## Commands

7 commands and tools that map to your local memory lifecycle. Each one enforces strict fail-closed boundaries automatically.

| What you're doing | Command / Tool | Key principle |
|---|---|---|
| **Capture session outcomes** | `/brain-save` | Explicit current-context staging; zero background scraping |
| **Stage candidate drafts** | `scripts/migrate.py` | Isolated staging outside Git and OneDrive |
| **Verify cryptographic pins** | `SHA-256 Grant Guard` | Tamper-evident integrity; changed bytes refuse replay |
| **Atomic vault publication** | `scripts/publish_memory.py` | Strict no-clobber; existing notes are never overwritten |
| **Recall verified notes** | `/brain-recall` | Read-only lexical & semantic search; hash-cited Markdown excerpts |
| **Maintain knowledge graph** | `scripts/wiki_memory.py` | Conservative wikilinks and cross-session claim deduplication |
| **Audit local health** | `scripts/install_toolkit.py --check-only` | Fail-closed diagnostic report; zero cloud telemetry |

---

## Quick Start

**Fastest path — clone and install in 30 seconds:**

```powershell
# 1. Clone the repository
git clone https://github.com/Heiron8/ai-second-brain-toolkit.git
cd ai-second-brain-toolkit

# 2. Run the non-destructive installer
.\scripts\install.ps1
```

The installer auto-discovers your Obsidian vault, initializes your private staging workspace outside Git, validates your dependencies, and runs self-diagnostics.

> **Privacy guarantee:** All personal transcripts, cryptographic grants, and private notes live outside this repository (in `.ai-second-brain-private/`). Your personal data is never committed to Git.

---

## Client Setup

Pick your AI client below to connect your Second Brain via local Model Context Protocol (MCP):

<details>
<summary><b>Claude Desktop &amp; Claude Code</b></summary>

Add to your `claude_desktop_config.json` (or Claude Code settings):

```json
{
  "mcpServers": {
    "second_brain": {
      "command": "python",
      "args": ["-B", "C:\\path\\to\\AI-Second-Brain-Tools\\scripts\\managed_memory_mcp.py"],
      "env": {
        "SECOND_BRAIN_MANAGED_GRANT": "C:\\path\\to\\.ai-second-brain-private\\grants\\managed-grant.json",
        "SECOND_BRAIN_MANAGED_GRANT_SHA256": "<YOUR_GRANT_SHA256>",
        "SECOND_BRAIN_MANAGED_CLIENT": "claude-anthropic"
      }
    }
  }
}
```
</details>

<details>
<summary><b>Cursor</b></summary>

Go to **Settings → Features → MCP → Add New MCP Server**:
- **Name:** `second_brain`
- **Type:** `command`
- **Command:** `python -B C:\path\to\AI-Second-Brain-Tools\scripts\managed_memory_mcp.py`
- Set environment variables (`SECOND_BRAIN_MANAGED_GRANT`, `SECOND_BRAIN_MANAGED_GRANT_SHA256`, `SECOND_BRAIN_MANAGED_CLIENT`).
</details>

<details>
<summary><b>Antigravity / Gemini CLI</b></summary>

Add to your `.gemini/antigravity-cli/mcp/second_brain.json`:

```json
{
  "command": "python",
  "args": ["-B", "C:\\path\\to\\AI-Second-Brain-Tools\\scripts\\managed_memory_mcp.py"],
  "env": {
    "SECOND_BRAIN_MANAGED_GRANT": "C:\\path\\to\\.ai-second-brain-private\\grants\\managed-grant.json",
    "SECOND_BRAIN_MANAGED_GRANT_SHA256": "<YOUR_GRANT_SHA256>",
    "SECOND_BRAIN_MANAGED_CLIENT": "antigravity-google"
  }
}
```
</details>

<details>
<summary><b>Codex &amp; GitHub Copilot CLI</b></summary>

Add to `.codex/config.toml`:

```toml
[mcp.servers.second_brain]
command = "python"
args = ["-B", "C:\\path\\to\\AI-Second-Brain-Tools\\scripts\\managed_memory_mcp.py"]
env = { SECOND_BRAIN_MANAGED_GRANT = "C:\\path\\to\\.ai-second-brain-private\\grants\\managed-grant.json", SECOND_BRAIN_MANAGED_GRANT_SHA256 = "<YOUR_GRANT_SHA256>", SECOND_BRAIN_MANAGED_CLIENT = "codex-openai" }
```
</details>

---

## Core Principles & Guarantees

* **Fail-Closed by Design:** If a cryptographic grant hash drifts, a file path redirects through a symlink or junction, or an argument is out of bounds, the engine halts immediately.
* **$0 Cloud Cost:** Runs entirely locally against your existing Obsidian vault and OneDrive/iCloud sync. Zero vector DB subscriptions, zero Pinecone, zero OpenAI embedding bills.
* **Zero PII Exposure:** Runs strictly over local stdio IPC. All emitted citations use vault-relative paths (`references/note.md`) rather than host filesystem paths (`C:\Users\<username>\...`), ensuring your computer username and folder structure are never leaked to external AI providers.
* **Single-Source Markdown Truth:** Your notes remain clean, human-readable Markdown files inside your Obsidian vault. No proprietary databases or out-of-sync indexes.

---

## Documentation

- [Client Compatibility Guide](docs/client-compatibility.md)
- [Export Intake & Migration](docs/export-intake.md)
- [Wiki Health Diagnostics](docs/wiki-health.md)
- [Contribution Guidelines](CONTRIBUTING.md)
- [Security Policy](SECURITY.md)
- [Third-Party Licenses & Attribution](THIRD_PARTY.md)

---

## License & Attribution

This toolkit is licensed under the [MIT License](LICENSE).  
Copyright (c) 2026 Ahron Vince Gingco (Heiron8).

It incorporates format references from [kepano/obsidian-skills](https://github.com/kepano/obsidian-skills) (MIT) and design inspiration from [Graphify](https://github.com/Graphify-Labs/graphify) (Apache-2.0 / MIT). See [THIRD_PARTY.md](THIRD_PARTY.md) for full upstream provenance.
