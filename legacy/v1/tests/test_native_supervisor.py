"""Bounded continuing host turns, real command gates and interruption fixtures."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from loop_engineering.cli import parser
from loop_engineering.contracts import ContractError, ROOT
from loop_engineering.native_host import NativeHostService, render_progress
from loop_engineering.native_supervisor import CodexSupervisorDriver, NativeSupervisor, codex_events, read_report, reconnect_supervisor
from loop_engineering.processes import ProcessResult
from tests import test_native_workbench as fixture
from tests import test_native_project as project_fixture
from tests import test_native_preflight as preflight_fixture

SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class FixtureDriver(CodexSupervisorDriver):
    def __init__(self):
        self.executable, self.model = sys.executable, None
        self.calls = []

    def preflight(self, approval_mode):
        return {"approval_mode": approval_mode, "sandbox": "workspace-write", "route_available": True,
                "execution_capability_verified": False, "basis": "Deterministic offline fixture"}

    def command(self, service, data, artifacts, state):
        argv = super().command(service, data, artifacts, state)
        self.calls.append((deepcopy(data), argv))
        return [sys.executable, str(ROOT / "tests/fixtures/supervisor_codex.py")] + argv[1:]


class NativeSupervisorTests(unittest.TestCase):
    setUp = fixture.NativeWorkbenchTests.setUp
    start = fixture.NativeWorkbenchTests.start
    prepare = fixture.NativeWorkbenchTests.prepare
    edit = fixture.NativeWorkbenchTests.edit
    submit = fixture.NativeWorkbenchTests.submit
    complete = fixture.NativeWorkbenchTests.complete
    receive = fixture.NativeWorkbenchTests.receive
    finish = project_fixture.NativeProjectTests.finish
    queue = project_fixture.NativeProjectTests.queue
    accepted = project_fixture.NativeProjectTests.accepted

    def controller(self, executor=None, sleeper=None, **kwargs):
        self.driver = FixtureDriver()
        options = dict(driver=self.driver, clock=lambda: self.tick,
                       sleeper=sleeper or self.advance)
        if executor:
            options["executor"] = executor
        return NativeSupervisor(self.service, **options, **kwargs)

    def advance(self, seconds):
        self.tick += seconds

    def create(self, supervisor, **kwargs):
        result = supervisor.create(self.team_id, host_idle_confirmed=True,
                                   max_wall_seconds=60, turn_timeout_seconds=30, **kwargs)
        self.supervisor_id = result["id"]
        return result

    def executor(self, actor=lambda: None, *, report=None, session=SESSION, outcome="completed", exit_code=0, usage=None):
        def run(argv, cwd, logs, **kwargs):
            actor()
            logs.mkdir(parents=True)
            stdout, stderr = logs / "stdout.txt", logs / "stderr.txt"
            stdout.write_text(json.dumps({"type": "thread.started", "thread_id": session}) + "\n" +
                              json.dumps({"type": "turn.completed", "usage": usage if usage is not None else
                                          {"input_tokens": 10, "output_tokens": 5}}) + "\n")
            stderr.write_text("")
            (logs.parent / "report.json").write_text(json.dumps(report or {
                "status": "checkpoint", "summary": "Actual fixture checkpoint", "blocker": None, "questions": []}))
            self.tick += .1
            return ProcessResult(exit_code, outcome, 100, stdout, stderr)
        return run

    def test_real_local_host_subprocesses_continue_same_session_to_actual_completion(self):
        self.start()
        supervisor = self.controller()
        initial = self.create(supervisor)
        result = supervisor.run(initial["id"])
        diagnostics = list(self.store.run_dir(initial["id"]).glob("turn-*/logs/stderr.txt"))
        self.assertEqual(result["status"], "COMPLETE", result["reason"] + "\n" + "\n".join(p.read_text() for p in diagnostics))
        self.assertEqual(result["turns"], 4)
        self.assertEqual(result["session_id"], SESSION)
        self.assertEqual(result["known_tokens"], 60)
        self.assertTrue(result["usage_complete"])
        self.assertGreater(result["dispatch_ms"], 0)
        state = self.service.progress(self.team_id)
        self.assertEqual(state["status"], "COMPLETE")
        self.assertTrue(state["final_candidate_current"])
        self.assertTrue(all(c["result"] == "pass" for c in state["check_results"]))
        self.assertEqual(self.driver.calls[0][0]["session_id"], None)
        self.assertTrue(all(call[0]["session_id"] == SESSION for call in self.driver.calls[1:]))
        supervisor.journal.audit(initial["id"])
        self.assertEqual(self.owner.transport.requests, [])

    def test_idle_confirmation_and_input_bounds_refuse_before_dispatch(self):
        self.start()
        supervisor = self.controller(executor=lambda *a, **k: self.fail("Must not dispatch"))
        with self.assertRaisesRegex(ContractError, "host-idle"):
            supervisor.create(self.team_id, host_idle_confirmed=False)
        for key in ("max_turns", "max_wall_seconds", "turn_timeout_seconds", "poll_seconds", "max_stagnant_turns"):
            with self.subTest(key=key), self.assertRaises(ContractError):
                supervisor.create(self.team_id, host_idle_confirmed=True, **{key: 0})
        self.assertEqual(supervisor.journal.rows(self.root), [])

    def test_framework_reconnect_preserves_ledger_without_dispatch_then_fresh_host_finishes(self):
        self.start()
        supervisor = self.controller(executor=lambda *a, **k: self.fail('No model during reconnect'))
        initial = self.create(supervisor)
        team_before = self.service._data(self.team_id)
        self.service.loaded_fingerprint = 'previous-framework'
        result = supervisor.run(initial['id'])
        self.assertEqual(result['status'], 'RECONNECT_REQUIRED')
        collected = supervisor.journal.get(initial['id'])
        self.assertIsNone(collected['process'])
        self.assertIsNone(collected['pending'])
        self.assertEqual(collected['framework_reconnects'], 1)
        for key in ['id', 'deadline_epoch', 'turns', 'dispatch_ms', 'known_tokens', 'session_id', 'policy']:
            self.assertEqual(collected[key], initial[key])
        self.assertEqual(self.service._data(self.team_id), team_before)
        self.service.close()
        fresh = NativeHostService(self.root, self.store, clock=lambda: self.tick)
        fresh.workbench_parent = self.store.directory
        self.addCleanup(fresh.close)
        next_supervisor = NativeSupervisor(fresh, driver=FixtureDriver(), clock=lambda: self.tick, sleeper=self.advance)
        final = next_supervisor.run(initial['id'])
        self.assertEqual(final['status'], 'COMPLETE', final['reason'])
        self.assertEqual(final['framework_reconnects'], 1)
        self.assertEqual(final['turns'], 4)
        self.assertEqual(final['deadline_epoch'], initial['deadline_epoch'])
        next_supervisor.journal.audit(initial['id'])

    def test_reconnect_exec_preserves_original_arguments_and_refuses_pending_effects(self):
        self.start()
        supervisor = self.controller()
        initial = self.create(supervisor)
        self.service.loaded_fingerprint = 'previous-framework'
        supervisor.run(initial['id'])
        collected = supervisor.journal.get(initial['id'])
        with patch('loop_engineering.native_supervisor.os.execv') as execute:
            reconnect_supervisor(collected, self.root, self.store.directory)
            argv = execute.call_args.args[1]
            self.assertEqual(argv, [sys.executable, str(ROOT/'scripts/loop.py'), 'host-supervisor-run', str(self.root),
                                  '--state-dir', str(self.store.directory), '--supervisor-id', initial['id']])
            for change in [{'pending':{'pid':123}}, {'process':{'pid':123}}, {'status':'PAUSED'}]:
                with self.subTest(change=change), self.assertRaises(ContractError):
                    reconnect_supervisor({**collected, **change}, self.root, self.store.directory)
            self.assertEqual(execute.call_count, 1)

    def test_reconnect_bound_stop_and_deadline_precede_another_dispatch(self):
        self.start()
        supervisor = self.controller(executor=lambda *a, **k: self.fail('No model while stale'))
        initial = self.create(supervisor)
        self.service.loaded_fingerprint = 'previous-framework'
        supervisor.journal.update(initial['id'], 'fixture.reconnect_limit', lambda row: row.update(framework_reconnects=3))
        result = supervisor.run(initial['id'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['framework_reconnects'], 3)
        supervisor.control(initial['id'], 'pause')
        self.assertEqual(supervisor.run(initial['id'])['status'], 'PAUSED')
        supervisor.control(initial['id'], 'resume')
        self.tick = initial['deadline_epoch']
        result = supervisor.run(initial['id'])
        self.assertEqual(result['status'], 'LIMIT_REACHED')
        self.assertEqual(result['turns'], 0)
        self.assertEqual(result['framework_reconnects'], 3)

    def test_running_legacy_locale_process_is_observed_without_duplicate_launch(self):
        self.start()
        supervisor = self.controller()
        initial = self.create(supervisor)
        supervisor.journal.update(initial['id'], 'fixture.legacy_identity', lambda row: row.update(
            process={'pid':49021,'identity':'Tue  6 Oct 15:32:36 2026     49021'}))
        with patch('loop_engineering.native_supervisor.process_identity', return_value='Tue Oct  6 15:32:36 2026     49021'), \
             patch('loop_engineering.native_supervisor.process_alive', return_value=True), \
             patch('loop_engineering.native_supervisor.subprocess.Popen', side_effect=AssertionError('Already owned')):
            self.assertEqual(supervisor.status(initial['id'])['process_observed'], 'running')
            self.assertTrue(supervisor.launch(initial['id'])['already_running'])

    def test_real_process_reconnect_reloads_framework_and_completes_original_team(self):
        import subprocess
        self.start()
        executable = self.store.directory/'offline-codex-reconnect'
        executable.write_text('#!'+sys.executable+'\nimport runpy\nrunpy.run_path('+repr(str(ROOT/'tests/fixtures/supervisor_codex.py'))+", run_name='__main__')\n")
        executable.chmod(0o700)
        supervisor = NativeSupervisor(self.service)
        initial = supervisor.create(self.team_id, host_idle_confirmed=True, max_wall_seconds=60,
                                    turn_timeout_seconds=30, executable=str(executable))
        launcher = self.store.directory/'reconnect-launcher.py'
        launcher.write_text('from pathlib import Path\nimport sys\nsys.path.insert(0, '+repr(str(ROOT))+')\n'
            'from loop_engineering.native_host import NativeHostService\nfrom loop_engineering.store import Store\n'
            'from loop_engineering.native_supervisor import NativeSupervisor,reconnect_supervisor\n'
            'project=Path('+repr(str(self.root))+')\nstate=Path('+repr(str(self.store.directory))+')\n'
            'service=NativeHostService(project,Store(state))\nsupervisor=NativeSupervisor(service)\n'
            "service.loaded_fingerprint='previous-framework'\n"
            'result=supervisor.run('+repr(initial['id'])+')\nservice.close()\n'
            'reconnect_supervisor(supervisor.journal.get(result["id"]),project,state)\n')
        completed = subprocess.run([sys.executable, str(launcher)], capture_output=True, text=True, timeout=45)
        self.assertEqual(completed.returncode, 0, completed.stderr+'\n'+completed.stdout)
        final = supervisor.journal.get(initial['id'])
        self.assertEqual(final['status'], 'COMPLETE', final['reason'])
        self.assertEqual(final['framework_reconnects'], 1)
        self.assertEqual(final['turns'], 4)
        self.assertEqual(final['session_id'], SESSION)
        self.assertEqual(final['deadline_epoch'], initial['deadline_epoch'])
        self.assertTrue(self.service.progress(self.team_id)['final_candidate_current'])
        supervisor.journal.audit(initial['id'])

    def test_real_supervisor_recovers_rejected_handoff_and_completes_original_team(self):
        self.owner.plan["native_rework"] = {"max_rounds": 2}
        self.owner.fixture.save_plan()
        self.start(); self.complete("implement")
        data = self.service._data(self.team_id)
        child_id = data["records"]["implement"]["child_run_id"]
        self.service.receive(self.team_id, "implement", data["records"]["implement"]["handoff"]["id"],
                             "coordinator", False, "Actual fixture requires scoped repair before acceptance")
        supervisor = self.controller()
        initial = self.create(supervisor)
        result = supervisor.run(initial["id"])
        self.assertEqual(result["status"], "COMPLETE", result["reason"])
        state = self.service.progress(self.team_id)
        self.assertEqual(state["status"], "COMPLETE")
        self.assertEqual(state["tasks"]["implement"]["child_run_id"], child_id)
        self.assertEqual(state["rework_rounds"], 1)
        self.assertEqual(result["turns"], 5)

    def test_duplicate_supervisor_retains_original_budget_and_shared_run_lock(self):
        self.start()
        supervisor = self.controller()
        data = self.create(supervisor)
        with self.assertRaisesRegex(ContractError, "existing supervisor"):
            self.create(supervisor)
        with supervisor._run_lock(), self.assertRaisesRegex(ContractError, "already driving"):
            supervisor.run(data["id"])
        self.assertEqual(supervisor.journal.get(data["id"]), data)

    def test_actual_question_waits_without_model_and_answer_automatically_dispatches(self):
        self.start(specification=True)
        packet = self.service.stage_request(self.team_id)
        self.service.stage_submit(self.team_id, packet["request_id"], {
            "status": "needs_input", "summary": "Actual scope choice", "spec_markdown": "", "sources": [],
            "questions": [{"id": "scope", "question": "Which actual scope?", "reason": "Material boundary", "blocking": True}]})
        sleeps = []
        def answer(seconds):
            sleeps.append(seconds)
            self.service.answer(self.team_id, "scope", "Actual fixture user: local delivery only")
            self.advance(seconds)
        supervisor = self.controller(executor=self.executor(), sleeper=answer)
        data = self.create(supervisor, max_turns=1)
        result = supervisor.run(data["id"])
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(result["turns"], 1)
        self.assertEqual(result["status"], "LIMIT_REACHED")
        self.assertEqual(self.service.progress(self.team_id)["planning"]["questions_remaining"], 0)

    def test_missing_human_evidence_waits_without_acceptance_or_empty_dispatch(self):
        preflight_fixture.NativePreflightTests.configure(self, check_type="human", key_role="human")
        self.start()
        self.complete("implement"); self.receive("implement"); self.complete("verify")
        supervisor = self.controller(executor=lambda *a, **k: self.fail("Missing human does not dispatch a model"))
        data = self.create(supervisor)
        before = self.service._data(self.team_id)
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "LIMIT_REACHED")
        self.assertEqual(result["turns"], 0)
        self.assertEqual(self.service._data(self.team_id), before)
        self.assertNotEqual(before["status"], "COMPLETE")

    def test_host_claims_blocker_without_turning_regressions_into_completion(self):
        self.start()
        report = {"status": "blocked", "summary": "Actual human observation still missing; procedure prepared",
                  "blocker": "human_input", "questions": ["Provide the required actual observation"]}
        supervisor = self.controller(executor=self.executor(report=report))
        data = self.create(supervisor)
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "LIMIT_REACHED")
        self.assertEqual(result["turns"], 1)
        self.assertEqual(result["last_report"], report)
        self.assertEqual(self.service.progress(self.team_id)["check_results"], [])

    def test_heartbeat_only_turns_hit_stagnation_bound(self):
        self.start()
        supervisor = self.controller(executor=self.executor(lambda: self.service.heartbeat(
            self.team_id, "fixture", "No implementation changed")))
        data = self.create(supervisor, max_stagnant_turns=2)
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "STALLED")
        self.assertEqual(result["turns"], 2)
        self.assertEqual(result["stagnant_turns"], 2)
        self.assertEqual(self.service.progress(self.team_id)["check_results"], [])

    def test_completed_controller_at_last_allowed_turn_is_complete_not_limit(self):
        self.start()
        supervisor = self.controller(executor=self.executor(self.finish))
        data = self.create(supervisor, max_turns=1)
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["turns"], 1)

    def test_actual_project_queue_advances_and_waits_in_new_requirements(self):
        self.accepted()
        root_id = self.team_id
        supervisor = self.controller(executor=self.executor(lambda: self.service.project_advance(root_id)))
        data = self.create(supervisor, max_turns=1)
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "LIMIT_REACHED")
        self.assertNotEqual(result["team_id"], root_id)
        self.assertEqual(self.service.progress(result["team_id"])["stage"], "SPEC")
        self.assertEqual(self.service._data(root_id)["status"], "COMPLETE")

    def test_pause_and_cancel_remain_explicit_and_do_not_rewrite_team(self):
        self.start()
        supervisor = self.controller()
        data = self.create(supervisor)
        before = self.service._data(self.team_id)
        self.assertEqual(supervisor.control(data["id"], "pause")["status"], "PAUSED")
        self.assertEqual(supervisor.run(data["id"])["status"], "PAUSED")
        resumed = supervisor.control(data["id"], "resume")
        self.assertEqual(resumed["deadline_epoch"], data["deadline_epoch"])
        self.assertEqual(supervisor.control(data["id"], "cancel")["status"], "CANCELLED")
        with self.assertRaisesRegex(ContractError, "terminal"):
            supervisor.control(data["id"], "resume")
        self.assertEqual(self.service._data(self.team_id), before)

    def test_original_team_pause_stops_supervisor_without_dispatch(self):
        self.start()
        supervisor = self.controller(executor=lambda *a, **k: self.fail("Paused team cannot dispatch"))
        data = self.create(supervisor)
        self.service.control(self.team_id, "pause")
        self.assertEqual(supervisor.run(data["id"])["status"], "PAUSED")

    def test_failed_and_timed_out_dispatches_need_actual_effect_reconciliation(self):
        self.start()
        supervisor = self.controller(executor=self.executor(outcome="timeout", exit_code=-9))
        data = self.create(supervisor)
        before = self.service._data(self.team_id)
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        pending = result["pending"]
        self.assertEqual(result["turns"], 1)
        self.assertEqual(supervisor.run(data["id"])["turns"], 1)
        with self.assertRaisesRegex(ContractError, "reconcile"):
            supervisor.control(data["id"], "resume")
        recovered = supervisor.reconcile(data["id"], "Fixture confirms original writer stopped; retained files inspected")
        self.assertEqual(recovered["dispatch_ms"], pending["reserved_ms"])
        self.assertEqual(recovered["turns"], 1)
        self.assertEqual(recovered["deadline_epoch"], data["deadline_epoch"])
        self.assertFalse(recovered["usage_complete"])
        self.assertEqual(self.service._data(self.team_id), before)
        supervisor.journal.audit(data["id"])

    def test_crash_after_reservation_preserves_identity_and_refuses_replay(self):
        self.start()
        def crash(*args, **kwargs):
            raise RuntimeError("Fixture crash after durable reservation")
        supervisor = self.controller(executor=crash)
        data = self.create(supervisor)
        with self.assertRaises(RuntimeError):
            supervisor.run(data["id"])
        self.assertIsNotNone(supervisor.journal.get(data["id"])["pending"])
        self.assertEqual(supervisor.run(data["id"])["status"], "RECOVERY_REQUIRED")
        self.assertEqual(len(self.driver.calls), 1)

    def test_reconciliation_refuses_live_or_unobservable_original_process(self):
        self.start()
        supervisor = self.controller(executor=self.executor(outcome="timeout"))
        data = self.create(supervisor)
        supervisor.run(data["id"])
        supervisor.journal.update(data["id"], "fixture.pid", lambda row: row["pending"].update(pid=123))
        with patch("loop_engineering.native_supervisor.process_group_alive", return_value=True), self.assertRaisesRegex(ContractError, "alive"):
            supervisor.reconcile(data["id"], "Cannot attest stopped")

    def test_different_session_or_invalid_report_preserves_pending_effects(self):
        self.start()
        supervisor = self.controller(executor=self.executor(session="ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee"))
        data = self.create(supervisor)
        supervisor.journal.update(data["id"], "fixture.session", lambda row: row.update(session_id=SESSION))
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertIsNotNone(result["pending"])
        self.assertEqual(result["session_id"], SESSION)

    def test_unknown_usage_never_reports_complete_event_coverage(self):
        self.start()
        supervisor = self.controller(executor=self.executor(usage={}))
        data = self.create(supervisor, max_turns=1)
        result = supervisor.run(data["id"])
        self.assertFalse(result["usage_complete"])
        self.assertEqual(result["known_tokens"], 0)
        self.assertIsNone(supervisor.status(data["id"])["host_cost"])

    def test_tampered_supervisor_projection_and_event_chain_fail_closed(self):
        self.start()
        supervisor = self.controller()
        data = self.create(supervisor)
        modified = {**data, "turns": 0, "deadline_epoch": data["deadline_epoch"] + 99999}
        with self.store.connect() as db:
            db.execute("UPDATE host_supervisors SET data=? WHERE id=?", (json.dumps(modified), data["id"]))
        with self.assertRaisesRegex(ContractError, "authenticated"):
            supervisor.status(data["id"])
        with self.store.connect() as db:
            db.execute("UPDATE host_supervisors SET data=? WHERE id=?", (json.dumps(data), data["id"]))
        supervisor.control(data["id"], "pause")
        with self.store.connect() as db:
            db.execute("DELETE FROM host_supervisor_events WHERE id=? AND revision=1", (data["id"],))
        with self.assertRaisesRegex(ContractError, "inconsistent"):
            supervisor.journal.audit(data["id"])

    def test_dashboard_inspection_does_not_launch_or_renew_supervisor(self):
        self.start()
        supervisor = self.controller()
        data = self.create(supervisor)
        with patch("subprocess.Popen", side_effect=AssertionError("Progress never launches hosts")):
            state = self.service.progress(self.team_id)
        self.assertEqual(state["host_supervisor"]["id"], data["id"])
        self.assertIn("持续执行监督器", render_progress(state))
        self.assertEqual(supervisor.journal.get(data["id"]), data)

    def test_cli_parser_has_explicit_dispatch_and_separate_readonly_status(self):
        argv = [str(self.root), "--state-dir", str(self.store.directory)]
        start = parser().parse_args(["host-supervise"] + argv + ["--team-id", "team-fixture", "--host-idle"])
        self.assertTrue(start.host_idle)
        status = parser().parse_args(["host-supervisor-status"] + argv + ["--supervisor-id", "supervisor-fixture"])
        self.assertEqual(status.command, "host-supervisor-status")
        self.assertFalse(hasattr(status, "model"))

    def test_detached_supervisor_executes_after_launch_returns_without_live_codex(self):
        self.start()
        # Real detached process + fake executable. No model/provider transport.
        executable = self.store.directory / "offline-codex"
        executable.write_text("#!" + sys.executable + "\nimport runpy\nrunpy.run_path(" +
                              repr(str(ROOT / "tests/fixtures/supervisor_codex.py")) + ", run_name='__main__')\n")
        executable.chmod(0o700)
        supervisor = NativeSupervisor(self.service)
        data = supervisor.create(self.team_id, host_idle_confirmed=True, max_wall_seconds=60,
                                 turn_timeout_seconds=30, executable=str(executable))
        launched = supervisor.launch(data["id"])
        self.assertGreater(launched["launcher_pid"], 0)
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                result = supervisor.status(data["id"])
                if result["status"] in {"COMPLETE", "FAILED", "RECOVERY_REQUIRED", "LIMIT_REACHED", "STALLED"}:
                    break
                time.sleep(.1)
            log = Path(launched["log"]).read_text()
            self.assertEqual(result["status"], "COMPLETE", result["reason"] + "\n" + log)
            self.assertEqual(result["turns"], 4)
            self.assertTrue(self.service.progress(self.team_id)["final_candidate_current"])
            self.assertEqual(result["session_id"], SESSION)
        finally:
            if supervisor.journal.get(data["id"])["status"] != "COMPLETE":
                supervisor.control(data["id"], "cancel")
            # Let the fixture release the original project lock before cleanup.
            for _ in range(30):
                if supervisor.journal.get(data["id"])["process"] is None:
                    break
                time.sleep(.1)

    def test_actual_owned_host_process_cancellation_retains_original_pending_dispatch(self):
        self.start()
        class SlowDriver(FixtureDriver):
            def command(self, *args):
                return [sys.executable, "-c", "import time; time.sleep(60)"]
        supervisor = NativeSupervisor(self.service, driver=SlowDriver())
        data = supervisor.create(self.team_id, host_idle_confirmed=True, max_wall_seconds=60, turn_timeout_seconds=30)
        from loop_engineering.processes import execute, process_group_alive
        def cancellation_fixture(*args, **kwargs):
            callback = kwargs["on_started"]
            def started(pid):
                callback(pid)
                supervisor.control(data["id"], "pause")
            kwargs["on_started"] = started
            return execute(*args, **kwargs)
        supervisor.executor = cancellation_fixture
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertEqual(result["signal"], "PAUSED")
        self.assertFalse(process_group_alive(result["pending"]["pid"]))
        recovered = supervisor.reconcile(data["id"], "Actual owned fixture process group stopped; no task effect started")
        self.assertEqual(recovered["status"], "PAUSED")
        self.assertEqual(recovered["turns"], 1)
        self.assertEqual(self.service._data(self.team_id)["status"], "ACTIVE")

    def test_unresolved_background_operation_prevents_another_host_turn(self):
        self.start()
        supervisor = self.controller(executor=self.executor())
        data = self.create(supervisor)
        original = supervisor._state
        calls = 0
        def state(row):
            nonlocal calls
            calls += 1
            result = original(row)
            if calls >= 2:
                result["operations"] = [{"id": "fixture-pending", "status": "RUNNING", "elapsed_seconds": 1.25}]
            return result
        supervisor._state = state
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertEqual(result["turns"], 1)
        self.assertIsNotNone(result["pending"])

    def test_invalid_structured_report_never_retries_or_claims_acceptance(self):
        self.start()
        invalid = {"status": [], "summary": "Invalid typed result", "blocker": None, "questions": []}
        supervisor = self.controller(executor=self.executor(report=invalid))
        data = self.create(supervisor)
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertIsNotNone(result["pending"])
        self.assertEqual(result["turns"], 1)

    def test_wait_polling_preserves_budget_and_does_not_append_empty_checkpoints(self):
        self.start(specification=True)
        packet = self.service.stage_request(self.team_id)
        self.service.stage_submit(self.team_id, packet["request_id"], {
            "status": "needs_input", "summary": "Actual missing input", "spec_markdown": "", "sources": [],
            "questions": [{"id": "scope", "question": "Local delivery?", "reason": "Scope", "blocking": True}]})
        supervisor = self.controller(executor=lambda *a, **k: self.fail("No model while waiting"))
        data = self.create(supervisor)
        result = supervisor.run(data["id"])
        with self.store.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM host_supervisor_events WHERE id=?", (data["id"],)).fetchone()[0]
        self.assertEqual(result["turns"], 0)
        self.assertEqual(result["deadline_epoch"], data["deadline_epoch"])
        self.assertLessEqual(count, 6)

    def test_missing_codex_executable_refuses_before_reservation_without_fallback_provider(self):
        self.start()
        supervisor = NativeSupervisor(self.service, clock=lambda: self.tick)
        data = supervisor.create(self.team_id, host_idle_confirmed=True, executable=str(self.store.directory / "no-codex"))
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIsNone(result["pending"])
        self.assertEqual(result["turns"], 0)
        self.assertEqual(self.owner.transport.requests, [])

    def test_progress_reports_do_not_attest_an_unobserved_supervisor_process(self):
        self.start()
        supervisor = self.controller()
        data = self.create(supervisor)
        self.assertEqual(supervisor.status(data["id"])["process_observed"], "unknown_or_stopped")
        self.assertEqual(supervisor.journal.get(data["id"]), data)

    def test_dedicated_driver_keeps_guarded_flags_and_current_backend_without_project_edits(self):
        self.start()
        supervisor = self.controller()
        data = self.create(supervisor)
        before = self.service._data(self.team_id)
        files = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        artifacts = self.store.run_dir(data["id"]) / "fixture-command"; artifacts.mkdir()
        argv = self.driver.command(self.service, data, artifacts, self.service.progress(self.team_id))
        self.assertNotIn("--last", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertIn("workspace-write", argv)
        self.assertIn("mcp_servers.loop-native.required=true", argv)
        permitted = {argv[i + 1] for i, item in enumerate(argv) if item == "--add-dir"}
        self.assertIn(str(artifacts), permitted)
        self.assertIn(str(self.store.directory / "host-workbenches"), permitted)
        self.assertNotIn(str(self.store.directory), permitted)
        for directory in permitted:
            self.assertFalse(self.store.authorities.directory.is_relative_to(Path(directory)))
        self.assertEqual(self.service._data(self.team_id), before)
        self.assertEqual(files, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_malformed_events_are_contract_errors_and_missing_usage_is_partial(self):
        path = self.store.directory / "events.jsonl"
        path.write_text(json.dumps({"type": []}) + "\n")
        with self.assertRaises(ContractError):
            codex_events(path)
        path.write_text(json.dumps({"type": "thread.started", "thread_id": SESSION}) + "\n" +
                        json.dumps({"type": "turn.completed", "usage": {}}) + "\n")
        identity, known, complete = codex_events(path)
        self.assertEqual(identity, SESSION)
        self.assertEqual(known, 0)
        self.assertFalse(complete)

    def test_starting_from_successor_retains_original_project_stop_authority(self):
        self.accepted()
        root_id = self.team_id
        child = self.service.project_advance(root_id)
        self.team_id = child["team_id"]
        supervisor = self.controller(executor=lambda *a, **k: self.fail("Actual project pause must stop dispatch"))
        data = self.create(supervisor)
        self.assertEqual(data["root_team_id"], root_id)
        self.service.project_control(root_id, "pause")
        self.assertEqual(supervisor.run(data["id"])["status"], "PAUSED")

    def test_corrupt_acceptance_artifacts_cannot_be_supervisor_complete(self):
        self.start()
        self.finish()
        supervisor = self.controller(executor=lambda *a, **k: self.fail("Cannot dispatch from corrupt acceptance"))
        data = self.create(supervisor)
        original = supervisor._state
        def corrupted(row):
            state = original(row)
            state["evidence_problems"] = [{"task_id": "verify", "error": "Actual fixture artifact unavailable"}]
            return state
        supervisor._state = corrupted
        self.assertEqual(supervisor.run(data["id"])["status"], "BLOCKED")

    def test_expired_state_preparation_never_dispatches_a_host(self):
        self.start()
        supervisor = self.controller(executor=lambda *a, **k: self.fail("Expired preparation cannot dispatch"))
        data = self.create(supervisor)
        original = supervisor._state
        def slow_state(row):
            state = original(row)
            self.advance(61)
            return state
        supervisor._state = slow_state
        result = supervisor.run(data["id"])
        self.assertEqual(result["status"], "LIMIT_REACHED")
        self.assertEqual(result["turns"], 0)
        self.assertEqual(self.driver.calls, [])
        self.assertIsNone(result["pending"])

    def test_expired_or_paused_command_preparation_aborts_reservation_without_effects(self):
        for action in ("expire", "pause"):
            with self.subTest(action=action):
                # An independent actual fixture store per run avoids a new
                # ledger for the same unfinished supervisor.
                case = NativeSupervisorTests(); case.setUp()
                try:
                    case.start()
                    supervisor = case.controller(executor=lambda *a, **k: self.fail("Stopped command preparation cannot dispatch"))
                    data = case.create(supervisor)
                    original = case.driver.command
                    def prepare(*args):
                        argv = original(*args)
                        if action == "expire":
                            case.advance(61)
                        else:
                            supervisor.control(data["id"], "pause")
                        return argv
                    case.driver.command = prepare
                    result = supervisor.run(data["id"])
                    self.assertEqual(result["status"], "LIMIT_REACHED" if action == "expire" else "PAUSED")
                    self.assertIsNone(result["pending"])
                    self.assertEqual(result["dispatch_ms"], 0)
                    self.assertEqual(result["turns"], 1)
                    self.assertTrue(result["usage_complete"])
                    self.assertIsNone(result["last_aborted_reservation"]["pid"])
                finally:
                    case.doCleanups()


if __name__ == "__main__":
    unittest.main()
