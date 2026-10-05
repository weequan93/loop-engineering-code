"""Old empty-stage plans recover offline without losing contracts or history."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, ROOT, load
from loop_engineering.native_mcp import NativeMCPServer
from loop_engineering.workspace import atomic_write
from tests import test_native_host as fixture


class NativePlanRepairTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start
    complete = fixture.NativeHostTests.complete
    receive = fixture.NativeHostTests.receive

    def legacy_plan(self, *, both=False):
        self.store.authorities.create("stage-fixture-reviewer", "reviewer")
        for task_id in (["implement", "verify"] if both else ["verify"]):
            path = self.root / (".loop/tasks/" + task_id + ".json")
            task = load(path)
            task["workflow"] = "staged"
            task["checks"].append({"id": "review", "type": "review", "independent": True,
                "description": "Required independent actual assessment", "procedure": ["Review current outputs"]})
            task["criteria"].append({"id": "review", "description": "Independent review passes", "check_ids": ["review"]})
            path.write_text(json.dumps(task))
            config = load(ROOT / "templates/engine.json")
            config["evaluator_keys"] = [{"key_id": "stage-fixture-reviewer", "role": "reviewer", "check_ids": ["review"]}]
            engine_path = ".loop/tasks/" + task_id + "-engine.json"
            (self.root / engine_path).write_text(json.dumps(config))
            next(item for item in self.owner.plan["tasks"] if item["id"] == task_id)["engine_file"] = engine_path
        self.owner.fixture.save_plan()
        # Emulate a plan accepted by the old build, then exercise current code.
        with patch("loop_engineering.team_engine.validate_stages"):
            self.start()

    def test_completed_delivery_and_failed_reserved_child_survive_repair_and_real_admission(self):
        self.legacy_plan()
        self.complete("implement")
        self.receive("implement")
        with self.assertRaisesRegex(ContractError, "stage dependency graph"):
            self.service.task_request(self.team_id, "verify")
        before = self.service.team.teams.get(self.team_id)
        original_run = deepcopy(self.store.get(before["records"]["implement"]["child_run_id"]))
        engine_path = self.root / before["tasks"]["verify"]["engine_file"]
        original_engine = load(engine_path)
        sources = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*")
                   if p.is_file() and ".loop" not in p.relative_to(self.root).parts}
        self.assertFalse(self.service.team._exists(before["records"]["verify"]["child_run_id"]))
        with patch("subprocess.Popen", side_effect=AssertionError("Repair dispatches no process")):
            status = self.service.progress(self.team_id)
            self.assertEqual(status["next_action"]["tool"], "loop_repair_plan")
            self.assertEqual(status["plan_configuration"]["task_ids"], ["verify"])
            result = self.service.repair_plan(self.team_id)
        after = self.service.team.teams.get(self.team_id)
        self.assertEqual(after["records"], before["records"])
        for key in ("tasks", "plan", "dependencies", "scenario_digest", "initial_snapshot"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.store.get(original_run["run_id"]), original_run)
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*")
                         if p.is_file() and ".loop" not in p.relative_to(self.root).parts}, sources)
        config = load(engine_path)
        self.assertEqual({k: v for k, v in config.items() if k != "stages"},
                         {k: v for k, v in original_engine.items() if k != "stages"})
        task = load(self.root / before["tasks"]["verify"]["task_file"])
        self.assertEqual(config["stages"][0]["criterion_ids"], [c["id"] for c in task["criteria"]])
        self.assertEqual(config["stages"][0]["write_allow"], task["scope"]["write_allow"])
        self.assertEqual(config["stages"][0]["write_deny"], task["scope"]["write_deny"])
        self.assertEqual(result["next_action"]["tool"], "loop_workbench_prepare")
        packet = self.service.task_request(self.team_id, "verify")
        self.assertEqual(self.service.team.teams.get(self.team_id)["records"]["verify"]["child_run_id"],
                         before["records"]["verify"]["child_run_id"])
        self.assertTrue(packet["task_context"]["bundle"]["task"]["workflow"] == "staged")
        self.assertEqual(self.owner.transport.requests, [])
        self.service.team.teams.audit(self.team_id)

    def test_dry_run_and_repeated_repair_do_not_change_records_or_dispatch(self):
        self.legacy_plan()
        before = deepcopy(self.service.team.teams.get(self.team_id))
        config = (self.root / before["tasks"]["verify"]["engine_file"]).read_bytes()
        with patch("subprocess.Popen", side_effect=AssertionError("No live dispatch")):
            result = self.service.repair_plan(self.team_id, dry_run=True)
            self.assertTrue(result["repairable"])
            self.assertFalse(result["applied"])
            self.assertEqual(self.service.team.teams.get(self.team_id), before)
            self.assertEqual((self.root / before["tasks"]["verify"]["engine_file"]).read_bytes(), config)
            self.service.repair_plan(self.team_id)
            once = deepcopy(self.service.team.teams.get(self.team_id))
            self.service.repair_plan(self.team_id)
            self.assertEqual(self.service.team.teams.get(self.team_id), once)

    def test_write_crash_replays_original_journal_and_reserved_identity(self):
        self.legacy_plan(both=True)
        before = deepcopy(self.service.team.teams.get(self.team_id))
        count = 0

        def crash(path, content):
            nonlocal count
            atomic_write(path, content)
            count += 1
            if count == 1:
                raise RuntimeError("Crash after first actual engine write")

        with patch("loop_engineering.native_plan_repair.atomic_write", side_effect=crash), \
                self.assertRaisesRegex(RuntimeError, "Crash after"):
            self.service.repair_plan(self.team_id)
        pending = self.service.team.teams.get(self.team_id)
        journal = deepcopy(pending["native_host"]["stage_repair"])
        self.assertEqual(self.service.progress(self.team_id)["next_action"]["tool"], "loop_repair_plan")
        with self.assertRaisesRegex(ContractError, "original stage configuration repair"):
            self.service.task_request(self.team_id, "implement")
        self.service.repair_plan(self.team_id)
        after = self.service.team.teams.get(self.team_id)
        self.assertEqual(after["records"], before["records"])
        self.assertEqual(after["native_host"]["stage_repair_history"], [journal])
        self.assertNotIn("stage_repair", after["native_host"])
        self.assertEqual(self.store.runs(), [])
        self.service.team.teams.audit(self.team_id)

    def test_repair_conflict_keeps_user_source_and_prepared_journal(self):
        self.legacy_plan()
        with patch("loop_engineering.native_plan_repair._apply", side_effect=RuntimeError("Crash before write")), \
                self.assertRaises(RuntimeError):
            self.service.repair_plan(self.team_id)
        (self.root / "slug.py").write_text("# actual later user change\n")
        before = deepcopy(self.service.team.teams.get(self.team_id))
        with self.assertRaisesRegex(ContractError, "sources changed"):
            self.service.repair_plan(self.team_id)
        self.assertEqual(self.service.team.teams.get(self.team_id), before)
        self.assertEqual((self.root / "slug.py").read_text(), "# actual later user change\n")

    def test_explicit_resume_replays_partially_written_journal_while_preserving_pause_until_safe(self):
        self.legacy_plan(both=True)

        def crash(path, content):
            atomic_write(path, content)
            raise RuntimeError("Crash after original write")

        with patch("loop_engineering.native_plan_repair.atomic_write", side_effect=crash), self.assertRaises(RuntimeError):
            self.service.repair_plan(self.team_id)
        original = deepcopy(self.service.team.teams.get(self.team_id))
        self.service.control(self.team_id, "pause")
        with self.assertRaisesRegex(ContractError, "Stopped team"):
            self.service.repair_plan(self.team_id)
        result = self.service.control(self.team_id, "resume")
        self.assertEqual(result["status"], "ACTIVE")
        self.assertIsNone(self.service.team.teams.signal(self.team_id))
        after = self.service.team.teams.get(self.team_id)
        self.assertEqual(after["records"], original["records"])
        self.assertEqual(after["native_host"]["stage_repair_history"], [original["native_host"]["stage_repair"]])
        self.service.team.teams.audit(self.team_id)

    def test_conflicting_source_prevents_explicit_resume_without_clearing_pause(self):
        self.legacy_plan()
        with patch("loop_engineering.native_plan_repair._apply", side_effect=RuntimeError("Crash before write")), \
                self.assertRaises(RuntimeError):
            self.service.repair_plan(self.team_id)
        self.service.control(self.team_id, "pause")
        (self.root / "slug.py").write_text("# user change during pause\n")
        before = deepcopy(self.service.team.teams.get(self.team_id))
        with self.assertRaisesRegex(ContractError, "sources changed"):
            self.service.control(self.team_id, "resume")
        self.assertEqual(self.service.team.teams.get(self.team_id), before)
        self.assertEqual(self.service.team.teams.signal(self.team_id), "PAUSED")

    def test_pause_during_control_write_retains_journal_and_requires_explicit_resume(self):
        self.legacy_plan(both=True)
        before = deepcopy(self.service.team.teams.get(self.team_id))

        def pause(path, content):
            atomic_write(path, content)
            self.service.team.teams.signal(self.team_id, "PAUSED")

        with patch("loop_engineering.native_plan_repair.atomic_write", side_effect=pause), \
                self.assertRaisesRegex(ContractError, "stopped during stage repair"):
            self.service.repair_plan(self.team_id)
        self.assertEqual(self.service.progress(self.team_id)["next_action"]["kind"], "paused")
        self.assertEqual(self.service.team.teams.get(self.team_id)["records"], before["records"])
        self.assertIn("stage_repair", self.service.team.teams.get(self.team_id)["native_host"])
        self.assertEqual(self.service.control(self.team_id, "resume")["status"], "ACTIVE")

    def test_source_change_during_control_write_never_commits_a_repaired_checkpoint(self):
        self.legacy_plan()
        before = deepcopy(self.service.team.teams.get(self.team_id))

        def change_source(path, content):
            atomic_write(path, content)
            (self.root / "slug.py").write_text("# actual concurrent user change\n")

        with patch("loop_engineering.native_plan_repair.atomic_write", side_effect=change_source), \
                self.assertRaisesRegex(ContractError, "sources changed"):
            self.service.repair_plan(self.team_id)
        after = self.service.team.teams.get(self.team_id)
        self.assertEqual(after["bound_files"], before["bound_files"])
        self.assertIn("stage_repair", after["native_host"])
        self.assertNotIn("stage_repair_history", after["native_host"])
        self.assertEqual((self.root / "slug.py").read_text(), "# actual concurrent user change\n")

    def test_frozen_contract_change_is_not_repaired(self):
        self.legacy_plan()
        path = self.root / ".loop/tasks/verify.json"
        task = load(path); task["limits"]["max_wall_seconds"] += 1
        path.write_text(json.dumps(task))
        with self.assertRaisesRegex(ContractError, "Frozen team"):
            self.service.repair_plan(self.team_id)

    def test_nonempty_invalid_graph_is_never_replaced(self):
        self.legacy_plan()
        path = self.root / ".loop/tasks/verify-engine.json"
        config = load(path)
        config["stages"] = [{"id": "user-graph", "depends_on": ["missing"], "criterion_ids": ["review"],
                             "write_allow": ["slug.py"], "write_deny": []}]
        path.write_text(json.dumps(config))
        with self.service.team.teams.writer(self.team_id) as data:
            from loop_engineering.workspace import byte_digest
            data["bound_files"][str(path.relative_to(self.root))] = byte_digest(path.read_bytes())
            self.service.team.teams.save(data, "fixture.accepted_old_invalid_declared_graph")
        before = path.read_bytes()
        self.assertEqual(self.service.progress(self.team_id)["next_action"]["kind"], "invalid_plan_configuration")
        with self.assertRaisesRegex(ContractError, "only supports empty"):
            self.service.repair_plan(self.team_id)
        self.assertEqual(path.read_bytes(), before)

    def test_paused_or_cancelled_team_does_not_repair_or_resume(self):
        self.legacy_plan()
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.service.team.stop(self.team_id, cancel=cancel)
                before = deepcopy(self.service.team.teams.get(self.team_id))
                with self.assertRaisesRegex(ContractError, "Stopped team"):
                    self.service.repair_plan(self.team_id)
                self.assertEqual(self.service.team.teams.get(self.team_id), before)

    def test_started_child_cannot_receive_configuration_repair(self):
        self.legacy_plan(both=True)
        with self.service.team.teams.writer(self.team_id) as data, \
                patch("loop_engineering.native_engine.validate_stages"):
            self.service.team._admit(data, "implement")
        before = deepcopy(self.service.team.teams.get(self.team_id))
        child = deepcopy(self.store.get(before["records"]["implement"]["child_run_id"]))
        self.assertFalse(self.service.progress(self.team_id)["plan_configuration"]["repairable"])
        with self.assertRaisesRegex(ContractError, "only supports empty"):
            self.service.repair_plan(self.team_id)
        self.assertEqual(self.store.get(child["run_id"]), child)
        self.assertEqual(self.service.team.teams.get(self.team_id), before)

    def test_shared_engine_file_cannot_change_another_tasks_frozen_configuration(self):
        self.legacy_plan(both=True)
        with self.service.team.teams.writer(self.team_id) as data:
            data["tasks"]["implement"]["engine_file"] = data["tasks"]["verify"]["engine_file"]
            self.service.team.teams.save(data, "fixture.accepted_old_shared_engine")
        before = deepcopy(self.service.team.teams.get(self.team_id))
        with self.assertRaisesRegex(ContractError, "only supports empty"):
            self.service.repair_plan(self.team_id)
        self.assertEqual(self.service.team.teams.get(self.team_id), before)

    def test_rpc_exposes_bounded_repair_and_checks_dry_run_boolean(self):
        self.legacy_plan()
        server = NativeMCPServer(self.service)
        server.message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
        server.message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = server.message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "loop_repair_plan", "arguments": {"team_id": self.team_id, "dry_run": True}}})
        self.assertFalse(result["result"]["isError"])
        self.assertTrue(json.loads(result["result"]["content"][0]["text"])["repairable"])
        with self.assertRaisesRegex(ContractError, "boolean"):
            self.service.repair_plan(self.team_id, dry_run="yes")
        from loop_engineering.cli import parser
        command = parser().parse_args(["host-plan-repair", str(self.root), "--state-dir", str(self.store.directory),
                                     "--team-id", self.team_id, "--dry-run"])
        self.assertTrue(command.dry_run)
        self.assertEqual(command.team_id, self.team_id)


if __name__ == "__main__":
    unittest.main()
