"""Offline authorized cap amendments retain one project ledger and all checks."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, load
from loop_engineering.native_host import NativeHostService, render_progress
from loop_engineering.native_mcp import NativeMCPServer
from loop_engineering.native_project import assert_plan_budget, effective_budget
from tests import test_native_project as fixture


class NativeProjectBudgetTests(unittest.TestCase):
    setUp = fixture.NativeProjectTests.setUp
    start = fixture.NativeProjectTests.start
    complete = fixture.NativeProjectTests.complete
    receive = fixture.NativeProjectTests.receive
    finish = fixture.NativeProjectTests.finish
    queue = fixture.NativeProjectTests.queue
    accepted = fixture.NativeProjectTests.accepted
    specify_successor = fixture.NativeProjectTests.specify_successor

    def register(self, budget=10197):
        self.start(specification=True)
        return self.service.project_supervise(**self.queue(budget=budget))

    def amendment(self, **changes):
        return {"team_id": self.team_id, "amendment_id": "fixture-authorized-f02",
            "expected_controller_budget_seconds": 10197, "controller_budget_seconds": 20337,
            "authorization": "Actual fixture user approves increasing only this controller cap by 10140 seconds to 20337; paid services remain off.",
            "reason": "Concrete successor contracts and their original checks need the retained child/integration reserve.",
            **changes}

    def amend(self, **changes):
        return self.service.project_budget_amend(**self.amendment(**changes))

    def test_accepted_evidence_original_declaration_usage_and_files_survive_exact_increase(self):
        self.accepted(budget=10197)
        before = self.service._data(self.team_id)
        files = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        usage = self.service.progress(self.team_id)["project_supervision"]["recorded_controller_ms"]
        children = {row[name]: self.store.get(row[name]) for row in before["records"].values()
                    for name in ("child_run_id", "integration_run_id") if row.get(name)}
        with patch("subprocess.Popen", side_effect=AssertionError("Amending launches no process")):
            result = self.amend()
        after = self.service._data(self.team_id)
        for key in ("status", "records", "tasks", "plan", "bound_files", "initial_snapshot"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["native_host"]["project_queue"], before["native_host"]["project_queue"])
        self.assertEqual(children, {identity: self.store.get(identity) for identity in children})
        self.assertEqual(files, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        project = result["project_supervision"]
        self.assertEqual(project["original_controller_budget_seconds"], 10197)
        self.assertEqual(project["controller_budget_seconds"], 20337)
        self.assertEqual(project["recorded_controller_ms"], usage)
        self.assertEqual(project["budget_amendments"][0]["recorded_controller_ms"], usage)
        self.assertEqual(project["budget_amendments"][0]["root_revision_before"], before["revision"])
        self.assertTrue(result["final_candidate_current"])
        self.assertEqual(self.service.team.teams.audit(self.team_id), after)
        self.assertEqual(self.owner.transport.requests, [])

    def test_successor_and_root_resolve_to_same_idempotent_ledger(self):
        self.accepted(budget=10197)
        child = self.service.project_advance(self.team_id)
        child_before = self.service._data(child["team_id"])
        self.amend(team_id=child["team_id"])
        revision = self.service._data(self.team_id)["revision"]
        result = self.amend()
        self.assertEqual(result["team_id"], child["team_id"])
        self.assertEqual(self.service._data(child["team_id"]), child_before)
        self.assertEqual(self.service._data(self.team_id)["revision"], revision)
        self.assertEqual(len(result["project_supervision"]["budget_amendments"]), 1)
        # Even the original registration remains idempotent; re-registering the
        # larger cap is still forbidden, preventing a second ledger/reset.
        self.service.project_supervise(**self.queue(budget=10197))
        with self.assertRaisesRegex(ContractError, "frozen"):
            self.service.project_supervise(**self.queue(budget=20337))

    def test_preflight_shortfall_and_actual_plan_admission_recover_without_weakening_contracts(self):
        self.accepted(budget=2)
        child = self.service.project_advance(self.team_id)
        self.team_id = child["team_id"]
        self.specify_successor()
        self.owner.coordinator_response()
        packet = self.service.next(self.team_id)
        response = deepcopy(self.owner.plan_response)
        contracts = [{"task": load(self.root / ".loop/tasks/implement.json"), "engine": None}]
        before = self.service.plan_preflight(self.team_id, contracts)["project_budget"]
        self.assertFalse(before["fits"])
        self.assertEqual(before["shortfall_controller_seconds"], before["minimum_controller_budget_seconds"] - 2)
        self.assertEqual(before["amendment_tool"], "loop_project_budget_amend")
        with self.assertRaisesRegex(ContractError, "remaining cumulative"):
            self.service.stage_submit(self.team_id, packet["request_id"], response)
        self.amend(expected_controller_budget_seconds=2, controller_budget_seconds=100000)
        self.assertTrue(self.service.plan_preflight(self.team_id, contracts)["project_budget"]["fits"])
        fresh = self.service.stage_request(self.team_id)
        context = fresh["context"]["project_supervision"]
        self.assertEqual(context["controller_budget_seconds"], 100000)
        self.assertEqual(context["latest_budget_amendment"]["authorization"], self.amendment()["authorization"])
        result = self.service.stage_submit(self.team_id, fresh["request_id"], response)
        self.assertEqual(result["stage"], "EXECUTION")
        self.assertEqual(self.owner.plan_response, response)
        self.assertEqual(result["tasks"]["implement"]["status"], "PENDING")
        self.assertEqual(self.owner.transport.requests, [])

    def test_ongoing_usage_is_retained_and_insufficient_amendment_does_not_bypass_reservation(self):
        self.accepted(budget=10197)
        child = self.service.project_advance(self.team_id)
        root = self.service._data(self.team_id)
        run_id = root["records"]["implement"]["child_run_id"]
        run = self.store.get(run_id)
        run["elapsed_ms"] += 10198000
        self.store.save(run, "fixture.elapsed", {})
        result = self.amend(controller_budget_seconds=10198)
        project = result["project_supervision"]
        self.assertEqual(project["remaining_controller_seconds"], 0)
        contract = load(self.root / ".loop/tasks/implement.json")
        preview = self.service.plan_preflight(child["team_id"], [{"task": contract, "engine": None}])
        p = preview["project_budget"]
        self.assertEqual(p["shortfall_controller_seconds"], p["minimum_controller_budget_seconds"] - 10198)
        with self.assertRaisesRegex(ContractError, "remaining cumulative"):
            assert_plan_budget(self.service, self.service._data(child["team_id"]), {".loop/tasks/implement.json": contract})

    def test_later_amendments_and_old_retry_never_duplicate_credit(self):
        self.register()
        self.amend()
        self.amend(amendment_id="second", expected_controller_budget_seconds=20337, controller_budget_seconds=30000)
        revision = self.service._data(self.team_id)["revision"]
        result = self.amend()
        self.assertEqual(result["project_supervision"]["controller_budget_seconds"], 30000)
        self.assertEqual(len(result["project_supervision"]["budget_amendments"]), 2)
        self.assertEqual(self.service._data(self.team_id)["revision"], revision)
        for changes in ({"controller_budget_seconds": 25000}, {"authorization": "Different approval"}, {"reason": "Different proposal"}):
            with self.assertRaisesRegex(ContractError, "different request"):
                self.amend(**changes)

    def test_stale_equal_and_decreasing_caps_refuse_without_writes(self):
        self.register()
        before = self.service._data(self.team_id)
        for changes in ({"expected_controller_budget_seconds": 10196},
                        {"controller_budget_seconds": 10197}, {"controller_budget_seconds": 1}):
            with self.assertRaises(ContractError):
                self.amend(**changes)
            self.assertEqual(self.service._data(self.team_id), before)

    def test_invalid_oversized_and_missing_authorization_refuse_without_writes(self):
        self.register()
        before = self.service._data(self.team_id)
        for changes in ({"authorization": ""}, {"authorization": " "}, {"reason": ""},
                        {"authorization": "x" * 8193}, {"reason": "x" * 2049}, {"amendment_id": "x" * 65},
                        {"controller_budget_seconds": True}, {"controller_budget_seconds": 20337.0},
                        {"controller_budget_seconds": 2592001}, {"expected_controller_budget_seconds": False}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.amend(**changes)
        self.assertEqual(self.service._data(self.team_id), before)

    def test_project_pause_and_cancel_require_control_not_budget_to_resume(self):
        self.register()
        self.amend()
        self.service.project_control(self.team_id, "pause")
        before = self.service._data(self.team_id)
        result = self.amend()  # Committed retry is read-only and preserves pause.
        self.assertEqual(result["status"], "PAUSED")
        with self.assertRaisesRegex(ContractError, "paused/cancelled"):
            self.amend(amendment_id="second", expected_controller_budget_seconds=20337, controller_budget_seconds=30000)
        self.assertEqual(self.service._data(self.team_id), before)
        self.service.project_control(self.team_id, "cancel")
        result = self.amend()
        self.assertEqual(result["status"], "CANCELLED")
        with self.assertRaisesRegex(ContractError, "paused/cancelled"):
            self.amend(amendment_id="second", expected_controller_budget_seconds=20337, controller_budget_seconds=30000)

    def test_individually_paused_successor_is_not_resumed_or_amended(self):
        self.accepted(budget=10197)
        child = self.service.project_advance(self.team_id)
        self.service.control(child["team_id"], "pause")
        before = self.service._data(self.team_id)
        with self.assertRaisesRegex(ContractError, "Stopped project batches"):
            self.amend()
        self.assertEqual(self.service._data(self.team_id), before)

    def test_unregistered_queue_and_missing_retained_usage_refuse(self):
        self.start()
        with self.assertRaisesRegex(ContractError, "registered queue"):
            self.amend()
        self.finish()
        self.service.project_supervise(**self.queue(budget=10197))
        root = self.service._data(self.team_id)
        with self.store.connect() as connection:
            connection.execute("DELETE FROM runs WHERE id=?", (root["records"]["implement"]["child_run_id"],))
        with self.assertRaisesRegex(ContractError, "Run does not exist"):
            self.amend()
        self.assertEqual(self.service._data(self.team_id), root)

    def test_lost_commit_response_recovers_one_amendment_after_reconnection(self):
        self.register()
        with patch.object(self.service, "progress", side_effect=RuntimeError("Lost amendment response")):
            with self.assertRaisesRegex(RuntimeError, "Lost"):
                self.amend()
        second = NativeHostService(self.root, self.store)
        self.addCleanup(second.close)
        result = second.project_budget_amend(**self.amendment())
        self.assertEqual(result["project_supervision"]["controller_budget_seconds"], 20337)
        self.assertEqual(len(result["project_supervision"]["budget_amendments"]), 1)
        self.service.team.teams.audit(self.team_id)

    def test_failed_checkpoint_does_not_partially_credit_budget_and_retry_commits_once(self):
        self.register()
        before = self.service._data(self.team_id)
        with patch.object(self.service.team.teams, "save", side_effect=RuntimeError("Crash before checkpoint")):
            with self.assertRaisesRegex(RuntimeError, "Crash"):
                self.amend()
        self.assertEqual(self.service._data(self.team_id), before)
        result = self.amend()
        self.assertEqual(len(result["project_supervision"]["budget_amendments"]), 1)

    def test_concurrent_different_requests_with_same_expected_cap_cannot_double_credit(self):
        self.register()
        def call(identity):
            try:
                return self.amend(amendment_id=identity)["project_supervision"]["controller_budget_seconds"]
            except ContractError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, ("first", "second")))
        self.assertIn(20337, results)
        result = self.service.progress(self.team_id)["project_supervision"]
        self.assertEqual(result["controller_budget_seconds"], 20337)
        self.assertEqual(len(result["budget_amendments"]), 1)
        loser = "second" if result["budget_amendments"][0]["amendment_id"] == "first" else "first"
        with self.assertRaisesRegex(ContractError, "Stale project budget"):
            self.amend(amendment_id=loser)

    def test_framework_changes_and_other_project_refuse_before_amendment(self):
        self.register()
        with patch.object(self.service, "handshake", return_value={"restart_required": True}):
            with self.assertRaisesRegex(ContractError, "Reconnect"):
                self.amend()
        other = self.root.parent / "other-project"
        other.mkdir()
        service = NativeHostService(other, self.store)
        self.addCleanup(service.close)
        with self.assertRaisesRegex(ContractError, "outside"):
            service.project_budget_amend(**self.amendment())
        self.assertNotIn("project_budget_amendments", self.service._data(self.team_id)["native_host"])

    def test_history_is_bounded_and_malformed_chain_cannot_supply_allowance(self):
        self.register()
        self.amend()
        root = self.service._data(self.team_id)
        row = root["native_host"]["project_budget_amendments"][0]
        for i in range(1, 64):
            root["native_host"]["project_budget_amendments"].append({**row, "amendment_id": f"fixture-{i}",
                "expected_controller_budget_seconds": 20336 + i, "controller_budget_seconds": 20337 + i})
        self.service.team.teams.save(root, "fixture.bounded_amendments")
        with self.assertRaisesRegex(ContractError, "history limit"):
            self.amend(amendment_id="sixty-fifth", expected_controller_budget_seconds=20400, controller_budget_seconds=30000)
        root = self.service._data(self.team_id)
        root["native_host"]["project_budget_amendments"][-1]["expected_controller_budget_seconds"] = 1
        with self.assertRaisesRegex(ContractError, "inconsistent"):
            effective_budget(root)

    def test_tool_schema_requires_actual_authorization_and_rejects_generic_execution(self):
        self.register()
        server = NativeMCPServer(self.service)
        info = server.message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "offline", "version": "1"}}})
        self.assertIn("result", info)
        server.message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tool = next(row for row in server.tools if row["name"] == "loop_project_budget_amend")
        self.assertTrue(tool["annotations"]["idempotentHint"])
        for changes in ({"argv": ["codex"]}, {"controller_budget_seconds": True}):
            result = server.message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": tool["name"], "arguments": self.amendment(**changes)}})
            self.assertIn("error", result)
        args = self.amendment(); args.pop("authorization")
        result = server.message({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": tool["name"], "arguments": args}})
        self.assertIn("error", result)
        with patch("subprocess.Popen", side_effect=AssertionError("No process")):
            result = server.message({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": tool["name"], "arguments": self.amendment()}})
        self.assertFalse(result["result"]["isError"])
        self.assertIn("project_budget_amendments", self.service.handshake()["features"])

    def test_dashboard_distinguishes_original_and_authorized_cap(self):
        self.register()
        result = self.amend(authorization="Actual user: <script>only budget</script>")
        page = render_progress(result)
        self.assertIn("原额度 10197 秒", page)
        self.assertIn("已授权扩额 1 次", page)
        self.assertNotIn("<script>only budget</script>", page)


if __name__ == "__main__":
    unittest.main()
