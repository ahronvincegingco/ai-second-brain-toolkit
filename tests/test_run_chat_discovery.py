"""Disposable local socket and launcher tests; never run cloudflared."""
import socket
import subprocess
import sys
import threading
import time
import json
import hashlib
import tempfile
import io
import contextlib
import os
import _thread
import types
import queue
import re
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from run_chat_discovery import open_exclusive_listener
from chat_recall_discovery import create_app
import run_chat_discovery as launcher


class LegacyOutputGuard:
    """Frozen pre-classification parser for same-input decision comparisons."""
    _url = re.compile(rb'https://[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.trycloudflare\.com')
    def __init__(self):
        self.origin, self.failed, self.line, self.total, self.started = None, False, bytearray(), 0, False
    def feed(self, data):
        for byte in data:
            if not self.started:
                self.total += 1
                if self.total > 65536: self.failed = True
            if byte == 10:
                line = bytes(self.line)
                self.line.clear()
                for match in re.finditer(rb'[a-z][a-z0-9+.-]*://[^\s|"\']+', line, re.IGNORECASE):
                    candidate = match.group()
                    authority = re.split(rb'[/#?]', candidate.split(b'://', 1)[1], maxsplit=1)[0]
                    if b'trycloudflare' in authority.lower() and not self._url.fullmatch(candidate): self.failed = True
                    if not self._url.fullmatch(candidate): continue
                    before = line[match.start()-1:match.start()] if match.start() else b' '
                    after = line[match.end():match.end()+1] or b' '
                    if before not in b' \t|"\'' or after not in b' \t|"\'\r':
                        self.failed = True
                        continue
                    found = candidate.decode('ascii')
                    if self.origin is not None and self.origin != found: self.failed = True
                    self.origin = found
            elif len(self.line) < 4096:
                self.line.append(byte)
            else:
                self.failed = True


class ProfileTests(unittest.TestCase):
    def test_guard_failure_categories_and_unchanged_decisions(self):
        cases = [
            (b'https://fake.trycloudflare.com\n', False, 0, True),
            (b'https://developers.cloudflare.com/info\n', False, 0, False),
            (b'https://fake.trycloudflare.com', False, 0, False),
            (b'x\n' * 32769, True, 1, False),
            (b'x' * 4097 + b'\n', True, 2, False),
            (b'https://fake.trycloudflare.com:443\n', True, 4, False),
            (b'=https://fake.trycloudflare.com\n', True, 8, False),
            (b'https://first.trycloudflare.com\nhttps://second.trycloudflare.com\n', True, 16, True),
            (b'=https://fake.trycloudflare.com\nhttps://fake.trycloudflare.com:443\n' + b'x' * 4097, True, 14, False),
            (b'https://first.trycloudflare.com\nhttps://second.trycloudflare.com\n'
             b'=https://fake.trycloudflare.com\nhttps://fake.trycloudflare.com:443\n' + b'x' * 4097 + b'\n' + b'x\n' * 32769, True, 31, True),
        ]
        for data, failed, mask, recognized in cases:
            with self.subTest(mask=mask, bytes=len(data)):
                whole, split, legacy = launcher.OutputGuard(), launcher.OutputGuard(), LegacyOutputGuard()
                whole.feed(data)
                legacy.feed(data)
                for byte in data:
                    split.feed(bytes([byte]))
                for guard in (whole, split):
                    self.assertEqual(guard.failed, failed)
                    self.assertEqual(guard.failure_mask, mask)
                    self.assertEqual(guard.origin is not None, recognized)
                self.assertEqual((whole.failed, whole.origin, whole.total, bytes(whole.line)),
                                 (split.failed, split.origin, split.total, bytes(split.line)))
                self.assertEqual((whole.failed, whole.origin, whole.total, bytes(whole.line)),
                                 (legacy.failed, legacy.origin, legacy.total, bytes(legacy.line)))

    def test_guard_total_limit_does_not_count_after_start(self):
        guard, legacy = launcher.OutputGuard(), LegacyOutputGuard()
        guard.started = legacy.started = True
        data = b'x\n' * 32769
        guard.feed(data)
        legacy.feed(data)
        self.assertEqual((guard.failed, guard.origin, guard.total), (legacy.failed, legacy.origin, legacy.total))
        self.assertEqual(guard.failure_mask, 0)
    @unittest.skipUnless(sys.platform == 'win32', 'Windows socket required')
    def test_late_server_start_without_ready_is_error(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            server_started = threading.Event()
            class Child:
                pid = 3212
                alive = True
                stdout = io.BytesIO(b'https://fake.trycloudflare.com\n')
                def poll(self): return None if self.alive else 0
                def terminate(self): self.alive = False
                def wait(self, timeout): return 0
                def kill(self): self.alive = False
            class Server:
                started = False
                should_exit = False
                def __init__(self, config): pass
                def run(self, sockets):
                    self.started = True
                    server_started.set()
                    while not self.should_exit: time.sleep(.01)
            def thread_factory(*args, **kwargs):
                thread = threading.Thread(*args, **kwargs)
                if kwargs.get('target').__name__ == 'serve':
                    start = thread.start
                    def start_after_server():
                        start()
                        if not server_started.wait(3): raise RuntimeError('server did not start')
                    thread.start = start_after_server
                return thread
            synthetic_threads = types.SimpleNamespace(Thread=thread_factory, Lock=threading.Lock)
            synthetic_clock = types.SimpleNamespace(monotonic=lambda: 2 if server_started.is_set() else 0,
                                                    sleep=time.sleep)
            child, output = Child(), io.StringIO()
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123), patch.object(launcher.subprocess, 'Popen', return_value=child), patch.object(launcher, 'threading', synthetic_threads), patch.object(launcher, 'time', synthetic_clock), patch('uvicorn.Server', Server), contextlib.redirect_stdout(output):
                self.assertEqual(launcher.main([str(manifest), digest]), 1)
            self.assertTrue(server_started.is_set())
            self.assertEqual(output.getvalue(), '')
            self.assertFalse(child.alive)
            receipt = json.loads(Path(profile['receipt_path']).read_text())
            self.assertEqual(receipt['status'], 'error')
            self.assertEqual(receipt['cleanup'], 'complete')
    @unittest.skipUnless(sys.platform == 'win32', 'Windows CLI and owned process required')
    def test_real_cli_entrypoint_with_synthetic_process_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            profile['lifetime_seconds'] = 3
            raw = json.dumps(profile).encode()
            manifest.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            adapter = Path(directory) / 'adapter'
            adapter.mkdir()
            (adapter / 'sitecustomize.py').write_text(
                'import subprocess, sys, uvicorn\n'
                '_real = subprocess.Popen\n'
                'def fake(argv, *args, **kwargs):\n'
                f'    if not isinstance(argv, list) or not argv or argv[0] != {profile["helper"]["path"]!r}: return _real(argv, *args, **kwargs)\n'
                '    code = "import time; print(\\"https://fake.trycloudflare.com\\", flush=True); time.sleep(10)"\n'
                '    return _real([sys.executable, "-u", "-c", code], **kwargs)\n'
                'subprocess.Popen = fake\n', encoding='utf-8')
            env = os.environ.copy()
            env['PYTHONPATH'] = str(adapter)
            process = subprocess.Popen([sys.executable, '-B', str(source), str(manifest), digest],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            lines = queue.Queue()
            reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
            reader.start()
            try:
                first = lines.get(timeout=10)
                self.assertIsNone(process.poll(), 'ready must arrive while launcher is alive')
                self.assertFalse(Path(profile['receipt_path']).exists())
                self.assertLessEqual(len(first), 512)
                self.assertEqual(json.loads(first), {'status': 'metadata-only',
                                 'origin': 'https://fake.trycloudflare.com',
                                 'mcp_url': 'https://fake.trycloudflare.com/mcp'})
                remaining, stderr = process.communicate(timeout=15)
                self.assertEqual(remaining, b'')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            detail = Path(profile['receipt_path']).read_text() if Path(profile['receipt_path']).exists() else 'no receipt'
            self.assertEqual(process.returncode, 0, stderr.decode(errors='replace') + detail)
            receipt = json.loads(Path(profile['receipt_path']).read_text())
            self.assertEqual(receipt['origin'], 'https://fake.trycloudflare.com')
            self.assertGreater(receipt['child_pid'], 0)
            self.assertGreater(receipt['child_creation_filetime'], 100000000000000000)
            self.assertEqual(receipt['cleanup'], 'complete')
    @unittest.skipUnless(sys.platform == 'win32', 'Windows process handle required')
    def test_creation_identity_from_owned_process_handle(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            creation = launcher._creation_filetime(child)
            self.assertGreater(creation, 100000000000000000)
            self.assertEqual(creation, launcher._creation_filetime(child))
        finally:
            child.terminate()
            child.wait(timeout=5)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows socket required')
    def test_creation_identity_failure_stops_child_before_serving(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            class Child:
                pid = 1234
                alive = True
                def terminate(self): self.alive = False
                def wait(self, timeout): return 0
                def kill(self): self.alive = False
            child = Child()
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', side_effect=OSError()), patch.object(launcher.subprocess, 'Popen', return_value=child), patch('uvicorn.Server') as server:
                self.assertEqual(launcher.main([str(manifest), digest]), 1)
                server.assert_not_called()
            receipt = json.loads(Path(profile['receipt_path']).read_text())
            self.assertIsNone(receipt['child_creation_filetime'])
            self.assertEqual(receipt['child_pid'], 1234)
            self.assertFalse(child.alive)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows socket required')
    def test_no_ready_before_server_started(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            class Child:
                pid = 3210
                alive = True
                stdout = io.BytesIO(b'https://fake.trycloudflare.com\n')
                def poll(self): return None if self.alive else 0
                def terminate(self): self.alive = False
                def wait(self, timeout): return 0
                def kill(self): self.alive = False
            class Server:
                started = False
                should_exit = False
                def __init__(self, config): pass
                def run(self, sockets):
                    while not self.should_exit: time.sleep(.01)
            output = io.StringIO()
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123), patch.object(launcher.subprocess, 'Popen', return_value=Child()), patch('uvicorn.Server', Server), contextlib.redirect_stdout(output):
                self.assertEqual(launcher.main([str(manifest), digest]), 1)
            self.assertEqual(output.getvalue(), '')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows socket required')
    def test_no_ready_when_guard_fails_before_server_started(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            ready_for_second = threading.Event()
            class Stream:
                first = io.BytesIO(b'https://fake.trycloudflare.com\n')
                second = io.BytesIO(b'https://other.trycloudflare.com\n')
                def read(self, size):
                    value = self.first.read(size)
                    if value: return value
                    ready_for_second.wait(2)
                    return self.second.read(size)
            class Child:
                pid = 3211
                alive = True
                stdout = Stream()
                def poll(self): return None if self.alive else 0
                def terminate(self): self.alive = False
                def wait(self, timeout): return 0
                def kill(self): self.alive = False
            class Server:
                started = False
                should_exit = False
                def __init__(self, config): pass
                def run(self, sockets):
                    ready_for_second.set()
                    time.sleep(.2)
                    self.started = True
                    while not self.should_exit: time.sleep(.01)
            output = io.StringIO()
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123), patch.object(launcher.subprocess, 'Popen', return_value=Child()), patch('uvicorn.Server', Server), contextlib.redirect_stdout(output):
                self.assertEqual(launcher.main([str(manifest), digest]), 1)
            self.assertEqual(output.getvalue(), '')
    def test_entrypoint_comes_after_listener_definition(self):
        import ast
        tree = ast.parse(Path(launcher.__file__).read_text(encoding='utf-8'))
        definition = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'open_exclusive_listener')
        entrypoint = next(node for node in tree.body if isinstance(node, ast.If) and "__name__" in ast.unparse(node.test))
        self.assertLess(definition.lineno, entrypoint.lineno)

    def test_outside_content_is_never_opened(self):
        for kind in ('runtime', 'helper', 'config'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
                profile, manifest, _, source = self.make_profile(directory)
                outer = Path(outside)
                if kind == 'runtime':
                    for name in profile['runtime_files']:
                        (outer / name).write_bytes((Path(profile['runtime_dir']) / name).read_bytes())
                    profile['runtime_dir'] = str(outer)
                else:
                    path = outer / kind
                    path.write_bytes(Path(profile[kind]['path']).read_bytes())
                    profile[kind]['path'] = str(path)
                raw = json.dumps(profile).encode()
                manifest.write_bytes(raw)
                original_open = Path.open
                def guarded_open(path, *args, **kwargs):
                    if path.is_relative_to(outer):
                        self.fail('outside content opened')
                    return original_open(path, *args, **kwargs)
                with patch.object(launcher, '__file__', str(source)), patch.object(Path, 'open', guarded_open):
                    with self.assertRaises(ValueError):
                        launcher.load_profile(str(manifest), hashlib.sha256(raw).hexdigest())

    def test_redirected_runtime_refused_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, _, source = self.make_profile(directory)
            redirect = Path(directory) / 'redirect'
            try:
                os.symlink(profile['runtime_dir'], redirect, target_is_directory=True)
            except OSError as exc:
                self.skipTest('directory symlink unavailable: ' + str(exc))
            profile['runtime_dir'] = str(redirect)
            raw = json.dumps(profile).encode()
            manifest.write_bytes(raw)
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher.subprocess, 'Popen') as child:
                self.assertEqual(launcher.main([str(manifest), hashlib.sha256(raw).hexdigest()]), 1)
                child.assert_not_called()

    def test_reparse_runtime_refused_before_content_open(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            runtime = Path(profile['runtime_dir'])
            original_stat = Path.lstat
            original_open = Path.open
            def synthetic_reparse(path):
                info = original_stat(path)
                if path == runtime:
                    return types.SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400, st_nlink=info.st_nlink)
                return info
            def guarded_open(path, *args, **kwargs):
                if path.is_relative_to(runtime):
                    self.fail('redirected content opened')
                return original_open(path, *args, **kwargs)
            with patch.object(launcher, '__file__', str(source)), patch.object(Path, 'lstat', synthetic_reparse), patch.object(Path, 'open', guarded_open):
                with self.assertRaises(ValueError):
                    launcher.load_profile(str(manifest), digest)
    def make_profile(self, base):
        root = Path(base)
        runtime = root / 'runtime'
        runtime.mkdir()
        temporary = root / 'temp'
        temporary.mkdir()
        source = Path(launcher.__file__).read_bytes()
        discovery = (Path(launcher.__file__).parent / 'chat_recall_discovery.py').read_bytes()
        (runtime / 'run_chat_discovery.py').write_bytes(source)
        (runtime / 'chat_recall_discovery.py').write_bytes(discovery)
        helper, config = root / 'fake.exe', root / 'empty.yml'
        helper.write_bytes(b'not an executable')
        config.write_bytes(b'{}\n')
        sha = lambda b: hashlib.sha256(b).hexdigest()
        profile = {'schema': 'brain-chat-discovery-launch-v1', 'state': 'approved',
                   'scope': 'metadata-discovery-only', 'owner_reference': 'synthetic',
                   'additional_spend_usd': 0, 'lifetime_seconds': 1, 'startup_timeout_seconds': 1,
                   'recipients': ['Cloudflare', 'OpenAI', 'Sentry'], 'private_root': str(root),
                   'runtime_dir': str(runtime), 'runtime_files': {'run_chat_discovery.py': sha(source),
                   'chat_recall_discovery.py': sha(discovery)}, 'helper': {'path': str(helper),
                   'sha256': sha(helper.read_bytes())}, 'config': {'path': str(config),
                   'sha256': sha(config.read_bytes())}, 'temp_dir': str(temporary),
                   'intent_path': str(root / 'intent.json'), 'receipt_path': str(root / 'receipt.json')}
        manifest = root / 'manifest.json'
        raw = json.dumps(profile).encode()
        manifest.write_bytes(raw)
        return profile, manifest, sha(raw), runtime / 'run_chat_discovery.py'

    def test_profile_pins_and_source_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            with patch.object(launcher, '__file__', str(source)):
                self.assertEqual(launcher.load_profile(str(manifest), digest), profile)
                Path(profile['helper']['path']).write_bytes(b'changed')
                with self.assertRaises(ValueError):
                    launcher.load_profile(str(manifest), digest)

    def test_manifest_profile_variants_fail_before_child(self):
        variants = [('state', 'pending'), ('state', 'disabled'), ('scope', 'recall'),
                    ('additional_spend_usd', True), ('lifetime_seconds', 901),
                    ('startup_timeout_seconds', 0), ('recipients', ['Cloudflare', 'OpenAI']),
                    ('owner_reference', '')]
        for key, value in variants:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                profile, manifest, _, source = self.make_profile(directory)
                profile[key] = value
                raw = json.dumps(profile).encode()
                manifest.write_bytes(raw)
                with patch.object(launcher, '__file__', str(source)), patch.object(launcher, 'open_exclusive_listener') as listener, patch.object(launcher.subprocess, 'Popen') as child:
                    self.assertEqual(launcher.main([str(manifest), hashlib.sha256(raw).hexdigest()]), 1)
                    listener.assert_not_called()
                    child.assert_not_called()

    def test_source_config_and_manifest_drift_fail(self):
        for kind in ('source', 'config', 'manifest'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                profile, manifest, digest, source = self.make_profile(directory)
                target = {'source': source, 'config': Path(profile['config']['path']), 'manifest': manifest}[kind]
                target.write_bytes(b'changed')
                with patch.object(launcher, '__file__', str(source)), patch.object(launcher.subprocess, 'Popen') as child:
                    self.assertEqual(launcher.main([str(manifest), digest]), 1)
                    child.assert_not_called()
    def test_pending_and_duplicate_keys_refused_before_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'manifest.json'
            for raw in (b'{"state":"pending"}', b'{"state":"approved","state":"approved"}'):
                manifest.write_bytes(raw)
                with patch.object(launcher, 'open_exclusive_listener') as listener, \
                     patch.object(launcher.subprocess, 'Popen') as child:
                    self.assertEqual(launcher.main([str(manifest), hashlib.sha256(raw).hexdigest()]), 1)
                    listener.assert_not_called()
                    child.assert_not_called()

    def test_output_requires_one_exact_origin_and_caps(self):
        parser = launcher.OutputGuard()
        parser.feed(b'hello https://abc-123.trycloudflare.com\n')
        self.assertEqual(parser.origin, 'https://abc-123.trycloudflare.com')
        parser.feed(b'https://another.trycloudflare.com\n')
        self.assertTrue(parser.failed)
        parser = launcher.OutputGuard()
        parser.feed(b'x' * 4097)
        self.assertTrue(parser.failed)
        parser = launcher.OutputGuard()
        parser.feed(b'x\n' * 32769)
        self.assertTrue(parser.failed)
        for url in (b'https://fake.trycloudflare.com:443', b'https://fake.trycloudflare.com/path',
                    b'http://fake.trycloudflare.com', b'ftp://fake.trycloudflare.com',
                    b'https://fake.trycloudflare.com.evil.example'):
            with self.subTest(url=url):
                parser = launcher.OutputGuard()
                parser.feed(url + b'\n')
                self.assertTrue(parser.failed)

    def test_informational_banner_urls_do_not_block_valid_origin(self):
        parser = launcher.OutputGuard()
        parser.feed(b'Terms: https://www.cloudflare.com/website-terms/\n'
                    b'Docs: https://developers.cloudflare.com/cloudflare-one/\n'
                    b'Example: https://docs.example/?url=https://fake.trycloudflare.com\n')
        self.assertFalse(parser.failed)
        self.assertIsNone(parser.origin)
        parser.feed(b'Your tunnel: https://fake.trycloudflare.com\n')
        self.assertFalse(parser.failed)
        self.assertEqual(parser.origin, 'https://fake.trycloudflare.com')

    def test_cleanup_keeps_socket_until_child_and_server_exit(self):
        events = []
        class Child:
            alive = True
            def poll(self): return None if self.alive else 0
            def terminate(self): events.append('terminate')
            def wait(self, timeout):
                events.append('reap')
                self.alive = False
                return 0
        class Thread:
            def is_alive(self): return False
            def join(self, timeout): events.append('join')
        class Server: should_exit = False
        class Socket:
            def close(self): events.append('close')
        launcher.cleanup(Child(), Server(), Thread(), Socket(), Socket())
        self.assertLess(events.index('reap'), events.index('close'))
        self.assertLess(events.index('join'), events.index('close'))

    def test_cleanup_waits_when_reaping_is_temporarily_blocked(self):
        events = []
        class Child:
            calls = 0
            def terminate(self): events.append('terminate')
            def kill(self): events.append('kill')
            def wait(self, timeout):
                self.calls += 1
                if self.calls < 3:
                    raise subprocess.TimeoutExpired('fake', timeout)
                events.append('reaped')
                return 0
        class Socket:
            def close(self): events.append('close')
        with contextlib.redirect_stderr(io.StringIO()):
            launcher.cleanup(Child(), None, None, None, Socket())
        self.assertLess(events.index('reaped'), events.index('close'))
        self.assertIn('kill', events)

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows socket semantics required')
    def test_fake_child_lifecycle_creates_records_after_cleanup(self):
        class Child:
            pid = 9876
            alive = True
            stdout = io.BytesIO(b'Terms https://www.cloudflare.com/website-terms/\n'
                                b'Docs https://developers.cloudflare.com/cloudflare-one/\n'
                                b'hello https://fake.trycloudflare.com\n')
            def poll(self): return None if self.alive else 0
            def terminate(self): self.alive = False
            def wait(self, timeout):
                if self.alive:
                    raise subprocess.TimeoutExpired('fake', timeout)
                return 0
            def kill(self): self.alive = False
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            child = Child()
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123456789), patch.object(launcher.subprocess, 'Popen', return_value=child) as popen:
                self.assertEqual(launcher.main([str(manifest), digest]), 0)
            self.assertFalse(child.alive)
            intent = json.loads(Path(profile['intent_path']).read_text())
            receipt = json.loads(Path(profile['receipt_path']).read_text())
            self.assertEqual(intent['manifest_sha256'], digest)
            self.assertEqual(receipt['origin'], 'https://fake.trycloudflare.com')
            self.assertEqual(receipt['cleanup'], 'complete')
            self.assertEqual(receipt['child_pid'], child.pid)
            self.assertEqual(receipt['child_creation_filetime'], 123456789)
            self.assertEqual(popen.call_args.kwargs['shell'], False)
            self.assertNotIn('PRIVATE_FAKE_SECRET', popen.call_args.kwargs['env'])
            args = popen.call_args.args[0]
            self.assertEqual(args[:4], [profile['helper']['path'], 'tunnel', '--no-autoupdate', '--no-prechecks'])
            self.assertEqual(args[-2], '--url')
            self.assertEqual(args[-1], 'http://127.0.0.1:' + str(receipt['port']))
            self.assertEqual(set(popen.call_args.kwargs['env']) - {'SystemRoot', 'WINDIR'}, {'TEMP', 'TMP'})

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows socket semantics required')
    def test_missing_origin_stops_owned_child_and_preserves_record_collision(self):
        class Child:
            pid = 9877
            alive = True
            stdout = io.BytesIO(b'Terms https://www.cloudflare.com/website-terms/\n'
                                b'Docs https://developers.cloudflare.com/cloudflare-one/\n')
            def poll(self): return None if self.alive else 0
            def terminate(self): self.alive = False
            def wait(self, timeout): return 0 if not self.alive else None
            def kill(self): self.alive = False
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            child = Child()
            output = io.StringIO()
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123456789), patch.object(launcher.subprocess, 'Popen', return_value=child), contextlib.redirect_stdout(output):
                self.assertEqual(launcher.main([str(manifest), digest]), 1)
            self.assertEqual(output.getvalue(), '')
            self.assertFalse(child.alive)
            receipt = json.loads(Path(profile['receipt_path']).read_text())
            self.assertEqual(receipt['status'], 'error')
            self.assertEqual(receipt['cleanup'], 'complete')
            original = Path(profile['intent_path']).read_bytes()
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher.subprocess, 'Popen') as popen:
                self.assertEqual(launcher.main([str(manifest), digest]), 1)
                popen.assert_not_called()
            self.assertEqual(Path(profile['intent_path']).read_bytes(), original)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows socket required')
    def test_hold_identity_written_before_reap_and_socket_release(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            hold = Path(profile['intent_path'] + '.hold.json')
            observed = []
            class Child:
                pid = 4321
                stdout = io.BytesIO(b'')
                calls = 0
                def poll(self): return None
                def terminate(self): pass
                def kill(self): pass
                def wait(self, timeout):
                    self.calls += 1
                    if self.calls < 3:
                        raise subprocess.TimeoutExpired('fake', timeout)
                    record = json.loads(hold.read_text())
                    record['receipt_exists'] = Path(profile['receipt_path']).exists()
                    with socket.socket() as competitor:
                        try:
                            competitor.bind(('127.0.0.1', record['port']))
                            record['bind_blocked'] = False
                        except OSError:
                            record['bind_blocked'] = True
                    observed.append(record)
                    return 0
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123456789), patch.object(launcher.subprocess, 'Popen', return_value=Child()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(launcher.main([str(manifest), digest]), 1)
            self.assertEqual(json.loads(hold.read_text())['reason'], 'child')
            self.assertEqual(observed[0]['child_pid'], 4321)
            self.assertEqual(observed[0]['child_creation_filetime'], 123456789)
            self.assertGreater(observed[0]['port'], 0)
            self.assertTrue(observed[0]['bind_blocked'])
            self.assertFalse(observed[0]['receipt_exists'])
            self.assertLessEqual(hold.stat().st_size, 2048)
            self.assertEqual(json.loads(Path(profile['receipt_path']).read_text())['cleanup'], 'complete')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows socket required')
    def test_hold_collision_preserves_existing_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            hold = Path(profile['intent_path'] + '.hold.json')
            class Child:
                pid = 4326
                stdout = io.BytesIO(b'')
                calls = 0
                def poll(self): return None
                def terminate(self): pass
                def kill(self): hold.write_bytes(b'preserve')
                def wait(self, timeout):
                    self.calls += 1
                    if self.calls < 3: raise subprocess.TimeoutExpired('fake', timeout)
                    return 0
            stderr = io.StringIO()
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123456789), patch.object(launcher.subprocess, 'Popen', return_value=Child()), contextlib.redirect_stderr(stderr):
                self.assertEqual(launcher.main([str(manifest), digest]), 1)
            self.assertEqual(hold.read_bytes(), b'preserve')
            self.assertIn('private hold report unavailable', stderr.getvalue())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows socket required')
    def test_server_hold_includes_server_thread_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            hold = Path(profile['intent_path'] + '.hold.json')
            observations = []
            class Child:
                pid = 4324
                alive = True
                stdout = io.BytesIO(b'https://fake.trycloudflare.com\n')
                def poll(self): return None if self.alive else 0
                def terminate(self): self.alive = False
                def wait(self, timeout): return 0
                def kill(self): self.alive = False
            class Server:
                started = False
                should_exit = False
                def __init__(self, config): pass
                def run(self, sockets):
                    self.started = True
                    deadline = time.monotonic() + 5
                    while not hold.exists() and time.monotonic() < deadline: time.sleep(.01)
                    if hold.exists():
                        port = json.loads(hold.read_text())['port']
                        with socket.socket() as competitor:
                            try:
                                competitor.bind(('127.0.0.1', port))
                                observations.append(False)
                            except OSError:
                                observations.append(True)
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123456789), patch.object(launcher.subprocess, 'Popen', return_value=Child()), patch('uvicorn.Server', Server), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(launcher.main([str(manifest), digest]), 0)
            record = json.loads(hold.read_text())
            self.assertEqual(record['reason'], 'server')
            self.assertGreater(record['server_thread_id'], 0)
            self.assertGreater(record['server_native_id'], 0)
            self.assertEqual(observations, [True])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows socket required')
    def test_owner_interrupt_and_final_receipt_collision(self):
        for action in ('interrupt', 'collision'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                profile, manifest, digest, source = self.make_profile(directory)
                receipt_path = Path(profile['receipt_path'])
                class Child:
                    pid = 4322
                    alive = True
                    stdout = io.BytesIO(b'https://fake.trycloudflare.com\n')
                    def poll(self): return None if self.alive else 0
                    def terminate(self):
                        self.alive = False
                        if action == 'collision': receipt_path.write_bytes(b'preserve')
                    def wait(self, timeout): return 0
                    def kill(self): self.alive = False
                class Server:
                    started = False
                    should_exit = False
                    def __init__(self, config): pass
                    def run(self, sockets):
                        self.started = True
                        if action == 'interrupt':
                            _thread.interrupt_main()
                        while not self.should_exit: time.sleep(.01)
                child = Child()
                with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123456789), patch.object(launcher.subprocess, 'Popen', return_value=child), patch('uvicorn.Server', Server):
                    self.assertEqual(launcher.main([str(manifest), digest]), 1)
                self.assertFalse(child.alive)
                if action == 'collision':
                    self.assertEqual(receipt_path.read_bytes(), b'preserve')
                else:
                    self.assertEqual(json.loads(receipt_path.read_text())['status'], 'error')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows socket required')
    def test_changed_origin_after_server_start_stops_owned_child(self):
        with tempfile.TemporaryDirectory() as directory:
            profile, manifest, digest, source = self.make_profile(directory)
            server_started = threading.Event()
            class Stream:
                first = io.BytesIO(b'https://first.trycloudflare.com\n')
                second = io.BytesIO(b'https://second.trycloudflare.com\n')
                def read(self, size):
                    value = self.first.read(size)
                    if value: return value
                    server_started.wait(2)
                    return self.second.read(size)
            class Child:
                pid = 4323
                alive = True
                stdout = Stream()
                def poll(self): return None if self.alive else 0
                def terminate(self): self.alive = False
                def wait(self, timeout): return 0
                def kill(self): self.alive = False
            class Server:
                started = False
                should_exit = False
                def __init__(self, config): pass
                def run(self, sockets):
                    self.started = True
                    server_started.set()
                    while not self.should_exit: time.sleep(.01)
            child = Child()
            with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123456789), patch.object(launcher.subprocess, 'Popen', return_value=child), patch('uvicorn.Server', Server):
                self.assertEqual(launcher.main([str(manifest), digest]), 1)
            self.assertTrue(server_started.is_set())
            self.assertFalse(child.alive)
            self.assertEqual(json.loads(Path(profile['receipt_path']).read_text())['status'], 'error')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows socket required')
    def test_output_limits_stop_fake_child_without_server_start(self):
        for data in (b'x' * 4097, b'x\n' * 32769):
            with self.subTest(size=len(data)), tempfile.TemporaryDirectory() as directory:
                profile, manifest, digest, source = self.make_profile(directory)
                class Child:
                    pid = 4325
                    alive = True
                    stdout = io.BytesIO(data)
                    def poll(self): return None if self.alive else 0
                    def terminate(self): self.alive = False
                    def wait(self, timeout): return 0
                    def kill(self): self.alive = False
                child = Child()
                with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123456789), patch.object(launcher.subprocess, 'Popen', return_value=child), patch('uvicorn.Server') as server:
                    self.assertEqual(launcher.main([str(manifest), digest]), 1)
                    server.assert_not_called()
                self.assertFalse(child.alive)
                self.assertEqual(json.loads(Path(profile['receipt_path']).read_text())['cleanup'], 'complete')

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows socket semantics required')
    def test_child_exit_server_failure_and_interrupt_stop_session(self):
        for failure in ('child', 'server', 'interrupt'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                profile, manifest, digest, source = self.make_profile(directory)
                class Child:
                    pid = 9878
                    stdout = io.BytesIO(b'https://fake.trycloudflare.com\n')
                    alive = True
                    def poll(self): return 1 if failure == 'child' else (None if self.alive else 0)
                    def terminate(self): self.alive = False
                    def wait(self, timeout): return 0
                    def kill(self): self.alive = False
                child = Child()
                def fail_server(*args, **kwargs): raise RuntimeError('synthetic failure')
                def interrupt(*args, **kwargs): raise KeyboardInterrupt()
                output = io.StringIO()
                with patch.object(launcher, '__file__', str(source)), patch.object(launcher, '_creation_filetime', return_value=123456789), patch.object(launcher.subprocess, 'Popen', return_value=child), patch('uvicorn.Server.run', side_effect=fail_server if failure == 'server' else interrupt), contextlib.redirect_stdout(output):
                    self.assertEqual(launcher.main([str(manifest), digest]), 1)
                if failure in ('child', 'server'):
                    self.assertEqual(output.getvalue(), '')
                self.assertFalse(child.alive)
                receipt = json.loads(Path(profile['receipt_path']).read_text())
                self.assertEqual(receipt['status'], 'error')
                self.assertEqual(receipt['cleanup'], 'complete')


class SocketTests(unittest.TestCase):
    def test_unsupported_platform_refuses(self):
        with patch('run_chat_discovery.sys.platform', 'not-windows'):
            with self.assertRaises(ValueError):
                open_exclusive_listener()

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows socket semantics required')
    def test_root_socket_excludes_competing_bind_through_server_lifecycle(self):
        import uvicorn
        root = open_exclusive_listener()
        self.addCleanup(root.close)
        port = root.getsockname()[1]
        code = ('import socket,sys; s=socket.socket(); '
                's.bind(("127.0.0.1",int(sys.argv[1]))); s.close()')
        def competing_bind():
            return subprocess.run([sys.executable, '-B', '-c', code, str(port)], capture_output=True).returncode
        self.assertNotEqual(competing_bind(), 0)
        duplicate = root.dup()
        self.addCleanup(duplicate.close)
        app = create_app('https://synthetic.trycloudflare.com', port)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False,
                                                proxy_headers=False, log_config=None, log_level='critical'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [duplicate]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(server.started)
            self.assertNotEqual(competing_bind(), 0)
            with socket.create_connection(('127.0.0.1', port), timeout=2) as conn:
                conn.sendall(b'GET /.well-known/oauth-authorization-server HTTP/1.1\r\n'
                             b'Host: synthetic.trycloudflare.com\r\nConnection: close\r\n\r\n')
                data = b''
                while chunk := conn.recv(4096):
                    data += chunk
            self.assertIn(b'200 OK', data)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertNotEqual(competing_bind(), 0)
        root.close()
        self.assertEqual(competing_bind(), 0)

    @unittest.skipUnless(sys.platform == 'win32', 'Native Windows socket semantics required')
    def test_cleanup_holds_real_socket_until_owned_child_is_reaped(self):
        root = open_exclusive_listener()
        self.addCleanup(root.close)
        port = root.getsockname()[1]
        code = 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1])))'
        def bind_result():
            return subprocess.run([sys.executable, '-B', '-c', code, str(port)], capture_output=True).returncode
        observations = []
        class Child:
            def terminate(self): observations.append(bind_result())
            def wait(self, timeout):
                observations.append(bind_result())
                return 0
        launcher.cleanup(Child(), None, None, None, root)
        self.assertTrue(observations)
        self.assertTrue(all(value != 0 for value in observations))
        self.assertEqual(bind_result(), 0)


if __name__ == '__main__':
    unittest.main()
