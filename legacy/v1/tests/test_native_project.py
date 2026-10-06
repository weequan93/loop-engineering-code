"""Offline project succession: accepted local batches, no native/model dispatch."""

from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, load
from loop_engineering.native_host import NativeHostService, render_progress
from loop_engineering.native_mcp import NativeMCPServer
from loop_engineering.native_project import assert_plan_budget, inspect
from tests import test_native_host as fixture


class NativeProjectTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start
    complete = fixture.NativeHostTests.complete
    receive = fixture.NativeHostTests.receive

    def finish(self):
        for task in ("implement", "verify"):
            self.complete(task)
            result = self.receive(task)
        return result

    def queue(self, *, count=2, budget=100000):
        return dict(team_id=self.team_id, objective="Deliver both authorized local followups; production qualification remains separate.",
            authorization="Actual fixture user: continue these two increments without another routine confirmation; paid services stay off.",
            milestones=[{"id": f"followup-{i}", "brief": f"Implement and independently verify local followup {i}; preserve prior evidence and paid-call restrictions."}
                        for i in range(count)], controller_budget_seconds=budget)

    def accepted(self, *, count=2, budget=100000):
        self.start()
        self.finish()
        self.root_team_id = self.team_id
        return self.service.project_supervise(**self.queue(count=count, budget=budget))

    def specify_successor(self):
        packet = self.service.next(self.team_id)
        self.assertEqual(packet["next_action"]["kind"], "prepare_spec")
        self.service.stage_submit(self.team_id, packet["request_id"], {
            "status": "ready", "summary": "Actual fixture successor requirements inspected",
            "spec_markdown": "# Local successor\nPreserve slug behavior and all earlier evidence.\n",
            "sources": ["brief"], "questions": []})

    def plan_successor(self):
        self.specify_successor()
        self.owner.coordinator_response()
        packet = self.service.next(self.team_id)
        return self.service.stage_submit(self.team_id, packet["request_id"], self.owner.plan_response)

    def test_batch_complete_without_queue_stays_terminal_and_explicitly_unassessed_for_project(self):
        self.start()
        result = self.finish()
        self.assertEqual(result["next_action"]["kind"], "complete")
        self.assertEqual(result["next_action"]["project_completion"], "unassessed")
        self.assertFalse(result["next_action"]["continue_work"])
        with self.assertRaises(ContractError):
            self.service.project_advance(self.team_id)

    def test_registration_preserves_completed_contracts_checks_usage_and_outputs(self):
        self.start()
        self.finish()
        before = self.service._data(self.team_id)
        files = {str(path): path.read_bytes() for path in (self.root / ".loop").rglob("*") if path.is_file()}
        result = self.service.project_supervise(**self.queue())
        after = self.service._data(self.team_id)
        for key in ("status", "records", "tasks", "plan", "bound_files", "initial_snapshot"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(files, {str(path): path.read_bytes() for path in (self.root / ".loop").rglob("*") if path.is_file()})
        self.assertEqual(result["next_action"]["kind"], "advance_project")
        self.assertTrue(result["next_action"]["continue_work"])
        self.assertGreater(result["project_supervision"]["recorded_controller_seconds"], 0)
        self.assertEqual(self.owner.transport.requests, [])

    def test_successor_starts_new_requirements_not_old_execution_and_duplicate_returns_same_team(self):
        self.accepted()
        with patch("subprocess.Popen", side_effect=AssertionError("No model or command during succession")):
            first = self.service.project_advance(self.team_id)
            second = self.service.project_advance(self.team_id)
        self.assertEqual(first["team_id"], second["team_id"])
        self.assertEqual(first["stage"], "SPEC")
        self.assertEqual(first["task_counts"]["total"], 0)
        self.assertEqual(self.service._data(self.team_id)["status"], "COMPLETE")
        self.assertEqual(self.service.begin()["team_id"], first["team_id"])
        with self.assertRaises(ContractError):
            self.service.begin("Replacement goal")
        packet = self.service.next(first["team_id"])
        self.assertIn("actual registered authorization", packet["context"]["project_supervision"]["instruction"])
        self.assertIn("paid-call restrictions", packet["context"]["brief"])

    def test_two_accepted_successors_form_one_queue_without_declaring_unlisted_product_complete(self):
        self.accepted()
        root_usage = self.service.progress(self.team_id)["project_supervision"]["recorded_controller_ms"]
        for index in range(2):
            result = self.service.project_advance(self.team_id)
            self.team_id = result["team_id"]
            self.plan_successor()
            result = self.finish()
            self.assertTrue(result["final_candidate_current"])
            self.assertEqual(result["next_action"]["kind"], "advance_project" if index == 0 else "project_queue_complete")
            self.service.team.teams.audit(self.team_id)
        self.assertGreater(result["project_supervision"]["recorded_controller_ms"], root_usage)
        self.assertEqual(result["project_supervision"]["successor_batches"], 2)
        self.assertFalse(result["next_action"]["continue_work"])
        self.assertEqual(result["completion_scope"], "batch")
        self.assertEqual(self.service.begin()["team_id"], self.team_id)
        with self.assertRaisesRegex(ContractError, "exhausted"):
            self.service.project_advance(self.team_id)
        # Historical candidates can change as actual successors integrate. The
        # root routes to the existing latest team, never reopens itself.
        root = self.service.next(self.root_team_id)
        self.assertEqual(root["status"], "COMPLETE")
        self.assertEqual(root["next_action"]["kind"], "follow_project_batch")
        self.assertEqual(root["next_action"]["arguments"]["team_id"], self.team_id)
        self.assertEqual(self.owner.transport.requests, [])

    def test_exact_registration_is_idempotent_and_changed_budget_or_goal_refuses(self):
        self.accepted()
        revision = self.service._data(self.team_id)["revision"]
        self.service.project_supervise(**self.queue())
        self.assertEqual(self.service._data(self.team_id)["revision"], revision)
        for name, value in (("objective", "Different goal"), ("controller_budget_seconds", 200000)):
            with self.assertRaisesRegex(ContractError, "frozen"):
                self.service.project_supervise(**{**self.queue(), name: value})
        child = self.service.project_advance(self.team_id)
        with self.assertRaisesRegex(ContractError, "reset"):
            self.service.project_supervise(**{**self.queue(), "team_id": child["team_id"]})

    def test_unaccepted_and_stale_predecessors_cannot_advance(self):
        self.start()
        self.service.project_supervise(**self.queue())
        with self.assertRaisesRegex(ContractError, "accepted predecessor"):
            self.service.project_advance(self.team_id)
        self.finish()
        (self.root / "slug.py").write_text("# changed after acceptance\n")
        self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "stale_completion")
        with self.assertRaisesRegex(ContractError, "accepted predecessor"):
            self.service.project_advance(self.team_id)

    def test_stale_completion_cannot_register_a_queue(self):
        self.start()
        self.finish()
        (self.root / "slug.py").write_text("# changed\n")
        with self.assertRaisesRegex(ContractError, "Stale"):
            self.service.project_supervise(**self.queue())

    def test_pause_reconnect_explicit_resume_and_cancel_preserve_completed_root(self):
        self.accepted()
        self.service.project_control(self.team_id, "pause")
        self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "project_stopped")
        self.assertEqual(self.service._data(self.team_id)["status"], "COMPLETE")
        with self.assertRaisesRegex(ContractError, "paused/cancelled"):
            self.service.project_advance(self.team_id)
        self.service.project_control(self.team_id, "resume")
        child = self.service.project_advance(self.team_id)
        self.service.project_control(self.team_id, "pause")
        self.assertEqual(self.service._data(child["team_id"])["status"], "PAUSED")
        self.assertEqual(self.service.begin()["status"], "PAUSED")
        self.service.project_control(self.team_id, "resume")
        self.assertEqual(self.service._data(child["team_id"])["status"], "ACTIVE")
        self.service.project_control(self.team_id, "cancel")
        self.assertEqual(self.service._data(child["team_id"])["status"], "CANCELLED")
        self.assertEqual(self.service._data(self.team_id)["status"], "COMPLETE")
        with self.assertRaisesRegex(ContractError, "terminal"):
            self.service.project_control(self.team_id, "resume")

    def test_reconnection_recovers_same_successor_and_accounting(self):
        self.accepted()
        child = self.service.project_advance(self.team_id)
        second = NativeHostService(self.root, self.store)
        self.addCleanup(second.close)
        result = second.project_advance(self.team_id)
        self.assertEqual(result["team_id"], child["team_id"])
        self.assertEqual(result["project_supervision"], child["project_supervision"])

    def test_lost_creation_response_recovers_authenticated_link_without_new_batch(self):
        self.accepted()
        original = self.service.progress
        def fail_reply(identity):
            if identity != self.team_id:
                raise RuntimeError("Simulated crash after create before response")
            return original(identity)
        with patch.object(self.service, "progress", side_effect=fail_reply):
            with self.assertRaises(RuntimeError):
                self.service.project_advance(self.team_id)
        result = self.service.project_advance(self.team_id)
        self.assertEqual(result["project_supervision"]["successor_batches"], 1)
        self.assertEqual(len(self.service.team.teams.runs()), 2)

    def test_concurrent_advances_cannot_create_duplicate_successors(self):
        self.accepted()
        def call():
            try:
                return self.service.project_advance(self.team_id)["team_id"]
            except ContractError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: call(), range(2)))
        self.assertEqual(len(self.service.team.teams.runs()), 2)
        expected = self.service.project_advance(self.team_id)["team_id"]
        self.assertTrue(all(value == expected or "owns project" in value for value in results))

    def test_budget_includes_prior_controller_time_and_plan_reserves_both_children(self):
        self.accepted(budget=2)
        child = self.service.project_advance(self.team_id)
        task = load(self.root / ".loop/tasks/implement.json")
        with self.assertRaisesRegex(ContractError, "remaining cumulative"):
            assert_plan_budget(self.service, self.service._data(child["team_id"]), {".loop/tasks/implement.json": task})
        before = self.service.progress(child["team_id"])["project_supervision"]["recorded_controller_seconds"]
        root = self.service._data(self.team_id)
        identity = root["records"]["implement"]["child_run_id"]
        run = self.store.get(identity)
        run["elapsed_ms"] += 2000
        self.store.save(run, "fixture.elapsed", {})
        project = inspect(self.service, self.service._data(child["team_id"]))
        self.assertGreater(project["recorded_controller_seconds"], before)
        self.assertEqual(project["remaining_controller_seconds"], 0)

    def test_missing_retained_child_is_not_counted_as_zero(self):
        self.accepted()
        root = self.service._data(self.team_id)
        identity = root["records"]["implement"]["child_run_id"]
        with self.store.connect() as connection:
            connection.execute("DELETE FROM runs WHERE id=?", (identity,))
        with self.assertRaisesRegex(ContractError, "Run does not exist"):
            self.service.project_advance(self.team_id)

    def test_previous_effects_and_spend_policy_refuse_before_successor_creation(self):
        self.accepted()
        for method in ("_check_previous_effects", "_check_spend_policy"):
            with patch.object(self.service, method, side_effect=ContractError("Actual missing capability/effect")):
                with self.assertRaisesRegex(ContractError, "Actual missing"):
                    self.service.project_advance(self.team_id)
            self.assertEqual(len(self.service.team.teams.runs()), 1)

    def test_invalid_and_oversized_queue_is_refused_without_writes(self):
        self.start()
        revision = self.service._data(self.team_id)["revision"]
        variants = [{"milestones": []}, {"milestones": [{"id": "same", "brief": "One"}] * 2},
                    {"controller_budget_seconds": True}, {"authorization": ""},
                    {"milestones": [{"id": "x", "brief": "x" * 16385}]},
                    {"milestones": [{"id": "x", "brief": "ok", "shell": "forbidden"}]}]
        for variant in variants:
            with self.assertRaises(ContractError):
                self.service.project_supervise(**{**self.queue(), **variant})
        self.assertEqual(self.service._data(self.team_id)["revision"], revision)

    def test_mcp_exposes_project_queue_and_advance_without_model_dispatch(self):
        self.accepted()
        server = NativeMCPServer(self.service)
        tools = {item["name"]: item for item in server.tools}
        self.assertTrue(tools["loop_project_advance"]["annotations"]["idempotentHint"])
        self.assertIn("loop_project_supervise", server.handlers())
        result = server.handlers()["loop_project_advance"](team_id=self.team_id)
        self.assertEqual(result["stage"], "SPEC")
        self.assertEqual(self.owner.transport.requests, [])

    def test_project_dashboard_distinguishes_batch_status_and_escapes_goal(self):
        self.start()
        self.finish()
        result = self.service.project_supervise(**{**self.queue(), "objective": "<script>goal</script>"})
        rendered = render_progress(result)
        self.assertIn("项目目标与后续批次", rendered)
        self.assertIn("&lt;script&gt;goal&lt;/script&gt;", rendered)
        self.assertNotIn("<script>goal</script>", rendered)
        self.assertIn("批次验收完成不等于整个产品完成", rendered)

    def test_framework_changes_refuse_project_mutations_and_wrong_project_refuses(self):
        self.accepted()
        with patch.object(self.service, "handshake", return_value={"restart_required": True}):
            for call in (lambda: self.service.project_supervise(**self.queue()),
                         lambda: self.service.project_advance(self.team_id),
                         lambda: self.service.project_control(self.team_id, "pause")):
                with self.assertRaisesRegex(ContractError, "Reconnect"):
                    call()
        other = self.root.parent / "other-project"
        other.mkdir()
        service = NativeHostService(other, self.store)
        self.addCleanup(service.close)
        with self.assertRaisesRegex(ContractError, "outside"):
            service.project_advance(self.team_id)

    def test_project_allowance_is_exposed_early_and_enforced_at_plan_submission(self):
        self.accepted(budget=2)
        child = self.service.project_advance(self.team_id)
        self.team_id = child["team_id"]
        task = load(self.root / ".loop/tasks/implement.json")
        preview = self.service.plan_preflight(self.team_id, [{"task": task, "engine": None}])
        self.assertFalse(preview["project_budget"]["fits"])
        self.assertTrue(any(item["code"] == "project_budget_insufficient" for item in preview["issues"]))
        self.specify_successor()
        self.owner.coordinator_response()
        workflow = (self.root / ".loop/workflow-tasks.json").read_bytes()
        packet = self.service.next(self.team_id)
        with self.assertRaisesRegex(ContractError, "remaining cumulative"):
            self.service.stage_submit(self.team_id, packet["request_id"], self.owner.plan_response)
        self.assertEqual((self.root / ".loop/workflow-tasks.json").read_bytes(), workflow)
        self.assertEqual(self.service._data(self.team_id)["tasks"], {})

    def test_actual_pending_question_still_waits_in_successor(self):
        self.accepted()
        child = self.service.project_advance(self.team_id)
        packet = self.service.next(child["team_id"])
        result = self.service.stage_submit(child["team_id"], packet["request_id"], {
            "status": "needs_input", "summary": "Actual unresolved release decision", "spec_markdown": "",
            "sources": ["brief"], "questions": [{"id": "release", "question": "Which actual release identity?",
                "reason": "Release needs an actual identity", "blocking": True}]})
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertFalse(result["next_action"]["continue_work"])
        with self.assertRaisesRegex(ContractError, "accepted predecessor"):
            self.service.project_advance(child["team_id"])
        result = self.service.answer(child["team_id"], "release", "Actual user: local delivery only, signing later.")
        self.assertTrue(result["next_action"]["continue_work"])
        self.assertEqual(self.service._data(self.team_id)["status"], "COMPLETE")

    def test_unadmitted_reserved_child_keeps_progress_and_original_identity_without_usage_reset(self):
        self.accepted()
        child = self.service.project_advance(self.team_id)
        self.team_id = child["team_id"]
        self.plan_successor()
        from loop_engineering.controller import Controller
        with patch.object(Controller, "start", side_effect=ContractError("Actual capability unavailable")):
            with self.assertRaisesRegex(ContractError, "Actual capability"):
                self.service.workbench_prepare(self.team_id, "implement")
        result = self.service.progress(self.team_id)
        self.assertFalse(result["tasks"]["implement"]["controller_created"])
        self.assertGreater(result["project_supervision"]["recorded_controller_ms"], 0)
        reserved = result["tasks"]["implement"]["child_run_id"]
        self.assertIsNotNone(reserved)
        packet = self.service.workbench_prepare(self.team_id, "implement")
        self.assertEqual(self.service._data(self.team_id)["records"]["implement"]["child_run_id"], reserved)
        self.assertEqual(packet["team_assignment"]["task_id"], "implement")
        self.assertGreater(self.service.progress(self.team_id)["project_supervision"]["recorded_controller_ms"], 0)


if __name__ == "__main__":
    unittest.main()
