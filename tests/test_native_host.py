"""Native conversations use deterministic proposals and actual local checks."""

from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from loop_engineering.contracts import ContractError, ROOT, load
from loop_engineering.native_host import NativeHostService, render_progress
from loop_engineering.native_mcp import NativeMCPServer
from loop_engineering.workspace import atomic_write
from tests import test_team_automation as fixture


SPEC = "# Library fix\nPreserve existing behavior and return an empty slug for whitespace.\n"


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        self.owner = fixture.AutomationTests()
        self.owner.setUp()
        self.addCleanup(self.owner.doCleanups)
        self.root, self.store = self.owner.root, self.owner.team.store
        self.service = NativeHostService(self.root, self.store)
        self.service.workbench_parent = self.store.directory
        self.addCleanup(self.service.close)

    def start(self, *, planning=False, specification=False):
        if planning or specification:
            self.owner.coordinator_response()
        if specification:
            (self.root / ".loop/spec.md").write_text("")
            (self.root / "docs").mkdir(exist_ok=True)
            (self.root / "docs/requirements.md").write_text(SPEC)
        result = self.service.begin()
        self.team_id = result["team_id"]
        return result

    def specification(self):
        return {"status": "ready", "summary": "Read the actual project requirements",
                "spec_markdown": SPEC, "sources": ["docs/requirements.md"], "questions": []}

    def ask(self):
        request = self.service.stage_request(self.team_id)
        response = {"status": "needs_input", "summary": "One scope answer is needed", "spec_markdown": "",
            "sources": [], "questions": [{"id": "scope", "question": "Preserve nonempty behavior?",
                                           "reason": "Define compatibility", "blocking": True}]}
        return self.service.stage_submit(self.team_id, request["request_id"], response)

    def complete(self, task_id):
        request = self.service.task_request(self.team_id, task_id)
        self.assertIn("response_schema", request)
        step = self.owner.respond(request)
        return self.service.task_submit(self.team_id, task_id, request["team_assignment"]["request_id"], step)

    def receive(self, task_id, *, accept=True):
        result = self.service.progress(self.team_id)
        handoff = result["tasks"][task_id]["handoff"]
        return self.service.receive(self.team_id, task_id, handoff["id"], "coordinator", accept,
            "Offline recipient inspected actual outputs, bound test artifacts and requirement coverage.")

    def test_full_native_team_applies_actual_changes_checks_receipts_and_final_gate(self):
        result = self.start()
        self.assertEqual(result["mode"], "native_host")
        self.assertIsNone(result["team_budget"])
        self.assertEqual(result["ready_tasks"], ["implement"])
        self.assertEqual(self.complete("implement")["tasks"]["implement"]["status"], "HANDOFF")
        self.assertEqual(self.receive("implement")["ready_tasks"], ["verify"])
        self.complete("verify")
        result = self.receive("verify")
        self.assertEqual(result["status"], "COMPLETE", result["reason"])
        self.assertTrue(result["final_candidate_current"])
        self.assertEqual(result["task_counts"]["COMPLETE"], 2)
        self.assertTrue(result["check_results"])
        self.assertTrue(all(c["result"] == "pass" for c in result["check_results"]))
        self.assertEqual(result["evidence_problems"], [])
        self.assertEqual(self.owner.transport.requests, [])
        self.service.team.teams.audit(self.team_id)
        (self.root / "slug.py").write_text("# changed after completion\n")
        self.assertFalse(self.service.progress(self.team_id)["final_candidate_current"])

    def test_requirements_plan_and_role_memory_are_durable_without_model_dispatch(self):
        with patch("loop_engineering.team_host.TeamHost.invoke", side_effect=AssertionError("No nested model")), \
                patch("subprocess.Popen", side_effect=AssertionError("Preparation is offline")):
            self.start(specification=True)
            request = self.service.stage_request(self.team_id)
            self.assertEqual(request["role"], "requirements_reviewer")
            result = self.service.stage_submit(self.team_id, request["request_id"], self.specification())
            self.assertEqual(result["stage"], "INTAKE")
            self.assertEqual((self.root / ".loop/spec.md").read_text(), SPEC)
            request = self.service.stage_request(self.team_id)
            self.assertEqual(request["role"], "coordinator")
            self.assertNotIn("source_manifest", request["context"])
            result = self.service.stage_submit(self.team_id, request["request_id"], self.owner.plan_response)
        self.assertEqual(result["stage"], "EXECUTION")
        self.assertEqual(result["ready_tasks"], ["implement"])
        self.assertEqual(self.service.begin()["team_id"], self.team_id)
        memory = self.service.memory(self.team_id, "backend", "implement")
        self.assertIn("memory", json.dumps(memory))
        self.assertIn("backend", json.dumps(self.service.role_context(self.team_id, "backend")))
        self.assertEqual(self.owner.transport.requests, [])

    def test_questions_stay_in_chat_and_resume_after_pause_with_actual_answer(self):
        self.start(specification=True)
        result = self.ask()
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertTrue(self.service.stage_request(self.team_id)["waiting_for_user"])
        self.service.control(self.team_id, "pause")
        with self.assertRaises(ContractError):
            self.service.answer(self.team_id, "scope", "Yes")
        self.assertEqual(self.service.control(self.team_id, "resume")["status"], "AWAITING_INPUT")
        self.service.answer(self.team_id, "scope", "Yes; preserve nonempty behavior.")
        request = self.service.stage_request(self.team_id)
        self.assertIn("answer:scope:1", request["context"]["allowed_source_refs"])
        response = self.specification(); response["sources"] = ["answer:scope:1", "docs/requirements.md"]
        self.assertEqual(self.service.stage_submit(self.team_id, request["request_id"], response)["stage"], "INTAKE")

    def test_stage_request_is_idempotent_and_late_response_cannot_replace_a_plan(self):
        self.start(planning=True)
        first = self.service.stage_request(self.team_id)
        self.assertEqual(first, self.service.stage_request(self.team_id))
        self.service.stage_submit(self.team_id, first["request_id"], self.owner.plan_response)
        with self.assertRaises(ContractError):
            self.service.stage_submit(self.team_id, first["request_id"], self.owner.plan_response)
        self.assertEqual(self.service.progress(self.team_id)["stage"], "EXECUTION")

    def staged_review_plan(self, *, independent=False, engine=None):
        self.owner.plan["tasks"][1].update(role="reviewer", phase="review", repair_task="implement")
        self.owner.fixture.save_plan()
        task = load(self.root / ".loop/tasks/verify.json")
        task["workflow"] = "staged"
        if independent:
            task["checks"].append({"id": "source-review", "type": "review", "independent": True,
                "description": "Inspect the exact integrated candidate", "procedure": ["Inspect actual source and test evidence"]})
            task["criteria"].append({"id": "source-review", "description": "Independent source assessment passes",
                                     "check_ids": ["source-review"]})
        self.owner.fixture.fixture.write(".loop/tasks/verify.json", task)
        self.owner.coordinator_response()
        if engine is not None:
            entry = next(c for c in self.owner.plan_response["contracts"] if c["path"] == ".loop/tasks/verify.json")
            entry["engine_json"] = json.dumps(engine)
        return task

    def review_stage_graph(self, task):
        criteria = [c["id"] for c in task["criteria"]]
        return [{"id": "source", "depends_on": [], "criterion_ids": [criteria[0]],
                 "write_allow": deepcopy(task["scope"]["write_allow"]), "write_deny": deepcopy(task["scope"]["write_deny"])},
                {"id": "compatibility", "depends_on": ["source"], "criterion_ids": criteria[1:],
                 "write_allow": deepcopy(task["scope"]["write_allow"]), "write_deny": deepcopy(task["scope"]["write_deny"])}]

    def begin_staged_review_plan(self):
        result = self.service.begin()
        self.team_id = result["team_id"]
        self.assertEqual(result["stage"], "INTAKE")
        return self.service.stage_request(self.team_id)

    def test_staged_review_without_engine_freezes_all_final_criteria_and_starts_a_native_child(self):
        original = self.staged_review_plan()
        request = self.begin_staged_review_plan()
        with patch("loop_engineering.team_host.TeamHost.invoke", side_effect=AssertionError("No nested model")):
            result = self.service.stage_submit(self.team_id, request["request_id"], self.owner.plan_response)
            self.assertEqual(result["stage"], "EXECUTION")
            data = self.service.team.teams.get(self.team_id)
            engine_path = data["tasks"]["verify"]["engine_file"]
            prepared = load(self.root / ".loop/tasks/verify.json")
            config = load(self.root / engine_path)
            self.assertEqual(prepared["criteria"][:-1], original["criteria"])
            self.assertEqual(prepared["checks"][:-1], original["checks"])
            self.assertTrue(prepared["checks"][-1]["independent"])
            self.assertEqual(config["stages"], [{"id": "delivery", "depends_on": [],
                "criterion_ids": [c["id"] for c in prepared["criteria"]],
                "write_allow": original["scope"]["write_allow"], "write_deny": original["scope"]["write_deny"]}])
            self.assertIn(engine_path, data["bound_files"])
            self.complete("implement")
            self.receive("implement")
            review = self.service.task_request(self.team_id, "verify")
        self.assertEqual(review["team_assignment"]["task_id"], "verify")
        record = self.service.team.teams.get(self.team_id)["records"]["verify"]
        self.assertEqual(record["status"], "RUNNING")
        child = self.store.get(record["child_run_id"])
        self.assertEqual(child["native"]["config"]["stages"], config["stages"])
        self.assertEqual(child["task"], prepared)
        self.assertEqual(self.owner.transport.requests, [])

    def test_explicit_valid_stage_graph_preserves_original_stages_and_adds_read_only_review(self):
        original = self.staged_review_plan()
        graph = self.review_stage_graph(original)
        config = load(ROOT / "templates/engine.json")
        config["stages"] = deepcopy(graph)
        config["response_limits"]["timeout_seconds"] = 73
        entry = next(c for c in self.owner.plan_response["contracts"] if c["path"] == ".loop/tasks/verify.json")
        entry["engine_json"] = json.dumps(config)
        request = self.begin_staged_review_plan()
        with patch("subprocess.Popen", side_effect=AssertionError("Freeze must not dispatch")):
            self.service.stage_submit(self.team_id, request["request_id"], self.owner.plan_response)
        data = self.service.team.teams.get(self.team_id)
        prepared = load(self.root / ".loop/tasks/verify.json")
        frozen = load(self.root / data["tasks"]["verify"]["engine_file"])
        self.assertEqual(frozen["stages"][:-1], graph)
        appended = frozen["stages"][-1]
        self.assertEqual(set(appended["depends_on"]), {s["id"] for s in graph})
        self.assertEqual(appended["criterion_ids"], [prepared["criteria"][-1]["id"]])
        self.assertEqual(appended["write_allow"], original["scope"]["write_allow"])
        self.assertEqual(appended["write_deny"], ["**"])
        self.assertEqual(frozen["response_limits"], config["response_limits"])
        self.assertEqual(prepared["checks"][:-1], original["checks"])
        self.assertEqual(prepared["criteria"][:-1], original["criteria"])
        self.assertEqual(prepared["limits"], original["limits"])
        self.assertEqual(prepared["authorization"], original["authorization"])
        self.assertEqual(self.store.runs(), [])

    def test_explicit_graph_already_owning_independent_review_is_preserved_at_freeze(self):
        original = self.staged_review_plan(independent=True)
        config = load(ROOT / "templates/engine.json")
        config["stages"] = self.review_stage_graph(original)
        entry = next(c for c in self.owner.plan_response["contracts"] if c["path"] == ".loop/tasks/verify.json")
        entry["engine_json"] = json.dumps(config)
        request = self.begin_staged_review_plan()
        with patch("subprocess.Popen", side_effect=AssertionError("Freeze must not dispatch")):
            self.service.stage_submit(self.team_id, request["request_id"], self.owner.plan_response)
        data = self.service.team.teams.get(self.team_id)
        frozen = load(self.root / data["tasks"]["verify"]["engine_file"])
        self.assertEqual(frozen["stages"], config["stages"])
        self.assertEqual(load(self.root / ".loop/tasks/verify.json"), original)
        self.assertEqual(self.store.runs(), [])

    def reject_staged_review_plan(self, mutation, message):
        original = self.staged_review_plan()
        config = load(ROOT / "templates/engine.json")
        config["stages"] = self.review_stage_graph(original)
        mutation(config["stages"])
        entry = next(c for c in self.owner.plan_response["contracts"] if c["path"] == ".loop/tasks/verify.json")
        entry["engine_json"] = json.dumps(config)
        request = self.begin_staged_review_plan()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch("subprocess.Popen", side_effect=AssertionError("Invalid plans must not dispatch")), \
                self.assertRaisesRegex(ContractError, message):
            self.service.stage_submit(self.team_id, request["request_id"], self.owner.plan_response)
        data = self.service.team.teams.get(self.team_id)
        self.assertEqual(data["stage"], "INTAKE")
        self.assertEqual(data["tasks"], {})
        self.assertEqual(data["records"], {})
        self.assertEqual(data["dependencies"], {})
        self.assertFalse(data.get("pending_control"))
        self.assertEqual(self.store.runs(), [])
        self.assertIn(message, data["preparation_feedback"][-1]["error"])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_explicit_empty_review_stage_graph_is_rejected_before_freeze(self):
        self.reject_staged_review_plan(lambda stages: stages.clear(), "stage dependency graph")

    def test_explicit_cyclic_review_stage_graph_is_rejected_before_freeze(self):
        self.reject_staged_review_plan(lambda stages: stages[0]["depends_on"].append("compatibility"), "cycle")

    def test_explicit_review_stage_graph_missing_a_criterion_is_rejected_before_freeze(self):
        self.reject_staged_review_plan(lambda stages: stages.pop(),
                                      "criterion must have exactly one stage owner")

    def test_source_changes_require_fresh_bound_context(self):
        self.start(specification=True)
        request = self.service.stage_request(self.team_id)
        (self.root / "docs/requirements.md").write_text(SPEC + "More detail.\n")
        with self.assertRaisesRegex(ContractError, "sources changed"):
            self.service.stage_submit(self.team_id, request["request_id"], self.specification())
        self.assertNotEqual(request["request_id"], self.service.stage_request(self.team_id)["request_id"])
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")

    def test_invalid_plans_have_bounded_repairs_and_preserve_feedback(self):
        self.start(planning=True)
        for i in range(3):
            request = self.service.stage_request(self.team_id)
            if i:
                self.assertTrue(request["context"]["preparation_feedback"])
            with self.assertRaises(ContractError):
                self.service.stage_submit(self.team_id, request["request_id"], {})
        self.assertEqual(self.service.progress(self.team_id)["status"], "BLOCKED")
        with self.assertRaises(ContractError):
            self.service.stage_request(self.team_id)
        self.assertEqual(load(self.root / ".loop/workflow-tasks.json")["tasks"], [])

    def test_failed_stage_write_recovers_once_after_service_restart(self):
        self.start(specification=True)
        request = self.service.stage_request(self.team_id)
        with patch("loop_engineering.team_automation.atomic_write", side_effect=OSError("Offline interrupted write")):
            with self.assertRaises(OSError):
                self.service.stage_submit(self.team_id, request["request_id"], self.specification())
        data = self.service.team.teams.get(self.team_id)
        self.assertTrue(data["pending_control"])
        self.assertNotIn("stage_reports", data)
        second = NativeHostService(self.root, self.store); self.addCleanup(second.close)
        self.assertEqual(second.begin()["team_id"], self.team_id)
        data = second.team.teams.get(self.team_id)
        self.assertIsNone(data["pending_control"])
        self.assertEqual(len(data["stage_reports"]), 1)
        self.assertEqual(data["specification"]["state"], "READY")
        self.assertEqual((self.root / ".loop/spec.md").read_text(), SPEC)

    def test_paused_pending_stage_needs_explicit_resume_and_cancel_is_terminal(self):
        self.start(specification=True)
        request = self.service.stage_request(self.team_id)
        with patch("loop_engineering.team_automation.atomic_write", side_effect=OSError("Interrupted")):
            with self.assertRaises(OSError):
                self.service.stage_submit(self.team_id, request["request_id"], self.specification())
        self.service.control(self.team_id, "pause")
        self.assertEqual(self.service.begin()["status"], "PAUSED")
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")
        self.assertEqual(self.service.control(self.team_id, "resume")["stage"], "INTAKE")
        self.service.control(self.team_id, "cancel")
        with self.assertRaises(ContractError):
            self.service.control(self.team_id, "resume")

    def test_pending_stage_source_or_control_conflict_never_overwrites_user_change(self):
        self.start(specification=True)
        request = self.service.stage_request(self.team_id)
        with patch("loop_engineering.team_automation.atomic_write", side_effect=OSError("Interrupted")):
            with self.assertRaises(OSError):
                self.service.stage_submit(self.team_id, request["request_id"], self.specification())
        (self.root / ".loop/spec.md").write_text("Actual user revision\n")
        with self.assertRaises(ContractError):
            self.service.begin()
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "Actual user revision\n")

    def test_cancelled_pending_stage_is_not_recovered_or_silently_replaced(self):
        self.start(specification=True)
        request = self.service.stage_request(self.team_id)
        with patch("loop_engineering.team_automation.atomic_write", side_effect=OSError("Interrupted")):
            with self.assertRaises(OSError):
                self.service.stage_submit(self.team_id, request["request_id"], self.specification())
        self.service.control(self.team_id, "cancel")
        with self.assertRaisesRegex(ContractError, "unreconciled"):
            self.service.begin()
        with self.assertRaises(ContractError):
            self.service.stage_request(self.team_id)
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")

    def test_cancel_during_admitted_stage_commit_cannot_be_overwritten_by_an_active_checkpoint(self):
        self.start(specification=True)
        request = self.service.stage_request(self.team_id)
        calls = 0
        def cancel_during_commit(path, content):
            nonlocal calls
            atomic_write(path, content)
            calls += 1
            if calls == 1:
                self.service.team.teams.signal(self.team_id, "CANCELLED")
        with patch("loop_engineering.team_automation.atomic_write", side_effect=cancel_during_commit):
            result = self.service.stage_submit(self.team_id, request["request_id"], self.specification())
        self.assertEqual(result["status"], "CANCELLED")
        data = self.service.team.teams.get(self.team_id)
        self.assertEqual(data["status"], "CANCELLED")
        self.assertIsNone(data["pending_control"])
        self.assertEqual(len(data["stage_reports"]), 1)

    def test_large_asset_tree_has_bounded_planning_context_without_a_full_manifest(self):
        self.owner.coordinator_response()
        (self.root / "assets").mkdir()
        for i in range(600):
            (self.root / f"assets/{i:04}.json").write_text(json.dumps({"asset": i, "description": "fixture asset"}))
        result = self.service.begin(); self.team_id = result["team_id"]
        request = self.service.stage_request(self.team_id)
        from loop_engineering.team_memory import byte_size
        self.assertLessEqual(byte_size(request), load(self.root / ".loop/project.json")["context_max_bytes"])
        self.assertNotIn("source_manifest", request["context"])
        self.assertGreaterEqual(request["context"]["source_coverage"]["files"], 600)
        self.assertTrue(request["context"]["scenario"])

    def test_existing_automatic_team_is_never_taken_over_or_rebudgeted(self):
        original = self.owner.start()
        before = (self.root / ".loop/project.json").read_bytes()
        with self.assertRaisesRegex(ContractError, "existing controller team owns"):
            self.service.begin()
        result = self.service.progress(original["team_id"])
        self.assertEqual(result["mode"], "automatic_controller")
        self.assertEqual(result["team_budget"], original["team_budget"])
        self.assertEqual((self.root / ".loop/project.json").read_bytes(), before)
        with self.assertRaises(ContractError):
            self.service.task_request(original["team_id"], "implement")

    def test_hard_team_spend_caps_refuse_native_admission(self):
        policy = load(self.root / ".loop/team-policy.json")
        policy["max_tokens"] = 100
        (self.root / ".loop/team-policy.json").write_text(json.dumps(policy))
        with self.assertRaisesRegex(ContractError, "hard team token/cost cap"):
            self.service.begin()
        self.assertEqual(self.service.team.teams.runs(), [])

    def test_required_agent_usage_capability_is_not_fabricated(self):
        task = load(self.root / ".loop/tasks/implement.json")
        task["required_capabilities"]["agent"].append("usage_reporting")
        (self.root / ".loop/tasks/implement.json").write_text(json.dumps(task))
        original = (self.root / "slug.py").read_bytes()
        self.start()
        with self.assertRaisesRegex(ContractError, "agent capabilities unavailable"):
            self.complete("implement")
        self.assertEqual((self.root / "slug.py").read_bytes(), original)

    def test_foreign_team_and_state_inside_project_are_refused(self):
        foreign = self.owner.base / "foreign"
        shutil.copytree(self.root, foreign)
        other = self.service.team.start(foreign)
        with self.assertRaisesRegex(ContractError, "outside the configured project"):
            self.service.progress(other["team_id"])
        from loop_engineering.store import Store
        with self.assertRaises(ContractError):
            NativeHostService(self.root, Store(self.root / "unsafe-state"))

    def test_dashboard_escapes_material_and_uses_actual_counts_events_and_checkpoints(self):
        result = self.start()
        self.assertEqual(result["task_counts"]["total"], 2)
        result["scope"] = "<script>alert('x')</script>"
        content = render_progress(result)
        self.assertNotIn("<script>", content)
        self.assertIn("&lt;script&gt;", content)
        self.assertIn("0 / 2", content)
        self.assertTrue(Path(result["dashboard_path"]).is_file())
        self.assertTrue(result["recent_events"])

    def test_review_and_executor_tools_cannot_sign_or_invent_evidence(self):
        self.start()
        self.service.task_request(self.team_id, "implement")
        with self.assertRaises(ContractError):
            self.service.evaluation(self.team_id, "implement", "suite")
        with self.assertRaises(ContractError):
            self.service.evaluation(self.team_id, "implement", envelope={"result": "pass"})
        with self.assertRaises(ContractError):
            self.service.execute_evaluation(self.team_id, "implement", "suite")

    def prepare_review(self):
        self.owner.plan["tasks"][1].update(phase="acceptance", role="acceptance", engine_file=".loop/review-engine.json")
        contract = load(self.root / ".loop/tasks/verify.json")
        contract["checks"].append({"id": "external", "type": "review", "independent": True,
            "description": "Independent offline assessment", "procedure": ["Inspect the exact candidate and actual test artifacts"]})
        contract["criteria"].append({"id": "reviewed", "description": "Registered review passes", "check_ids": ["external"]})
        (self.root / ".loop/tasks/verify.json").write_text(json.dumps(contract))
        self.store.authorities.create("native-fixture-reviewer", "reviewer")
        config = load(ROOT / "templates/engine.json")
        config["evaluator_keys"] = [{"key_id": "native-fixture-reviewer", "role": "reviewer", "check_ids": ["external"]}]
        (self.root / ".loop/review-engine.json").write_text(json.dumps(config))
        self.owner.fixture.save_plan()
        self.start(); self.complete("implement"); self.receive("implement")
        return self.complete("verify")

    def test_required_independent_review_is_pending_until_actual_signed_evidence_then_reverified(self):
        result = self.prepare_review()
        self.assertEqual(result["tasks"]["verify"]["status"], "RUNNING")
        self.assertIsNone(result["tasks"]["verify"]["handoff"])
        with self.assertRaises(ContractError):
            self.service.execute_evaluation(self.team_id, "verify", "external")
        request = self.service.evaluation(self.team_id, "verify", "external")
        from loop_engineering.evaluators import sign_result
        artifact = self.owner.base / "native-review.txt"
        artifact.write_text("Offline reviewer fixture inspected real slug results; not a live agent assessment.\n")
        signed = sign_result(self.store.authorities, request, "native-fixture-reviewer", result="pass",
            summary="Actual signed offline reviewer fixture", artifacts=[artifact], findings=[])
        result = self.service.evaluation(self.team_id, "verify", envelope=signed)
        self.assertEqual(result["tasks"]["verify"]["status"], "HANDOFF")
        self.assertTrue(any(c["check_id"] == "external" and c["result"] == "pass" for c in result["check_results"]))
        self.assertTrue(self.receive("verify")["final_candidate_current"])

    def test_signed_review_from_old_candidate_is_refused(self):
        self.prepare_review()
        request = self.service.evaluation(self.team_id, "verify", "external")
        from loop_engineering.evaluators import sign_result
        artifact = self.owner.base / "native-review.txt"; artifact.write_text("Offline old-candidate review\n")
        signed = sign_result(self.store.authorities, request, "native-fixture-reviewer", result="pass",
            summary="Stale offline result", artifacts=[artifact], findings=[])
        (self.root / "slug.py").write_text("# user changed this candidate\n")
        with self.assertRaises(ContractError):
            self.service.evaluation(self.team_id, "verify", envelope=signed)
        self.assertIsNone(self.service.progress(self.team_id)["tasks"]["verify"]["handoff"])

    def test_registered_executor_runs_actual_subprocess_and_reaches_handoff(self):
        plan = {"kind": "browser", "check_id": "ui", "timeout_seconds": 2,
            "node": "/missing/node", "playwright_module": "/missing/playwright", "browser_executable": "/missing/browser",
            "document": "ui.html", "url": None, "allowed_origins": [],
            "steps": [{"action": "assert_text", "selector": "#status", "value": "done"}],
            "viewport": {"width": 800, "height": 600}}
        tools = load(self.root / ".loop/execution-tools.json"); tools["executors"] = [plan]
        (self.root / ".loop/execution-tools.json").write_text(json.dumps(tools))
        self.owner.fixture.fixture.select_team()
        (self.root / "ui.html").write_text('<div id="status">done</div>')
        contract = load(self.root / ".loop/tasks/verify.json")
        contract["checks"].append({"id": "ui", "type": "interaction", "description": "Registered procedure",
                                   "procedure": ["Execute the frozen plan and retain actual artifacts"]})
        contract["criteria"].append({"id": "ui", "description": "Registered procedure passes", "check_ids": ["ui"]})
        (self.root / ".loop/tasks/verify.json").write_text(json.dumps(contract))
        config = load(ROOT / "templates/engine.json")
        self.store.authorities.create("native-fixture-interaction", "interaction")
        config["evaluator_keys"] = [{"key_id": "native-fixture-interaction", "role": "interaction", "check_ids": ["ui"]}]
        (self.root / ".loop/review-engine.json").write_text(json.dumps(config))
        self.owner.plan["tasks"][1]["engine_file"] = ".loop/review-engine.json"
        self.owner.fixture.save_plan()
        self.start(); self.complete("implement"); self.receive("implement"); self.complete("verify")
        from loop_engineering.native_engine import NativeController
        original = NativeController.launch
        def launch(controller, data, argv, cwd, logs, **kwargs):
            if kwargs["kind"] == "evaluator":
                argv = [sys.executable, "-B", str(ROOT / "tests/executor_fixture.py")]
            return original(controller, data, argv, cwd, logs, **kwargs)
        with patch.object(NativeController, "launch", launch):
            result = self.service.execute_evaluation(self.team_id, "verify", "ui")
        self.assertEqual(result["tasks"]["verify"]["status"], "HANDOFF")
        self.assertEqual(self.owner.transport.requests, [])

    def test_loopback_progress_view_refreshes_actual_state_and_has_no_control_or_path_endpoints(self):
        result = self.start()
        self.service.watch_progress(self.team_id)
        url = self.service.dashboard_url(self.team_id)
        self.assertIsNotNone(url, self.service._view.problem)
        with urlopen(url, timeout=2) as response:
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertIn("0 / 2", response.read().decode())
        self.service.control(self.team_id, "pause")
        with urlopen(url, timeout=2) as response:
            self.assertIn("已暂停", response.read().decode())
        base = url.rsplit("/", 1)[0]
        for path in ("/", "/control", "/../metadata.sqlite", "/unknown", url[len(base):] + "?control=cancel"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                urlopen(base + path, timeout=2)
            self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(url, headers={"Host": "external.invalid"}), timeout=2)
        self.assertEqual(caught.exception.code, 403)

    def test_progress_watcher_reads_real_checkpoints_and_file_fallback_when_viewer_unavailable(self):
        result = self.start()
        with patch("loop_engineering.progress_view.ThreadingHTTPServer", side_effect=OSError("Fixture port unavailable")):
            self.service.watch_progress(self.team_id)
        self.assertIsNone(self.service.dashboard_url(self.team_id))
        with self.service.team.teams.writer(self.team_id) as data:
            data["reason"] = "Actual checkpoint from another tool"
            self.service.team.teams.save(data, "fixture.progress")
        path = Path(result["dashboard_path"])
        deadline = time.monotonic() + 4
        while "Actual checkpoint from another tool" not in path.read_text() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertIn("Actual checkpoint from another tool", path.read_text())
        self.assertIn("Fixture port unavailable", self.service.progress(self.team_id)["dashboard_problem"])

    def test_recoverable_check_failure_retries_with_new_context_without_resetting_child_limits(self):
        self.start()
        request = self.service.task_request(self.team_id, "implement")
        step = self.owner.respond(request); step["changes"][0]["new_content"] = "def slug(value): return 'incorrect'\n"
        result = self.service.task_submit(self.team_id, "implement", request["team_assignment"]["request_id"], step)
        run_id = result["tasks"]["implement"]["child_run_id"]
        self.assertEqual(result["tasks"]["implement"]["status"], "RUNNING")
        before = self.store.get(run_id)["state"]["iteration"]
        self.service.control(self.team_id, "resume")
        result = self.complete("implement")
        self.assertEqual(result["tasks"]["implement"]["status"], "HANDOFF")
        self.assertEqual(result["tasks"]["implement"]["child_run_id"], run_id)
        self.assertGreater(self.store.get(run_id)["state"]["iteration"], before)

    def test_mcp_strict_tool_schemas_only_and_no_model_or_generic_shell(self):
        server = NativeMCPServer(self.service)
        def message(method, params=None, request_id=1):
            return server.message({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        self.assertIn("error", message("tools/list"))
        info = message("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                     "clientInfo": {"name": "offline", "version": "1"}})
        self.assertEqual(info["result"]["serverInfo"]["name"], "loop-native")
        server.message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names = {t["name"] for t in message("tools/list")["result"]["tools"]}
        self.assertEqual(len(names), 36)
        self.assertIn("loop_repair_plan", names)
        self.assertTrue({"loop_worker_assign", "loop_worker_update", "loop_worker_collect", "loop_dispatch_board"} <= names)
        self.assertIn("loop_stage_submit", names)
        self.assertNotIn("loop_submit_response", names)
        with patch("subprocess.Popen", side_effect=AssertionError("Native entry does not dispatch")):
            result = message("tools/call", {"name": "loop_begin", "arguments": {}})
        self.assertFalse(result["result"]["isError"])
        self.assertIn("error", message("tools/call", {"name": "loop_begin", "arguments": {"argv": ["codex"]}}))
        self.assertIn("error", message("tools/call", {"name": "evaluation-sign"}))
        for name, args in (
            ("loop_worker_assign", {"team_id": "unknown", "task_id": "implement", "workbench_id": "unknown",
                "assignment_id": "backend", "role": "backend", "title": "Concrete job", "goal": "Concrete scoped result",
                "write_paths": ["slug.py"], "depends_on": [], "directory": "/arbitrary/path"}),
            ("loop_worker_update", {"team_id": "unknown", "worker_id": "unknown", "agent_id": "actual-id",
                "status": "COMPLETE", "detail": "Cannot invent completion"}),
            ("loop_worker_collect", {"team_id": "unknown", "worker_id": "unknown", "summary": "Actual files", "argv": ["codex"]}),
            ("loop_dispatch_board", {"team_id": "unknown", "shell": "untrusted"}),
        ):
            with self.subTest(tool=name):
                self.assertEqual(message("tools/call", {"name": name, "arguments": args})["error"]["code"], -32602)
        self.assertEqual(self.owner.transport.requests, [])

    def test_real_stdio_server_negotiates_lists_tools_and_exits_cleanly_at_eof(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "loop_begin", "arguments": {}}}]
        result = subprocess.run([sys.executable, str(ROOT / "scripts/native_mcp.py"), "--project", str(self.root),
            "--state-dir", str(self.store.directory)], input="\n".join(json.dumps(m) for m in messages) + "\n",
            text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])
        self.assertFalse(responses[-1]["result"]["isError"])
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
