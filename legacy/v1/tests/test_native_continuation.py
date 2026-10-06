"""Task-local evidence gates do not mask authorized independent engineering."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

from loop_engineering.contracts import load
from loop_engineering.native_flow import next_action
from tests import test_native_preflight as fixture


class ContinuationRoutingTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "team_id": "team-fixture", "status": "ACTIVE", "reason": None,
            "stage": "EXECUTION", "inputs_current": True, "input_problem": None,
            "final_candidate_current": False, "pending_questions": [],
            "tasks": {"acceptance": {"status": "RUNNING", "controller_status": "REVIEWING"},
                      "producer": {"status": "PENDING"}},
            "ready_tasks": ["producer"],
            "pending_evaluations": [{"task_id": "acceptance", "check_id": "human", "type": "human",
                                     "executor_registered": False}],
        }
        self.data = {"native_host": {}, "plan": {"native_parallel": {"max_parallel": 2}},
                     "tasks": {key: {"handoff_to": []} for key in self.state["tasks"]}}

    def assert_independent(self, kind):
        before = deepcopy((self.state, self.data))
        action = next_action(self.state, self.data)
        self.assertEqual(action["arguments"]["task_id"], "producer")
        self.assertTrue(action["continue_work"])
        self.assertEqual(action["deferred_blockers"][0]["kind"], kind)
        self.assertEqual(action["deferred_blockers"][0]["task_id"], "acceptance")
        self.assertEqual((self.state, self.data), before)
        return action

    def test_missing_human_result_continues_dependency_ready_engineering_without_signoff(self):
        self.assert_independent("await_evaluator")

    def test_missing_authority_is_retained_without_preventing_independent_work(self):
        self.state["pending_evaluations"][0]["route"] = "missing_authority"
        self.assert_independent("missing_evaluator_authority")

    def test_inconclusive_material_does_not_retry_same_evaluation_or_mask_other_work(self):
        self.state["pending_evaluations"][0].update(result="inconclusive", findings=[{"id": "original"}],
            evaluation_summary="Current-candidate raw material missing", evidence_digest="sha256:original")
        self.assert_independent("await_evaluator_input")

    def test_out_of_scope_failed_review_retains_recovery_gate_while_other_work_continues(self):
        self.state["pending_evaluations"][0].update(result="fail", findings=[{"id": "original"}],
            evaluation_summary="Needs reviewed recovery", evidence_digest="sha256:original",
            repair_task="acceptance", repair_owner="backend", repair_write_allow=[],
            repair_write_deny=["protected"], repair_requires_reviewed_plan=True)
        self.assert_independent("review_failed")

    def test_running_other_draft_is_supervised_without_replaying_blocked_review(self):
        self.state["ready_tasks"] = []
        self.state["tasks"]["producer"].update(status="RUNNING", controller_status="PLANNING")
        self.assertEqual(self.assert_independent("await_evaluator")["kind"], "prepare_task")

    def test_several_task_local_blockers_remain_visible(self):
        self.state["tasks"]["second_acceptance"] = {"status": "RUNNING", "controller_status": "REVIEWING"}
        self.data["tasks"]["second_acceptance"] = {"handoff_to": []}
        self.state["pending_evaluations"].append({"task_id": "second_acceptance", "check_id": "code",
            "type": "review", "executor_registered": False})
        action = self.assert_independent("await_evaluator")
        self.assertEqual({x["task_id"] for x in action["deferred_blockers"]}, {"acceptance", "second_acceptance"})

    def test_serial_policy_never_admits_another_task_through_this_route(self):
        self.data["plan"].pop("native_parallel")
        action = next_action(self.state, self.data)
        self.assertEqual(action["kind"], "await_evaluator")
        self.assertFalse(action["continue_work"])

    def test_dependent_or_slot_blocked_work_is_not_invented_as_ready(self):
        self.state["ready_tasks"] = []
        action = next_action(self.state, self.data)
        self.assertEqual(action["kind"], "await_evaluator")
        self.assertFalse(action["continue_work"])
        self.assertNotIn("deferred_blockers", action)

    def test_pause_cancel_terminal_budget_and_input_conflict_remain_global_gates(self):
        for status in ("PAUSED", "CANCELLED", "BLOCKED"):
            with self.subTest(status=status):
                self.state["status"] = status
                action = next_action(self.state, self.data)
                self.assertEqual(action["kind"], status.lower())
                self.assertFalse(action["continue_work"])
        self.state.update(status="ACTIVE", inputs_current=False)
        self.assertEqual(next_action(self.state, self.data)["kind"], "input_conflict")
        self.state["inputs_current"] = True
        self.state["evidence_problems"] = [{"error": "artifact corrupted"}]
        self.assertEqual(next_action(self.state, self.data)["kind"], "evidence_problem")

    def test_inflight_effect_or_recovery_is_not_hidden_by_continuation(self):
        self.state["tasks"]["acceptance"]["pending_process"] = {"pid": 123}
        self.assertEqual(next_action(self.state, self.data)["kind"], "pending_effect")
        self.state["tasks"]["acceptance"].pop("pending_process")
        self.state["operations"] = [{"id": "original-op", "status": "RUNNING", "recovery_required": True}]
        action = next_action(self.state, self.data)
        self.assertEqual(action["kind"], "recover_operation")
        self.assertEqual(action["arguments"]["operation_id"], "original-op")
        self.assertNotIn("deferred_blockers", action)

    def test_real_user_question_and_verification_reconciliation_still_take_priority(self):
        self.state["pending_questions"] = [{"blocking": True, "answers": [], "question": "Actual missing decision"}]
        self.assertEqual(next_action(self.state, self.data)["kind"], "answer_questions")
        self.state["pending_questions"] = []
        self.state["verification_sessions"] = [{"id": "original-session", "unresolved_attempt": True}]
        action = next_action(self.state, self.data)
        self.assertEqual(action["kind"], "inspect_verification")
        self.assertFalse(action["replay_allowed"])


class ContinuationIntegrationTests(unittest.TestCase):
    setUp = fixture.NativePreflightTests.setUp
    start = fixture.NativePreflightTests.start
    configure = fixture.NativePreflightTests.configure
    complete = fixture.NativePreflightTests.complete
    receive = fixture.NativePreflightTests.receive

    def test_actual_human_gate_remains_pending_while_disjoint_producer_draft_is_prepared(self):
        self.configure(check_type="human", key_role="human")
        task = load(self.root / ".loop/tasks/implement.json")
        producer = deepcopy(task); producer.update(task_id="producer")
        producer["scope"]["write_allow"] = ["measurement.txt"]
        producer["checks"] = [{"id": "measurement", "type": "command", "description": "Read actual fixture output",
            "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('measurement.txt').read_text() == 'actual'"],
            "cwd": ".", "timeout_seconds": 10}]
        producer["criteria"] = [{"id": "measurement", "description": "Actual fixture producer output", "check_ids": ["measurement"]}]
        (self.root / ".loop/tasks/producer.json").write_text(json.dumps(producer))
        plan = self.owner.plan
        plan["native_parallel"] = {"max_parallel": 2, "max_refreshes": 2}
        plan["tasks"][0]["draft_paths"] = ["slug.py"]
        plan["tasks"][1]["draft_paths"] = ["slug.py"]
        second = deepcopy(plan["tasks"][0]); second.update(id="producer", task_file=".loop/tasks/producer.json",
            phase="verify", role="tester", engine_file=None, draft_paths=["measurement.txt"], outputs=["measurement.txt"], depends_on=[])
        plan["tasks"].append(second)
        final = deepcopy(task); final["task_id"] = "final"
        (self.root / ".loop/tasks/final.json").write_text(json.dumps(final))
        final_entry = deepcopy(plan["tasks"][1]); final_entry.update(id="final", task_file=".loop/tasks/final.json",
            engine_file=None, depends_on=["implement", "verify", "producer"])
        plan["tasks"].append(final_entry); plan["final_task"] = "final"
        self.owner.fixture.save_plan()
        self.start(); self.complete("implement"); self.receive("implement")
        blocked = self.complete("verify")
        before = self.service._data(self.team_id)
        child_id = before["records"]["verify"]["child_run_id"]
        original = self.store.get(child_id)
        packet = self.service.next(self.team_id)
        self.assertEqual(packet["team_assignment"]["task_id"], "producer")
        self.assertEqual(packet["next_action"]["deferred_blockers"][0]["check_id"], "external")
        self.assertEqual(packet["continuation_instruction"], packet["next_action"]["continuation_instruction"])
        saved = json.loads(Path(packet["workbench"]["context_path"]).read_text())
        self.assertEqual(saved["continuation_instruction"], packet["continuation_instruction"])
        after = self.service.progress(self.team_id)
        self.assertEqual(after["tasks"]["verify"]["status"], "RUNNING")
        self.assertFalse(after["final_candidate_current"])
        self.assertFalse(any(item["check_id"] == "external" for item in after["check_results"]))
        current = self.store.get(child_id)
        self.assertEqual(current["state"]["usage"], original["state"]["usage"])
        self.assertEqual(current["selected_evidence"], original["selected_evidence"])
        self.assertEqual(blocked["pending_evaluations"], after["pending_evaluations"])
        self.assertFalse(self.owner.transport.requests)
