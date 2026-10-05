"""Registered native reviews use deterministic local processes, never models."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlparse

from loop_engineering.cli import main
from loop_engineering.contracts import ContractError, ROOT, load
from loop_engineering.native_engine import NativeController
from loop_engineering import native_review as review
from loop_engineering.team_host import TeamCodexDriver
from loop_engineering.team_automation import REVIEW_RESPONSE
from loop_engineering.workspace import byte_digest
from tests import test_native_host as fixture


LOCAL_REVIEW = r'''
import json, pathlib, sys, time
directory, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
raw = sys.stdin.buffer.read()
request = json.loads(raw)
(directory / 'observed-request.json').write_bytes(raw)
(directory / 'started').write_text('owned deterministic fixture')
if mode == 'slow':
    time.sleep(10)
if mode == 'mutation':
    pathlib.Path('slug.py').write_text('# forbidden fixture mutation\n')
finding = {'id': 'fixture-blocker', 'severity': 'blocking', 'criterion_id': 'reviewed',
           'description': 'Offline fixture finding'}
response = {'result': 'pass', 'summary': 'Actual deterministic subprocess reviewed the fixture packet', 'findings': []}
if mode == 'fail':
    response.update(result='fail', findings=[finding])
if mode == 'blocking-pass':
    response['findings'] = [finding]
if mode == 'inconclusive':
    response['result'] = 'inconclusive'
if mode == 'invalid':
    response = {'result': 'pass'}
(directory / 'response.json').write_text(json.dumps(response))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 7, 'output_tokens': 2}}), flush=True)
'''


class NativeReviewTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start
    complete = fixture.NativeHostTests.complete
    receive = fixture.NativeHostTests.receive
    prepare_review = fixture.NativeHostTests.prepare_review

    def register(self, **kwargs):
        return review.register(self.store, self.root, executable=sys.executable, **kwargs)

    def child(self):
        team = self.service.team.teams.get(self.team_id)
        return self.store.get(team['records']['verify']['child_run_id'])

    def attempts(self):
        return self.child()['native'].get('execution_attempts', {})

    def driver(self, mode='pass'):
        def factory(schema, **kwargs):
            driver = TeamCodexDriver(schema, **kwargs)
            def command(work, artifacts):
                return [sys.executable, '-B', '-c', LOCAL_REVIEW, str(artifacts), mode]
            driver.command = command
            return driver
        return patch.object(review, 'TeamCodexDriver', side_effect=factory)

    def actual(self, mode='pass', *, retry=False):
        with self.driver(mode) as driver:
            result = self.service.execute_evaluation(self.team_id, 'verify', 'external', retry=retry)
        return result, driver.call_count

    def packet(self):
        attempt = list(self.attempts().values())[-1]
        path = self.store.run_dir(self.child()['run_id']) / attempt['id'] / 'observed-request.json'
        return json.loads(path.read_bytes()), path.read_bytes()

    def test_registration_is_private_idempotent_and_dispatches_nothing(self):
        files = {str(p.relative_to(self.root)): p.read_bytes() for p in (self.root / '.loop').rglob('*') if p.is_file()}
        with patch('subprocess.Popen', side_effect=AssertionError('Registration cannot dispatch')):
            first = self.register(model='offline-fixture')
            self.assertEqual(self.register(model='offline-fixture'), first)
            self.assertTrue(review.status(self.store, self.root)['available'])
        self.assertEqual(files, {str(p.relative_to(self.root)): p.read_bytes()
                                for p in (self.root / '.loop').rglob('*') if p.is_file()})
        _, path = review._location(self.store, self.root)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(path.is_relative_to(self.root))
        self.assertEqual(first['executable'], str(Path(sys.executable).resolve()))
        self.assertEqual(self.owner.transport.requests, [])

    def test_cli_registration_and_status_do_not_start_a_process(self):
        for command in ('host-review-register', 'host-review-status'):
            argv = [command, str(self.root), '--state-dir', str(self.store.directory)]
            if command.endswith('register'):
                argv += ['--adapter', 'codex', '--executable', sys.executable]
            output = io.StringIO()
            with patch('subprocess.Popen', side_effect=AssertionError('CLI registration is offline')), \
                    patch('sys.stdout', output):
                self.assertEqual(main(argv), 0)
            result = json.loads(output.getvalue())
            if command.endswith('status'):
                self.assertFalse(result['model_dispatched'])
                self.assertEqual(result['registration']['adapter'], 'codex')
            else:
                self.assertEqual(result['adapter'], 'codex')

    def test_registry_tamper_is_never_overwritten_or_dispatched(self):
        self.register()
        _, path = review._location(self.store, self.root)
        envelope = json.loads(path.read_text())
        envelope['payload']['timeout_seconds'] += 1
        path.write_text(json.dumps(envelope))
        with patch('subprocess.Popen', side_effect=AssertionError('Untrusted registry cannot dispatch')):
            self.assertFalse(review.status(self.store, self.root)['available'])
            with self.assertRaises(ContractError):
                self.register()
        self.assertEqual(json.loads(path.read_text()), envelope)

    def test_changed_executable_is_rejected_and_can_be_explicitly_reregistered(self):
        executable = self.owner.base / 'offline-executable'
        executable.write_text('#!/bin/sh\nexit 0\n'); executable.chmod(0o700)
        with patch('subprocess.Popen', side_effect=AssertionError('Executable identity is only read')):
            first = review.register(self.store, self.root, executable=str(executable))
            executable.write_text('#!/bin/sh\nexit 1\n')
            self.assertFalse(review.status(self.store, self.root)['available'])
            with self.assertRaisesRegex(ContractError, 'executable changed'):
                review.registration(self.store, self.root)
            refreshed = review.register(self.store, self.root, executable=str(executable))
            self.assertNotEqual(first['digest'], refreshed['digest'])

    def test_identity_cache_is_stat_bound_and_dispatch_verification_forces_bytes(self):
        executable = self.owner.base / 'identity-fixture'
        executable.write_text('#!/bin/sh\nexit 0\n'); executable.chmod(0o700)
        review._IDENTITIES.clear()
        original = Path.read_bytes
        reads = []
        def read(path):
            if path == executable:
                reads.append(path)
            return original(path)
        with patch.object(Path, 'read_bytes', read):
            review.register(self.store, self.root, executable=str(executable))
            count = len(reads)
            review.registration(self.store, self.root)
            review.registration(self.store, self.root)
            self.assertEqual(len(reads), count)
            review.registration(self.store, self.root, force=True)
            self.assertEqual(len(reads), count + 1)

    def test_registration_validation_and_existing_authority_are_required(self):
        for value in ({'adapter': 'claude'}, {'timeout_seconds': 0}, {'max_attempts': 9}, {'model': ''}):
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.register(**value)
        self.register()
        self.prepare_review()
        child = self.child()
        self.assertIsNone(review.executor_for(self.service, child, 'suite'))
        self.assertEqual(review.executor_for(self.service, child, 'external')['kind'], 'registered_review')
        child['native']['config']['evaluator_keys'] = []
        self.assertIsNone(review.executor_for(self.service, child, 'external'))

    def test_real_cli_command_is_fresh_read_only_and_ignores_user_configuration(self):
        directory = self.owner.base / 'argv'; directory.mkdir()
        work = directory / 'workspace'; work.mkdir()
        argv = TeamCodexDriver(REVIEW_RESPONSE, executable=sys.executable).command(work, directory)
        for value in ('--no-daemon', '--ephemeral', '--ignore-user-config', '--json', '--output-schema'):
            self.assertIn(value, argv)
        self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')
        self.assertEqual(argv[argv.index('--ask-for-approval') + 1], 'never')
        self.assertNotIn('resume', argv)
        self.assertEqual(argv[-1], '-')

    def test_actual_fixture_review_signs_independent_evidence_and_preserves_final_gate(self):
        self.register()
        self.prepare_review()
        frozen = {name: (self.root / name).read_bytes() for name in
                  ('.loop/tasks/verify.json', '.loop/project.json', '.loop/review-engine.json')}
        result, count = self.actual()
        self.assertEqual(count, 1)
        self.assertEqual(result['tasks']['verify']['status'], 'HANDOFF')
        child = self.child()
        digest = child['native']['external_by_check']['external']
        evidence = self.store.evidence(child['run_id'], [digest])[0]
        self.assertEqual(evidence['result'], 'pass')
        self.assertNotEqual(evidence['executor_run_id'], child['native']['implementer_context_id'])
        for artifact in evidence['artifacts']:
            self.assertTrue(Path(unquote(urlparse(artifact['uri']).path)).is_file())
        self.assertEqual(child['known_tokens'], 9)
        self.assertIsNone(child['state']['usage']['tokens'])
        self.assertIsNone(child['state']['usage']['cost_microunits'])
        self.assertTrue(self.receive('verify')['final_candidate_current'])
        self.assertEqual(frozen, {name: (self.root / name).read_bytes() for name in frozen})
        self.assertEqual(self.owner.transport.requests, [])

    def test_packet_keeps_contract_procedure_rules_and_never_sends_signing_keys(self):
        self.register()
        self.prepare_review()
        self.actual()
        packet, raw = self.packet()
        child = self.child()
        self.assertEqual(packet['task_contract'], child['task'])
        self.assertEqual(packet['request']['procedure'], child['task']['checks'][-1]['procedure'])
        self.assertTrue(packet['repository_instructions'])
        self.assertTrue(packet['inspection']['scenario']['instructions'])
        self.assertIn('rg, cat, ls, and sed', packet['instruction'])
        self.assertIn('Do not execute project code, tests, installers', packet['instruction'])
        self.assertIn('independently of implementer conversations', packet['instruction'])
        self.assertNotIn(child['native']['implementer_context_id'].encode(), raw)
        self.assertNotIn(str(self.store.directory).encode(), raw)
        self.assertNotIn('secret', packet)
        self.assertEqual(raw, review.context_bytes(packet))

    def test_large_manifests_are_bound_by_digest_and_actual_utf8_packet_fits_frozen_limit(self):
        profile = load(self.root / '.loop/project.json')
        profile['context_max_bytes'] = 320000
        (self.root / '.loop/project.json').write_text(json.dumps(profile))
        assets = self.root / 'assets'; assets.mkdir()
        for index in range(710):
            (assets / f'{index:04}.txt').write_text('源文件内容' * 64)
        self.register()
        self.prepare_review()
        self.actual()
        packet, raw = self.packet()
        self.assertLessEqual(len(raw), 320000)
        self.assertNotIn('manifest', packet['comparison_and_checks']['initial_snapshot'])
        self.assertGreaterEqual(packet['comparison_and_checks']['initial_snapshot']['file_count'], 710)
        self.assertNotIn('source_manifest', packet['inspection'])
        self.assertGreaterEqual(packet['inspection']['source_coverage']['file_count'], 710)
        originals = packet['comparison_and_checks']['original_changed_sources']
        self.assertTrue(any(item['path'] == 'slug.py' and item['content'] for item in originals))
        self.assertIn('slug.py', packet['comparison_and_checks']['observed_changed_paths'])

    def test_frozen_native_request_response_and_timeout_limits_are_enforced(self):
        profile = load(self.root / '.loop/project.json')
        profile['context_max_bytes'] = 320000
        (self.root / '.loop/project.json').write_text(json.dumps(profile))
        assets = self.root / 'assets'; assets.mkdir()
        for index in range(80):
            (assets / f'{index:03}.txt').write_text('待审查内容' * 200)
        original_load = fixture.load
        def limits(path, *args):
            result = original_load(path, *args)
            if path == ROOT / 'templates/engine.json':
                result['response_limits'].update(max_request_bytes=120000, max_response_bytes=4096, timeout_seconds=3)
            return result
        self.register(timeout_seconds=10)
        with patch.object(fixture, 'load', side_effect=limits):
            self.prepare_review()
        original_launch = NativeController.launch
        observed = []
        def launch(controller, data, argv, cwd, logs, **kwargs):
            if kwargs['kind'] == 'evaluator':
                observed.append(kwargs)
            return original_launch(controller, data, argv, cwd, logs, **kwargs)
        with patch.object(NativeController, 'launch', launch):
            result, _ = self.actual()
        self.assertEqual(result['tasks']['verify']['status'], 'HANDOFF')
        self.assertEqual(observed[0]['max_output_bytes'], 4096)
        self.assertEqual(observed[0]['timeout'], 3)
        packet, raw = self.packet()
        self.assertLessEqual(len(raw), 120000)
        self.assertGreater(packet.get('omitted_source_excerpts', 0), 0)
        self.assertEqual(packet['task_contract'], self.child()['task'])

    def test_review_json_above_frozen_response_bound_is_not_signed(self):
        original_load = fixture.load
        def limits(path, *args):
            result = original_load(path, *args)
            if path == ROOT / 'templates/engine.json':
                result['response_limits']['max_response_bytes'] = 96
            return result
        self.register()
        with patch.object(fixture, 'load', side_effect=limits):
            self.prepare_review()
        with self.driver():
            with self.assertRaisesRegex(ContractError, 'frozen response byte limit'):
                review.run(self.service, self.team_id, 'verify', 'external')
        self.assertFalse(self.child()['native']['external_by_check'])

    def test_literal_private_paths_in_task_rules_and_source_preserve_exact_bindings(self):
        literal = str(self.store.directory)
        (self.root / 'README.md').write_text('Document the literal fixture path: ' + literal + '\n')
        (self.root / 'AGENTS.md').write_text('Fixture literal instruction path: ' + literal + '\n')
        contract = load(self.root / '.loop/tasks/verify.json')
        contract['objective'] += ' Preserve literal fixture path ' + literal
        (self.root / '.loop/tasks/verify.json').write_text(json.dumps(contract))
        self.register()
        self.prepare_review()
        controller, run_id = self.service._evaluator(self.team_id, 'verify')
        child = self.child()
        request = controller.evaluation_request(run_id, 'external')
        snapshot = controller.snapshots.capture(self.root, child['profile'])
        packet = review._packet(self.service, self.service._active(self.team_id), controller, child, request, snapshot)
        self.assertEqual(packet['task_contract'], child['task'])
        self.assertIn(literal, packet['task_contract']['objective'])
        self.assertTrue(any(literal in item['content'] for item in packet['repository_instructions']))
        source = next(item for item in packet['inspection']['sources'] if item['path'] == 'README.md')
        self.assertEqual(source['content'], (self.root / 'README.md').read_text())
        self.assertEqual(source['sha256'], byte_digest(source['content'].encode()))

    def test_fail_and_inconclusive_results_are_real_cached_judgments_not_completion(self):
        self.register()
        self.prepare_review()
        result, count = self.actual('fail')
        self.assertEqual(count, 1)
        self.assertIsNone(result['tasks']['verify']['handoff'])
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('Cached failure is not redispatched')):
            record = review.run(self.service, self.team_id, 'verify', 'external')
        self.assertEqual(record['result'], 'fail')
        self.assertEqual(record['extensions']['findings'][0]['severity'], 'blocking')

    def test_explicit_retry_keeps_same_child_findings_and_cumulative_attempt_limit(self):
        self.register(max_attempts=2)
        self.prepare_review()
        run_id = self.child()['run_id']
        self.actual('fail')
        first = self.child()['native']['external_by_check']['external']
        result, count = self.actual('inconclusive', retry=True)
        self.assertEqual(count, 1)
        self.assertIsNone(result['tasks']['verify']['handoff'])
        child = self.child()
        self.assertEqual(child['run_id'], run_id)
        self.assertIn(first, child['native']['external_origins'])
        self.assertEqual(child['known_tokens'], 18)
        packet, _ = self.packet()
        self.assertTrue(any(item['finding']['id'] == 'fixture-blocker' for item in packet['pending_findings']))
        contexts = [item['request']['payload']['context_id'] for attempt in self.attempts().values()
                    for item in [*attempt.get('history', []), attempt]]
        self.assertEqual(len(set(contexts)), 2)
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('No unbounded retry')):
            with self.assertRaisesRegex(ContractError, 'attempt limit exhausted'):
                review.run(self.service, self.team_id, 'verify', 'external', retry=True)

    def test_pending_findings_bind_both_environment_and_snapshot_and_keep_history(self):
        self.register()
        self.prepare_review()
        self.actual('fail')
        controller, run_id = self.service._evaluator(self.team_id, 'verify')
        child = self.child()
        request = controller.evaluation_request(run_id, 'external')
        snapshot = controller.snapshots.capture(self.root, child['profile'])
        child['native']['runtime_probe']['identity'] = 'offline-changed-runtime-identity'
        packet = review._packet(self.service, self.service._active(self.team_id), controller, child, request, snapshot)
        self.assertTrue(packet['pending_findings'])
        finding = packet['pending_findings'][0]
        self.assertFalse(finding['current'])
        self.assertEqual(finding['snapshot_digest'], snapshot['digest'])
        self.assertNotEqual(finding['environment_digest'], controller.environment_digest(child))
        self.assertFalse(finding['superseded'])

    def test_timeout_is_retained_default_cached_then_explicitly_retried(self):
        self.register(timeout_seconds=1, max_attempts=2)
        self.prepare_review()
        result, _ = self.actual('slow')
        self.assertIsNone(result['tasks']['verify']['handoff'])
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('Timeout stays cached')):
            record = review.run(self.service, self.team_id, 'verify', 'external')
        self.assertEqual(record['result'], 'inconclusive')
        self.assertIn('timeout', record['summary'])
        refreshed = self.register(timeout_seconds=2, max_attempts=2)
        self.assertTrue(refreshed)
        result, count = self.actual(retry=True)
        self.assertEqual(count, 1)
        self.assertEqual(result['tasks']['verify']['status'], 'HANDOFF')
        self.assertEqual(sum(1 + len(a.get('history', [])) for a in self.attempts().values()), 2)

    def test_invalid_judgment_or_modified_review_copy_never_becomes_evidence(self):
        self.register()
        self.prepare_review()
        original = (self.root / 'slug.py').read_bytes()
        with self.driver('invalid'):
            with self.assertRaisesRegex(ContractError, 'invalid structured judgment'):
                review.run(self.service, self.team_id, 'verify', 'external')
        self.assertFalse(self.child()['native']['external_by_check'])
        self.assertTrue(any(a['status'] == 'RUNNING' for a in self.attempts().values()))
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('Unresolved effect cannot repeat')):
            with self.assertRaisesRegex(ContractError, 'unresolved'):
                review.run(self.service, self.team_id, 'verify', 'external')
        self.assertEqual((self.root / 'slug.py').read_bytes(), original)

    def test_copy_mutation_is_detected_and_main_candidate_is_preserved(self):
        self.register()
        self.prepare_review()
        original = (self.root / 'slug.py').read_bytes()
        with self.driver('mutation'):
            with self.assertRaisesRegex(ContractError, 'modified its read-only copy'):
                review.run(self.service, self.team_id, 'verify', 'external')
        self.assertEqual((self.root / 'slug.py').read_bytes(), original)
        self.assertFalse(self.child()['native']['external_by_check'])

    def test_blocking_findings_cannot_be_signed_as_pass(self):
        self.register()
        self.prepare_review()
        with self.driver('blocking-pass'):
            with self.assertRaisesRegex(ContractError, 'blocking finding'):
                review.run(self.service, self.team_id, 'verify', 'external')
        self.assertFalse(self.child()['native']['external_by_check'])

    def test_unresolved_dispatch_needs_original_reconciliation_and_keeps_unknown_usage(self):
        self.register(max_attempts=2)
        self.prepare_review()
        run_id = self.child()['run_id']
        with self.driver(), patch.object(NativeController, 'launch', side_effect=OSError('Offline unknown dispatch')):
            with self.assertRaises(OSError):
                review.run(self.service, self.team_id, 'verify', 'external')
        attempt = list(self.attempts().values())[0]
        self.assertEqual(attempt['status'], 'RUNNING')
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('No retry of unknown effect')):
            with self.assertRaisesRegex(ContractError, 'unresolved'):
                review.run(self.service, self.team_id, 'verify', 'external')
        controller, _ = self.service._evaluator(self.team_id, 'verify')
        controller.reconcile_process(run_id, attempt['id'], 'Inspected the deterministic fixture; no earlier process exists')
        controller.resume(run_id); controller.verify(run_id)
        result, count = self.actual()
        self.assertEqual(count, 1)
        self.assertEqual(result['tasks']['verify']['status'], 'HANDOFF')
        self.assertEqual(self.child()['run_id'], run_id)
        latest = list(self.attempts().values())[0]
        self.assertEqual(len(latest['history']), 1)
        self.assertEqual(self.child()['native']['reservations'][0]['status'], 'unknown')
        self.assertIsNone(self.child()['state']['usage']['tokens'])

    def test_preparation_time_is_persisted_and_exhaustion_prevents_paid_dispatch(self):
        self.register()
        self.prepare_review()
        run_id = self.child()['run_id']
        with self.store.writer(run_id) as data:
            data['elapsed_ms'] = data['task']['limits']['max_wall_seconds'] * 1000 - 2000
            data['state']['usage']['wall_seconds'] = data['elapsed_ms'] // 1000
            self.store.save(data, 'fixture.near_time_limit')
        original = review._packet
        def slow_packet(*args):
            current = self.child()
            self.assertIsNotNone(current['operation_started_ms'])
            self.assertIn('review_preparation', current['native'])
            time.sleep(2.1)
            return original(*args)
        with patch.object(review, '_packet', side_effect=slow_packet), \
                patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('Preparation used the remaining wall budget')):
            with self.assertRaises(ContractError):
                review.run(self.service, self.team_id, 'verify', 'external')
        child = self.child()
        self.assertGreaterEqual(child['elapsed_ms'], child['task']['limits']['max_wall_seconds'] * 1000)
        self.assertIsNone(child['operation_started_ms'])
        self.assertNotIn('review_preparation', child['native'])
        self.assertFalse(self.attempts())

    def test_preparation_crash_requires_recovery_and_charges_gap(self):
        self.register()
        self.prepare_review()
        controller, run_id = self.service._evaluator(self.team_id, 'verify')
        started = review._start_preparation(controller, run_id)
        with self.store.writer(run_id) as data:
            data['operation_started_ms'] -= 2500
            self.store.save(data, 'fixture.crashed_preparation')
        before = self.child()['elapsed_ms']
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('Interrupted preparation cannot dispatch')):
            with self.assertRaisesRegex(ContractError, 'unresolved'):
                review.run(self.service, self.team_id, 'verify', 'external')
        controller.resume(run_id)
        self.assertGreaterEqual(self.child()['elapsed_ms'] - before, 2500)
        controller.verify(run_id)
        self.assertEqual(self.actual()[0]['tasks']['verify']['status'], 'HANDOFF')
        self.assertNotIn('review_preparation', self.child()['native'])

    def test_parallel_preparations_cannot_dispatch_twice_or_reset_attempt_history(self):
        self.register(max_attempts=2)
        self.prepare_review()
        ready, release = threading.Event(), threading.Event()
        original = review._packet
        def blocked_packet(*args):
            ready.set()
            self.assertTrue(release.wait(6))
            return original(*args)
        with self.driver() as driver, patch.object(review, '_packet', side_effect=blocked_packet), \
                ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(review.run, self.service, self.team_id, 'verify', 'external')
            self.assertTrue(ready.wait(6))
            try:
                with self.assertRaisesRegex(ContractError, 'unresolved'):
                    review.run(self.service, self.team_id, 'verify', 'external')
            finally:
                release.set()
            self.assertEqual(future.result(timeout=6)['result'], 'pass')
        self.assertEqual(driver.call_count, 1)
        self.assertEqual(sum(1 + len(a.get('history', [])) for a in self.attempts().values()), 1)

    def test_cached_signed_result_recovers_import_without_a_second_process(self):
        self.register()
        self.prepare_review()
        with self.driver(), patch.object(NativeController, 'import_evaluation', side_effect=OSError('Offline import interruption')):
            with self.assertRaises(OSError):
                review.run(self.service, self.team_id, 'verify', 'external')
        attempt = list(self.attempts().values())[0]
        self.assertEqual(attempt['status'], 'RESULT')
        self.assertIsNone(self.child()['pending_process'])
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('Recovery replays original result')):
            record = review.run(self.service, self.team_id, 'verify', 'external')
        self.assertEqual(record['result'], 'pass')
        self.assertEqual(self.child()['known_tokens'], 9)

    def test_corrupt_cached_artifact_is_rejected_without_another_dispatch(self):
        self.register()
        self.prepare_review()
        with self.driver(), patch.object(NativeController, 'import_evaluation', side_effect=OSError('Offline interruption')):
            with self.assertRaises(OSError):
                review.run(self.service, self.team_id, 'verify', 'external')
        attempt = list(self.attempts().values())[0]
        artifact = attempt['envelope']['payload']['artifacts'][0]
        Path(unquote(urlparse(artifact['uri']).path)).write_text('tampered fixture result')
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('Tamper cannot cause redispatch')):
            with self.assertRaisesRegex(ContractError, 'artifact bytes changed'):
                review.run(self.service, self.team_id, 'verify', 'external')

    def test_cached_result_crash_gap_is_recovered_before_import(self):
        self.register()
        self.prepare_review()
        with self.driver(), patch.object(NativeController, 'import_evaluation', side_effect=OSError('Offline import interruption')):
            with self.assertRaises(OSError):
                review.run(self.service, self.team_id, 'verify', 'external')
        run_id = self.child()['run_id']
        with self.store.writer(run_id) as data:
            data['operation_started_ms'] = time.time_ns() // 1000000 - 2500
            self.store.save(data, 'fixture.cached_result_crash_gap')
        before = self.child()['elapsed_ms']
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('Cached crash does not redispatch')):
            record = review.run(self.service, self.team_id, 'verify', 'external')
        self.assertEqual(record['result'], 'pass')
        self.assertGreaterEqual(self.child()['elapsed_ms'] - before, 2500)
        self.assertIsNone(self.child()['operation_started_ms'])

    def test_candidate_change_after_actual_review_prevents_signing(self):
        self.register()
        self.prepare_review()
        original_launch = NativeController.launch
        def launch(controller, data, argv, cwd, logs, **kwargs):
            result = original_launch(controller, data, argv, cwd, logs, **kwargs)
            if kwargs['kind'] == 'evaluator':
                (self.root / 'slug.py').write_text('# fixture candidate changed after review\n')
            return result
        with self.driver(), patch.object(NativeController, 'launch', launch):
            with self.assertRaisesRegex(ContractError, 'candidate/environment changed'):
                review.run(self.service, self.team_id, 'verify', 'external')
        self.assertFalse(self.child()['native']['external_by_check'])

    def test_stop_before_dispatch_cannot_consume_or_start_a_review(self):
        self.register()
        self.prepare_review()
        self.service.control(self.team_id, 'pause')
        with patch('subprocess.Popen', side_effect=AssertionError('Paused review cannot start')):
            with self.assertRaisesRegex(ContractError, 'stopped'):
                review.run(self.service, self.team_id, 'verify', 'external')
        self.assertFalse(self.attempts())

    def test_pause_during_process_caches_inconclusive_usage_and_requires_explicit_resume(self):
        self.register(timeout_seconds=10)
        self.prepare_review()
        with self.driver('slow'), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(review.run, self.service, self.team_id, 'verify', 'external')
            deadline = time.monotonic() + 6
            started = False
            while time.monotonic() < deadline:
                attempts = self.attempts()
                if attempts:
                    attempt = list(attempts.values())[0]
                    marker = self.store.run_dir(self.child()['run_id']) / attempt['id'] / 'started'
                    if marker.exists():
                        started = True; break
                time.sleep(.02)
            self.assertTrue(started, 'Owned local fixture did not start')
            self.service.team.teams.signal(self.team_id, 'PAUSED')
            with self.assertRaisesRegex(ContractError, 'stopped'):
                future.result(timeout=6)
        child = self.child()
        attempt = list(self.attempts().values())[0]
        self.assertEqual(attempt['status'], 'RESULT')
        self.assertEqual(attempt['envelope']['payload']['result'], 'inconclusive')
        self.assertEqual(attempt['outcome'], 'cancelled')
        self.assertIsNone(attempt['tokens'])
        self.assertFalse(child['native']['external_by_check'])
        self.assertIsNone(child['pending_process'])
        self.assertGreater(child['elapsed_ms'], 0)
        self.assertEqual(child['native']['reservations'][-1]['status'], 'unknown')
        self.service.control(self.team_id, 'resume')
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('Resume consumes cached actual result')):
            record = review.run(self.service, self.team_id, 'verify', 'external')
        self.assertEqual(record['result'], 'inconclusive')

    def test_cancel_during_process_keeps_actual_result_and_cannot_resume_or_import(self):
        self.register(timeout_seconds=10)
        self.prepare_review()
        with self.driver('slow'), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(review.run, self.service, self.team_id, 'verify', 'external')
            deadline = time.monotonic() + 6
            started = False
            while time.monotonic() < deadline:
                attempts = self.attempts()
                if attempts:
                    attempt = list(attempts.values())[0]
                    marker = self.store.run_dir(self.child()['run_id']) / attempt['id'] / 'started'
                    if marker.exists():
                        started = True; break
                time.sleep(.02)
            self.assertTrue(started)
            self.service.team.teams.signal(self.team_id, 'CANCELLED')
            with self.assertRaisesRegex(ContractError, 'stopped'):
                future.result(timeout=6)
        attempt = list(self.attempts().values())[0]
        self.assertEqual(attempt['status'], 'RESULT')
        self.assertEqual(attempt['envelope']['payload']['result'], 'inconclusive')
        self.assertEqual(attempt['outcome'], 'cancelled')
        self.assertFalse(self.child()['native']['external_by_check'])
        self.service.control(self.team_id, 'cancel')
        with self.assertRaises(ContractError):
            self.service.control(self.team_id, 'resume')
        with patch.object(review, 'TeamCodexDriver', side_effect=AssertionError('Terminal cancellation cannot restart review')):
            with self.assertRaisesRegex(ContractError, 'stopped'):
                review.run(self.service, self.team_id, 'verify', 'external')


if __name__ == '__main__':
    unittest.main()
