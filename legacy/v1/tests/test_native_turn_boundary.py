"""Recorded executable work remains visible after checks/consultations return."""

from unittest.mock import patch
import unittest

from loop_engineering.native_host import render_progress
from tests import test_native_workbench as workbench
from tests import test_native_preflight as preflight
from tests import test_native_project as project


class NativeTurnBoundaryTests(unittest.TestCase):
    setUp = workbench.NativeWorkbenchTests.setUp
    start = workbench.NativeWorkbenchTests.start
    prepare = workbench.NativeWorkbenchTests.prepare
    edit = workbench.NativeWorkbenchTests.edit
    submit = workbench.NativeWorkbenchTests.submit
    complete = workbench.NativeWorkbenchTests.complete
    receive = workbench.NativeWorkbenchTests.receive
    queue = project.NativeProjectTests.queue
    finish = project.NativeProjectTests.finish
    accepted = project.NativeProjectTests.accepted

    def test_actual_passing_checks_still_require_handoff_and_remaining_task(self):
        packet = self.prepare()
        self.edit(packet)
        result = self.submit()
        self.assertTrue(all(check["result"] == "pass" for check in result["check_results"]))
        self.assertFalse(result["turn_boundary"]["may_end_turn"])
        self.assertEqual(result["turn_boundary"]["next_tool"], "loop_receive_handoff")
        self.assertEqual(result["task_counts"]["COMPLETE"], 0)
        result = self.receive("implement")
        self.assertFalse(result["turn_boundary"]["may_end_turn"])
        self.assertEqual(result["turn_boundary"]["next_tool"], "loop_workbench_prepare")
        self.complete("verify")
        result = self.receive("verify")
        self.assertTrue(result["turn_boundary"]["may_end_turn"])
        self.assertEqual(result["turn_boundary"]["next_action_kind"], "complete")
        self.assertTrue(result["final_candidate_current"])
        self.assertEqual(self.owner.transport.requests, [])

    def test_all_collected_specialists_do_not_finish_original_product_work(self):
        self.prepare()
        worker = self.service.worker_assign(self.team_id, "implement", self.workbench["id"],
            "scoped-advice", "backend", "Review the open defect", "Return advice; implementation is still required.", [], [])
        self.service.worker_update(self.team_id, worker["id"], "fixture:backend", "RUNNING", "Offline fixture advice only")
        self.service.worker_update(self.team_id, worker["id"], "fixture:backend", "DRAFT_READY", "Actual advice returned without source repair")
        self.service.worker_collect(self.team_id, worker["id"], "Read the returned fixture advice")
        before = self.service._data(self.team_id)
        with patch("subprocess.Popen", side_effect=AssertionError("Read-only supervision launches nothing")):
            result = self.service.progress(self.team_id)
        self.assertTrue(all(w["status"] == "COLLECTED" for w in result["dispatch_board"]["workers"]))
        self.assertFalse(result["turn_boundary"]["may_end_turn"])
        self.assertEqual(result["turn_boundary"]["next_tool"], "loop_workbench_prepare")
        self.assertEqual(self.service._data(self.team_id), before)
        self.assertEqual(result["check_results"], [])
        self.assertEqual(result["task_counts"]["COMPLETE"], 0)
        self.assertIn("主线程是否应继续", render_progress(result))
        self.assertEqual(self.owner.transport.requests, [])

    def test_expired_writer_requires_takeover_without_renewing_deadline_or_budget(self):
        self.prepare()
        before = self.service._data(self.team_id)
        run_id = before["records"]["implement"]["child_run_id"]
        child = self.store.get(run_id)
        self.tick += 10000
        result = self.service.progress(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "recover_worker")
        self.assertFalse(result["turn_boundary"]["may_end_turn"])
        self.assertEqual(self.store.get(run_id), child)
        self.assertEqual(self.service._data(self.team_id), before)

    def test_actual_unanswered_question_remains_a_wait_gate_and_answer_restores_work(self):
        self.start(specification=True)
        packet = self.service.stage_request(self.team_id)
        result = self.service.stage_submit(self.team_id, packet["request_id"], {
            "status": "needs_input", "summary": "Actual fixture release decision missing", "spec_markdown": "",
            "sources": [], "questions": [{"id": "release", "question": "Which actual delivery scope?",
                "reason": "Needs real user input", "blocking": True}]})
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertTrue(result["turn_boundary"]["may_end_turn"])
        self.assertEqual(result["turn_boundary"]["next_action_kind"], "answer_questions")
        self.assertIsNone(result["final_candidate_current"])
        result = self.service.answer(self.team_id, "release", "Actual fixture user: local delivery only.")
        self.assertFalse(result["turn_boundary"]["may_end_turn"])
        self.assertEqual(self.owner.transport.requests, [])

    def test_missing_actual_human_judgment_is_a_gate_not_product_acceptance(self):
        preflight.NativePreflightTests.configure(self, check_type="human", key_role="human")
        self.start()
        self.complete("implement")
        self.receive("implement")
        self.complete("verify")
        before = self.service._data(self.team_id)
        result = self.service.progress(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "await_evaluator")
        self.assertTrue(result["turn_boundary"]["may_end_turn"])
        self.assertNotEqual(result["status"], "COMPLETE")
        self.assertEqual(result["task_counts"]["COMPLETE"], 1)
        self.assertEqual(self.service._data(self.team_id), before)
        self.assertEqual(result["pending_evaluations"][0]["type"], "human")
        self.assertEqual(self.owner.transport.requests, [])

    def test_accepted_batch_with_authorized_queue_requires_successor_and_pause_still_stops(self):
        result = self.accepted()
        self.assertEqual(result["status"], "COMPLETE")
        self.assertTrue(result["final_candidate_current"])
        self.assertFalse(result["turn_boundary"]["may_end_turn"])
        self.assertEqual(result["turn_boundary"]["next_tool"], "loop_project_advance")
        result = self.service.project_control(self.team_id, "pause")
        self.assertTrue(result["turn_boundary"]["may_end_turn"])
        self.assertEqual(result["turn_boundary"]["next_action_kind"], "project_stopped")
        self.assertEqual(result["status"], "COMPLETE")


if __name__ == "__main__":
    unittest.main()
