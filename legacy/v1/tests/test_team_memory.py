"""Durable role memory: actual local effects and injected hosts, no models."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

from loop_engineering.cli import main
from loop_engineering.contracts import ContractError, load
from loop_engineering.models import ModelError
from loop_engineering.store import Store
from loop_engineering.team_engine import TeamController
from loop_engineering.team_memory import byte_size, fit_request
from loop_engineering.workspace import atomic_write, byte_digest
from tests import test_team_automation as fixture


class TeamMemoryTests(unittest.TestCase):
    def setUp(self):
        self.owner = fixture.AutomationTests()
        self.owner.setUp()
        self.addCleanup(self.owner.doCleanups)
        self.root, self.team, self.auto = self.owner.root, self.owner.team, self.owner.auto

    def start(self):
        self.owner.start()
        self.team_id = self.owner.team_id

    def memory(self, **kwargs):
        return self.team.memory(self.team_id, **kwargs)

    def test_restart_projects_same_memory_without_dispatch_or_budget_change(self):
        self.start()
        self.auto.run(self.team_id)
        before = self.team.teams.get(self.team_id)
        memory = self.memory()
        self.assertTrue(any(e["kind"] == "agent_report" for e in memory["entries"]))
        restarted = TeamController(Store(self.team.store.directory))
        with patch("subprocess.Popen", side_effect=AssertionError("Memory retrieval must not dispatch")):
            self.assertEqual(restarted.memory(self.team_id), memory)
        self.assertEqual(self.team.teams.get(self.team_id), before)
        self.assertEqual(memory["checkpoint"]["resume_team_id"], self.team_id)
        self.assertEqual(memory["checkpoint"]["dispatches"], 4)

    def test_memory_refuses_a_child_from_a_different_task(self):
        self.start()
        self.auto.run(self.team_id)
        with self.team.teams.writer(self.team_id) as data:
            data["records"]["implement"]["child_run_id"] = data["records"]["verify"]["child_run_id"]
            self.team.teams.save(data, "test.mismatched_child")
        with self.assertRaisesRegex(ContractError, "Memory child does not match"):
            self.memory()

    def test_real_answers_and_stage_reports_reach_coordinator_workers_and_receipts(self):
        self.owner.coordinator_response()
        self.owner.plan_response["questions"] = [{"id": "scope", "question": "Keep compatible input?",
            "reason": "Define scope", "blocking": True}]
        self.start()
        self.assertEqual(self.auto.run(self.team_id)["status"], "AWAITING_INPUT")
        self.assertEqual(next(e for e in self.memory()["entries"] if e["kind"] == "open_question")["answers"], [])
        self.auto.answer(self.team_id, "scope", "是的，保留其他输入的现有行为。")
        self.owner.plan_response["questions"] = []
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        for request in self.owner.transport.requests[1:]:
            context = request.get("team_assignment", request.get("context", {}))
            memory = context["memory"]
            answer = next(e for e in memory["entries"] if e["kind"] == "human_answers")
            self.assertEqual(answer["answers"], ["是的，保留其他输入的现有行为。"])
            self.assertEqual(answer["source"]["path"], ".loop/questions.json")
        worker = next(r for r in self.owner.transport.requests if r["kind"] == "team-host-request")
        self.assertEqual(worker["team_assignment"]["memory"]["task_id"], "implement")
        self.assertTrue(any(e["kind"] == "stage_report" for e in worker["team_assignment"]["memory"]["entries"]))
        self.assertEqual(len(self.team.teams.get(self.team_id)["stage_reports"]), 2)

    def test_independent_review_and_acceptance_do_not_receive_agent_self_assessments(self):
        self.owner.native_acceptance()
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        reviews = [r for r in self.owner.transport.requests if r["kind"] == "team-evaluator-request"]
        self.assertEqual(len(reviews), 2)
        for request in reviews:
            memory = request["inspection"]["memory"]
            self.assertEqual(memory["candidate"], request["request"]["snapshot_digest"])
            self.assertFalse(any(e["kind"] in {"agent_report", "stage_report"} for e in memory["entries"]))
            indexes = [e for e in memory["entries"] if e["kind"] == "check_index"]
            self.assertTrue(indexes)
            self.assertTrue(all(e["source"]["evidence_digest"].startswith("sha256:") for e in indexes))
            self.assertTrue(all("receipts" not in e for e in memory["entries"] if e["kind"] == "handoff_index"))
        acceptance = self.memory(role="acceptance", task_id="verify")
        self.assertFalse(any(e["kind"] == "agent_report" for e in acceptance["entries"]))

    def test_repaired_attempt_stays_invalidated_even_for_identical_candidate(self):
        self.owner.plan["tasks"][1]["repair_task"] = "implement"
        self.owner.fixture.save_plan()
        self.owner.transport.reject_once = True
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        memory = self.memory(role="backend", task_id="implement", max_bytes=200000)
        old = [e for e in memory["entries"] if e["source"].get("round") == 1 and "record" in e["source"]]
        self.assertTrue(old)
        self.assertTrue(all(e["validity"] == "invalidated" for e in old))
        same = [e for e in old if e["kind"] == "handoff_index" and e["handoff"]["candidate"] == memory["candidate"]]
        self.assertTrue(same, "Content equality must not revive rejected receipts")
        workers = [r for r in self.owner.transport.requests if r["kind"] == "team-host-request"]
        self.assertTrue(any(e["kind"] == "repair_finding" for e in workers[-2]["team_assignment"]["memory"]["entries"]))

    def test_changed_source_marks_past_checks_and_handoffs_historical(self):
        self.start()
        self.auto.run(self.team_id)
        (self.root / "slug.py").write_text((self.root / "slug.py").read_text() + "\n# A new candidate\n")
        memory = self.memory(max_bytes=200000)
        indexes = [e for e in memory["entries"] if e["kind"] in {"check_index", "handoff_index"}]
        self.assertTrue(indexes)
        self.assertTrue(all(e["validity"] == "historical_candidate" for e in indexes))
        self.assertFalse(self.team.status(self.team_id)["final_candidate_current"])

    def test_role_filters_unrelated_parallel_task_and_its_repair_history(self):
        self.owner.fixture.fixture.select_team(all_roles=True)
        contract = load(self.root / ".loop/tasks/implement.json")
        contract["task_id"] = "unrelated"
        (self.root / ".loop/tasks/unrelated.json").write_text(json.dumps(contract))
        unrelated = {**self.owner.plan["tasks"][0], "id": "unrelated", "role": "frontend",
            "task_file": ".loop/tasks/unrelated.json"}
        self.owner.plan["tasks"].append(unrelated)
        self.owner.fixture.save_plan()
        self.start()
        with self.team.teams.writer(self.team_id) as data:
            data["automation"]["feedback"].append({"failed_task": "unrelated", "repair_task": "unrelated",
                "round": 1, "finding": "Unrelated frontend finding"})
            self.team.teams.save(data, "test.memory_scopes")
        memory = self.memory(role="backend", task_id="implement")
        self.assertFalse(any(e["task_id"] == "unrelated" for e in memory["entries"]))
        self.assertNotIn("Unrelated frontend finding", json.dumps(memory))
        self.assertTrue(any(e["task_id"] == "unrelated" for e in self.memory()["entries"]))

    def test_stage_report_commits_once_with_control_files_after_crash(self):
        self.owner.coordinator_response()
        self.start()
        def crash_at_install(path, content):
            if path.is_relative_to(self.root):
                raise RuntimeError("Injected crash")
            return atomic_write(path, content)
        with patch("loop_engineering.team_automation.atomic_write", side_effect=crash_at_install):
            with self.assertRaisesRegex(RuntimeError, "Injected crash"):
                self.auto.prepare(self.team_id)
        pending = self.team.teams.get(self.team_id)
        self.assertIsNotNone(pending["pending_control"])
        self.assertIn("pending_memory_report", pending)
        self.assertEqual(pending.get("stage_reports", []), [])
        self.assertFalse(any(e["kind"] == "stage_report" for e in self.memory()["entries"]))
        self.auto.prepare(self.team_id)
        current = self.team.teams.get(self.team_id)
        self.assertNotIn("pending_memory_report", current)
        self.assertEqual(len(current["stage_reports"]), 1)
        self.assertEqual(current["stage_reports"][0]["scenario_digest"], current["scenario_digest"])
        self.team.teams.audit(self.team_id)
        self.assertEqual(len(self.owner.transport.requests), 1)

    def test_received_planning_response_reuses_frozen_memory_after_processing_crash(self):
        self.owner.coordinator_response()
        self.start()
        with patch.object(self.auto, "_preparation_result", side_effect=RuntimeError("Crash after response")):
            with self.assertRaisesRegex(RuntimeError, "Crash after response"):
                self.auto.prepare(self.team_id)
        saved = self.team.teams.get(self.team_id)["preparation_request"]["packet"]
        self.assertEqual(len(self.owner.transport.requests), 1)
        self.assertGreater(self.team.teams.get(self.team_id)["revision"], saved["context"]["memory"]["team_revision"])
        self.auto.prepare(self.team_id)
        self.assertEqual(len(self.owner.transport.requests), 1, "An already paid result must not be regenerated")
        current = self.team.teams.get(self.team_id)
        self.assertNotIn("preparation_request", current)
        self.assertEqual(len(current["stage_reports"]), 1)

    def test_received_setup_response_reuses_frozen_memory_after_processing_crash(self):
        self.owner.coordinator_response()
        (self.root / ".loop/spec.md").write_text("")
        (self.root / "docs").mkdir()
        spec = "# Existing input compatibility\nPreserve nonempty input behavior.\n"
        (self.root / "docs/requirements.md").write_text(spec)
        self.owner.respond = lambda request: {"status": "ready", "summary": "Grounded spec fixture",
            "spec_markdown": spec, "sources": ["docs/requirements.md"], "questions": []}
        self.start()
        with patch("loop_engineering.spec_setup.validate_result", side_effect=RuntimeError("Crash after setup response")):
            with self.assertRaisesRegex(RuntimeError, "Crash after setup response"):
                self.auto.prepare_spec(self.team_id)
        self.assertEqual(len(self.owner.transport.requests), 1)
        self.auto.prepare_spec(self.team_id)
        self.assertEqual(len(self.owner.transport.requests), 1)
        current = self.team.teams.get(self.team_id)
        self.assertNotIn("specification_request", current)
        self.assertEqual(current["specification"]["state"], "READY")
        self.assertEqual(len(current["stage_reports"]), 1)

    def test_failed_dispatch_retry_receives_actual_failure_and_keeps_unknown_usage(self):
        self.owner.coordinator_response()
        self.start()
        post = self.owner.transport.post
        failed = False
        def fail_once(operation, payload, **kwargs):
            nonlocal failed
            if operation == "respond" and not failed:
                failed = True
                raise ModelError("transient", "Injected transport timeout; no received response")
            return post(operation, payload, **kwargs)
        self.owner.transport.post = fail_once
        with self.assertRaisesRegex(ModelError, "Injected transport timeout"):
            self.auto.prepare(self.team_id)
        self.auto.prepare(self.team_id)
        memory = self.owner.transport.requests[-1]["context"]["memory"]
        failure = next(e for e in memory["entries"] if e["kind"] == "operation_state")
        self.assertEqual(failure["status"], "FAILED")
        self.assertIn("Injected transport timeout", failure["error"])
        self.assertEqual(self.team.status(self.team_id)["team_budget"]["dispatches"], 2)
        self.assertIsNone(self.team.status(self.team_id)["team_budget"]["actual_tokens"])

    def test_bounded_unicode_memory_omits_entries_and_keeps_complete_records(self):
        self.start()
        with self.team.teams.writer(self.team_id) as data:
            data["stage_reports"] = [{"stage": "INTAKE", "role": "coordinator", "candidate": "sha256:" + "a" * 64,
                "summary": "这是一份很长的历史报告。" * 5000, "response_digest": "sha256:" + "b" * 64,
                "scenario_digest": data["scenario_digest"], "revision": data["revision"], "source_refs": []}]
            self.team.teams.save(data, "test.long_report")
        before = self.team.teams.get(self.team_id)
        memory = self.memory(max_bytes=2048)
        self.assertLessEqual(byte_size(memory), 2048)
        self.assertGreater(memory["coverage"]["omitted"], 0)
        self.assertEqual(memory["coverage"]["eligible"], memory["coverage"]["included"] + memory["coverage"]["omitted"])
        self.assertEqual(self.team.teams.get(self.team_id), before)
        self.assertTrue(any(e["kind"] == "stage_report" for e in self.memory(max_bytes=300000)["entries"]))

    def test_request_trimming_keeps_mandatory_instructions_and_tracks_memory_omissions(self):
        self.start()
        memory = self.memory()
        request = {"context": {"instructions": "MANDATORY", "sources": [{"path": "large", "content": "x" * 3000}],
            "memory": memory}}
        original_count = memory["coverage"]["included"]
        minimal = deepcopy(request)
        minimal["context"]["sources"] = []
        minimal["context"]["memory"]["entries"] = []
        minimal["context"]["memory"]["coverage"].update(included=0, omitted=memory["coverage"]["eligible"])
        minimal["omitted_source_excerpts"] = 1
        fit_request(request, byte_size(minimal) + 5)
        self.assertEqual(request["context"]["instructions"], "MANDATORY")
        self.assertEqual(request["omitted_source_excerpts"], 1)
        self.assertEqual(request["context"]["memory"]["entries"], [])
        self.assertEqual(request["context"]["memory"]["coverage"]["omitted"], original_count)
        with self.assertRaisesRegex(ContractError, "Required team host context"):
            fit_request(request, 100)

    def test_cli_inspection_is_offline_and_matches_source_linked_memory(self):
        self.start()
        output, errors = io.StringIO(), io.StringIO()
        with patch("subprocess.Popen", side_effect=AssertionError("No live host")), redirect_stdout(output), redirect_stderr(errors):
            code = main(["team-memory", self.team_id, "--state-dir", str(self.team.store.directory),
                "--role", "backend", "--task", "implement", "--max-bytes", "8192"])
        self.assertEqual(code, 0, errors.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result, self.memory(role="backend", task_id="implement", max_bytes=8192))
        self.assertEqual(result["entries"][0]["source"]["team_id"], self.team_id)

    def test_changed_frozen_inputs_or_forged_projection_refuse_memory(self):
        self.start()
        path = self.root / ".loop/questions.json"
        original = path.read_bytes()
        path.write_text('{"schema_version":"1.0","questions":[]}\n')
        with self.assertRaisesRegex(ContractError, "changed"):
            self.memory()
        path.write_bytes(original)
        with self.team.store.connect() as connection:
            data = self.team.teams.get(self.team_id)
            data["reason"] = "Forged memory"
            connection.execute("UPDATE teams SET data=? WHERE id=?", (json.dumps(data), self.team_id))
        with self.assertRaisesRegex(ContractError, "authenticated checkpoint"):
            self.memory()

    def test_failed_and_unresolved_operations_are_retained_without_fake_success(self):
        self.start()
        with self.team.teams.writer(self.team_id) as data:
            data["automation"]["operations"]["owned-operation"] = {"status": "FAILED", "phase": "command",
                "request": {"kind": "team-coordinator-request"}, "request_digest": "sha256:" + "c" * 64,
                "error": "Actual timeout; usage unknown"}
            self.team.teams.save(data, "test.failed_operation")
        memory = self.memory()
        failed = next(e for e in memory["entries"] if e["kind"] == "operation_state")
        self.assertEqual(failed["error"], "Actual timeout; usage unknown")
        self.assertEqual(failed["status"], "FAILED")
        self.assertFalse(any(e["kind"] in {"agent_report", "check_index"} for e in memory["entries"]))
        self.assertEqual(memory["checkpoint"]["dispatches"], 1)
        self.assertTrue(all(e["source"].get("team_id") == self.team_id for e in memory["entries"]))

    def test_setup_answers_and_spec_memory_continue_into_every_development_stage(self):
        self.owner.coordinator_response()
        (self.root / ".loop/spec.md").write_text("")
        (self.root / "docs").mkdir()
        spec = "# Library requirements\nWhitespace-only input returns empty; preserve other inputs.\n"
        (self.root / "docs/requirements.md").write_text(spec)
        response = {"status": "needs_input", "summary": "Clarify existing input compatibility",
            "spec_markdown": "", "sources": ["docs/requirements.md"], "questions": [
                {"id": "scope", "question": "Preserve other inputs?", "reason": "Compatibility", "blocking": True}]}
        original = self.owner.respond
        def respond(request):
            return deepcopy(response) if request["kind"] == "team-specification-request" else original(request)
        self.owner.respond = respond
        self.start()
        self.assertEqual(self.auto.run(self.team_id)["status"], "AWAITING_INPUT")
        self.auto.answer(self.team_id, "scope", "Yes, preserve existing nonempty behavior.")
        response.update(status="ready", summary="Spec prepared from docs and actual answer", spec_markdown=spec,
            sources=["docs/requirements.md", "answer:scope:1"], questions=[])
        prepared = self.auto.run(self.team_id, setup_only=True)
        self.assertEqual(prepared["stage"], "INTAKE")
        setup_memory = self.owner.transport.requests[-1]["context"]["memory"]
        self.assertTrue(any(e["kind"] == "human_answers" for e in setup_memory["entries"]))
        result = self.auto.run(self.team_id)
        self.assertEqual(result["status"], "COMPLETE", result["reason"])
        for request in self.owner.transport.requests[2:]:
            memory = request.get("team_assignment", request.get("context", {}))["memory"]
            self.assertEqual(memory["spec_source"]["sha256"], byte_digest(spec.encode()))
            self.assertTrue(any(e["kind"] == "human_answers" for e in memory["entries"]))
        reports = self.team.teams.get(self.team_id)["stage_reports"]
        self.assertEqual([r["stage"] for r in reports], ["SPEC", "SPEC", "INTAKE"])
        self.assertEqual(self.team.teams.get(self.team_id)["automation"]["reworks"], 0)


if __name__ == "__main__":
    unittest.main()
