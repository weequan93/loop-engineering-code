"""Offline host progression: real records/checks, deterministic planning only."""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, load
from loop_engineering.native_host import render_progress
from loop_engineering.native_mcp import NativeMCPServer
from tests import test_native_host as fixture


class NativeFlowTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start
    specification = fixture.NativeHostTests.specification
    complete = fixture.NativeHostTests.complete
    receive = fixture.NativeHostTests.receive
    prepare_review = fixture.NativeHostTests.prepare_review

    def questions(self):
        return [{"id": f"scope-{n}", "question": f"Confirm scope {n}?",
                 "reason": "Actual fixture scope needs a human decision", "blocking": True} for n in range(3)]

    def ask_three(self, *, planning=False):
        self.start(planning=planning, specification=not planning)
        packet = self.service.next(self.team_id)
        if planning:
            response = deepcopy(self.owner.plan_response)
        else:
            response = self.specification()
            response.update(status="needs_input", spec_markdown="")
        response["questions"] = self.questions()
        return self.service.stage_submit(self.team_id, packet["request_id"], response)

    def typed_plan(self, packet):
        response = self.owner.plan_response
        return self.service.plan_submit(self.team_id, packet["request_id"],
            json.loads(response["selection_json"]), json.loads(response["workflow_json"]),
            [{"path": c["path"], "task": json.loads(c["task_json"]), "engine_path": c["engine_path"],
              "engine": json.loads(c["engine_json"]) if c["engine_json"] else None} for c in response["contracts"]],
            response["summary"])

    def activity(self, packet, *, activity_id="foundation", status="RUNNING", detail="Actual offline host assignment"):
        return self.service.planning_activity(self.team_id, packet["request_id"], activity_id,
            "architect", "Foundation planning", "/fixture/foundation", status, detail)

    def answer_three(self, *, planning=False):
        self.ask_three(planning=planning)
        for n in range(3):
            result = self.service.answer(self.team_id, f"scope-{n}", f"Actual answer {n}")
            self.assertEqual(result["status"], "ACTIVE" if n == 2 else "AWAITING_INPUT")
            self.assertEqual(result["planning"]["questions_remaining"], 2 - n)
            self.assertEqual(result["next_action"]["continue_work"], n == 2)
        packet = self.service.next(self.team_id)
        self.assertEqual(packet["next_action"]["kind"], "prepare_plan" if planning else "prepare_spec")
        recorded = load(self.root / ".loop/questions.json")["questions"]
        self.assertEqual([q["answers"] for q in recorded], [[f"Actual answer {n}"] for n in range(3)])
        self.assertEqual(self.owner.transport.requests, [])

    def test_spec_three_answers_stay_waiting_until_last_without_resume_or_model_calls(self):
        self.answer_three()

    def test_intake_three_answers_stay_waiting_until_last_without_resume_or_model_calls(self):
        self.answer_three(planning=True)

    def test_paused_assigned_task_remains_recorded_without_claiming_active_execution(self):
        self.start()
        packet = self.service.next(self.team_id)
        task_id = packet["team_assignment"]["task_id"]
        result = self.service.control(self.team_id, "pause")
        self.assertEqual(result["next_action"]["kind"], "paused")
        self.assertFalse(result["next_action"]["continue_work"])
        self.assertEqual(result["tasks"][task_id]["status"], "RUNNING")
        content = render_progress(result)
        self.assertIn("等待恢复", content)
        self.assertNotIn("正在执行", content)
        self.assertEqual(self.owner.transport.requests, [])

    def test_document_answers_typed_plan_tasks_checks_handoffs_and_final_gate_form_one_flow(self):
        self.ask_three()
        for n in range(3):
            self.service.answer(self.team_id, f"scope-{n}", f"Confirmed {n}")
        packet = self.service.next(self.team_id)
        specification = self.specification()
        specification["sources"].extend([f"answer:scope-{n}:1" for n in range(3)])
        result = self.service.stage_submit(self.team_id, packet["request_id"], specification)
        self.assertEqual(result["next_action"]["kind"], "prepare_plan")
        packet = self.service.next(self.team_id)
        self.assertIn("task_schema", packet)
        self.assertIn("selection_schema", packet)
        self.assertEqual(packet["preferred_submission"]["tool"], "loop_plan_submit")
        self.activity(packet, status="COMPLETE", detail="Actual fixture planner inspected requirements and returned its plan")
        self.assertEqual(self.service.progress(self.team_id)["task_counts"]["total"], 0)
        result = self.typed_plan(packet)
        self.assertEqual(result["stage"], "EXECUTION")
        self.assertEqual(result["next_action"]["kind"], "prepare_task")
        for task_id in ("implement", "verify"):
            packet = self.service.next(self.team_id)
            self.assertEqual(packet["team_assignment"]["task_id"], task_id)
            result = self.service.task_submit(self.team_id, task_id,
                packet["team_assignment"]["request_id"], self.owner.respond(packet))
            action = result["next_action"]
            self.assertEqual(action["kind"], "receive_handoff")
            result = self.service.receive(**action["arguments"], accept=True,
                note="Actual offline recipient inspected bound outputs and real local check results")
        self.assertEqual(result["next_action"]["kind"], "complete")
        self.assertFalse(result["next_action"]["continue_work"])
        self.assertTrue(result["final_candidate_current"])
        self.assertEqual(result["task_counts"]["COMPLETE"], 2)
        self.assertTrue(result["check_results"])
        self.assertTrue(all(c["result"] == "pass" for c in result["check_results"]))
        self.assertEqual(self.owner.transport.requests, [])
        self.service.team.teams.audit(self.team_id)

    def test_next_prepares_one_idempotent_request_without_dispatching_or_inventing_tasks(self):
        self.start(planning=True)
        with patch("subprocess.Popen", side_effect=AssertionError("No model or worker dispatch")):
            first = self.service.next(self.team_id)
            second = self.service.next(self.team_id)
        self.assertEqual(first["request_id"], second["request_id"])
        result = self.service.progress(self.team_id)
        self.assertEqual(result["task_counts"]["total"], 0)
        self.assertEqual(result["next_action"]["submission_tool"], "loop_plan_submit")
        self.assertIn("计划待提交", render_progress(result))
        self.assertNotIn("Replace with", render_progress(result))

    def test_typed_plan_keeps_empty_stale_and_invalid_plan_gates(self):
        self.start(planning=True)
        packet = self.service.next(self.team_id)
        selection = json.loads(self.owner.plan_response["selection_json"])
        original = (self.root / ".loop/workflow-tasks.json").read_bytes()
        with self.assertRaises(ContractError):
            self.service.plan_submit(self.team_id, packet["request_id"], selection,
                {**json.loads(self.owner.plan_response["workflow_json"]), "tasks": []}, [], "Empty plan")
        self.assertEqual((self.root / ".loop/workflow-tasks.json").read_bytes(), original)
        fresh = self.service.next(self.team_id)
        with self.assertRaisesRegex(ContractError, "matching current request"):
            self.typed_plan(packet)
        self.assertNotEqual(packet["request_id"], fresh["request_id"])

    def test_planning_activity_is_durable_visible_escaped_and_separate_from_task_completion(self):
        self.start(planning=True)
        packet = self.service.next(self.team_id)
        result = self.activity(packet, status="COMPLETE", detail="<script>host report</script>")
        self.assertEqual(result["task_counts"]["total"], 0)
        self.assertEqual(result["task_counts"]["COMPLETE"], 0)
        self.assertTrue(result["planning"]["activities"][0]["current"])
        content = render_progress(result)
        self.assertIn("Foundation planning", content)
        self.assertIn("/fixture/foundation", content)
        self.assertIn("&lt;script&gt;", content)
        self.assertNotIn("<script>", content)
        self.assertIn("规划完成不等于开发或验收通过", content)
        restored = self.service.progress(self.team_id)
        self.assertEqual(restored["planning"]["activities"], result["planning"]["activities"])
        self.assertIn("host.planning_activity_recorded", [e["event"] for e in restored["recent_events"]])

    def test_changed_candidate_labels_planning_history_and_refuses_stale_activity(self):
        self.start(planning=True)
        packet = self.service.next(self.team_id)
        self.activity(packet, status="COMPLETE")
        (self.root / "slug.py").write_text("# actual changed source\n")
        result = self.service.progress(self.team_id)
        self.assertFalse(result["planning"]["activities"][0]["current"])
        self.assertIn("历史规划", render_progress(result))
        with self.assertRaisesRegex(ContractError, "Project changed"):
            self.activity(packet)
        fresh = self.service.next(self.team_id)
        self.assertNotEqual(fresh["request_id"], packet["request_id"])
        with self.assertRaisesRegex(ContractError, "current bound"):
            self.activity(packet)
        self.assertFalse(self.service.progress(self.team_id)["planning"]["activities"][0]["current"])

    def test_unknown_role_blank_report_and_stopped_activity_are_refused(self):
        self.start(planning=True)
        packet = self.service.next(self.team_id)
        with self.assertRaisesRegex(ContractError, "Unknown planning role"):
            self.service.planning_activity(self.team_id, packet["request_id"], "x", "invented", "Work", "/fixture/x", "RUNNING", "Assignment")
        with self.assertRaises(ContractError):
            self.activity(packet, detail=" ")
        self.service.control(self.team_id, "pause")
        result = self.service.next(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "paused")
        self.assertFalse(result["next_action"]["continue_work"])
        with self.assertRaises(ContractError):
            self.activity(packet)
        self.assertEqual(self.service.progress(self.team_id)["planning"]["activities"], [])

    def test_planning_activity_limit_keeps_actual_records_bounded(self):
        self.start(planning=True)
        packet = self.service.next(self.team_id)
        for n in range(32):
            self.activity(packet, activity_id=f"fixture-{n}")
        with self.assertRaisesRegex(ContractError, "limit reached"):
            self.activity(packet, activity_id="overflow")
        result = self.activity(packet, activity_id="fixture-0", status="COMPLETE")
        self.assertEqual(len(result["planning"]["activities"]), 32)

    def test_next_refuses_existing_controller_ownership_without_migrating_it(self):
        original = self.owner.team.start(self.root)
        result = self.service.progress(original["team_id"])
        self.assertEqual(result["next_action"]["kind"], "existing_controller")
        self.assertFalse(result["next_action"]["continue_work"])
        with self.assertRaisesRegex(ContractError, "native-host team"):
            self.service.next(original["team_id"])
        self.assertNotIn("native_host", self.service.team.teams.get(original["team_id"]))

    def test_changed_frozen_inputs_stop_continuation_and_cancel_keeps_priority(self):
        self.start(planning=True)
        self.service.next(self.team_id)
        path = self.root / ".loop/spec.md"
        path.write_text(path.read_text() + "\nActual changed scope\n")
        result = self.service.next(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "input_conflict")
        self.assertFalse(result["next_action"]["continue_work"])
        self.service.control(self.team_id, "cancel")
        result = self.service.next(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "cancelled")
        self.assertFalse(result["next_action"]["continue_work"])

    def test_cancellation_arriving_during_activity_commit_prevents_further_progress(self):
        self.start(planning=True)
        packet = self.service.next(self.team_id)
        original = self.service.team.teams.save
        def cancel_during_save(data, event):
            if event == "host.planning_activity_recorded":
                self.service.team.teams.signal(self.team_id, "CANCELLED")
            return original(data, event)
        with patch.object(self.service.team.teams, "save", cancel_during_save):
            result = self.activity(packet)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(result["next_action"]["kind"], "cancelled")
        self.assertEqual(self.service.team.teams.get(self.team_id)["status"], "CANCELLED")

    def test_missing_independent_evaluator_is_a_visible_stop_never_a_new_coding_task(self):
        self.prepare_review()
        result = self.service.next(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "await_evaluator")
        self.assertFalse(result["next_action"]["continue_work"])
        self.assertEqual(result["next_action"]["tool"], "loop_evaluation_request")
        self.assertEqual(result["pending_evaluations"][0]["check_id"], "external")
        self.assertFalse(result["pending_evaluations"][0]["executor_registered"])
        self.assertEqual(result["task_counts"]["COMPLETE"], 1)
        self.assertIsNone(result["final_candidate_current"])

    def test_next_recovers_failed_command_with_same_child_and_cumulative_iterations(self):
        self.start()
        packet = self.service.next(self.team_id)
        run_id = self.service.progress(self.team_id)["tasks"]["implement"]["child_run_id"]
        step = self.owner.respond(packet)
        step["changes"][0]["new_content"] = "def slug(value): return 'incorrect'\n"
        # A long verification can outlive the original writer's deadline. Once
        # that request is consumed, its deadline must not block the next repair.
        self.service.clock = lambda: 9999999999
        result = self.service.task_submit(self.team_id, "implement", packet["team_assignment"]["request_id"], step)
        before = self.store.get(run_id)["state"]["iteration"]
        self.assertEqual(result["next_action"]["kind"], "prepare_task")
        packet = self.service.next(self.team_id)
        result = self.service.task_submit(self.team_id, "implement", packet["team_assignment"]["request_id"], self.owner.respond(packet))
        self.assertEqual(result["tasks"]["implement"]["child_run_id"], run_id)
        self.assertGreater(self.store.get(run_id)["state"]["iteration"], before)
        self.assertEqual(result["next_action"]["kind"], "receive_handoff")

    def test_new_mcp_tools_are_typed_and_no_extra_authority_is_exposed(self):
        server = NativeMCPServer(self.service)
        server.message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
        server.message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = server.message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        self.assertTrue({"loop_next", "loop_plan_submit", "loop_planning_activity"}.issubset(names))
        self.assertNotIn("shell", names)
        self.start(planning=True)
        rejected = server.message({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "loop_next", "arguments": {"team_id": self.team_id, "argv": ["codex"]}}})
        self.assertIn("error", rejected)
        with patch("subprocess.Popen", side_effect=AssertionError("No nested agent")):
            accepted = server.message({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "loop_next", "arguments": {"team_id": self.team_id}}})
        self.assertFalse(accepted["result"]["isError"])
