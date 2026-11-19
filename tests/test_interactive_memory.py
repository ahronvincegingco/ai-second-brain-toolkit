import copy
from concurrent.futures import ThreadPoolExecutor
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from save_interactive_memory import stage


class InteractiveMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.project = base / 'private', base / 'project'
        self.root.mkdir()
        self.project.mkdir()
        (self.root / '.private-staging.json').write_text(json.dumps({'live_writes': False, 'external_exports': False}))
        (self.project / 'example.py').write_text('value = 42\n')
        self.packet = {'schema': 'brain-save-context-v1', 'role': 'reviewer', 'coverage': 'visible-context',
                       'items': [{'kind': 'test', 'text': 'The focused test run reported one failure; no fix was applied.',
                                  'status': 'reported-failed', 'actor': 'this-session', 'support': 'visible-tool-output',
                                  'references': ['visible tool result: unittest exit 1']}], 'artifacts': ['example.py']}
        self.environment = {'CODEX_THREAD_ID': 'synthetic-session-1'}

    def save(self, packet=None, environment=None):
        return stage(self.root, self.project, packet or self.packet, self.environment if environment is None else environment)

    def test_stage_role_evidence_and_duplicate(self):
        first = self.save()
        markdown = self.root / first['proposal']
        before = markdown.read_bytes()
        second = self.save()
        self.assertEqual((first['status'], second['status'], second['proposals']), ('STAGED', 'DUPLICATE_SOURCE', 0))
        self.assertEqual(markdown.read_bytes(), before)
        note = json.loads((self.root / first['candidate']).read_bytes())
        self.assertEqual(note['interactive_capture']['role'], 'reviewer')
        self.assertFalse(note['interactive_capture']['full_transcript'])
        self.assertEqual(note['claims'][0]['evidence'], 'summary-derived')
        self.assertIn('reported-failed', note['claims'][0]['text'])
        self.assertEqual(len(note['interactive_capture']['artifacts'][0]['sha256']), 64)
        self.assertFalse(note['external_processing'])

    def test_parallel_projects_sessions_and_roles_do_not_mix(self):
        jobs = [('planner', 'plan-session'), ('reviewer', 'review-session'), ('implementer', 'implementation-session')]
        def run(job):
            packet = copy.deepcopy(self.packet)
            packet['role'] = job[0]
            return self.save(packet, {'CODEX_THREAD_ID': job[1]})
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(run, jobs))
        self.assertEqual(len({x['scope'] for x in results}), 3)
        self.assertEqual(len({x['proposal'] for x in results}), 3)
        for result, (_, session) in zip(results, jobs):
            candidate = json.loads((self.root / result['candidate']).read_bytes())
            self.assertEqual(candidate['interactive_capture']['session_reference'], session)

    def test_different_projects_never_dedupe(self):
        first = self.save()
        other = self.project.parent / 'other-project'
        other.mkdir()
        (other / 'example.py').write_text('value = 42\n')
        second = stage(self.root, other, self.packet, self.environment)
        self.assertNotEqual(first['scope'], second['scope'])

    def test_unknown_host_identity_is_not_fabricated_session(self):
        result = self.save(environment={})
        note = json.loads((self.root / result['candidate']).read_bytes())
        self.assertEqual(note['interactive_capture']['session_reference_kind'], 'invocation-id-not-a-session-id')

    def test_no_outcomes_and_repeat(self):
        packet = {**self.packet, 'items': [], 'artifacts': []}
        first, second = self.save(packet), self.save(packet)
        self.assertEqual((first['status'], first['proposals']), ('NO_MEANINGFUL_OUTCOMES', 0))
        self.assertNotIn('proposal', first)
        self.assertEqual((second['status'], second['proposals']), ('DUPLICATE_SOURCE', 0))

    def test_quoted_credential_and_report_fields_refused_before_archive(self):
        for field in ('text', 'references'):
            packet = copy.deepcopy(self.packet)
            secret = 'Synthetic example: "password": "fixture-only-secret"'
            packet['items'][0][field] = [secret] if field == 'references' else secret
            with self.assertRaises(ValueError):
                self.save(packet)
        self.assertFalse((self.root / 'archive').exists())

    def test_reviewer_and_planner_cannot_claim_implementation(self):
        for role in ('reviewer', 'planner'):
            packet = copy.deepcopy(self.packet)
            packet['role'] = role
            packet['items'][0].update(kind='work', status='reported-completed')
            with self.assertRaisesRegex(ValueError, 'ROLE_CANNOT'):
                self.save(packet)

    def test_unexecuted_tests_and_next_actions(self):
        packet = copy.deepcopy(self.packet)
        packet['items'][0].update(kind='next-action', status='reported-completed')
        with self.assertRaises(ValueError):
            self.save(packet)
        packet['items'][0].update(kind='test', status='proposed', support='inference')
        self.assertEqual(self.save(packet)['status'], 'STAGED')

    def test_unsupported_result_and_missing_reference(self):
        packet = copy.deepcopy(self.packet)
        packet['items'][0]['support'] = 'unsupported'
        with self.assertRaises(ValueError):
            self.save(packet)
        packet['items'][0].update(status='unknown', references=[])
        with self.assertRaises(ValueError):
            self.save(packet)

    def test_inferred_reported_results_refused_before_any_staging_write(self):
        for status in ('reported-completed', 'reported-passed', 'reported-failed'):
            for support in ('inference', 'unsupported'):
                with self.subTest(status=status, support=support):
                    packet = copy.deepcopy(self.packet)
                    packet.update(role='mixed', coverage='compacted-context', artifacts=[])
                    packet['items'][0].update(
                        kind='decision', status=status, support=support,
                        references=['supplied synthetic compacted checkpoint'])
                    with self.assertRaisesRegex(ValueError, '^RESULT_NEEDS_SUPPORT$'):
                        self.save(packet)
        self.assertEqual({p.name for p in self.root.iterdir()}, {'.private-staging.json'})

    def test_compacted_inferred_decision_stages_without_promoting_evidence(self):
        packet = copy.deepcopy(self.packet)
        packet.update(role='mixed', coverage='compacted-context', artifacts=[])
        packet['items'][0].update(
            kind='decision', text='An inherited synthetic planning preference remains provisional.',
            status='unknown', actor='other-session', support='inference',
            references=['supplied synthetic compacted checkpoint'])
        result = self.save(packet)
        self.assertEqual(result['status'], 'STAGED')
        archive = json.loads((self.root / result['source']).read_bytes())
        self.assertEqual(archive['context_summary'], packet)
        note = json.loads((self.root / result['candidate']).read_bytes())
        self.assertEqual(note['interactive_capture']['coverage'], 'compacted-context')
        self.assertIn('[unknown; actor=other-session; support=inference]', note['claims'][0]['text'])
        self.assertNotIn('reported-completed', note['claims'][0]['text'])

    def test_external_history_credentials_and_traversal_artifacts_refused(self):
        for name in ('../outside.py', '/tmp/outside.py', '.codex/sessions/log.jsonl', '.env', 'auth.json', 'C:/outside.md'):
            with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                self.save({**self.packet, 'artifacts': [name]})

    def test_missing_or_altered_cached_source_candidate_markdown_receipt(self):
        for target in ('source', 'candidate', 'proposal', 'receipt'):
            # Fresh role/session namespace for every negative case.
            environment = {'CODEX_THREAD_ID': 'tamper-' + target}
            result = self.save(environment=environment)
            path = self.root / result[target] if target != 'receipt' else self.root / result['candidate'].replace('candidate.json', 'receipt.json')
            path.write_text('{}')
            with self.assertRaises((ValueError, OSError, KeyError)):
                self.save(environment=environment)

    def test_deleted_markdown_is_not_healed(self):
        result = self.save()
        path = self.root / result['proposal']
        path.unlink()
        with self.assertRaises((ValueError, OSError)):
            self.save()
        self.assertFalse(path.exists())

    def test_artifact_changed_results_in_new_proposal(self):
        first = self.save()
        (self.project / 'example.py').write_text('value = 43\n')
        second = self.save()
        self.assertNotEqual(first['source_sha256'], second['source_sha256'])

    def test_staging_root_boundary(self):
        outside = self.root.parent / 'unmarked'
        outside.mkdir()
        with self.assertRaises((ValueError, OSError)):
            stage(outside, self.project, self.packet, self.environment)
    @unittest.skipIf(os.name == 'nt', 'Native Windows symlink creation needs unavailable privilege; not a passing symlink test')
    def test_symlink_boundary(self):
        linked = self.project / 'redirect.py'
        linked.symlink_to(self.project / 'example.py')
        with self.assertRaises(ValueError):
            self.save({**self.packet, 'artifacts': ['redirect.py']})

    def test_hardlinked_artifact_refused(self):
        linked = self.project / 'linked.py'
        os.link(self.project / 'example.py', linked)
        with self.assertRaises(ValueError):
            self.save({**self.packet, 'artifacts': ['linked.py']})

    def test_cli_failure_contains_no_secret_and_no_model_process(self):
        adapter = Path(__file__).resolve().parents[1] / 'scripts/save_interactive_memory.py'
        result = subprocess.run([sys.executable, '-B', str(adapter), '--root', str(self.root)],
                                input=b'{"password":"fixture-only-secret"}', capture_output=True, cwd=self.project)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(b'fixture-only-secret', result.stderr)
        self.assertEqual(json.loads(result.stderr)['status'], 'REFUSED')


class PublicationAdapterTests(unittest.TestCase):
    def setUp(self):
        import test_publish_memory as fixtures
        self.fixture = fixtures.PublicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def cli(self, flags=None):
        fixture = self.fixture
        adapter = Path(__file__).resolve().parents[1] / 'scripts/save_interactive_memory.py'
        env = {**os.environ, 'CODEX_THREAD_ID': 'synthetic-publication'}
        return subprocess.run([sys.executable, '-B', str(adapter), '--root', str(fixture.root), *(flags or [])],
                              input=json.dumps(fixture.packet).encode(), capture_output=True,
                              cwd=fixture.project, env=env)

    def flags(self):
        return ['--policy', str(self.fixture.policy_path), '--policy-sha256', self.fixture.pin]

    def test_inferred_reported_decision_cli_refuses_before_staging_or_publication(self):
        fixture = self.fixture
        fixture.packet.update(role='mixed', coverage='compacted-context', artifacts=[])
        fixture.packet['items'][0].update(support='inference')
        before = {p.relative_to(fixture.root.parent): p.read_bytes()
                  for p in fixture.root.parent.rglob('*') if p.is_file()}
        process = self.cli(self.flags())
        self.assertEqual(process.returncode, 2)
        self.assertEqual(process.stdout, b'')
        self.assertEqual(json.loads(process.stderr), {
            'status': 'REFUSED', 'error_code': 'RESULT_NEEDS_SUPPORT', 'proposals': 0})
        after = {p.relative_to(fixture.root.parent): p.read_bytes()
                 for p in fixture.root.parent.rglob('*') if p.is_file()}
        self.assertEqual(after, before)

    def test_io_refusal_can_leave_partial_staging_without_publication(self):
        import save_interactive_memory as adapter
        import publish_memory as publisher
        from types import SimpleNamespace
        fixture = self.fixture
        fixture.packet['items'][0]['text'] = 'A second synthetic decision for I/O diagnostics.'
        before = set((fixture.root / 'archive').iterdir())
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(fixture.packet).encode()))
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['adapter', '--root', str(fixture.root), *self.flags()]), \
             patch.object(sys, 'stdin', stdin), patch.object(sys, 'stdout', output), \
             patch.object(sys, 'stderr', errors), \
             patch.object(Path, 'cwd', return_value=fixture.project), \
             patch.object(adapter, 'render', side_effect=PermissionError('PRIVATE_EXCEPTION_CANARY')), \
             patch.object(publisher, 'publish_staged') as publish:
            self.assertEqual(adapter.main(), 2)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(json.loads(errors.getvalue()), {
            'status': 'REFUSED', 'error_code': 'SAVE_REFUSED_IO_OR_FORMAT', 'proposals': 0})
        self.assertEqual(len(set((fixture.root / 'archive').iterdir()) - before), 1)
        self.assertEqual(list(fixture.vault.iterdir()), [])
        publish.assert_not_called()

    def test_without_policy_preserves_exact_legacy_stage_output(self):
        fixture = self.fixture
        expected = stage(fixture.root, fixture.project, fixture.packet, {'CODEX_THREAD_ID': 'synthetic-publication'})
        process = self.cli()
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(process.stdout), expected)
        self.assertEqual(list(fixture.vault.rglob('*.md')), [])

    def test_single_invocation_publishes_and_repeat_retains_stage_receipt(self):
        fixture = self.fixture
        process = self.cli(self.flags())
        self.assertEqual(process.returncode, 0, process.stderr)
        value = json.loads(process.stdout)
        self.assertEqual(value['source_sha256'], fixture.receipt['source_sha256'])
        self.assertEqual(value['publication']['status'], 'SAVED')
        self.assertEqual(value['publication']['counts']['saved'], 1)
        self.assertEqual(value['status'], 'DUPLICATE_SOURCE')
        again = self.cli(self.flags())
        self.assertEqual(again.returncode, 0)
        self.assertEqual(json.loads(again.stdout)['publication']['status'], 'NO_CHANGE')

    def test_disabled_and_refused_publication_preserve_actual_staging(self):
        fixture = self.fixture
        fixture.policy['enabled'] = False
        fixture.pin_policy()
        process = self.cli(self.flags())
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(process.stdout)['publication']['status'], 'DISABLED')
        flags = ['--policy', str(fixture.root / 'PRIVATE_PATH_CANARY.json'), '--policy-sha256', '0' * 64]
        process = self.cli(flags)
        self.assertEqual(process.returncode, 2)
        value = json.loads(process.stdout)
        self.assertEqual(value['status'], 'DUPLICATE_SOURCE')
        self.assertEqual(value['source_sha256'], fixture.receipt['source_sha256'])
        self.assertIn(value['publication']['status'], ('REFUSED', 'PARTIAL'))
        self.assertNotIn(b'PRIVATE_PATH_CANARY', process.stdout + process.stderr)

    def test_publication_conflict_keeps_staged_receipt_and_nonzero_exit(self):
        fixture = self.fixture
        self.assertEqual(self.cli(self.flags()).returncode, 0)
        target = next(fixture.vault.rglob('*.md'))
        target.write_bytes(b'SYNTHETIC_OWNER_EDIT')
        process = self.cli(self.flags())
        self.assertEqual(process.returncode, 2)
        value = json.loads(process.stdout)
        self.assertEqual(value['publication']['status'], 'QUARANTINED')
        self.assertEqual(value['source_sha256'], fixture.receipt['source_sha256'])
        self.assertEqual(target.read_bytes(), b'SYNTHETIC_OWNER_EDIT')

    def test_paired_host_flags_required(self):
        for flags in [['--policy', str(self.fixture.policy_path)], ['--policy-sha256', self.fixture.pin]]:
            with self.subTest(flags=flags):
                process = self.cli(flags)
                self.assertEqual(process.returncode, 2)
                self.assertEqual(list(self.fixture.vault.rglob('*.md')), [])

    def test_partial_and_busy_keep_staging_and_uncertain_exception_is_redacted(self):
        import save_interactive_memory as adapter
        import publish_memory as publisher
        from types import SimpleNamespace
        for status in ['PARTIAL', 'BUSY', 'REFUSED', 'OS_ERROR']:
            output = io.StringIO()
            stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(self.fixture.packet).encode()))
            options = {'side_effect': OSError('PRIVATE_EXCEPTION_CANARY')} if status == 'OS_ERROR' else {
                'return_value': publisher.result(status, 'synthetic-transaction', saved=int(status == 'PARTIAL'))}
            with self.subTest(status=status), patch.object(sys, 'argv', ['adapter', '--root', str(self.fixture.root), *self.flags()]), \
                 patch.object(sys, 'stdin', stdin), patch.object(sys, 'stdout', output), \
                 patch.object(Path, 'cwd', return_value=self.fixture.project), \
                 patch.dict(os.environ, {'CODEX_THREAD_ID': 'synthetic-publication'}), \
                 patch.object(publisher, 'publish_staged', **options):
                self.assertEqual(adapter.main(), 2)
                value = json.loads(output.getvalue())
                self.assertEqual(value['source_sha256'], self.fixture.receipt['source_sha256'])
                self.assertEqual(value['publication']['status'], 'PARTIAL' if status == 'OS_ERROR' else status)
                self.assertNotIn('PRIVATE_EXCEPTION_CANARY', output.getvalue())


if __name__ == '__main__':
    unittest.main()
