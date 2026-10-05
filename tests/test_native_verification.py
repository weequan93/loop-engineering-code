"""Offline host observation bridge; no UI/model dispatch and no invented passes."""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, load, validate
from loop_engineering.native_host import render_progress
from tests import test_native_host as fixture


SCENARIOS = [
    {"id": "launch", "check_id": "tests", "surface": "android-emulator", "title": "Launch and backend round trip",
     "kind": "capability", "depends_on": [], "max_attempts": 2, "timeout_seconds": 30},
    {"id": "stop", "check_id": "tests", "surface": "android-emulator", "title": "Observe stop and reconnect",
     "kind": "acceptance", "depends_on": ["launch"], "max_attempts": 2, "timeout_seconds": 30},
]


class NativeVerificationTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start

    def test_cancelled_unknown_action_requires_inspection_before_a_new_batch(self):
        self.prepare(); result = self.update()
        self.service.control(self.team_id, "cancel")
        with self.assertRaisesRegex(ContractError, "previous host verification"):
            self.service.begin()
        self.update(action="reconcile", attempt_id=result["unresolved_attempt"]["id"],
                    observation="Inspected actual external state; original action has stopped", safe_to_retry=True)
        self.assertEqual(self.service.progress(self.team_id)["status"], "CANCELLED")
        with self.assertRaisesRegex(ContractError, "stopped"):
            self.update()
        next_team = self.service.begin()
        self.assertNotEqual(next_team["team_id"], self.team_id)

    def prepare(self):
        path = self.root / ".loop/tasks/implement.json"
        task = load(path)
        self.scenarios = deepcopy(SCENARIOS)
        for s in self.scenarios:
            s["check_id"] = task["checks"][0]["id"]
        task.setdefault("extensions", {})["verification_scenarios"] = self.scenarios
        path.write_text(json.dumps(task))
        self.now = 1000
        self.service.clock = lambda: self.now
        self.start()
        packet = self.service.workbench_prepare(self.team_id, "implement")
        self.workbench = packet["workbench"]
        self.begin_args = dict(team_id=self.team_id, task_id="implement", workbench_id=self.workbench["id"],
                               owner="/root/actual-tester", environment="Offline injected emulator fixture; no physical UI")
        self.session = self.service.verification_begin(**self.begin_args)
        self.update_args = dict(team_id=self.team_id, session_id=self.session["id"], owner="/root/actual-tester")
        return self.session

    def update(self, scenario_id="launch", action="start", **kw):
        return self.service.verification_update(**self.update_args, scenario_id=scenario_id, action=action, **kw)

    def report(self, scenario_id="launch", action="pass", **kw):
        session = self.service.verification_progress(self.team_id, self.session["id"])["sessions"][0]
        attempt = next(s for s in session["scenarios"] if s["id"] == scenario_id)["attempt"]
        path = Path(session["artifacts_directory"]) / (scenario_id + ".log")
        path.write_text("Actual deterministic observation fixture: " + action)
        return self.update(scenario_id, action, attempt_id=attempt["id"], observation="Observed actual fixture " + action,
                           artifacts=[path.name], **kw)

    def test_dependency_order_artifacts_progress_and_reconnect_do_not_attest_acceptance(self):
        self.prepare()
        before = deepcopy(self.store.get(self.service._data(self.team_id)["records"]["implement"]["child_run_id"]))
        with patch("subprocess.Popen", side_effect=AssertionError("Observation cannot dispatch a process/model")):
            with self.assertRaisesRegex(ContractError, "dependencies"):
                self.update("stop")
            first = self.update()
            self.assertEqual(first["unresolved_attempt"]["status"], "RUNNING")
            self.report()
            self.update("stop"); final = self.report("stop")
            self.assertEqual(final["observed_pass"], 2)
            self.assertTrue(final["current"])
            reconnect = type(self.service)(self.root, self.store, clock=lambda: self.now)
            self.addCleanup(reconnect.close)
            restored = reconnect.verification_begin(**self.begin_args)
            self.assertEqual(restored["id"], self.session["id"])
            self.assertEqual(restored["observed_pass"], 2)
        after = self.store.get(before["run_id"])
        self.assertEqual(before, after)
        self.assertEqual(after["state"]["status"], "PLANNING")
        self.assertEqual(after["selected_evidence"], [])
        html = render_progress(self.service.progress(self.team_id))
        self.assertIn("逐项验证与中断恢复", html)
        self.assertIn("android-emulator", html)
        artifact = final["scenarios"][1]["attempt"]["artifacts"][0]
        self.assertIn(b"fixture", self.service.team.snapshots.read(artifact["sha256"]))

    def test_lost_ack_and_expiry_require_reconciliation_without_replay(self):
        self.prepare(); first = self.update()
        self.assertEqual(first["unresolved_attempt"]["id"], self.update()["unresolved_attempt"]["id"])
        self.now += 31
        progress = self.service.verification_progress(self.team_id, self.session["id"])["sessions"][0]
        self.assertEqual(progress["scenarios"][0]["status"], "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(ContractError, "reconciliation"):
            self.report()
        args = dict(attempt_id=first["unresolved_attempt"]["id"], observation="Inspected original effect; outcome still unknown")
        self.update(action="reconcile", safe_to_retry=False, **args)
        with self.assertRaisesRegex(ContractError, "original running"):
            self.update()
        self.update(action="reconcile", safe_to_retry=True, **{**args, "observation": "Inspected actual state; no effect remains"})
        retried = self.update()
        self.assertNotEqual(first["unresolved_attempt"]["id"], retried["unresolved_attempt"]["id"])
        self.assertEqual(retried["scenarios"][0]["attempts_remaining"], 0)

    def test_candidate_change_invalidates_observations_and_cannot_reset_attempt_budget(self):
        self.prepare(); self.update(); self.report()
        path = Path(self.workbench["directory"]) / "slug.py"
        path.write_text(path.read_text() + "\n# actual candidate changed\n")
        old = self.service.verification_progress(self.team_id, self.session["id"])["sessions"][0]
        self.assertFalse(old["current"]); self.assertEqual(old["observed_pass"], 0)
        self.session = self.service.verification_begin(**self.begin_args)
        self.update_args["session_id"] = self.session["id"]
        self.update(); self.report()
        path.write_text(path.read_text() + "\n# second change\n")
        self.session = self.service.verification_begin(**self.begin_args)
        self.update_args["session_id"] = self.session["id"]
        with self.assertRaisesRegex(ContractError, "allowance is exhausted"):
            self.update()
        action = self.service.progress(self.team_id)["next_action"]
        self.assertEqual(action["kind"], "verification_limit")
        self.assertFalse(action["continue_work"])

    def test_unresolved_stale_action_blocks_new_session_and_both_submission_routes(self):
        self.prepare(); self.update()
        path = Path(self.workbench["directory"]) / "slug.py"
        path.write_text(path.read_text() + "\n# change during unknown action\n")
        with self.assertRaisesRegex(ContractError, "original verification action"):
            self.service.verification_begin(**self.begin_args)
        with self.assertRaisesRegex(ContractError, "Reconcile running"):
            self.service.workbench_submit(self.team_id, "implement", self.workbench["id"], "Cannot submit uncertain action")
        packet = self.service.task_request(self.team_id, "implement")
        with self.assertRaisesRegex(ContractError, "Reconcile running"):
            self.service.task_submit(self.team_id, "implement", packet["team_assignment"]["request_id"], self.owner.respond(packet))
        self.assertEqual(self.service.progress(self.team_id)["next_action"]["kind"], "inspect_verification")

    def test_missing_or_escaping_artifacts_and_changed_owner_cannot_report_pass(self):
        self.prepare(); started = self.update(); attempt_id = started["unresolved_attempt"]["id"]
        with self.assertRaisesRegex(ContractError, "captured artifacts"):
            self.update(action="pass", attempt_id=attempt_id, observation="Claim without bytes")
        for path in ["../slug.py", "/etc/hosts", "missing.log"]:
            with self.subTest(path=path), self.assertRaises((ContractError, OSError)):
                self.update(action="pass", attempt_id=attempt_id, observation="Invalid source", artifacts=[path])
        link = Path(self.session["artifacts_directory"]) / "redirect.log"
        link.symlink_to(self.root / "slug.py")
        with self.assertRaises(ContractError):
            self.update(action="pass", attempt_id=attempt_id, observation="Symlink", artifacts=[link.name])
        with self.assertRaisesRegex(ContractError, "original session and owner"):
            self.service.verification_update(**{**self.update_args, "owner": "invented-other-worker"},
                scenario_id="launch", action="pass", attempt_id=attempt_id, observation="Other report")

    def test_failed_observation_requires_effect_reconciliation_before_retry(self):
        self.prepare(); self.update(); failed = self.report(action="fail")
        self.assertIsNotNone(failed["unresolved_attempt"])
        with self.assertRaisesRegex(ContractError, "original running"):
            self.update()
        self.assertEqual(self.service.progress(self.team_id)["task_counts"]["COMPLETE"], 0)

    def test_pause_blocks_updates_and_resume_requires_reconciliation(self):
        self.prepare(); self.update()
        self.service.control(self.team_id, "pause")
        with self.assertRaisesRegex(ContractError, "stopped"):
            self.report()
        # Team resume invalidates the old request; a running scenario must be reconciled, not silently reused.
        self.service.control(self.team_id, "resume")
        with self.assertRaisesRegex(ContractError, "reconciliation"):
            self.report()

    def test_report_retry_keeps_original_capture_and_html_escapes_host_text(self):
        self.prepare(); self.update(); self.report()
        before = deepcopy(self.service._data(self.team_id))
        self.report()
        self.assertEqual(before, self.service._data(self.team_id))
        self.update("stop")
        attempt = self.service.verification_progress(self.team_id, self.session["id"])["sessions"][0]["unresolved_attempt"]
        self.update("stop", "blocked", attempt_id=attempt["id"], observation='<script>alert("fixture")</script>')
        html = render_progress(self.service.progress(self.team_id))
        self.assertNotIn('<script>alert("fixture")</script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_artifact_capture_race_preserves_unknown_attempt(self):
        self.prepare(); self.update()
        from loop_engineering.native_verification import _artifacts
        def change(service, session, paths):
            result = _artifacts(service, session, paths)
            p = Path(self.workbench["directory"]) / "slug.py"
            p.write_text(p.read_text() + "\n# concurrent draft edit\n")
            return result
        with patch("loop_engineering.native_verification._artifacts", side_effect=change), \
                self.assertRaisesRegex(ContractError, "Candidate changed"):
            self.report()
        session = self.service.verification_progress(self.team_id, self.session["id"])["sessions"][0]
        self.assertEqual(session["unresolved_attempt"]["status"], "RUNNING")

    def test_schema_rejects_unknown_checks_cycles_and_duplicate_ids(self):
        path = self.root / ".loop/tasks/implement.json"
        task = load(path)
        for scenarios in ([{**SCENARIOS[0], "check_id": "missing"}], [SCENARIOS[0], SCENARIOS[0]],
                          [{**SCENARIOS[0], "check_id": task["checks"][0]["id"], "depends_on": ["launch"]}]):
            candidate = deepcopy(task)
            candidate.setdefault("extensions", {})["verification_scenarios"] = scenarios
            with self.assertRaises(ContractError):
                validate("task", candidate)


if __name__ == "__main__":
    unittest.main()
