"""Early material decisions and measurable acceptance, without invoking a provider."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, ROOT, load, validate
from loop_engineering.native_readiness import validate_decisions, effective_budget
from tests import test_native_host as fixture


class NativeReadinessTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start

    def test_effective_review_timeout_and_repeated_command_passes_are_counted(self):
        task = self.task()
        task["checks"][0]["timeout_seconds"] = 300
        task["checks"] += [{"id": key, "type": "review"} for key in ("safety", "code", "acceptance")]
        task["limits"]["max_wall_seconds"] = 2400
        with patch("loop_engineering.native_review.registration", return_value={"timeout_seconds": 600}):
            budget = effective_budget(self.service, task, None, None)
        self.assertEqual(budget["command_passes"], 4)
        self.assertEqual(budget["external_seconds"]["safety"], 600)
        self.assertGreater(budget["required_seconds"], 2400)
        self.assertFalse(budget["fits"])

    def test_unknown_external_duration_is_not_zero_and_declared_ids_are_checked(self):
        task = self.task()
        task["checks"].append({"id": "manual", "type": "human", "description": "Actual review", "procedure": ["Observe"]})
        budget = effective_budget(self.service, task, None, None)
        self.assertFalse(budget["complete"])
        self.assertEqual(budget["unknown_check_ids"], ["manual"])
        task["extensions"]["verification_plan"]["external_timeouts"] = {"missing": 600}
        with self.assertRaisesRegex(ContractError, "non-command check"):
            validate("task", task)

    def test_plan_submission_rejects_effective_review_overflow_before_control_writes(self):
        self.start(planning=True)
        packet = self.service.stage_request(self.team_id)
        response = deepcopy(self.owner.plan_response)
        first = response["contracts"][0]
        task = json.loads(first["task_json"])
        task["checks"].append({"id": "safety", "type": "review", "independent": True,
                              "description": "Independent review", "procedure": ["Inspect actual candidate"]})
        task["criteria"].append({"id": "safety", "description": "Safety accepted", "check_ids": ["safety"]})
        task["limits"]["max_wall_seconds"] = 100
        task["extensions"]["verification_plan"] = {"measurements": [], "decision_ids": [], "repair_reserve_seconds": 20}
        first["task_json"] = json.dumps(task)
        before = (self.root / first["path"]).read_bytes()
        with patch("loop_engineering.native_review.registration", return_value={"timeout_seconds": 600}):
            with self.assertRaisesRegex(ContractError, "Effective check time"):
                self.service.stage_submit(self.team_id, packet["request_id"], response)
        self.assertEqual((self.root / first["path"]).read_bytes(), before)
        self.assertEqual(self.service.progress(self.team_id)["tasks"], {})

    def task(self):
        task = load(self.root / ".loop/tasks/implement.json")
        task["extensions"]["verification_plan"] = {
            "measurements": [{"check_id": task["checks"][0]["id"],
                "required": ["component_cpu", "product_cpu"], "produced": ["component_cpu", "product_cpu"],
                "producer_paths": ["slug.py"]}],
            "decision_ids": [], "repair_reserve_seconds": 20}
        return task

    def test_preflight_reads_proposed_checks_before_admission_without_effects(self):
        self.start(planning=True)
        task = self.task()
        before = self.service._data(self.team_id)
        files = {p.name: p.read_bytes() for p in (self.root / ".loop/tasks").iterdir()}
        with patch("subprocess.Popen", side_effect=AssertionError("Preflight must be offline")):
            result = self.service.plan_preflight(self.team_id, [{"task": task, "engine": None}])
        self.assertTrue(result["ready_for_automatic_acceptance"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(self.service._data(self.team_id), before)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / ".loop/tasks").iterdir()}, files)
        self.assertEqual(self.owner.transport.requests, [])

    def test_missing_producer_and_unknown_decision_are_different_actions(self):
        self.start(planning=True)
        task = self.task()
        plan = task["extensions"]["verification_plan"]
        plan["decision_ids"] = ["acceptance-device"]
        plan["measurements"][0]["producer_paths"] = ["missing-product-measurement.py"]
        result = self.service.plan_preflight(self.team_id, [{"task": task, "engine": None}])
        kinds = {r["code"]: r["kind"] for r in result["issues"]}
        self.assertEqual(kinds["producer_missing"], "engineering")
        self.assertEqual(kinds["decision_pending"], "user_input")
        self.assertFalse(result["ready_for_automatic_acceptance"])
        self.assertEqual(result["questions_to_resolve"][0]["question_id"], "acceptance-device")

    def test_recorded_answer_is_reused_and_cannot_be_invented_at_plan_freeze(self):
        task = self.task()
        task["extensions"]["verification_plan"]["decision_ids"] = ["environment"]
        questions = {"questions": [{"id": "environment", "answers": []}]}
        with self.assertRaisesRegex(ContractError, "actual recorded answer"):
            validate_decisions(task, questions)
        questions["questions"][0]["answers"] = ["Current Mac only"]
        validate_decisions(task, questions)
        self.assertEqual(questions["questions"][0]["answers"], ["Current Mac only"])

    def test_partial_measurement_producer_is_rejected_before_freezing(self):
        task = self.task()
        task["extensions"]["verification_plan"]["measurements"][0]["produced"] = ["component_cpu"]
        with self.assertRaisesRegex(ContractError, "product_cpu"):
            validate("task", task)

    def test_check_time_and_repair_reserve_must_fit_existing_wall_limit(self):
        task = self.task()
        task["extensions"]["verification_plan"]["repair_reserve_seconds"] = task["limits"]["max_wall_seconds"]
        with self.assertRaisesRegex(ContractError, "exceed the task wall limit"):
            validate("task", task)

    def test_bad_paths_unknown_checks_and_duplicate_checks_fail_closed(self):
        for mutation in ("../outside", "unknown", "duplicate"):
            task = self.task()
            rows = task["extensions"]["verification_plan"]["measurements"]
            if mutation == "unknown":
                rows[0]["check_id"] = "absent"
            elif mutation == "duplicate":
                rows.append(deepcopy(rows[0]))
            else:
                rows[0]["producer_paths"] = [mutation]
            with self.assertRaises(ValueError):
                validate("task", task)

    def test_missing_independent_executor_is_visible_before_any_worker(self):
        self.start(planning=True)
        task = self.task()
        task["checks"].append({"id": "safety", "type": "review", "independent": True,
                              "description": "Independent safety assessment", "procedure": ["Inspect actual candidate"]})
        task["criteria"].append({"id": "safety", "description": "Safety accepted", "check_ids": ["safety"]})
        result = self.service.plan_preflight(self.team_id, [{"task": task, "engine": load(ROOT / "templates/engine.json")}])
        issue = next(r for r in result["issues"] if r.get("check_id") == "safety")
        self.assertEqual(issue["kind"], "external_dependency")
        self.assertFalse(result["ready_for_automatic_acceptance"])
        self.assertEqual(self.service.progress(self.team_id)["tasks"], {})

    def test_legacy_tasks_remain_valid_but_undeclared_coverage_is_visible(self):
        self.start(planning=True)
        task = self.task(); del task["extensions"]["verification_plan"]
        validate("task", task)
        result = self.service.plan_preflight(self.team_id, [{"task": task, "engine": None}])
        self.assertIn("coverage_undeclared", [r["code"] for r in result["issues"]])
        self.assertFalse(result["ready_for_automatic_acceptance"])

    def test_intake_response_cannot_bypass_unanswered_declared_decision(self):
        self.start(planning=True)
        packet = self.service.stage_request(self.team_id)
        response = deepcopy(self.owner.plan_response)
        first = response["contracts"][0]
        task = json.loads(first["task_json"])
        task["extensions"]["verification_plan"] = {"measurements": [], "decision_ids": ["missing"], "repair_reserve_seconds": 0}
        first["task_json"] = json.dumps(task)
        with self.assertRaisesRegex(ContractError, "actual recorded answer"):
            self.service.stage_submit(self.team_id, packet["request_id"], response)
        result = self.service.progress(self.team_id)
        self.assertEqual(result["stage"], "INTAKE")
        self.assertEqual(result["tasks"], {})
