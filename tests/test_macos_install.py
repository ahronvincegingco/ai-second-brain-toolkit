"""Synthetic cross-platform installer regressions; never inspect a real vault."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install_toolkit as installer


class PortableInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.vault = self.base / 'test-vault with spaces'
        self.vault.mkdir()
        (self.vault / '.obsidian').mkdir()
        self.private = self.base / 'private with spaces'
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def link(self, path, target):
        try:
            path.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as exc:
            self.skipTest(f'Native symlinks unavailable: {exc}')

    def test_supported_python_matrix(self):
        for minor in (11, 12, 13):
            self.assertTrue(installer.check_python_version((3, minor, 0))[0])
        for version in ((3, 10, 0), (3, 14, 0), (4, 0, 0)):
            self.assertFalse(installer.check_python_version(version)[0])

    def test_macos_without_configuration_never_checks_home(self):
        with patch('sys.platform', 'darwin'), patch.object(Path, 'home', side_effect=AssertionError('home scan')):
            result = installer.discover_onedrive_vault()
        self.assertIsNone(result['path'])

    def test_explicit_vault_outside_onedrive_and_invalid_env(self):
        self.assertTrue(installer.discover_onedrive_vault(self.vault)['has_obsidian_marker'])
        with patch.dict(os.environ, {'SECOND_BRAIN_VAULT': str(self.base / 'missing')}):
            result = installer.discover_onedrive_vault()
        self.assertFalse(result['exists'])
        self.assertEqual(result['path'], str(self.base / 'missing'))
        with patch.dict(os.environ, {'SECOND_BRAIN_VAULT': str(self.base / 'missing')}):
            self.assertFalse(installer.run_diagnostics(private_path=self.private)['overall_healthy'])

    def test_vault_and_private_redirects_refused_before_resolution(self):
        linked = self.base / 'linked'
        self.link(linked, self.vault)
        self.assertFalse(installer.discover_onedrive_vault(linked)['redirect_free'])
        with self.assertRaises(ValueError):
            installer.init_private_workspace(linked / 'private')
        with self.assertRaises(ValueError):
            installer.init_vault_templates(linked)

    def test_nested_template_redirect_refused(self):
        other = self.base / 'other'
        other.mkdir()
        self.link(self.vault / 'system', other)
        with self.assertRaises(ValueError):
            installer.init_vault_templates(self.vault)
        self.assertEqual(list(other.iterdir()), [])

    def test_private_child_redirect_refused(self):
        self.private.mkdir()
        self.link(self.private / 'logs', self.vault)
        with self.assertRaises(ValueError):
            installer.init_private_workspace(self.private)

    def test_bundle_destination_redirect_refused(self):
        import zipfile
        bundle = self.base / 'bundle.zip'
        with zipfile.ZipFile(bundle, 'w') as archive:
            archive.writestr('logs/receipt.json', '{}')
        self.private.mkdir()
        self.link(self.private / 'logs', self.vault)
        with self.assertRaises(ValueError):
            installer.migrate_private_bundle(bundle, self.private)
        self.assertFalse((self.vault / 'receipt.json').exists())

    def test_atomic_template_collision_never_overwrites(self):
        target = self.base / 'config.json'

        def competing_writer(path, data):
            path.write_bytes(b'other writer')
            raise FileExistsError(path)

        with patch('publish_memory.write_immutable', side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                installer.create_template(target, b'ours')
        self.assertEqual(target.read_bytes(), b'other writer')

    def test_disabled_private_setup_and_permissions(self):
        installer.init_private_workspace(self.private, self.vault)
        config = json.loads((self.private / 'config.json').read_text())
        self.assertFalse(config['live_publication'])
        self.assertFalse(config['external_processing'])
        self.assertFalse(config['automatic_capture'])
        if os.name == 'posix':
            self.assertEqual(self.private.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.private / 'config.json').stat().st_mode & 0o777, 0o600)

    def test_install_failure_returns_nonzero_json(self):
        with patch('install_toolkit.install_dependencies', return_value=False), \
             patch('install_toolkit.check_dependencies', return_value={'all_present': False}), \
             patch('install_toolkit.run_diagnostics', return_value={'overall_healthy': True, 'dependencies': {'all_present': False}}), \
             patch('builtins.print'):
            self.assertEqual(installer.main(['--install-deps', '--json', '--private-dir', str(self.private)]), 1)

    def test_missing_vault_diagnostics_fail(self):
        diag = installer.run_diagnostics(self.base / 'missing', self.private)
        self.assertFalse(diag['overall_healthy'])

    def test_dependency_install_retains_integrity_flags(self):
        with patch('install_toolkit.subprocess.run') as run:
            run.return_value.returncode = 0
            self.assertTrue(installer.install_dependencies())
        command = run.call_args.args[0]
        self.assertIn('--require-hashes', command)
        self.assertIn('--only-binary=:all:', command)

    def test_locks_keep_windows_marker_and_hash_every_pin(self):
        root = Path(__file__).resolve().parents[1]
        for file in (root / 'requirements-mcp.lock', root / 'docs/requirements-graphify.lock'):
            logical = file.read_text().replace('\\\n', '')
            pins = [line for line in logical.splitlines() if line and not line.startswith('#')]
            for pin in pins:
                self.assertRegex(pin, r'^[\w.-]+==[^ ]+')
                self.assertRegex(pin, r'--hash=sha256:[a-f0-9]{64}')
            win = next(pin for pin in pins if pin.startswith('pywin32=='))
            self.assertIn('; sys_platform == "win32"', win)

    @unittest.skipIf(os.name == 'nt', 'POSIX entry point')
    def test_shell_entrypoint_forwards_spaced_paths_and_dry_run(self):
        shell = Path(__file__).resolve().parents[1] / 'scripts/install.sh'
        result = subprocess.run(['sh', str(shell), '--dry-run', '--json', '--vault', str(self.vault),
                                 '--private-dir', str(self.private)], cwd=self.base,
                                env={**os.environ, 'SECOND_BRAIN_PYTHON': sys.executable},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['actions_taken']['private_workspace']['dry_run'])
        self.assertFalse(self.private.exists())


if __name__ == '__main__':
    unittest.main()
