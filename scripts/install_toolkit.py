"""Single-command toolkit installer, environment validator, and self-diagnostics.

Deterministic local setup and portability validator for AI Second Brain:
- Enforces Python version check (>= 3.11).
- Discovers or validates the OneDrive Obsidian vault path.
- Checks MCP runtime dependencies against pinned requirements-mcp.lock.
- Initializes private workspace directory structures and default templates
  without overwriting existing credentials or user files (strict no-clobber).
- Optional vault template bootstrap (no-clobber).
- Runs comprehensive self-diagnostics / dry-run checks.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re

# Polyfill Path.is_junction for Python 3.11 compatibility
if not hasattr(Path, "is_junction"):
    def _is_junction(self: Path) -> bool:
        try:
            st = os.lstat(self)
            tag = getattr(st, "st_reparse_tag", 0)
            return bool(tag == 0xA0000003 or (tag & 0x20000000))
        except (OSError, ValueError):
            return False
    Path.is_junction = _is_junction  # type: ignore[attr-defined]
import shutil
import subprocess
import sys
import zipfile

MIN_PYTHON_VERSION = (3, 11)
DEFAULT_PRIVATE_DIRNAME = ".ai-second-brain-private"

CORE_DEPENDENCIES = (
    "mcp",
    "pydantic",
    "anyio",
    "uvicorn",
    "starlette",
    "jsonschema",
)

VAULT_CANDIDATE_SUBDIRS = (
    "AI-Second-Brain",
    "Obsidian",
    "Obsidian Vault",
    "Second-Brain",
    "Notes",
    os.path.join("Documents", "AI-Second-Brain"),
    os.path.join("Documents", "Obsidian"),
)

VAULT_BOOTSTRAP_TEMPLATES = (
    "AGENTS.md",
    "CLAUDE.md",
    "index.md",
    "log.md",
    os.path.join("system", "brain.md"),
)


def get_repo_root() -> Path:
    """Return the absolute Path to the repository root directory."""
    return Path(__file__).resolve().parents[1]


def check_python_version(version_info: tuple[int, ...] | None = None) -> tuple[bool, str]:
    """Check that Python runtime version is at least 3.11."""
    if version_info is None:
        version_info = sys.version_info[:3]
    version_str = ".".join(str(part) for part in version_info)
    is_supported = version_info[:2] >= MIN_PYTHON_VERSION
    return is_supported, version_str


def check_redirected_path(path: Path | str) -> None:
    """Ensure path and its ancestors contain no symlinks, junctions, or name-surrogate reparse tags."""
    target = Path(path)
    for part in (target, *target.parents):
        if os.path.lexists(part):
            info = part.lstat()
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)() or (getattr(info, "st_reparse_tag", 0) & 0x20000000):
                raise ValueError(f"Redirected path refused (symlink/junction/reparse): {part}")


def is_onedrive_path(path: Path | str) -> bool:
    """Check if the path resides inside OneDrive based on path segments or env variables."""
    p = Path(path).resolve()
    onedrive_envs = [
        os.environ.get("ONEDRIVE"),
        os.environ.get("OneDriveConsumer"),
        os.environ.get("OneDriveCommercial"),
    ]
    for env_val in onedrive_envs:
        if env_val:
            try:
                env_p = Path(env_val).resolve()
                if p == env_p or env_p in p.parents:
                    return True
            except (OSError, ValueError):
                pass
    return any(part.lower().startswith("onedrive") for part in p.parts)


def discover_onedrive_vault(explicit_path: str | Path | None = None) -> dict:
    """Discover or validate the Obsidian vault path in OneDrive.

    Returns a dict with path, existence, OneDrive status, redirects check, and diagnostics.
    """
    result = {
        "path": None,
        "exists": False,
        "is_dir": False,
        "in_onedrive": False,
        "has_obsidian_marker": False,
        "redirect_free": True,
        "discovered": False,
        "error": None,
    }

    if explicit_path:
        target = Path(explicit_path).expanduser().resolve()
        result["path"] = str(target)
        try:
            check_redirected_path(target)
            result["redirect_free"] = True
        except ValueError as err:
            result["redirect_free"] = False
            result["error"] = str(err)
            return result

        result["exists"] = target.exists()
        result["is_dir"] = target.is_dir() if result["exists"] else False
        result["in_onedrive"] = is_onedrive_path(target)
        result["has_obsidian_marker"] = (target / ".obsidian").is_dir() if result["exists"] else False
        return result

    # Discovery strategy:
    # 1. Environment variables
    for env_name in ("SECOND_BRAIN_VAULT", "OBSIDIAN_VAULT", "OBSIDIAN_VAULT_ROOT"):
        val = os.environ.get(env_name)
        if val:
            candidate = Path(val).expanduser().resolve()
            if candidate.is_dir():
                try:
                    check_redirected_path(candidate)
                    result["path"] = str(candidate)
                    result["exists"] = True
                    result["is_dir"] = True
                    result["in_onedrive"] = is_onedrive_path(candidate)
                    result["has_obsidian_marker"] = (candidate / ".obsidian").is_dir()
                    result["discovered"] = True
                    return result
                except ValueError:
                    pass

    # 2. Search known OneDrive locations
    candidate_roots: list[Path] = []
    for env_name in ("ONEDRIVE", "OneDriveConsumer", "OneDriveCommercial"):
        val = os.environ.get(env_name)
        if val:
            try:
                p = Path(val).expanduser().resolve()
                if p.is_dir() and p not in candidate_roots:
                    candidate_roots.append(p)
            except Exception:
                pass

    try:
        user_home = Path.home().resolve()
    except (RuntimeError, OSError):
        user_home_str = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        user_home = Path(user_home_str).resolve() if user_home_str else None

    if user_home:
        for fallback_name in ("OneDrive", "OneDrive - Personal", "OneDrive - Commercial"):
            p = user_home / fallback_name
            if p.is_dir() and p not in candidate_roots:
                candidate_roots.append(p)

    for root in candidate_roots:
        for sub in VAULT_CANDIDATE_SUBDIRS:
            candidate = (root / sub).resolve()
            if candidate.is_dir():
                try:
                    check_redirected_path(candidate)
                    result["path"] = str(candidate)
                    result["exists"] = True
                    result["is_dir"] = True
                    result["in_onedrive"] = True
                    result["has_obsidian_marker"] = (candidate / ".obsidian").is_dir()
                    result["discovered"] = True
                    return result
                except ValueError:
                    continue

    # Fallback suggestion if discovery found a OneDrive root but no vault folder yet
    if candidate_roots:
        suggested = candidate_roots[0] / "AI-Second-Brain"
        result["path"] = str(suggested)
        result["in_onedrive"] = True
        result["exists"] = False
        result["is_dir"] = False
    elif user_home:
        suggested = user_home / "Documents" / "AI-Second-Brain"
        result["path"] = str(suggested)
        result["in_onedrive"] = False
        result["exists"] = False
        result["is_dir"] = False
    else:
        suggested = Path("/AI-Second-Brain")
        result["path"] = str(suggested)
        result["in_onedrive"] = False
        result["exists"] = False
        result["is_dir"] = False

    return result


def check_dependencies(lockfile_path: Path | str | None = None) -> dict:
    """Validate MCP runtime dependencies against requirements-mcp.lock."""
    repo_root = get_repo_root()
    if lockfile_path is None:
        lockfile = repo_root / "requirements-mcp.lock"
    else:
        lockfile = Path(lockfile_path).resolve()

    installed = []
    missing = []
    for mod_name in CORE_DEPENDENCIES:
        try:
            importlib.import_module(mod_name)
            installed.append(mod_name)
        except ImportError:
            missing.append(mod_name)

    lockfile_exists = lockfile.is_file()
    cmd_guide = (
        f"{sys.executable} -m pip install --require-hashes -r {lockfile}"
        if lockfile_exists
        else "pip install mcp pydantic anyio uvicorn starlette jsonschema"
    )

    return {
        "all_present": len(missing) == 0,
        "installed": installed,
        "missing": missing,
        "lockfile_exists": lockfile_exists,
        "lockfile_path": str(lockfile) if lockfile_exists else None,
        "guide_command": cmd_guide,
    }


def install_dependencies(lockfile_path: Path | str | None = None, python_exe: str | None = None) -> bool:
    """Attempt installation of pinned dependencies from lockfile."""
    repo_root = get_repo_root()
    if lockfile_path is None:
        lockfile = repo_root / "requirements-mcp.lock"
    else:
        lockfile = Path(lockfile_path).resolve()

    if not lockfile.is_file():
        raise FileNotFoundError(f"Lockfile not found at {lockfile}")

    exe = python_exe or sys.executable
    cmd = [exe, "-m", "pip", "install", "--require-hashes", "-r", str(lockfile)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode == 0


def validate_private_path_safety(private_path: Path) -> None:
    """Ensure private workspace path is strictly outside git, outside OneDrive, and redirect-free."""
    p = private_path.resolve()
    check_redirected_path(p)

    # Must be outside OneDrive
    if is_onedrive_path(p):
        raise ValueError(f"Private workspace cannot reside inside OneDrive (found in {p})")

    # Must be outside any git repository
    for ancestor in (p, *p.parents):
        if (ancestor / ".git").exists():
            raise ValueError(f"Private workspace cannot reside inside a git repository (found .git in {ancestor})")


def init_private_workspace(
    private_path: str | Path | None = None,
    vault_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Initialize local private directory structures and inert templates without clobbering existing files."""
    if private_path is None:
        env_priv = os.environ.get("SECOND_BRAIN_PRIVATE")
        if env_priv:
            target_private = Path(env_priv).expanduser().resolve()
        else:
            target_private = (Path.home() / DEFAULT_PRIVATE_DIRNAME).resolve()
    else:
        target_private = Path(private_path).expanduser().resolve()

    validate_private_path_safety(target_private)

    subdirs = [
        "bootstrap-intents",
        "publications",
        "archive",
        "proposals",
        "receipts",
        "logs",
    ]

    report = {
        "private_root": str(target_private),
        "created_dirs": [],
        "created_files": [],
        "preserved_files": [],
        "dry_run": dry_run,
    }

    if not dry_run:
        target_private.mkdir(parents=True, exist_ok=True)
        for s in subdirs:
            subdir_path = target_private / s
            if not subdir_path.exists():
                subdir_path.mkdir(parents=True, exist_ok=True)
                report["created_dirs"].append(str(subdir_path))
    else:
        for s in subdirs:
            subdir_path = target_private / s
            if not subdir_path.exists():
                report["created_dirs"].append(str(subdir_path))

    # 1. .private-staging.json
    staging_marker = target_private / ".private-staging.json"
    if staging_marker.exists():
        report["preserved_files"].append(str(staging_marker))
    else:
        content = {
            "schema": "private-staging-v1",
            "live_writes": False,
            "external_exports": False,
        }
        if not dry_run:
            staging_marker.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
        report["created_files"].append(str(staging_marker))

    # 2. config.json (inert local settings template)
    config_file = target_private / "config.json"
    if config_file.exists():
        report["preserved_files"].append(str(config_file))
    else:
        config_content = {
            "canonical_vault": str(vault_path) if vault_path else "OWNER_CONFIG_ONLY_OUTSIDE_THIS_REPOSITORY",
            "private_workspace": str(target_private),
            "live_publication": False,
            "external_processing": False,
            "automatic_capture": False,
            "graphify": {"enabled": False, "scope": "synthetic-or-authorized-code-only"},
            "agent_input": "explicitly selected context packet only",
        }
        if not dry_run:
            config_file.write_text(json.dumps(config_content, indent=2) + "\n", encoding="utf-8")
        report["created_files"].append(str(config_file))

    # 3. brain-publication-policy.json (inert disabled publication policy)
    policy_file = target_private / "brain-publication-policy.json"
    if policy_file.exists():
        report["preserved_files"].append(str(policy_file))
    else:
        repo_root = get_repo_root()
        example_policy = repo_root / "examples" / "brain-publication-policy.example.json"
        if example_policy.is_file():
            policy_data = json.loads(example_policy.read_text(encoding="utf-8"))
        else:
            policy_data = {
                "schema": "brain-publication-policy-v1",
                "policy_id": "inert-example",
                "enabled": False,
                "vault_root": str(vault_path) if vault_path else "<absolute-owner-approved-vault-root>",
                "owner_authorization": {"state": "pending", "reference": None},
                "write_scope": {
                    "create_prefixes": ["logs/brain-save/", "projects/", "permanent/engineering/"],
                    "updates": [],
                    "bootstrap": [],
                },
                "local_read_scope": {"prefixes": [], "notes": []},
                "model_output": {"paths": False, "titles": False},
                "limits": {"max_managed_notes": 256, "max_note_bytes": 128000},
            }
        if vault_path:
            policy_data["vault_root"] = str(vault_path)
        policy_data["enabled"] = False
        if not dry_run:
            policy_file.write_text(json.dumps(policy_data, indent=2) + "\n", encoding="utf-8")
        report["created_files"].append(str(policy_file))

    return report


def init_vault_templates(
    vault_path: str | Path,
    templates_dir: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Bootstrap default templates into the vault if missing, never overwriting existing files."""
    vault = Path(vault_path).expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"Vault directory does not exist or is not a directory: {vault}")

    check_redirected_path(vault)

    repo_root = get_repo_root()
    src_dir = Path(templates_dir).resolve() if templates_dir else repo_root / "templates" / "vault"
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Vault templates directory not found: {src_dir}")

    report = {
        "vault_path": str(vault),
        "created_files": [],
        "preserved_files": [],
        "dry_run": dry_run,
    }

    for rel_path in VAULT_BOOTSTRAP_TEMPLATES:
        src_file = src_dir / rel_path
        dst_file = vault / rel_path
        if not src_file.is_file():
            continue

        if dst_file.exists():
            report["preserved_files"].append(str(dst_file))
        else:
            report["created_files"].append(str(dst_file))
            if not dry_run:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)

    return report


def migrate_private_bundle(
    bundle_path: str | Path,
    private_dest: str | Path,
    dry_run: bool = False,
) -> dict:
    """Safely restore/migrate private state from a migration bundle (.zip or directory).

    Adheres strictly to no-clobber and path-traversal safety.
    """
    bundle = Path(bundle_path).expanduser().resolve()
    dest = Path(private_dest).expanduser().resolve()
    validate_private_path_safety(dest)

    if not bundle.exists():
        raise FileNotFoundError(f"Migration bundle not found: {bundle}")

    report = {
        "bundle": str(bundle),
        "destination": str(dest),
        "restored_files": [],
        "preserved_files": [],
        "errors": [],
        "dry_run": dry_run,
    }

    device_pattern = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]|CONIN\$|CONOUT\$|CLOCK\$)(\..*)?$', re.I)
    dest_resolved = dest.resolve()

    if bundle.is_file() and bundle.suffix.lower() == ".zip":
        with zipfile.ZipFile(bundle, "r") as zf:
            for item in zf.infolist():
                name = item.filename
                # Path traversal, drive, colon, and device name checks
                if (
                    ".." in name
                    or name.startswith("/")
                    or name.startswith("\\")
                    or any(c in name for c in ':*?"<>|\x00')
                ):
                    report["errors"].append(f"Skipped unsafe path: {name}")
                    continue
                parts = [p for p in name.replace("\\", "/").split("/") if p]
                if any(device_pattern.fullmatch(part.strip()) for part in parts):
                    report["errors"].append(f"Skipped reserved device path: {name}")
                    continue

                out_path = (dest / name).resolve()
                if not out_path.is_relative_to(dest_resolved):
                    report["errors"].append(f"Skipped path escaping destination: {name}")
                    continue

                if item.is_dir():
                    if not dry_run:
                        out_path.mkdir(parents=True, exist_ok=True)
                    continue
                if out_path.exists():
                    report["preserved_files"].append(str(out_path))
                else:
                    report["restored_files"].append(str(out_path))
                    if not dry_run:
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(item) as src, open(out_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)
    elif bundle.is_dir():
        for root, dirs, files in os.walk(bundle, followlinks=False):
            # Exclude symlinks and reparse points/junctions
            for d in list(dirs):
                dp = Path(root) / d
                if dp.is_symlink() or (getattr(dp.lstat(), 'st_reparse_tag', 0) & 0x20000000):
                    dirs.remove(d)
            rel_root = Path(root).relative_to(bundle)
            for f in files:
                src_file = Path(root) / f
                if src_file.is_symlink() or (getattr(src_file.lstat(), 'st_reparse_tag', 0) & 0x20000000):
                    report["errors"].append(f"Skipped symlink/junction: {src_file}")
                    continue
                if device_pattern.fullmatch(f.strip()):
                    report["errors"].append(f"Skipped reserved device file: {src_file}")
                    continue
                out_path = (dest / rel_root / f).resolve()
                if not out_path.is_relative_to(dest_resolved):
                    report["errors"].append(f"Skipped path escaping destination: {src_file}")
                    continue
                if out_path.exists():
                    report["preserved_files"].append(str(out_path))
                else:
                    report["restored_files"].append(str(out_path))
                    if not dry_run:
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, out_path)
    else:
        raise ValueError(f"Unsupported migration bundle format: {bundle}")

    return report


def run_diagnostics(
    vault_path: str | Path | None = None,
    private_path: str | Path | None = None,
) -> dict:
    """Run comprehensive self-diagnostics on the runtime, repository, vault, and private workspace."""
    repo_root = get_repo_root()

    py_ok, py_ver = check_python_version()
    deps = check_dependencies()
    vault_info = discover_onedrive_vault(vault_path)

    # Check private workspace status
    priv_info = {
        "path": None,
        "exists": False,
        "valid_staging_marker": False,
        "outside_git": True,
        "outside_onedrive": True,
        "redirect_free": True,
        "error": None,
    }

    if private_path is None:
        env_priv = os.environ.get("SECOND_BRAIN_PRIVATE")
        priv_target = Path(env_priv).resolve() if env_priv else (Path.home() / DEFAULT_PRIVATE_DIRNAME).resolve()
    else:
        priv_target = Path(private_path).resolve()

    priv_info["path"] = str(priv_target)
    priv_info["exists"] = priv_target.is_dir()
    try:
        validate_private_path_safety(priv_target)
    except ValueError as exc:
        priv_info["error"] = str(exc)
        if "git" in str(exc).lower():
            priv_info["outside_git"] = False
        if "onedrive" in str(exc).lower():
            priv_info["outside_onedrive"] = False
        if "redirect" in str(exc).lower():
            priv_info["redirect_free"] = False

    marker = priv_target / ".private-staging.json"
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if data.get("live_writes") is False and data.get("external_exports") is False:
                priv_info["valid_staging_marker"] = True
        except Exception:
            priv_info["valid_staging_marker"] = False

    # Check importability of core toolkit scripts
    core_modules = {}
    sys_path_added = False
    scripts_dir = str(repo_root / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
        sys_path_added = True

    for mod in ("selected_memory", "publish_memory", "managed_memory", "bootstrap_vault", "wiki_memory", "wiki_health"):
        try:
            importlib.import_module(mod)
            core_modules[mod] = True
        except Exception as exc:
            core_modules[mod] = str(exc)

    if sys_path_added:
        sys.path.remove(scripts_dir)

    # Repository structure checks
    repo_checks = {
        "root": str(repo_root),
        "templates_exist": (repo_root / "templates" / "vault").is_dir(),
        "skills_exist": (repo_root / "skills").is_dir(),
        "lockfile_exists": (repo_root / "requirements-mcp.lock").is_file(),
    }

    overall_healthy = (
        py_ok
        and all(v is True for v in core_modules.values())
        and repo_checks["templates_exist"]
        and repo_checks["lockfile_exists"]
        and priv_info["outside_git"]
        and priv_info["outside_onedrive"]
        and priv_info["redirect_free"]
    )

    return {
        "python": {
            "version": py_ver,
            "min_required": ".".join(str(x) for x in MIN_PYTHON_VERSION),
            "ok": py_ok,
        },
        "dependencies": deps,
        "vault": vault_info,
        "private_workspace": priv_info,
        "core_modules": core_modules,
        "repository": repo_checks,
        "overall_healthy": overall_healthy,
    }


def format_text_report(diag: dict) -> str:
    """Format diagnostics into a clean, human-readable terminal report."""
    lines = [
        "============================================================",
        "          AI Second Brain Toolkit - Health Report",
        "============================================================",
        f"Python Version:       {diag['python']['version']} (>= {diag['python']['min_required']}) -> {'[PASS]' if diag['python']['ok'] else '[FAIL]'}",
        f"Repository Root:      {diag['repository']['root']}",
        f"Templates Available:  {'[PASS]' if diag['repository']['templates_exist'] else '[FAIL]'}",
        f"MCP Lockfile:         {'[PASS]' if diag['repository']['lockfile_exists'] else '[FAIL]'}",
        "------------------------------------------------------------",
        "Dependencies Status:",
        f"  All Present:        {'[PASS]' if diag['dependencies']['all_present'] else '[ACTION REQUIRED]'}",
        f"  Installed:          {', '.join(diag['dependencies']['installed']) if diag['dependencies']['installed'] else 'None'}",
    ]
    if diag["dependencies"]["missing"]:
        lines.append(f"  Missing:            {', '.join(diag['dependencies']['missing'])}")
        lines.append(f"  Install Guide:      {diag['dependencies']['guide_command']}")

    lines.extend([
        "------------------------------------------------------------",
        "Obsidian Vault Status:",
        f"  Path:               {diag['vault']['path'] or 'Not specified / not found'}",
        f"  Exists on Disk:     {'Yes' if diag['vault']['exists'] else 'No'}",
        f"  In OneDrive:        {'Yes' if diag['vault']['in_onedrive'] else 'No'}",
        f"  Obsidian Marker:    {'Yes (.obsidian found)' if diag['vault']['has_obsidian_marker'] else 'No'}",
        f"  Redirects Check:    {'[PASS]' if diag['vault']['redirect_free'] else '[FAIL]'}",
    ])
    if diag["vault"].get("error"):
        lines.append(f"  Vault Error:        {diag['vault']['error']}")

    lines.extend([
        "------------------------------------------------------------",
        "Private Workspace Status:",
        f"  Path:               {diag['private_workspace']['path']}",
        f"  Exists:             {'Yes' if diag['private_workspace']['exists'] else 'No'}",
        f"  Outside Git:        {'[PASS]' if diag['private_workspace']['outside_git'] else '[FAIL]'}",
        f"  Outside OneDrive:   {'[PASS]' if diag['private_workspace']['outside_onedrive'] else '[FAIL]'}",
        f"  Staging Marker:     {'[PASS]' if diag['private_workspace']['valid_staging_marker'] else 'Absent/Uninitialized'}",
    ])
    if diag["private_workspace"].get("error"):
        lines.append(f"  Private Error:      {diag['private_workspace']['error']}")

    lines.extend([
        "------------------------------------------------------------",
        "Core Module Imports:",
    ])
    for mod, status in diag["core_modules"].items():
        st_text = "[PASS]" if status is True else f"[FAIL: {status}]"
        lines.append(f"  {mod:<20} {st_text}")

    lines.extend([
        "============================================================",
        f"Overall Health:       {'[HEALTHY]' if diag['overall_healthy'] else '[NEEDS ATTENTION]'}",
        "============================================================",
    ])
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI Second Brain Toolkit Installer and Diagnostics",
    )
    parser.add_argument(
        "--vault",
        "--vault-path",
        dest="vault_path",
        default=None,
        help="Explicit path to the Obsidian vault root directory",
    )
    parser.add_argument(
        "--private-dir",
        dest="private_dir",
        default=None,
        help="Path for local private workspace (must be outside Git and OneDrive)",
    )
    parser.add_argument(
        "--install-deps",
        action="store_true",
        help="Attempt automatic pip installation of missing dependencies from lockfile",
    )
    parser.add_argument(
        "--init-vault-templates",
        action="store_true",
        help="Copy default bootstrap templates to vault (strictly no-clobber)",
    )
    parser.add_argument(
        "--migrate-bundle",
        dest="migrate_bundle",
        default=None,
        help="Restore private state from a migration bundle (.zip or directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate installation and checks without writing files to disk",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Perform diagnostics and exit without making changes",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output diagnostics and actions in JSON format",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress human-readable progress messages",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 1. Python version check
    py_ok, py_ver = check_python_version()
    if not py_ok:
        err_msg = f"Python {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}+ required. Current: {py_ver}"
        if args.json:
            print(json.dumps({"error": "PYTHON_VERSION_INCOMPATIBLE", "message": err_msg}))
        else:
            print(f"Error: {err_msg}", file=sys.stderr)
        return 1

    actions_taken: dict = {
        "dependencies_installed": False,
        "private_workspace": None,
        "vault_templates": None,
        "migration_bundle": None,
    }

    # 2. Resolve vault path (explicit or discovered)
    effective_vault = args.vault_path
    if not effective_vault:
        disc = discover_onedrive_vault()
        if disc.get("exists") and disc.get("path"):
            effective_vault = disc["path"]

    # 3. Dependency handling
    if args.install_deps and not (args.dry_run or args.check_only):
        deps = check_dependencies()
        if not deps["all_present"]:
            if not args.quiet:
                print("Installing missing dependencies from requirements-mcp.lock...")
            try:
                success = install_dependencies()
                actions_taken["dependencies_installed"] = success
            except Exception as e:
                actions_taken["dependencies_error"] = str(e)

    # 4. Private workspace initialization
    if not args.check_only:
        try:
            priv_res = init_private_workspace(
                private_path=args.private_dir,
                vault_path=effective_vault,
                dry_run=args.dry_run,
            )
            actions_taken["private_workspace"] = priv_res
        except Exception as e:
            actions_taken["private_workspace_error"] = str(e)

    # 5. Vault template bootstrap
    if args.init_vault_templates and not args.check_only:
        if effective_vault:
            try:
                vt_res = init_vault_templates(
                    vault_path=effective_vault,
                    dry_run=args.dry_run,
                )
                actions_taken["vault_templates"] = vt_res
            except Exception as e:
                actions_taken["vault_templates_error"] = str(e)

    # 6. Migration bundle restoration
    if args.migrate_bundle and not args.check_only:
        dest = args.private_dir or (Path.home() / DEFAULT_PRIVATE_DIRNAME)
        try:
            mb_res = migrate_private_bundle(
                bundle_path=args.migrate_bundle,
                private_dest=dest,
                dry_run=args.dry_run,
            )
            actions_taken["migration_bundle"] = mb_res
        except Exception as e:
            actions_taken["migration_bundle_error"] = str(e)

    # 7. Run diagnostics
    diag = run_diagnostics(vault_path=effective_vault, private_path=args.private_dir)
    diag["actions_taken"] = actions_taken

    if args.json:
        print(json.dumps(diag, indent=2))
    elif not args.quiet:
        print(format_text_report(diag))

    return 0 if diag["overall_healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
