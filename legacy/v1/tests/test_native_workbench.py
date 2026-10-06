"""Real draft files and controller checks, with a deterministic host clock."""

from pathlib import Path
import json
import os
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, load
from loop_engineering.native_host import render_progress
from loop_engineering.native_mcp import NativeMCPServer
from tests import test_native_host as fixture


class NativeWorkbenchTests(unittest.TestCase):
    start = fixture.NativeHostTests.start
    complete = fixture.NativeHostTests.complete
    receive = fixture.NativeHostTests.receive
    prepare_review = fixture.NativeHostTests.prepare_review

    def setUp(self):
        fixture.NativeHostTests.setUp(self)
        self.tick = 1000.0
        self.service.clock = lambda: self.tick

    def prepare(self):
        self.start()
        packet = self.service.workbench_prepare(self.team_id, "implement")
        self.workbench = packet["workbench"]
        self.draft = Path(self.workbench["directory"])
        return packet

    def edit(self, packet):
        change = self.owner.respond(packet)["changes"][0]
        (self.draft / change["path"]).write_text(change["new_content"])

    def submit(self):
        return self.service.workbench_submit(self.team_id, "implement", self.workbench["id"], "Collected actual fixture files")

    def test_actual_draft_files_import_original_checks_handoff_and_final_completion(self):
        packet = self.prepare()
        before = (self.root / "slug.py").read_bytes()
        self.edit(packet)
        status = self.service.workbench_status(self.team_id, "implement")
        self.assertEqual(status["workbenches"][0]["changed_files"], ["slug.py"])
        self.assertEqual((self.root / "slug.py").read_bytes(), before)
        self.assertFalse(self.service.progress(self.team_id)["check_results"])
        result = self.submit()
        self.assertEqual(result["tasks"]["implement"]["status"], "HANDOFF")
        self.assertTrue(result["check_results"])
        self.assertTrue(all(c["result"] == "pass" for c in result["check_results"]))
        self.assertEqual(self.service.workbench_status(self.team_id, "implement")["workbenches"][0]["status"], "SUBMITTED")
        self.receive("implement")
        verify = self.service.workbench_prepare(self.team_id, "verify")
        result = self.service.workbench_submit(self.team_id, "verify", verify["workbench"]["id"], "Run actual final checks on unchanged copy")
        self.receive("verify")
        result = self.service.progress(self.team_id)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertTrue(result["final_candidate_current"])
        self.assertEqual(self.owner.transport.requests, [])

    def test_cached_copy_and_progress_polling_never_renew_the_wait_deadline(self):
        packet = self.prepare()
        deadline = self.workbench["deadline_epoch"]
        self.tick += 20
        again = self.service.workbench_prepare(self.team_id, "implement")
        self.assertEqual(again["workbench"]["id"], self.workbench["id"])
        self.assertEqual(again["workbench"]["deadline_epoch"], deadline)
        self.tick = deadline
        for _ in range(3):
            result = self.service.next(self.team_id)
            self.assertEqual(result["next_action"]["kind"], "recover_worker")
            self.assertEqual(result["tasks"]["implement"]["host_wait"]["deadline_epoch"], deadline)
        self.assertIn("等待已超时", render_progress(result))
        self.assertEqual(result["tasks"]["implement"]["controller_status"], "PLANNING")
        self.assertEqual(self.owner.transport.requests, [])

    def test_expired_wait_preserves_draft_for_supervised_collection_without_budget_reset(self):
        packet = self.prepare()
        self.edit(packet)
        self.tick = self.workbench["deadline_epoch"] + 1
        self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "recover_worker")
        run_id = self.service.team.teams.get(self.team_id)["records"]["implement"]["child_run_id"]
        self.assertEqual(self.service.team.store.get(run_id)["state"]["iteration"], 0)
        result = self.submit()
        self.assertEqual(result["tasks"]["implement"]["status"], "HANDOFF")
        self.assertEqual(self.service.team.store.get(run_id)["state"]["iteration"], 1)

    def test_reassigned_request_keeps_old_draft_and_cumulative_child_identity(self):
        packet = self.prepare()
        self.edit(packet)
        data = self.service.team.teams.get(self.team_id)
        run_id = data["records"]["implement"]["child_run_id"]
        self.service.control(self.team_id, "resume")
        fresh = self.service.workbench_prepare(self.team_id, "implement")
        self.assertNotEqual(fresh["workbench"]["id"], self.workbench["id"])
        self.assertEqual(self.service.team.teams.get(self.team_id)["records"]["implement"]["child_run_id"], run_id)
        self.assertTrue((self.draft / "slug.py").exists())
        with self.assertRaisesRegex(ContractError, "matching current request"):
            self.submit()
        self.assertEqual(len(self.service.workbench_status(self.team_id, "implement")["workbenches"]), 2)

    def test_pause_cancel_override_expired_wait_and_cannot_integrate_draft(self):
        packet = self.prepare()
        self.edit(packet)
        self.tick = self.workbench["deadline_epoch"] + 1
        self.service.control(self.team_id, "pause")
        self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "paused")
        with self.assertRaises(ContractError):
            self.submit()
        self.service.control(self.team_id, "cancel")
        self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "cancelled")
        with self.assertRaises(ContractError):
            self.service.workbench_prepare(self.team_id, "implement")

    def test_control_files_or_out_of_scope_edits_refuse_all_shared_project_changes(self):
        for name in (".loop", ".git", "README.md"):
            with self.subTest(name=name):
                packet = self.prepare()
                before = (self.root / "slug.py").read_bytes()
                self.edit(packet)
                if name.startswith("."):
                    (self.draft / name).mkdir()
                    (self.draft / name / "config").write_text("unauthorized")
                else:
                    (self.draft / name).write_text("unauthorized")
                with self.assertRaises(ContractError):
                    self.submit()
                self.assertEqual((self.root / "slug.py").read_bytes(), before)
                self.service.control(self.team_id, "cancel")

    def test_redirected_workbench_and_symlink_edits_cannot_read_or_import_external_files(self):
        packet = self.prepare()
        before = (self.root / "slug.py").read_bytes()
        saved = self.draft.with_name("saved")
        self.draft.rename(saved)
        self.draft.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ContractError, "redirected"):
            self.submit()
        self.draft.unlink()
        saved.rename(self.draft)
        (self.draft / "slug.py").unlink()
        (self.draft / "slug.py").symlink_to(self.root / "slug.py")
        with self.assertRaisesRegex(ContractError, "symlink"):
            self.submit()
        self.assertEqual((self.root / "slug.py").read_bytes(), before)

    def test_permission_binary_and_deletion_refuse_legacy_import(self):
        packet = self.prepare()
        before = (self.root / "slug.py").read_bytes()
        mode = (self.draft / "slug.py").stat().st_mode & 0o777
        os.chmod(self.draft / "slug.py", 0o600)
        with self.assertRaisesRegex(ContractError, "permission"):
            self.submit()
        os.chmod(self.draft / "slug.py", mode)
        (self.draft / "slug.py").write_bytes(b"\xff\x00")
        with self.assertRaisesRegex(ContractError, "Binary"):
            self.submit()
        (self.draft / "slug.py").unlink()
        with self.assertRaisesRegex(ContractError, "Deletion"):
            self.submit()
        self.assertEqual((self.root / "slug.py").read_bytes(), before)

    def test_stale_shared_candidate_refuses_draft_without_overwriting_user_edit(self):
        packet = self.prepare()
        self.edit(packet)
        (self.root / "slug.py").write_text("Actual user edit\n")
        with self.assertRaisesRegex(ContractError, "stale"):
            self.submit()
        self.assertEqual((self.root / "slug.py").read_text(), "Actual user edit\n")

    def test_negotiated_binary_and_deletion_import_remain_bound_to_original_checks(self):
        task_path = self.root / ".loop/tasks/implement.json"
        task = load(task_path)
        task.setdefault("extensions", {})["agent_step_version"] = "0.3"
        task["scope"]["write_allow"].extend(["data.bin", "old.bin"])
        task_path.write_text(json.dumps(task))
        (self.root / "old.bin").write_bytes(b"old fixture\x00")
        packet = self.prepare()
        self.edit(packet)
        (self.draft / "old.bin").unlink()
        (self.draft / "data.bin").write_bytes(b"\xff\x00\x80actual fixture")
        result = self.submit()
        self.assertEqual(result["tasks"]["implement"]["status"], "HANDOFF")
        self.assertFalse((self.root / "old.bin").exists())
        self.assertEqual((self.root / "data.bin").read_bytes(), b"\xff\x00\x80actual fixture")

    def test_workbench_inside_shared_project_is_refused_before_request_or_draft_creation(self):
        self.start()
        self.service.workbench_parent = self.root
        with self.assertRaisesRegex(ContractError, "outside"):
            self.service.workbench_prepare(self.team_id, "implement")
        self.assertIsNone(self.service.team.teams.get(self.team_id)["records"]["implement"]["child_run_id"])
        self.assertEqual(list(self.root.glob("loop-native-workbench-*")), [])

    def test_bounded_workbench_history_refuses_new_copy_without_erasing_old_records(self):
        self.start()
        with self.service.team.teams.writer(self.team_id) as data:
            data["native_host"]["workbenches"] = {str(n): {"task_id": "unrelated", "request_id": "old", "id": str(n)} for n in range(64)}
            self.service.team.teams.save(data, "fixture.history_limit")
        with self.assertRaisesRegex(ContractError, "history limit"):
            self.service.workbench_prepare(self.team_id, "implement")
        self.assertEqual(len(self.service.team.teams.get(self.team_id)["native_host"]["workbenches"]), 64)
        self.assertEqual(list(self.store.directory.glob("loop-native-workbench-*")), [])

    def test_cancellation_during_copy_checkpoint_cannot_dispatch_or_integrate_work(self):
        self.start()
        original = self.service.team.teams.save
        def cancel_during_save(data, event):
            original(data, event)
            if event == "host.workbench_prepared":
                self.service.team.teams.signal(self.team_id, "CANCELLED")
        with patch.object(self.service.team.teams, "save", side_effect=cancel_during_save):
            with self.assertRaisesRegex(ContractError, "stopped"):
                self.service.workbench_prepare(self.team_id, "implement")
        result = self.service.progress(self.team_id)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(result["next_action"]["kind"], "cancelled")
        self.assertFalse(result["check_results"])

    def test_mcp_workbench_tools_refuse_arbitrary_paths_shell_and_extra_arguments(self):
        self.start()
        server = NativeMCPServer(self.service)
        server.message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "offline", "version": "1"}}})
        server.message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for name in ("loop_workbench_prepare", "loop_workbench_status", "loop_workbench_submit"):
            args = {"team_id": self.team_id, "task_id": "implement", "directory": "/arbitrary/path", "shell": "untrusted"}
            if name.endswith("submit"):
                args.update(workbench_id="unregistered", summary="actual fixture")
            result = server.message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
            self.assertEqual(result["error"]["code"], -32602)
        self.assertIsNone(self.service.team.teams.get(self.team_id)["records"]["implement"]["child_run_id"])

    def test_changed_frozen_contract_refuses_draft_and_empty_summary_is_not_imported(self):
        packet = self.prepare()
        self.edit(packet)
        with self.assertRaisesRegex(ContractError, "summary"):
            self.service.workbench_submit(self.team_id, "implement", self.workbench["id"], " ")
        path = self.root / ".loop/spec.md"
        path.write_text(path.read_text() + "\nActual amended scope\n")
        with self.assertRaisesRegex(ContractError, "inputs changed"):
            self.submit()

    def test_missing_independent_review_cannot_be_satisfied_by_draft_files(self):
        self.prepare_review()
        result = self.service.next(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "await_evaluator")
        self.assertFalse(result["next_action"]["continue_work"])
        self.assertIsNone(result["final_candidate_current"])
