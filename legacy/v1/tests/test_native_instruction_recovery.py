"""Published instruction recovery with real files/checks; no model dispatch."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, ROOT, load
from loop_engineering.host_install import SKILL
from loop_engineering.native_instruction_recovery import MAX_RECOVERIES
from loop_engineering.native_mcp import NativeMCPServer
from tests import test_native_workers as fixture

NAME = ".agents/skills/loop-engineering/SKILL.md"


class InstructionRecoveryTests(unittest.TestCase):
    setUp = fixture.NativeWorkerTests.setUp
    start = fixture.NativeWorkerTests.start
    assign = fixture.NativeWorkerTests.assign
    bind = fixture.NativeWorkerTests.bind
    row = fixture.NativeWorkerTests.row
    mark = fixture.NativeWorkerTests.mark
    collect = fixture.NativeWorkerTests.collect
    receive = fixture.NativeWorkerTests.receive
    allow_second_file = fixture.NativeWorkerTests.allow_second_file

    def prepare(self, workers=False, customized=False):
        self.allow_second_file() if workers else None
        task_path = self.root / ".loop/tasks/implement.json"
        task = load(task_path)
        task["scope"]["write_allow"].append("repair-notes.md")
        task_path.write_text(json.dumps(task))
        path = self.root / NAME
        path.parent.mkdir(parents=True)
        self.old_skill = (ROOT / "tests/fixtures/native-host-v11-skill.md").read_bytes()
        path.write_bytes(b"# Customized instructions\n" if customized else self.old_skill)
        self.start()
        self.packet = self.service.workbench_prepare(self.team_id, "implement")
        self.workbench = self.packet["workbench"]
        self.original_draft = Path(self.workbench["directory"])
        (self.original_draft / "repair-notes.md").write_text("Preserved coordinator draft\n")
        self.workers = []
        if workers:
            worker = self.assign()
            self.bind(worker)
            change = self.owner.respond(self.packet)["changes"][0]
            (Path(worker["directory"]) / "slug.py").write_text(change["new_content"])
            self.mark(worker, "STOPPED")
            second = self.assign("tester-artifact", role="tester", write_paths=["worker-result.txt"])
            self.bind(second)
            (Path(second["directory"]) / "worker-result.txt").write_text("actual parallel worker\n")
            self.mark(second, "STOPPED")
            self.workers = [worker, second]
        else:
            change = self.owner.respond(self.packet)["changes"][0]
            (self.original_draft / "slug.py").write_text(change["new_content"])
        (self.root / NAME).write_bytes((SKILL / "SKILL.md").read_bytes())
        self.before = deepcopy(self.service._data(self.team_id))

    def recover(self, reviewed=None, dry_run=False):
        return self.service.workbench_reconcile_instructions(self.team_id, "implement", self.workbench["id"],
            [NAME] if reviewed is None else reviewed, dry_run)

    def assert_retained(self, after):
        for key in ("plan", "tasks", "dependencies", "bound_files", "scenario_digest"):
            self.assertEqual(after[key], self.before[key], key)
        self.assertEqual(after["records"]["implement"]["child_run_id"], self.before["records"]["implement"]["child_run_id"])
        old_wait = self.before["native_host"]["workers"]["implement"]
        new_wait = after["native_host"]["workers"]["implement"]
        self.assertEqual({k:v for k,v in old_wait.items() if k != "request_id"},
                         {k:v for k,v in new_wait.items() if k != "request_id"})
        for original in self.workers:
            old = self.before["native_host"]["specialists"][original["id"]]
            new = after["native_host"]["specialists"][original["id"]]
            for key in ("base", "directory", "agent_id", "deadline_epoch", "status", "write_paths", "depends_on"):
                self.assertEqual(old[key], new[key], key)
        self.assertEqual((self.original_draft / NAME).read_bytes(), self.old_skill)
        self.assertEqual((self.original_draft / "repair-notes.md").read_text(), "Preserved coordinator draft\n")
        self.assertNotIn("native_parallel", after["plan"])

    def test_serial_stopped_workers_recover_collect_original_checks_and_final_acceptance(self):
        self.prepare(workers=True)
        with self.assertRaisesRegex(ContractError, "stale"):
            self.collect(self.workers[0])
        self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "inspect_instruction_upgrade")
        preview = self.recover(dry_run=True)
        self.assertEqual(preview["changed_paths"], [NAME])
        self.assertEqual(self.service._data(self.team_id), self.before)
        result = self.recover()
        self.assert_retained(self.service._data(self.team_id))
        replacement = result["workbench"]
        self.assertEqual(self.recover()["workbench"], replacement)
        self.assertEqual((Path(replacement["directory"]) / NAME).read_bytes(), (SKILL / "SKILL.md").read_bytes())
        for worker in self.workers:
            self.collect(worker)
        self.service.workbench_submit(self.team_id, "implement", replacement["id"], "Preserved scoped workers and coordinator edits")
        self.receive("implement")
        final = self.service.workbench_prepare(self.team_id, "verify")
        self.service.workbench_submit(self.team_id, "verify", final["workbench"]["id"], "Fresh original final check")
        status = self.receive("verify")
        self.assertEqual(status["status"], "COMPLETE")
        self.assertTrue(status["final_candidate_current"])
        self.service.team.teams.audit(self.team_id)

    def test_expired_wait_is_preserved_without_new_dispatch_allowance(self):
        self.prepare()
        self.tick = self.workbench["deadline_epoch"] + 100
        result = self.recover()
        self.assert_retained(self.service._data(self.team_id))
        self.assertEqual(self.service.progress(self.team_id)["next_action"]["kind"], "recover_worker")
        self.service.workbench_submit(self.team_id, "implement", result["workbench"]["id"], "Collect stable original expired draft")
        self.assertEqual(self.service.progress(self.team_id)["tasks"]["implement"]["status"], "HANDOFF")

    def test_customized_instructions_are_never_silently_migrated(self):
        self.prepare(customized=True)
        with self.assertRaisesRegex(ContractError, "Customized"):
            self.recover()
        self.assertEqual(self.service._data(self.team_id), self.before)

    def test_unrelated_source_change_refuses_before_journal_or_file_effects(self):
        self.prepare(); (self.root / "other.txt").write_text("Other source change")
        with self.assertRaisesRegex(ContractError, "unrelated"):
            self.recover()
        self.assertEqual(self.service._data(self.team_id), self.before)

    def test_unpublished_new_skill_refuses(self):
        self.prepare(); (self.root / NAME).write_text("# Unpublished modified skill\n")
        with self.assertRaisesRegex(ContractError, "unpublished"):
            self.recover()

    def test_exact_reviewed_paths_required_and_dry_run_is_read_only(self):
        self.prepare()
        for paths in ([], [NAME, "slug.py"], "not-a-list"):
            with self.subTest(paths=paths), self.assertRaisesRegex(ContractError, "exact changed"):
                self.recover(paths)
        self.assertEqual(self.service._data(self.team_id), self.before)

    def test_actual_running_worker_refuses_and_stopped_draft_remains(self):
        self.prepare(workers=True)
        with self.service.team.teams.writer(self.team_id) as data:
            data["native_host"]["specialists"][self.workers[0]["id"]]["status"] = "RUNNING"
            self.service.team.teams.save(data, "fixture.actual_running_writer")
        with self.assertRaisesRegex(ContractError, "Stop actual"):
            self.recover()
        self.assertNotIn("refresh_pending", self.service._data(self.team_id)["native_host"])

    def test_stopped_worker_out_of_scope_refuses(self):
        self.prepare(workers=True)
        (Path(self.workers[0]["directory"]) / "worker-result.txt").write_text("Wrong worker")
        with self.assertRaisesRegex(ContractError, "ownership"):
            self.recover()

    def test_pending_import_refuses(self):
        self.prepare(workers=True)
        with self.service.team.teams.writer(self.team_id) as data:
            data["native_host"]["specialists"][self.workers[0]["id"]]["pending_worker_import"] = {"unresolved": True}
            self.service.team.teams.save(data, "fixture.pending_import")
        with self.assertRaisesRegex(ContractError, "pending imports"):
            self.recover()

    def test_child_pending_effect_and_pause_refuse(self):
        self.prepare()
        run_id = self.before["records"]["implement"]["child_run_id"]
        with self.store.writer(run_id) as child:
            child["state"]["outstanding_action_ids"] = ["fixture-unknown-effect"]
            self.store.save(child, "fixture.unresolved")
        with self.assertRaisesRegex(ContractError, "quiescent"):
            self.recover()
        with self.store.writer(run_id) as child:
            child["state"]["outstanding_action_ids"] = []
            self.store.save(child, "fixture.effects_collected")
        self.service.control(self.team_id, "pause")
        with self.assertRaisesRegex(ContractError, "unstopped"):
            self.recover()

    def test_bounded_history_refuses_without_new_allowance(self):
        self.prepare()
        with self.service.team.teams.writer(self.team_id) as data:
            data["native_host"]["instruction_refresh_history"] = [{"original": "other-" + str(i)} for i in range(MAX_RECOVERIES)]
            self.service.team.teams.save(data, "fixture.full_history")
        with self.assertRaisesRegex(ContractError, "history exhausted"):
            self.recover()

    def test_original_child_time_exhaustion_refuses_without_reset(self):
        self.prepare()
        run_id = self.before["records"]["implement"]["child_run_id"]
        with self.store.writer(run_id) as child:
            child["elapsed_ms"] = child["task"]["limits"]["max_wall_seconds"] * 1000
            self.store.save(child, "fixture.original_budget_exhausted")
        with self.assertRaisesRegex(ContractError, "allowance is exhausted"):
            self.recover()
        self.assertEqual(self.store.get(run_id)["elapsed_ms"], self.store.get(run_id)["task"]["limits"]["max_wall_seconds"] * 1000)
        self.assertNotIn("refresh_pending", self.service._data(self.team_id)["native_host"])

    def test_unknown_partial_copy_changes_refuse_replay(self):
        self.prepare()
        with patch.object(self.service.team.snapshots, "apply_prepared", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError): self.recover()
        pending = self.service._data(self.team_id)["native_host"]["refresh_pending"]
        path = Path(pending["directory"]) / "unrecognized.txt"
        path.write_text("External writer in recovery copy")
        with self.assertRaisesRegex(ContractError, "unknown changes"):
            self.recover()
        self.assertEqual(path.read_text(), "External writer in recovery copy")

    def test_replaced_instruction_symlink_refuses(self):
        self.prepare()
        path = self.root / NAME
        path.unlink(); path.symlink_to(SKILL / "SKILL.md")
        with self.assertRaisesRegex(ContractError, "redirected"):
            self.recover()

    def test_completed_journal_reply_replays_once_and_wrong_task_refuses(self):
        self.prepare()
        save = self.service.team.teams.save
        def crash(data, event):
            save(data, event)
            if event == "host.instruction_refresh_completed": raise RuntimeError("lost reply")
        with patch.object(self.service.team.teams, "save", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "lost reply"): self.recover()
        after = self.service._data(self.team_id)
        self.assertTrue(self.recover()["replayed"])
        self.assertEqual(after, self.service._data(self.team_id))
        with self.assertRaisesRegex(ContractError, "another task"):
            self.service.workbench_reconcile_instructions(self.team_id, "verify", self.workbench["id"], [NAME])

    def test_crash_before_and_after_context_recovers_same_request_and_deadline(self):
        for event in ("host.instruction_refresh_reserved", "host.request_prepared", "host.instruction_refresh_context_prepared"):
            with self.subTest(event=event):
                # Each fixture is a fresh original task; recovery never resets it.
                self.prepare()
                save = self.service.team.teams.save
                def crash(data, name):
                    save(data, name)
                    if name == event:
                        raise RuntimeError("fixture crash")
                with patch.object(self.service.team.teams, "save", side_effect=crash):
                    with self.assertRaisesRegex(RuntimeError, "fixture crash"):
                        self.recover()
                pending = self.service._data(self.team_id)["native_host"]["refresh_pending"]
                self.assertEqual(self.service.progress(self.team_id)["next_action"]["kind"], "recover_draft_refresh")
                result = self.recover()
                self.assertEqual(result["workbench"]["id"], pending["id"])
                self.assertEqual(result["workbench"]["request_id"], pending["request_id"])
                self.assert_retained(self.service._data(self.team_id))
                # Finish the fixture before another original team can begin.
                self.service.workbench_submit(self.team_id, "implement", result["workbench"]["id"], "Crash recovered original draft")
                self.receive("implement")
                final = self.service.workbench_prepare(self.team_id, "verify")
                self.service.workbench_submit(self.team_id, "verify", final["workbench"]["id"], "Final")
                self.receive("verify")
                # Original source goes back only in the deterministic next test project.
                self.doCleanups(); self.setUp()

    def test_mutated_preserved_worker_after_crash_refuses_without_overwrite(self):
        self.prepare(workers=True)
        with patch.object(self.service.team.snapshots, "apply_prepared", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError): self.recover()
        (Path(self.workers[0]["directory"]) / "slug.py").write_text("Late writer mutation\n")
        with self.assertRaisesRegex(ContractError, "drafts changed"):
            self.recover()
        self.assertIn("refresh_pending", self.service._data(self.team_id)["native_host"])

    def test_mcp_registration_and_reconnect_gate(self):
        self.prepare()
        server = NativeMCPServer(self.service)
        tool = next(t for t in server.tools if t["name"] == "loop_workbench_reconcile_instructions")
        self.assertTrue(tool["annotations"]["idempotentHint"])
        with patch.object(self.service, "handshake", return_value={"restart_required": True}):
            with self.assertRaisesRegex(ContractError, "Reconnect"):
                self.recover()
