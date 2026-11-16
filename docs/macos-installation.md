# macOS installation and verification

Use CPython 3.11, 3.12 or 3.13; 3.13 is recommended. Apple Silicon is supported
by wheel-only dependency installation. Windows retains `scripts/install.ps1`.
The same Python installer implements both entry points. No installer starts MCP,
enables capture, enrolls notes, or authorizes personal-data processing.

## Prerequisites and isolated setup

Install Git and Python using an existing trusted installation or Homebrew. If
Homebrew is missing, follow its official instructions at <https://brew.sh> and
review the installer before running it. In Terminal:

```sh
brew install git python@3.13
git --version
python3.13 --version
```

VS Code and Obsidian are optional for CLI tests. Install the native Apple Silicon
apps from their official websites if needed. An Obsidian app installation does
not authorize opening an existing personal vault.

From the existing toolkit checkout, create separate runtime and synthetic paths:

```sh
mkdir -p "$HOME/.local/share/second-brain"
python3.13 -m venv "$HOME/.local/share/second-brain/venv"
. "$HOME/.local/share/second-brain/venv/bin/activate"
python -m pip install --require-hashes --only-binary=:all: -r requirements-mcp.lock
mkdir -p "$HOME/.local/share/second-brain/test-vault/.obsidian"
export SECOND_BRAIN_VAULT="$HOME/.local/share/second-brain/test-vault"
export SECOND_BRAIN_PRIVATE="$HOME/.local/share/second-brain/private"
sh scripts/install.sh --dry-run --json
sh scripts/install.sh --json
sh scripts/install.sh --check-only
```

Keep the repository, virtual environment, private workspace, disposable vault and
machine-specific client configuration separate. Private workspaces must be outside
Git and OneDrive; choose a local unsynced directory. Do not use iCloud Documents or
`~/Library/CloudStorage` for private staging. New private directories use mode 700
and template files mode 600 on POSIX; existing permissions are not rewritten.
The disabled publication policy created here is an inert template, not a grant.

`SECOND_BRAIN_PYTHON=/absolute/venv/bin/python sh scripts/install.sh ...` selects
an interpreter explicitly. Otherwise the wrapper uses the activated virtual
environment, then `python3`. All Python installer options are forwarded, including
`--vault`, `--private-dir`, `--install-deps`, `--dry-run`, `--check-only` and `--json`.
Dependency installation is explicit; always run it from an isolated environment.

## Vault paths and filesystem boundaries

macOS requires an explicit `--vault "/absolute/path/to/test-vault"` or one of
`SECOND_BRAIN_VAULT`, `OBSIDIAN_VAULT`, `OBSIDIAN_VAULT_ROOT` (in that precedence).
There is no home-directory, Documents, Obsidian registry or cloud-folder scan.
Windows retains its bounded OneDrive discovery. OneDrive is not required for a
vault on either platform. Paths with spaces and Unicode are supported; quote them.
Typical owner-selected vault locations include local folders or iCloud/OneDrive,
but cloud synchronization and redirected File Provider paths are not certified.

Symlinks, junctions and name-surrogate reparse points remain refused. Configure
the physical path directly: macOS `/tmp` and `/var` are aliases, so use
`/private/tmp` and `/private/var` for disposable fixtures. Do not resolve an
untrusted vault path to bypass a refusal. Private configuration still needs the
owner-approved physical path. No-clobber writes never replace an existing file;
installation of several files is recoverable, not a globally atomic transaction.
Concurrent same-account parent-directory replacement is not an OS sandbox boundary.

## Synthetic MCP and Codex configuration

First run the existing protocol regression, which generates a temporary synthetic
grant and notes, initializes MCP, discovers both read tools, calls them, exercises
refusals and checks that source bytes are unchanged:

```sh
export TMPDIR="$(python -c 'import pathlib,tempfile; print(pathlib.Path(tempfile.gettempdir()).resolve())')"
python -m unittest tests.test_selected_memory_mcp tests.test_managed_memory_mcp -v
```

For an actual Codex client test, create a separate synthetic-only grant following
[selected-memory-retrieval.md](selected-memory-retrieval.md). The exact note must
contain `privacy: "synthetic"`, reside under `test-vault`, and have its byte length
and SHA-256 pinned. Keep the grant and its independently recorded hash outside Git.
Do not reuse or redirect a personal-memory profile. No server configuration alone
grants note access. In a separate opt-in Codex configuration, use absolute paths:

```toml
[mcp_servers.second_brain_synthetic]
command = "/absolute/path/to/venv/bin/python"
args = ["-B", "/absolute/path/to/ai-second-brain-toolkit/scripts/selected_memory_mcp.py"]
required = true
enabled_tools = ["search_selected_memory", "read_selected_memory"]

[mcp_servers.second_brain_synthetic.env]
SECOND_BRAIN_GRANT = "/absolute/path/to/private/synthetic-grant.json"
SECOND_BRAIN_GRANT_SHA256 = "<independently-pinned-grant-sha256>"
SECOND_BRAIN_CLIENT = "codex-openai"
PYTHONDONTWRITEBYTECODE = "1"
PYTHONIOENCODING = "utf-8"
```

This uses the current [`mcp_servers` Codex configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
Keep global defaults unchanged and unrelated servers disabled for the test. A fresh
client must actually call search/read and return the hidden fixture phrase and
relative citation before claiming integration success. Protocol success does not
prove VS Code extension discovery. Do not point at personal notes, install native
hooks, start watchers, or activate automatic capture. Other MCP clients can use the
same absolute command, args and environment in their own configuration format.

## Tests, diagnostics and troubleshooting

```sh
python -m pip check
python -m unittest discover -s tests -v
sh scripts/install.sh --check-only --json
```

Set physical `TMPDIR` as above before the full suite; otherwise macOS fixture paths
can trigger the intentional redirect guards. Tests use synthetic fixtures only.
Some HTTP lifecycle tests bind loopback; restricted sandboxes may deny these and
require a local unrestricted test run. No public server is started by installation.

* Missing Python: activate the venv or set `SECOND_BRAIN_PYTHON`; Apple's system
  Python may be too old. Python 3.14+ is outside the pinned support matrix.
* Hash mismatch/no matching wheel: stop, verify the package/version and platform;
  never remove `--require-hashes` or bypass hashes to install. Both locks preserve
  exact versions, retain prior hashes and include PyPI wheel hashes. `pywin32` has
  a Windows-only marker. Maintainers can explicitly regenerate hashes with
  `python scripts/refresh_lock_hashes.py requirements-mcp.lock` (or the Graphify
  lock), then review the diff. This is not automatic dependency updating.
* Missing vault: pass the explicit physical path. An invalid configured path never
  falls back to a different vault. Optional vault absence is reported in diagnostics.
* Missing MCP packages: core diagnostics can pass without optional MCP dependencies;
  read `dependencies.all_present` separately. `--install-deps` failure exits nonzero.
* Refused path/grant/hash: stop and inspect the synthetic configuration; never
  weaken the guard or repin personal material as part of troubleshooting.

CI covers Windows and macOS with Python 3.11–3.13. A configured matrix is not proof
that a remote run passed. Local verification and actual CI outcomes must be reported
separately. Graphify is optional; these instructions neither install nor run it.
The experimental hosted Chat discovery/synthetic launchers and supervisor still
require native Windows process identities; they fail closed on macOS and their
Windows-only integration tests skip there. This contribution covers local intake,
installer and stdio retrieval, not a port of the experimental HTTP launchers.
