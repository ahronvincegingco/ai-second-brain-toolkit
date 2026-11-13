import hashlib,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_chat_supervisor as sup
import test_chat_ready_lifecycle as fixture
import test_run_chat_synthetic as launcher_fixture

class Child:
    pid=123
    def __init__(self,exited=False,timeout=False): self.exited=exited; self.timeout=timeout; self.waits=0
    def poll(self): return 0 if self.exited else None
    def wait(self,timeout):
        self.waits+=1
        if self.timeout: raise subprocess.TimeoutExpired('fake',timeout)
        self.exited=True; return 0

class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.real_creation=sup.process_creation
        self.real_grace=sup.wait_startup_grace
        grace=patch.object(sup,'wait_startup_grace',return_value=None); grace.start(); self.addCleanup(grace.stop)
        identity=patch.object(sup,'process_creation',return_value=123); identity.start(); self.addCleanup(identity.stop)
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
        self.files={}
        for name in ('launcher','profile','checker'):
            path=self.root/name; path.write_bytes(json.dumps({'intent_path':str(self.root/'intent.json'),'lifetime_seconds':60}).encode() if name=='profile' else name.encode()); self.files[name]=path
        # POSIX venv executables are normally symlinks. Pin the physical fixture
        # interpreter so the production supervisor's redirect guard stays intact.
        interpreter=Path(sys.executable).resolve()
        self.cfg={'root':str(self.root),'interpreter':str(interpreter),
                  'interpreter_sha256':hashlib.sha256(interpreter.read_bytes()).hexdigest(),
                  'ready_timeout':1,'lifetime_timeout':1}
        for name,path in self.files.items(): self.cfg[name]=str(path); self.cfg[name+'_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    def start(self,child,content=None):
        def fake(args,**kwargs):
            ready={'status':'synthetic-only','origin':'https://fake.trycloudflare.com','mcp_url':'https://fake.trycloudflare.com/mcp'}
            kwargs['stdout'].write(content if content is not None else json.dumps(ready).encode()+b'\n')
            return child
        return fake
    def checker(self,result=None):
        def fake(args,**kwargs):
            data=json.dumps(fixture.passed() if result is None else result,separators=(',',':')).encode()+b'\n'
            (self.root/'supervisor-checker.json').write_bytes(data)
            return data
        return fake
    @unittest.skipUnless(sys.platform == 'win32', 'Windows fictional CLI required')
    def test_real_fictional_launcher_with_fake_checker_and_natural_cleanup(self):
        setup=launcher_fixture.BoundaryTests(methodName='test_actual_cli_fictional_oauth_and_socket_read')
        data,profile,path,_,launcher=setup.synthetic_profile(); self.addCleanup(setup.doCleanups)
        profile['lifetime_seconds']=12; profile['startup_timeout_seconds']=5
        raw=json.dumps(profile).encode(); path.write_bytes(raw)
        adapter=data.base/'supervisor-test-adapter'; adapter.mkdir()
        helper_code = 'import time;print("https://fake.trycloudflare.com",flush=True);time.sleep(20)'
        (adapter/'sitecustomize.py').write_text(
            'import subprocess,sys,uvicorn,mcp.server.mcpserver\n'
            '_real=subprocess.Popen\n'
            'def fake(argv,*args,**kwargs):\n'
            f'    if not isinstance(argv,list) or not argv or argv[0] != {profile["helper"]["path"]!r}: return _real(argv,*args,**kwargs)\n'
            f'    return _real([sys.executable,"-u","-c",{helper_code!r}],**kwargs)\n'
            'subprocess.Popen=fake\n',encoding='utf-8')
        fakechecker=data.base/'fake-checker.py'
        body=json.dumps(fixture.passed(),separators=(',',':'))+'\n'
        fakechecker.write_text('import sys,pathlib\n' +
            f'body={body!r}\n' +
            'pathlib.Path(sys.argv[sys.argv.index("--receipt")+1]).write_bytes(body.encode())\n' +
            'print(body,end="",flush=True)\n',encoding='utf-8')
        def digest(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
        cfg={'root':str(data.base),'interpreter':sys.executable,'interpreter_sha256':digest(sys.executable),
             'launcher':str(launcher),'launcher_sha256':digest(launcher),'profile':str(path),'profile_sha256':digest(path),
             'checker':str(fakechecker),'checker_sha256':digest(fakechecker),'ready_timeout':10,'lifetime_timeout':20}
        cfgpath=data.base/'supervisor-config.json'; cfgpath.write_text(json.dumps(cfg),encoding='utf-8')
        env=os.environ.copy(); env['PYTHONPATH']=str(adapter)
        runner=subprocess.Popen([sys.executable,'-B',str(Path(sup.__file__)),str(cfgpath),digest(cfgpath)],
                                env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            import time
            deadline=time.monotonic()+15; observed=None
            while time.monotonic()<deadline and runner.poll() is None:
                gate=data.base/'supervisor-gate.json'
                if gate.exists() and gate.stat().st_size:
                    try: observed=json.loads(gate.read_text())
                    except json.JSONDecodeError: pass
                    if observed: break
                time.sleep(.02)
            self.assertIsNone(runner.poll())
            self.assertIsNotNone(observed)
            self.assertTrue(observed['passed'])
            self.assertGreater(observed['creation_filetime'],0)
        finally:
            runner.wait(timeout=25)
        detail = repr({f: (data.base/f).read_text() if (data.base/f).exists() else 'missing' for f in ('supervisor-ready.json','supervisor-gate.json','supervisor-final.json','supervisor-checker.json')})
        self.assertEqual(runner.returncode,0,detail)
        receipt=json.loads(Path(profile['receipt_path']).read_text())
        self.assertEqual(receipt['cleanup'],'complete')
    @unittest.skipUnless(sys.platform == 'win32', 'Windows fictional CLI required')
    def test_failed_checker_requests_real_fictional_launcher_stop(self):
        setup=launcher_fixture.BoundaryTests(methodName='test_actual_cli_fictional_oauth_and_socket_read')
        data,profile,path,_,launcher=setup.synthetic_profile(); self.addCleanup(setup.doCleanups)
        profile['lifetime_seconds']=12; profile['startup_timeout_seconds']=5
        raw=json.dumps(profile).encode(); path.write_bytes(raw)
        adapter=data.base/'supervisor-test-adapter'; adapter.mkdir()
        helper_code = 'import time;print("https://fake.trycloudflare.com",flush=True);time.sleep(20)'
        (adapter/'sitecustomize.py').write_text(
            'import subprocess,sys,uvicorn,mcp.server.mcpserver\n'
            '_real=subprocess.Popen\n'
            'def fake(argv,*args,**kwargs):\n'
            f'    if not isinstance(argv,list) or not argv or argv[0] != {profile["helper"]["path"]!r}: return _real(argv,*args,**kwargs)\n'
            f'    return _real([sys.executable,"-u","-c",{helper_code!r}],**kwargs)\n'
            'subprocess.Popen=fake\n',encoding='utf-8')
        fakechecker=data.base/'fake-checker.py'
        failing=fixture.passed(); failing['checks']['challenge_match']=False
        body=json.dumps(failing,separators=(',',':'))+'\n'
        fakechecker.write_text('import sys,pathlib\n' +
            f'body={body!r}\n' +
            'pathlib.Path(sys.argv[sys.argv.index("--receipt")+1]).write_bytes(body.encode())\n' +
            'print(body,end="",flush=True)\n',encoding='utf-8')
        def digest(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
        cfg={'root':str(data.base),'interpreter':sys.executable,'interpreter_sha256':digest(sys.executable),
             'launcher':str(launcher),'launcher_sha256':digest(launcher),'profile':str(path),'profile_sha256':digest(path),
             'checker':str(fakechecker),'checker_sha256':digest(fakechecker),'ready_timeout':10,'lifetime_timeout':20}
        cfgpath=data.base/'supervisor-config.json'; cfgpath.write_text(json.dumps(cfg),encoding='utf-8')
        env=os.environ.copy(); env['PYTHONPATH']=str(adapter)
        runner=subprocess.Popen([sys.executable,'-B',str(Path(sup.__file__)),str(cfgpath),digest(cfgpath)],
                                env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            import time
            deadline=time.monotonic()+15; observed=None
            while time.monotonic()<deadline:
                gate=data.base/'supervisor-gate.json'
                if gate.exists() and gate.stat().st_size:
                    try: observed=json.loads(gate.read_text())
                    except json.JSONDecodeError: pass
                    if observed: break
                time.sleep(.02)
            self.assertIsNotNone(observed)
            self.assertFalse(observed['passed'])
            self.assertGreater(observed['creation_filetime'],0)
        finally:
            runner.wait(timeout=25)
        detail = repr({f: (data.base/f).read_text() if (data.base/f).exists() else 'missing' for f in ('supervisor-ready.json','supervisor-gate.json','supervisor-final.json','supervisor-checker.json')})
        self.assertEqual(runner.returncode,1,detail)
        receipt=json.loads(Path(profile['receipt_path']).read_text())
        self.assertEqual(receipt['cleanup'],'complete')
        self.assertEqual(receipt['diagnostic']['reason'],'stop_requested')
        self.assertEqual(receipt['diagnostic']['post_cleanup_child']['state'],'exited')
    def test_pass_then_natural_exit(self):
        child=Child()
        with patch.object(sup.subprocess,'Popen',side_effect=self.start(child)), patch.object(sup,'checker_result',side_effect=self.checker()) as checker:
            self.assertEqual(sup.run(self.cfg),0)
        checker.assert_called_once()
        self.assertEqual(child.waits,1)
        self.assertTrue(json.loads((self.root/'supervisor-gate.json').read_text())['passed'])
        self.assertTrue(json.loads((self.root/'supervisor-final.json').read_text())['launcher_exited'])
    def test_failure_does_not_pass_and_waits(self):
        child=Child(); result=fixture.passed(); result['checks']['challenge_match']=None
        with patch.object(sup.subprocess,'Popen',side_effect=self.start(child)), patch.object(sup,'checker_result',side_effect=self.checker(result)):
            self.assertEqual(sup.run(self.cfg),1)
        self.assertEqual(child.waits,1); self.assertFalse(json.loads((self.root/'supervisor-gate.json').read_text())['passed'])
    @unittest.skipUnless(sys.platform == 'win32', 'Windows process identity required')
    def test_creation_identity_from_real_owned_process_handle(self):
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.5)'])
        try:
            self.assertGreater(self.real_creation(child),0)
        finally:
            child.wait(timeout=5)
    def test_identity_failure_still_waits_and_never_checks(self):
        child=Child()
        with patch.object(sup.subprocess,'Popen',side_effect=self.start(child)), patch.object(sup,'checker_result') as checker, patch.object(sup,'process_creation',side_effect=ValueError('failed')):
            self.assertEqual(sup.run(self.cfg),1)
        checker.assert_not_called(); self.assertEqual(child.waits,1)
    def test_launcher_exit_during_checker_refuses_gate(self):
        child=Child()
        def checker(args,**kwargs):
            result=self.checker()(args,**kwargs); child.exited=True; return result
        with patch.object(sup.subprocess,'Popen',side_effect=self.start(child)), patch.object(sup,'checker_result',side_effect=checker):
            self.assertEqual(sup.run(self.cfg),1)
        self.assertFalse(json.loads((self.root/'supervisor-gate.json').read_text())['passed'])
    def test_checker_output_overflow_is_bounded_and_stopped(self):
        import time
        started=time.monotonic()
        with self.assertRaises(sup.Refused):
            sup.checker_result([sys.executable,'-c','import sys,time;sys.stdout.buffer.write(b"x"*4096);sys.stdout.flush();time.sleep(10)'])
        self.assertLess(time.monotonic()-started,5)
    def test_all_package_paths_checked_before_any_open_or_hash(self):
        with tempfile.TemporaryDirectory() as outside:
            foreign=Path(outside)/'foreign'; foreign.write_bytes(b'private')
            for key in ('launcher','profile','checker'):
                cfg=dict(self.cfg); cfg[key]=str(foreign)
                with self.subTest(key=key), patch.object(Path,'open',side_effect=AssertionError('opened file')), patch.object(sup,'hash_file') as hashed:
                    with self.assertRaises(sup.Refused): sup.validate(cfg)
                    hashed.assert_not_called()
    def test_reparse_package_input_refused_before_any_open_or_hash(self):
        original=Path.lstat
        target=self.files['checker']
        def synthetic_reparse(path):
            info=original(path)
            if path == target:
                class Reparse:
                    st_mode=info.st_mode
                    st_nlink=info.st_nlink
                    st_file_attributes=0x400
                return Reparse()
            return info
        with patch.object(Path,'lstat',new=synthetic_reparse), patch.object(Path,'open',side_effect=AssertionError('opened file')), patch.object(sup,'hash_file') as hashed:
            with self.assertRaises(sup.Refused): sup.validate(self.cfg)
            hashed.assert_not_called()
    def test_post_gate_wait_error_requests_stop_and_reports_pending(self):
        child=Child(timeout=True)
        with patch.object(sup.subprocess,'Popen',side_effect=self.start(child)), patch.object(sup,'checker_result',side_effect=self.checker()):
            self.assertEqual(sup.run(self.cfg),1)
        self.assertEqual(child.waits,2)
        self.assertEqual((self.root/'intent.json.stop.json').read_bytes(),b'stop\n')
        final=json.loads((self.root/'supervisor-final.json').read_text())
        self.assertEqual(final['phase'],'wait_error'); self.assertFalse(final['gate_observed'])
        self.assertTrue(final['stop_written']); self.assertFalse(final['launcher_exited'])
        self.assertEqual((final['pid'],final['creation_filetime']),(child.pid,123))
    def test_checker_outside_package_refused_before_launch(self):
        with tempfile.TemporaryDirectory() as outside:
            path=Path(outside)/'checker'; path.write_bytes(b'checker')
            self.cfg['checker']=str(path)
            with patch.object(sup.subprocess,'Popen') as start:
                with self.assertRaises(sup.Refused): sup.run(self.cfg)
            start.assert_not_called()
    def test_keyboard_interrupt_requests_launcher_stop(self):
        child=Child()
        with patch.object(sup.subprocess,'Popen',side_effect=self.start(child)), patch.object(sup,'checker_result',side_effect=KeyboardInterrupt):
            self.assertEqual(sup.run(self.cfg),1)
        self.assertEqual((self.root/'intent.json.stop.json').read_bytes(),b'stop\n')
        self.assertTrue(json.loads((self.root/'supervisor-final.json').read_text())['stop_written'])
    def test_interrupt_after_gate_requests_stop_and_waits_again(self):
        class Interrupted(Child):
            def wait(self,timeout):
                self.waits+=1
                if self.waits==1: raise KeyboardInterrupt()
                self.exited=True; return 0
        child=Interrupted()
        with patch.object(sup.subprocess,'Popen',side_effect=self.start(child)), patch.object(sup,'checker_result',side_effect=self.checker()):
            self.assertEqual(sup.run(self.cfg),1)
        self.assertEqual(child.waits,2)
        self.assertEqual((self.root/'intent.json.stop.json').read_bytes(),b'stop\n')
        final=json.loads((self.root/'supervisor-final.json').read_text())
        self.assertEqual(final['phase'],'interrupted')
        self.assertFalse(final['gate_observed'])
        self.assertTrue(final['stop_written'])
        self.assertTrue(final['launcher_exited'])
    def test_grace_waits_five_seconds_without_exceeding_deadline(self):
        class Clock:
            now=0.0
            def monotonic(self): return self.now
            def sleep(self,seconds): self.now+=seconds
        clock=Clock(); child=Child()
        with patch.object(sup.time,'monotonic',side_effect=clock.monotonic), patch.object(sup.time,'sleep',side_effect=clock.sleep):
            self.real_grace(child,6.0)
        self.assertGreaterEqual(clock.now,5.0); self.assertLess(clock.now,6.0)
    def test_grace_refuses_deadline_or_exit(self):
        class Clock:
            now=0.0
            def monotonic(self): return self.now
            def sleep(self,seconds): self.now+=seconds
        clock=Clock()
        with patch.object(sup.time,'monotonic',side_effect=clock.monotonic), patch.object(sup.time,'sleep',side_effect=clock.sleep):
            with self.assertRaises(sup.Refused): self.real_grace(Child(),5.0)
            self.assertEqual(clock.now,0.0)
            with self.assertRaises(sup.Refused): self.real_grace(Child(exited=True),10.0)
    def test_grace_stops_when_child_exits_mid_wait(self):
        class Clock:
            now=0.0
            def monotonic(self): return self.now
            def sleep(self,seconds): self.now+=seconds
        clock=Clock()
        class ExitingChild:
            def poll(self): return 0 if clock.now>=2.0 else None
        with patch.object(sup.time,'monotonic',side_effect=clock.monotonic), patch.object(sup.time,'sleep',side_effect=clock.sleep):
            with self.assertRaises(sup.Refused): self.real_grace(ExitingChild(),10.0)
        self.assertGreaterEqual(clock.now,2.0); self.assertLess(clock.now,5.0)
    def test_grace_failure_or_interruption_stops_without_checker(self):
        for number,error in enumerate((sup.Refused('exited'),KeyboardInterrupt())):
            child=Child()
            if number:
                for name in ('supervisor-ready.json','supervisor-gate.json','supervisor-final.json','supervisor-stdout.jsonl','intent.json.stop.json'):
                    path=self.root/name
                    if path.exists(): path.unlink()
            with patch.object(sup.subprocess,'Popen',side_effect=self.start(child)), patch.object(sup,'checker_result') as checker, patch.object(sup,'wait_startup_grace',side_effect=error):
                self.assertEqual(sup.run(self.cfg),1)
            checker.assert_not_called()
            self.assertEqual((self.root/'intent.json.stop.json').read_bytes(),b'stop\n')
    def test_bad_ready_no_checker_and_wait(self):
        child=Child()
        with patch.object(sup.subprocess,'Popen',side_effect=self.start(child,b'wrong\n')), patch.object(sup,'checker_result') as checker:
            self.assertEqual(sup.run(self.cfg),1)
        checker.assert_not_called(); self.assertEqual(child.waits,1)
        self.assertEqual((self.root/'intent.json.stop.json').read_bytes(),b'stop\n')
    def test_timeout_does_not_kill_launcher(self):
        child=Child(timeout=True)
        with patch.object(sup.subprocess,'Popen',side_effect=self.start(child)), patch.object(sup,'checker_result',side_effect=subprocess.TimeoutExpired('checker',30)):
            self.assertEqual(sup.run(self.cfg),1)
        self.assertFalse(json.loads((self.root/'supervisor-final.json').read_text())['launcher_exited']); self.assertEqual(child.waits,2)
    def test_collision_and_pin_mismatch_refuse_before_launch(self):
        (self.root/'supervisor-gate.json').write_bytes(b'preserve')
        with patch.object(sup.subprocess,'Popen') as start:
            with self.assertRaises(FileExistsError): sup.run(self.cfg)
        start.assert_not_called(); self.assertEqual((self.root/'supervisor-gate.json').read_bytes(),b'preserve')
    def test_checker_output_receipt_mismatch_refused(self):
        child=Child()
        def checker(args,**kwargs):
            (self.root/'supervisor-checker.json').write_bytes(b'{}\n')
            return json.dumps(fixture.passed()).encode()+b'\n'
        with patch.object(sup.subprocess,'Popen',side_effect=self.start(child)),patch.object(sup,'checker_result',side_effect=checker):
            self.assertEqual(sup.run(self.cfg),1)
        self.assertFalse(json.loads((self.root/'supervisor-gate.json').read_text())['passed'])
if __name__=='__main__': unittest.main()
