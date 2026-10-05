"""Offline routing visibility does not grant evaluator authority or create evidence."""

from copy import deepcopy
import json
import time
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, ROOT, load
from loop_engineering.external_execution import executor_for
from loop_engineering.native_host import render_progress
from loop_engineering.native_preflight import check_route, compact_preflight
from tests import test_native_host as fixture


def load_plan():
    return {"kind": "http_load", "check_id": "load", "timeout_seconds": 2,
        "url": "http://fixture.invalid/items", "allowed_origins": ["http://fixture.invalid"],
        "requests": 3, "concurrency": 2, "requests_per_second": 100,
        "request_timeout_ms": 100, "max_response_bytes": 16, "expected_status": 200,
        "thresholds": {"max_p95_ms": 100, "max_errors": 0, "min_successes": 3}}


def browser_plan():
    return {"kind": "browser", "check_id": "ui", "timeout_seconds": 2,
        "node": "/missing/node", "playwright_module": "/missing/playwright", "browser_executable": "/missing/browser",
        "document": "ui.html", "url": None, "allowed_origins": [],
        "steps": [{"action": "assert_text", "selector": "#status", "value": "done"}],
        "viewport": {"width": 800, "height": 600}}


class NativePreflightTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start
    complete = fixture.NativeHostTests.complete
    receive = fixture.NativeHostTests.receive
    prepare_review = fixture.NativeHostTests.prepare_review

    def configure(self, *, check_type="review", key_role="reviewer", authority=True,
                  register_key=True, plan=None):
        path = self.root / ".loop/tasks/verify.json"
        task = load(path)
        task["checks"].append({"id": "external", "type": check_type,
            **({"independent": True} if check_type == "review" else {}),
            "description": "Actual required fixture procedure", "procedure": ["Inspect the candidate and retain actual results"]})
        task["criteria"].append({"id": "external", "description": "Required actual result", "check_ids": ["external"]})
        path.write_text(json.dumps(task))
        config = load(ROOT / "templates/engine.json")
        self.key_id = "preflight-fixture-evaluator"
        if authority:
            self.store.authorities.create(self.key_id, key_role)
        config["evaluator_keys"] = [{"key_id": self.key_id, "role": key_role, "check_ids": ["external"]}] if register_key else []
        (self.root / ".loop/review-engine.json").write_text(json.dumps(config))
        self.owner.plan["tasks"][1]["engine_file"] = ".loop/review-engine.json"
        self.owner.fixture.save_plan()
        tools = load(self.root / ".loop/execution-tools.json")
        if plan:
            tools["executors"] = [{**plan, "check_id": "external"}]
            (self.root / ".loop/execution-tools.json").write_text(json.dumps(tools))
            self.owner.fixture.fixture.select_team()
        return task, config, tools

    def route(self, result, check_id="external", task_id="verify"):
        return next(item for item in result["verification_preflight"]["checks"]
                    if item["task_id"] == task_id and item["check_id"] == check_id)

    def register_review(self):
        from loop_engineering.native_review import register
        executable = self.owner.base / "offline-codex-review"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        with patch("subprocess.Popen", side_effect=AssertionError("Registration cannot dispatch a process")):
            register(self.store, self.root, executable=str(executable))
        return executable

    def test_all_formal_checks_visible_before_child_admission_and_development_continues(self):
        self.configure()
        with patch("subprocess.Popen", side_effect=AssertionError("Preflight must not execute checks or models")):
            result = self.start()
        self.assertTrue(all(row["child_run_id"] is None for row in result["tasks"].values()))
        self.assertEqual(self.route(result)["route"], "signed_import")
        self.assertEqual(result["next_action"]["kind"], "prepare_task")
        self.assertFalse(result["verification_preflight"]["blocks_development"])
        self.assertEqual(result["check_results"], [])
        self.assertEqual(result["verification_preflight"]["counts"]["controller_command"], 4)
        self.assertEqual(self.owner.transport.requests, [])

    def test_exact_registered_executor_has_no_passing_evidence_or_runtime_claim(self):
        self.configure(check_type="artifact", key_role="artifact", plan=load_plan())
        result = self.start()
        route = self.route(result)
        self.assertEqual((route["route"], route["executor_registered"]), ("registered_executor", True))
        self.assertTrue(route["authority_registered"])
        self.assertFalse(route["runtime_validated"])
        self.assertEqual(result["check_results"], [])
        self.assertTrue(result["next_action"]["continue_work"])
        self.assertEqual(self.owner.transport.requests, [])

    def test_same_id_wrong_kind_is_not_registered_executor_even_with_authority(self):
        task, config, tools = self.configure(check_type="interaction", key_role="interaction", plan=load_plan())
        result = self.start()
        route = self.route(result)
        self.assertFalse(route["executor_registered"])
        self.assertFalse(route["executor_plan_registered"])
        self.assertEqual(route["route"], "signed_import")
        self.assertIn("kind does not match", " ".join(route["problems"]))
        fake_child = {"workspace": str(self.root), "profile": self.service.team.profile(self.service._data(self.team_id)),
                      "task": task, "native": {"config": config}}
        self.assertIsNone(executor_for(fake_child, "external"))

    def test_matching_plan_without_authority_is_missing_and_does_not_block_implementation(self):
        self.configure(check_type="artifact", key_role="artifact", authority=False, register_key=False, plan=load_plan())
        result = self.start()
        route = self.route(result)
        self.assertTrue(route["executor_plan_registered"])
        self.assertFalse(route["executor_registered"])
        self.assertEqual(route["route"], "missing_authority")
        self.assertEqual(self.complete("implement")["tasks"]["implement"]["status"], "HANDOFF")
        result = self.receive("implement")
        self.assertEqual(result["ready_tasks"], ["verify"])
        self.assertIsNone(result["final_candidate_current"])

    def test_wrong_authority_role_and_missing_private_key_are_not_import_routes(self):
        for options in ({"key_role": "human"}, {"authority": False}):
            with self.subTest(options=options):
                task, config, tools = self.configure(**options)
                route = check_route(task["checks"][-1], task, config, tools, self.store.authorities)
                self.assertEqual(route["route"], "missing_authority")
                self.assertFalse(route["authority_registered"])
                if options.get("authority", True):
                    self.store.authorities.path(self.key_id).unlink()

    def test_revoked_authority_is_visible_without_secret_disclosure(self):
        self.configure()
        result = self.start()
        secret = self.store.authorities.key(self.key_id)["secret"]
        self.assertNotIn(secret, json.dumps(result))
        self.store.authorities.revoke(self.key_id)
        result = self.service.progress(self.team_id)
        self.assertEqual(self.route(result)["route"], "missing_authority")
        self.assertFalse(self.route(result)["authority_registered"])
        self.assertNotIn(secret, json.dumps(result))

    def test_browser_registration_and_missing_runtime_are_reported_separately(self):
        self.configure(check_type="interaction", key_role="interaction", plan=browser_plan())
        route = self.route(self.start())
        self.assertTrue(route["executor_plan_registered"])
        self.assertFalse(route["executor_registered"])
        self.assertEqual(route["route"], "signed_import")
        self.assertIn("node is unavailable", route["problems"])
        self.assertFalse(route["runtime_validated"])

    def test_missing_authority_next_action_never_dispatches_or_manufactures_signoff(self):
        self.configure(authority=False, register_key=False)
        self.start(); self.complete("implement"); self.receive("implement")
        result = self.complete("verify")
        self.assertEqual(result["next_action"]["kind"], "missing_evaluator_authority")
        self.assertFalse(result["next_action"]["continue_work"])
        self.assertIsNone(result["next_action"]["tool"])
        self.assertFalse(any(item["check_id"] == "external" for item in result["check_results"]))
        self.assertEqual(self.owner.transport.requests, [])

    def test_signed_import_is_pending_until_actual_bound_signed_result(self):
        result = self.prepare_review()
        route = self.route(result)
        self.assertEqual(route["route"], "signed_import")
        self.assertEqual(result["next_action"]["kind"], "await_evaluator")
        self.assertFalse(result["next_action"]["continue_work"])
        request = self.service.evaluation(self.team_id, "verify", "external")
        from loop_engineering.evaluators import sign_result
        artifact = self.owner.base / "preflight-review.txt"
        artifact.write_text("Actual deterministic separate evaluator fixture inspected the bound candidate.\n")
        signed = sign_result(self.store.authorities, request, "native-fixture-reviewer", result="pass",
            summary="Actual signed deterministic result", artifacts=[artifact], findings=[])
        result = self.service.evaluation(self.team_id, "verify", envelope=signed)
        self.assertEqual(result["tasks"]["verify"]["status"], "HANDOFF")
        self.assertTrue(any(item["check_id"] == "external" and item["result"] == "pass" for item in result["check_results"]))
        self.assertTrue(self.receive("verify")["final_candidate_current"])
        self.assertEqual(self.owner.transport.requests, [])

    def test_pause_cancel_and_frozen_input_conflict_take_priority_over_routes(self):
        self.configure(authority=False, register_key=False)
        self.start()
        self.assertEqual(self.service.control(self.team_id, "pause")["next_action"]["kind"], "paused")
        self.service.control(self.team_id, "resume")
        path = self.root / ".loop/tasks/verify.json"
        task = load(path); task["objective"] += " Changed by the actual user."
        path.write_text(json.dumps(task))
        result = self.service.progress(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "input_conflict")
        self.assertTrue(result["verification_preflight"]["problems"])
        self.assertEqual(result["verification_preflight"]["checks"], [])
        self.assertEqual(self.service.control(self.team_id, "cancel")["next_action"]["kind"], "cancelled")

    def test_dashboard_shows_routes_escapes_details_and_keeps_them_separate_from_results(self):
        self.configure()
        result = self.start()
        self.route(result)["problems"] = ["<script>untrusted finding</script>"]
        rendered = render_progress(result)
        self.assertIn("正式检查与执行入口", rendered)
        self.assertIn("外部签名导入", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertEqual(result["check_results"], [])
        self.assertEqual(result["task_counts"]["COMPLETE"], 0)

    def test_compact_announcements_remain_bounded_and_do_not_claim_final_acceptance(self):
        self.configure()
        result = self.start()
        preflight = deepcopy(result["verification_preflight"])
        entry = self.route(result)
        preflight["checks"] += [deepcopy(entry) for _ in range(50)]
        compact = compact_preflight(preflight)
        self.assertEqual(len(compact["unresolved"]), 12)
        self.assertEqual(compact["omitted_unresolved"], 39)
        self.assertNotIn("secret", json.dumps(compact))
        self.assertIn("not executed checks", compact["assurance"])

    def test_registered_review_visible_before_admission_without_model_or_signoff(self):
        self.configure()
        self.register_review()
        with patch("subprocess.Popen", side_effect=AssertionError("Preflight cannot run a model")):
            result = self.start()
        route = self.route(result)
        self.assertEqual(route["route"], "registered_review")
        self.assertTrue(route["executor_registered"])
        self.assertTrue(result["verification_preflight"]["review_runner"]["available"])
        self.assertEqual(result["check_results"], [])
        self.assertIsNone(result["final_candidate_current"])
        self.assertEqual(self.owner.transport.requests, [])

    def test_changed_registered_review_executable_is_an_explicit_gap_not_automatic_route(self):
        self.configure()
        executable = self.register_review()
        self.start()
        executable.write_text("#!/bin/sh\nexit 1\n")
        result = self.service.progress(self.team_id)
        route = self.route(result)
        self.assertEqual(route["route"], "signed_import")
        self.assertFalse(route["executor_registered"])
        self.assertIn("executable changed", " ".join(route["problems"]))
        self.assertFalse(result["verification_preflight"]["review_runner"]["available"])
        self.assertEqual(result["next_action"]["kind"], "prepare_task")

    def test_registered_review_with_hard_task_spend_cap_is_not_advertised_as_automatic(self):
        self.configure()
        self.register_review()
        path = self.root / ".loop/tasks/verify.json"
        task = load(path); task["limits"]["max_tokens"] = 100
        path.write_text(json.dumps(task))
        route = self.route(self.start())
        self.assertEqual(route["route"], "signed_import")
        self.assertFalse(route["executor_registered"])
        self.assertIn("hard token/cost caps", " ".join(route["problems"]))

    def signed_outcome(self, result, *, findings=None):
        request = self.service.evaluation(self.team_id, "verify", "external")
        from loop_engineering.evaluators import sign_result
        artifact = self.owner.base / ("preflight-" + result + ".txt")
        artifact.write_text("Deterministic evaluator fixture retained its actual assessment.\n")
        signed = sign_result(self.store.authorities, request, "native-fixture-reviewer", result=result,
            summary="Actual fixture " + result + " requires specific follow-up", artifacts=[artifact], findings=findings or [])
        return self.service.evaluation(self.team_id, "verify", envelope=signed)

    def test_cached_inconclusive_stops_empty_retry_cycle_even_when_review_runner_is_registered(self):
        self.prepare_review()
        self.register_review()
        result = self.signed_outcome("inconclusive")
        self.assertEqual(result["next_action"]["kind"], "await_evaluator_input")
        self.assertFalse(result["next_action"]["continue_work"])
        self.assertIn("Actual fixture inconclusive", result["next_action"]["summary"])
        self.assertEqual(result["pending_evaluations"][0]["result"], "inconclusive")
        run_id = result["tasks"]["verify"]["child_run_id"]
        before = self.store.get(run_id)
        with patch("loop_engineering.native_review.run", side_effect=AssertionError("No repeated review")), \
                patch("subprocess.Popen", side_effect=AssertionError("Progress never dispatches")):
            for _ in range(3):
                result = self.service.next(self.team_id)
                self.assertEqual(result["next_action"]["kind"], "await_evaluator_input")
        after = self.store.get(run_id)
        self.assertEqual(after, before)
        self.assertEqual(self.owner.transport.requests, [])

    def test_cached_failure_routes_actual_findings_to_frozen_owner_and_does_not_repeat_review(self):
        self.owner.plan["tasks"][1]["repair_task"] = "implement"
        self.owner.fixture.save_plan()
        self.prepare_review()
        self.register_review()
        finding = {"id": "actual-review-gap", "criterion_id": "reviewed", "severity": "blocking",
                   "description": "Actual fixture failure needs compatible behavior."}
        result = self.signed_outcome("fail", findings=[finding])
        action = result["next_action"]
        self.assertEqual(action["kind"], "review_failed")
        self.assertEqual((action["repair_task"], action["repair_owner"]), ("implement", "backend"))
        self.assertIn("slug.py", action["repair_write_allow"])
        self.assertTrue(action["repair_requires_reviewed_plan"])
        self.assertEqual(action["findings"], [finding])
        self.assertFalse(action["continue_work"])
        with patch("loop_engineering.native_review.run", side_effect=AssertionError("No repeated review")):
            for _ in range(3):
                self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "review_failed")
        self.assertEqual(self.owner.transport.requests, [])

    def test_current_failed_writable_task_prepares_bounded_repair_context_without_repeating_review(self):
        self.prepare_review()
        finding = {"id": "same-task-repair", "criterion_id": "reviewed", "severity": "blocking",
                   "description": "Actual fixture evaluator requires a scoped compatibility correction."}
        result = self.signed_outcome("fail", findings=[finding])
        run_id = result["tasks"]["verify"]["child_run_id"]
        before = self.store.get(run_id)
        with patch("loop_engineering.native_review.run", side_effect=AssertionError("Repair preparation cannot rerun review")), \
                patch("subprocess.Popen", side_effect=AssertionError("Repair preparation cannot dispatch")):
            first = self.service.next(self.team_id)
            second = self.service.next(self.team_id)
        action = first["next_action"]
        self.assertEqual(action["kind"], "review_failed")
        self.assertTrue(action["continue_work"])
        self.assertEqual(action["tool"], "loop_workbench_prepare")
        self.assertEqual(action["arguments"], {"team_id": self.team_id, "task_id": "verify"})
        self.assertEqual(action["findings"], [finding])
        self.assertEqual(action["repair_owner"], first["team_assignment"]["owner"])
        current = first["task_context"]["pending_findings"]
        self.assertTrue(any(row["current"] and row["finding"] == finding for row in current))
        self.assertEqual(first["workbench"]["id"], second["workbench"]["id"])
        self.assertEqual(first["team_assignment"]["request_id"], second["team_assignment"]["request_id"])
        self.assertEqual(first["host_wait"]["deadline_epoch"], second["host_wait"]["deadline_epoch"])
        child = self.store.get(run_id)
        self.assertEqual(child["task"], before["task"])
        self.assertEqual(child["state"]["iteration"], before["state"]["iteration"])
        self.assertEqual(child["native"]["external_by_check"], before["native"]["external_by_check"])
        self.assertEqual(child["native"].get("execution_attempts", {}), before["native"].get("execution_attempts", {}))
        self.assertGreaterEqual(child["elapsed_ms"], before["elapsed_ms"])
        self.assertEqual(self.owner.transport.requests, [])

    def test_failed_read_only_scope_requires_reviewed_repair_plan_instead_of_auto_prepare(self):
        path = self.root / ".loop/tasks/verify.json"
        task = load(path)
        task["scope"]["write_deny"].append("**")
        path.write_text(json.dumps(task))
        self.prepare_review()
        result = self.signed_outcome("fail")
        run_id = result["tasks"]["verify"]["child_run_id"]
        before = self.store.get(run_id)
        with patch.object(self.service, "workbench_prepare", side_effect=AssertionError("Read-only repair has no write authority")), \
                patch("loop_engineering.native_review.run", side_effect=AssertionError("No repeated review")):
            result = self.service.next(self.team_id)
        action = result["next_action"]
        self.assertEqual(action["kind"], "review_failed")
        self.assertFalse(action["continue_work"])
        self.assertIsNone(action["tool"])
        self.assertFalse(action["repair_edit_authorized"])
        self.assertEqual(self.store.get(run_id), before)
        self.assertEqual(self.owner.transport.requests, [])

    def test_unresolved_executor_attempt_precedes_any_new_evaluation_or_cached_verdict(self):
        self.prepare_review()
        self.register_review()
        result = self.signed_outcome("inconclusive")
        run_id = result["tasks"]["verify"]["child_run_id"]
        with self.store.writer(run_id) as child:
            child["native"]["execution_attempts"] = {"unresolved-review": {"status": "RUNNING", "kind": "registered_review"}}
            self.store.save(child, "fixture.unresolved_review")
        result = self.service.next(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "pending_effect")
        self.assertFalse(result["next_action"]["continue_work"])
        self.assertEqual(result["next_action"]["unresolved_attempts"], ["unresolved-review"])

    def test_stale_authenticated_outcome_does_not_stop_fresh_candidate_evaluation(self):
        self.prepare_review()
        self.register_review()
        self.signed_outcome("inconclusive")
        (self.root / "candidate-note.txt").write_text("Actual different candidate.\n")
        result = self.service.next(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "run_evaluation")
        self.assertIsNone(result["pending_evaluations"][0]["result"])
        self.assertEqual(self.owner.transport.requests, [])

    def cache_result(self, verdict="inconclusive", *, pause_before_save=False):
        from loop_engineering.evaluators import sign_result
        request = self.service.evaluation(self.team_id, "verify", "external")
        run_id = self.service.progress(self.team_id)["tasks"]["verify"]["child_run_id"]
        artifact = self.store.run_dir(run_id) / "cached-review.txt"
        artifact.write_text("Actual deterministic fixture produced a signed result before interruption.\n")
        signed = sign_result(self.store.authorities, request, "native-fixture-reviewer", result=verdict,
            summary="Cached actual fixture " + verdict, artifacts=[artifact], findings=[])
        if pause_before_save:
            self.service.control(self.team_id, "pause")
        with self.store.writer(run_id) as child:
            child["native"]["execution_attempts"] = {"cached-review": {"id": "cached-review",
                "status": "RESULT", "kind": "registered_review", "check_id": "external", "envelope": signed}}
            self.store.save(child, "fixture.result_cached_before_import")
        return run_id, signed, artifact

    def test_pause_cached_result_resume_imports_original_without_fresh_review_or_development(self):
        self.prepare_review()
        run_id, signed, _ = self.cache_result(pause_before_save=True)
        self.assertEqual(self.service.progress(self.team_id)["next_action"]["kind"], "paused")
        result = self.service.control(self.team_id, "resume")
        child = self.store.get(run_id)
        self.assertEqual(child["state"]["status"], "PLANNING")
        self.assertEqual(child["selected_evidence"], [])
        with patch("loop_engineering.native_review.run", side_effect=AssertionError("No fresh review")), \
                patch("subprocess.Popen", side_effect=AssertionError("next cannot dispatch")):
            action = self.service.next(self.team_id)["next_action"]
        self.assertEqual(action["kind"], "import_evaluation")
        self.assertTrue(action["replay_only"])
        self.assertEqual(action["arguments"]["envelope"], signed)
        self.assertFalse(any(row["check_id"] == "external" for row in result["check_results"]))
        with patch("loop_engineering.native_review.run", side_effect=AssertionError("No fresh review")), \
                patch("loop_engineering.models.HTTPTransport.post", side_effect=AssertionError("No provider")):
            result = self.service.evaluation(self.team_id, "verify", envelope=signed)
        self.assertEqual(result["next_action"]["kind"], "await_evaluator_input")
        self.assertIn("Cached actual fixture inconclusive", result["next_action"]["summary"])
        self.assertEqual(result["tasks"]["verify"]["child_run_id"], run_id)
        commands = {check["id"] for check in child["task"]["checks"] if check["type"] == "command"}
        passing = {row["check_id"] for row in result["check_results"]
                   if row["task_id"] == "verify" and row["result"] == "pass"}
        self.assertTrue(commands.issubset(passing))
        self.assertEqual(self.owner.transport.requests, [])

    def test_active_cached_result_recovery_charges_crash_gap_before_import(self):
        self.prepare_review()
        run_id, signed, _ = self.cache_result()
        with self.store.writer(run_id) as child:
            before = child["elapsed_ms"]
            child["operation_started_ms"] = time.time_ns() // 1000000 - 2500
            self.store.save(child, "fixture.result_import_crash")
        with patch("loop_engineering.native_review.run", side_effect=AssertionError("No fresh review")), \
                patch("loop_engineering.models.HTTPTransport.post", side_effect=AssertionError("No provider")):
            result = self.service.evaluation(self.team_id, "verify", envelope=signed)
        child = self.store.get(run_id)
        self.assertGreaterEqual(child["elapsed_ms"] - before, 2500)
        self.assertIsNone(child["operation_started_ms"])
        self.assertEqual(result["next_action"]["kind"], "await_evaluator_input")
        self.assertTrue(all(row["consumed"] for row in child["native"]["evaluation_requests"].values()))
        self.assertEqual(self.owner.transport.requests, [])

    def test_active_cached_result_exhausted_recovery_preserves_unconsumed_result(self):
        self.prepare_review()
        run_id, signed, _ = self.cache_result()
        with self.store.writer(run_id) as child:
            child["operation_started_ms"] = time.time_ns() // 1000000 - child["task"]["limits"]["max_wall_seconds"] * 1000
            self.store.save(child, "fixture.result_import_exhausted_crash")
        with patch("subprocess.Popen", side_effect=AssertionError("Budget recovery cannot dispatch")):
            with self.assertRaisesRegex(ContractError, "signal or budget"):
                self.service.evaluation(self.team_id, "verify", envelope=signed)
        child = self.store.get(run_id)
        self.assertEqual(child["state"]["status"], "BUDGET_EXHAUSTED")
        self.assertIsNone(child["operation_started_ms"])
        self.assertFalse(any(row["consumed"] for row in child["native"]["evaluation_requests"].values()))
        self.assertEqual(child["native"]["external_by_check"], {})
        self.assertEqual(self.owner.transport.requests, [])

    def test_review_preparation_requires_original_recovery_and_never_starts_new_work(self):
        self.prepare_review()
        run_id = self.service.progress(self.team_id)["tasks"]["verify"]["child_run_id"]
        with self.store.writer(run_id) as child:
            started = time.time_ns() // 1000000
            child["operation_started_ms"] = started
            child["native"]["review_preparation"] = {"id": "fixture-preparation", "started_ms": started,
                "elapsed_before_ms": child["elapsed_ms"]}
            self.store.save(child, "fixture.interrupted_review_preparation")
        with patch("subprocess.Popen", side_effect=AssertionError("Interrupted preparation cannot dispatch")):
            action = self.service.next(self.team_id)["next_action"]
        self.assertEqual(action["kind"], "pending_effect")
        self.assertFalse(action["continue_work"])
        self.assertEqual(action["review_preparation"]["id"], "fixture-preparation")
        self.assertEqual(self.owner.transport.requests, [])

    def test_cached_result_integrity_and_candidate_binding_are_required_before_replay(self):
        self.prepare_review()
        run_id, _, artifact = self.cache_result("pass")
        result = self.service.progress(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "import_evaluation")
        self.assertIsNone(result["final_candidate_current"])
        self.assertFalse(any(row["check_id"] == "external" for row in result["check_results"]))
        artifact.write_text("Actual tampered artifact.\n")
        self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "evidence_problem")
        (self.root / "candidate-note.txt").write_text("Actual different candidate.\n")
        result = self.service.next(self.team_id)
        self.assertFalse(any(row.get("import_pending") for row in result["pending_evaluations"]))
        self.assertEqual(result["next_action"]["kind"], "await_evaluator")
        self.assertEqual(self.store.get(run_id)["native"]["external_by_check"], {})
