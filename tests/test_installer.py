"""Unit tests for single-command installer and packaging logic (T17)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_toolkit as installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.private = self.root / "private_workspace"
        self.private.mkdir()

    def test_python_version_check(self):
        """Test Python version enforcement (>= 3.11)."""
        ok, ver = installer.check_python_version((3, 11, 0))
        self.assertTrue(ok)
        self.assertEqual(ver, "3.11.0")

        ok, ver = installer.check_python_version((3, 13, 2))
        self.assertTrue(ok)
        self.assertEqual(ver, "3.13.2")

        ok, ver = installer.check_python_version((3, 10, 12))
        self.assertFalse(ok)
        self.assertEqual(ver, "3.10.12")

        ok, ver = installer.check_python_version((2, 7, 18))
        self.assertFalse(ok)
        self.assertEqual(ver, "2.7.18")

    def test_redirected_path_detection(self):
        """Ensure check_redirected_path rejects symlinks or reparse points."""
        test_file = self.root / "file.txt"
        test_file.write_text("hello")
        installer.check_redirected_path(test_file)

        # Mock is_symlink
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                installer.check_redirected_path(test_file)
            self.assertIn("Redirected path refused", str(ctx.exception))

        # Mock reparse tag with name surrogate bit 0x20000000
        mock_stat = MagicMock()
        mock_stat.st_mode = 0o100644
        mock_stat.st_reparse_tag = 0xA0000003  # Directory junction
        with patch.object(Path, "lstat", return_value=mock_stat), \
             patch.object(Path, "is_symlink", return_value=False), \
             patch.object(Path, "is_junction", return_value=False):
            with self.assertRaises(ValueError) as ctx:
                installer.check_redirected_path(test_file)
            self.assertIn("Redirected path refused", str(ctx.exception))

    def test_is_onedrive_path(self):
        """Test detection of OneDrive paths."""
        od_path = Path("C:/Users/test/OneDrive/Notes")
        with patch.dict(os.environ, {"ONEDRIVE": "C:\\Users\\test\\OneDrive"}):
            self.assertTrue(installer.is_onedrive_path(od_path))

        local_path = Path("C:/local/private_staging")
        with patch.dict(os.environ, {"ONEDRIVE": "C:\\Users\\test\\OneDrive"}):
            self.assertFalse(installer.is_onedrive_path(local_path))

    def test_discover_onedrive_vault_explicit(self):
        """Test explicit vault path validation."""
        (self.vault / ".obsidian").mkdir()
        res = installer.discover_onedrive_vault(self.vault)
        self.assertEqual(res["path"], str(self.vault))
        self.assertTrue(res["exists"])
        self.assertTrue(res["is_dir"])
        self.assertTrue(res["has_obsidian_marker"])
        self.assertTrue(res["redirect_free"])

    def test_discover_onedrive_vault_env(self):
        """Test discovery via environment variable."""
        with patch.dict(os.environ, {"SECOND_BRAIN_VAULT": str(self.vault)}):
            res = installer.discover_onedrive_vault()
            self.assertEqual(res["path"], str(self.vault))
            self.assertTrue(res["exists"])
            self.assertTrue(res["discovered"])

    def test_discover_onedrive_vault_candidate_subdir(self):
        """Test discovery in OneDrive candidate subdirectories."""
        od_dir = self.root / "FakeOneDrive"
        od_dir.mkdir()
        vault_candidate = od_dir / "AI-Second-Brain"
        vault_candidate.mkdir()
        (vault_candidate / ".obsidian").mkdir()

        with patch.dict(os.environ, {"ONEDRIVE": str(od_dir)}), patch('sys.platform', 'win32'):
            res = installer.discover_onedrive_vault()
            self.assertEqual(res["path"], str(vault_candidate))
            self.assertTrue(res["exists"])
            self.assertTrue(res["in_onedrive"])
            self.assertTrue(res["has_obsidian_marker"])

    def test_check_dependencies(self):
        """Test dependency inspection and guide command."""
        deps = installer.check_dependencies()
        self.assertIn("all_present", deps)
        self.assertIn("installed", deps)
        self.assertIn("missing", deps)
        self.assertIn("guide_command", deps)

    def test_private_workspace_safety_refuses_git_and_onedrive(self):
        """Private workspace must strictly refuse being in .git or OneDrive."""
        # Inside git repo
        git_dir = self.root / "fake_repo"
        git_dir.mkdir()
        (git_dir / ".git").mkdir()
        nested_priv = git_dir / "private"
        nested_priv.mkdir()

        with self.assertRaises(ValueError) as ctx:
            installer.validate_private_path_safety(nested_priv)
        self.assertIn("git repository", str(ctx.exception))

        # Inside OneDrive
        od_dir = self.root / "OneDriveFake"
        od_dir.mkdir()
        with patch.dict(os.environ, {"ONEDRIVE": str(od_dir)}):
            with self.assertRaises(ValueError) as ctx:
                installer.validate_private_path_safety(od_dir / "private")
            self.assertIn("OneDrive", str(ctx.exception))

    def test_init_private_workspace_creates_structures_and_no_clobber(self):
        """Test initialization of private directory structures and strict no-clobber."""
        res = installer.init_private_workspace(
            private_path=self.private,
            vault_path=self.vault,
            dry_run=False,
        )
        self.assertEqual(res["private_root"], str(self.private))
        self.assertIn(str(self.private / ".private-staging.json"), res["created_files"])
        self.assertIn(str(self.private / "config.json"), res["created_files"])
        self.assertIn(str(self.private / "brain-publication-policy.json"), res["created_files"])

        # Check that .private-staging.json contains valid contents
        staging_data = json.loads((self.private / ".private-staging.json").read_text(encoding="utf-8"))
        self.assertEqual(staging_data.get("schema"), "private-staging-v1")
        self.assertFalse(staging_data.get("live_writes"))
        self.assertFalse(staging_data.get("external_exports"))

        # Check required subdirectories
        for sub in ("bootstrap-intents", "publications", "archive", "proposals", "receipts", "logs"):
            self.assertTrue((self.private / sub).is_dir())

        # Test no-clobber: modify config.json and re-run init
        custom_config = {"canonical_vault": "CUSTOM_VAULT_MODIFIED", "custom_key": 123}
        (self.private / "config.json").write_text(json.dumps(custom_config), encoding="utf-8")

        res2 = installer.init_private_workspace(
            private_path=self.private,
            vault_path=self.vault,
            dry_run=False,
        )
        self.assertIn(str(self.private / "config.json"), res2["preserved_files"])
        preserved_data = json.loads((self.private / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(preserved_data["canonical_vault"], "CUSTOM_VAULT_MODIFIED")
        self.assertEqual(preserved_data["custom_key"], 123)

    def test_init_private_workspace_dry_run(self):
        """Dry-run must not create files or directories on disk."""
        dry_dir = self.root / "dry_private"
        res = installer.init_private_workspace(
            private_path=dry_dir,
            vault_path=self.vault,
            dry_run=True,
        )
        self.assertTrue(res["dry_run"])
        self.assertFalse(dry_dir.exists())

    def test_init_vault_templates(self):
        """Test copying templates to vault and preserving existing files."""
        # Create a mock templates dir
        tpl_dir = self.root / "templates" / "vault"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "AGENTS.md").write_text("Agents guide", encoding="utf-8")
        (tpl_dir / "index.md").write_text("Index guide", encoding="utf-8")
        (tpl_dir / "system").mkdir()
        (tpl_dir / "system" / "brain.md").write_text("Brain guide", encoding="utf-8")

        # Existing file in vault should be preserved
        (self.vault / "index.md").write_text("User existing index", encoding="utf-8")

        res = installer.init_vault_templates(
            vault_path=self.vault,
            templates_dir=tpl_dir,
            dry_run=False,
        )
        self.assertIn(str(self.vault / "AGENTS.md"), res["created_files"])
        self.assertIn(str(self.vault / "index.md"), res["preserved_files"])
        self.assertEqual((self.vault / "index.md").read_text(encoding="utf-8"), "User existing index")
        self.assertEqual((self.vault / "AGENTS.md").read_text(encoding="utf-8"), "Agents guide")
        self.assertEqual((self.vault / "system" / "brain.md").read_text(encoding="utf-8"), "Brain guide")

    def test_migrate_private_bundle_zip(self):
        """Test restoring private state from a zip bundle."""
        bundle_zip = self.root / "bundle.zip"
        with zipfile.ZipFile(bundle_zip, "w") as zf:
            zf.writestr("receipts/receipt-1.json", json.dumps({"status": "OK"}))
            zf.writestr("archive/doc-1.txt", "archived content")
            zf.writestr("../unsafe.txt", "traversal attempt")

        dest = self.root / "migrated_private"
        dest.mkdir()

        res = installer.migrate_private_bundle(bundle_zip, dest, dry_run=False)
        self.assertIn(str(dest / "receipts" / "receipt-1.json"), res["restored_files"])
        self.assertIn(str(dest / "archive" / "doc-1.txt"), res["restored_files"])
        self.assertTrue(any("unsafe" in err for err in res["errors"]))
        self.assertFalse((self.root / "unsafe.txt").exists())

        # No-clobber check on second run
        res2 = installer.migrate_private_bundle(bundle_zip, dest, dry_run=False)
        self.assertIn(str(dest / "receipts" / "receipt-1.json"), res2["preserved_files"])

    def test_run_diagnostics(self):
        """Test diagnostic structure and health evaluation."""
        diag = installer.run_diagnostics(vault_path=self.vault, private_path=self.private)
        self.assertIn("python", diag)
        self.assertIn("dependencies", diag)
        self.assertIn("vault", diag)
        self.assertIn("private_workspace", diag)
        self.assertIn("core_modules", diag)
        self.assertIn("repository", diag)
        self.assertIn("overall_healthy", diag)

    def test_main_cli_dry_run_json(self):
        """Test CLI main execution in dry-run mode with JSON output."""
        stdout_mock = []
        with patch("sys.stdout.write", side_effect=lambda s: stdout_mock.append(s)):
            code = installer.main(["--dry-run", "--json", "--vault", str(self.vault), "--private-dir", str(self.private)])
        full_out = "".join(stdout_mock)
        data = json.loads(full_out)
        self.assertIn("overall_healthy", data)
        self.assertIn("actions_taken", data)

    def test_main_cli_check_only(self):
        """Test CLI check-only invocation."""
        stdout_mock = []
        with patch("sys.stdout.write", side_effect=lambda s: stdout_mock.append(s)):
            code = installer.main(["--check-only", "--vault", str(self.vault), "--private-dir", str(self.private)])
        full_out = "".join(stdout_mock)
        self.assertIn("AI Second Brain Toolkit - Health Report", full_out)

    def test_main_python_version_failure(self):
        """Test CLI exit code when Python version is incompatible."""
        with patch("install_toolkit.check_python_version", return_value=(False, "3.8.0")):
            code = installer.main(["--check-only"])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
