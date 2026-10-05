"""Independent drafts, inspected refresh, serial checks and durable recovery."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, load
from tests import test_native_host as fixture


class NativeParallelTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start
    receive = fixture.NativeHostTests.receive

    def prepare(self, overlap=False):
        task = load(self.root / ".loop/tasks/implement.json")
        docs = deepcopy(task); docs["task_id"] = "docs"
        docs["scope"]["write_allow"] = ["slug.py"] if overlap else ["notes.txt"]
        docs["checks"] = [{"id": "notes", "type": "command", "description": "Read actual notes", "argv": [sys.executable, "-c",
            "from pathlib import Path; assert Path('notes.txt').read_text() == 'actual draft'"], "cwd": ".", "timeout_seconds": 10}]
        docs["criteria"] = [{"id": "notes", "description": "Actual documentation exists", "check_ids": ["notes"]}]
        (self.root / ".loop/tasks/docs.json").write_text(json.dumps(docs))
        plan = deepcopy(self.owner.plan)
        plan["native_parallel"] = {"max_parallel": 2, "max_refreshes": 2}
        plan["tasks"][0]["draft_paths"] = ["slug.py"]
        second = deepcopy(plan["tasks"][0]); second.update(id="docs", task_file=".loop/tasks/docs.json",
            draft_paths=["slug.py"] if overlap else ["notes.txt"], outputs=["slug.py"] if overlap else ["notes.txt"])
        plan["tasks"].insert(1, second)
        (self.root / ".loop/workflow-tasks.json").write_text(json.dumps(plan))
        self.start()
        self.a = self.service.workbench_prepare(self.team_id, "implement")
        if overlap:
            return
        self.b = self.service.workbench_prepare(self.team_id, "docs")
        self.aw = self.a["workbench"]; self.bw = self.b["workbench"]
        change = self.owner.respond(self.a)["changes"][0]
        (Path(self.aw["directory"]) / "slug.py").write_text(change["new_content"])
        (Path(self.bw["directory"]) / "notes.txt").write_text("actual draft")

    def integrate_first(self):
        self.service.workbench_submit(self.team_id, "implement", self.aw["id"], "Actual first draft")
        self.receive("implement")

    def refresh(self):
        return self.service.workbench_refresh(self.team_id, "docs", self.bw["id"], ["slug.py"])

    def test_two_independent_drafts_refresh_and_finish_with_fresh_integrated_checks(self):
        self.prepare()
        self.assertNotEqual(self.aw["directory"], self.bw["directory"])
        self.assertEqual(self.service.progress(self.team_id)["task_counts"]["RUNNING"], 2)
        before = self.service._data(self.team_id)
        run_id = before["records"]["docs"]["child_run_id"]
        deadline = before["native_host"]["workers"]["docs"]["deadline_epoch"]
        self.integrate_first()
        with self.assertRaisesRegex(ContractError, "stale"):
            self.service.workbench_submit(self.team_id, "docs", self.bw["id"], "Cannot import stale draft")
        self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "inspect_draft_base")
        result = self.refresh(); replacement = result["workbench"]
        self.assertEqual(self.refresh()["workbench"]["id"], replacement["id"])
        after = self.service._data(self.team_id)
        self.assertEqual(after["records"]["docs"]["child_run_id"], run_id)
        self.assertEqual(after["native_host"]["workers"]["docs"]["deadline_epoch"], deadline)
        self.assertEqual((Path(self.bw["directory"]) / "notes.txt").read_text(), "actual draft")
        self.assertEqual((Path(replacement["directory"]) / "slug.py").read_bytes(), (self.root / "slug.py").read_bytes())
        self.service.workbench_submit(self.team_id, "docs", replacement["id"], "Rechecked refreshed draft")
        self.receive("docs")
        final = self.service.workbench_prepare(self.team_id, "verify")
        self.service.workbench_submit(self.team_id, "verify", final["workbench"]["id"], "Final integrated check")
        result = self.receive("verify")
        self.assertEqual(result["status"], "COMPLETE")
        self.assertTrue(result["final_candidate_current"])
        self.assertFalse(self.owner.transport.requests)

    def test_overlap_and_out_of_ownership_proposal_refuse_without_project_changes(self):
        self.prepare(overlap=True)
        with self.assertRaisesRegex(ContractError, "disjoint"):
            self.service.workbench_prepare(self.team_id, "docs")
        self.assertNotIn("docs", self.service.progress(self.team_id)["ready_tasks"])

    def test_uninspected_conflicting_or_stopped_refresh_preserves_drafts(self):
        self.prepare(); self.integrate_first()
        with self.assertRaisesRegex(ContractError, "exact changed"):
            self.service.workbench_refresh(self.team_id, "docs", self.bw["id"], [])
        (self.root / "notes.txt").write_text("other writer")
        with self.assertRaises(ContractError):
            self.service.workbench_refresh(self.team_id, "docs", self.bw["id"], ["slug.py", "notes.txt"])
        self.assertEqual((self.root / "notes.txt").read_text(), "other writer")
        self.assertNotIn("refresh_pending", self.service._data(self.team_id)["native_host"])
        self.service.control(self.team_id, "pause")
        with self.assertRaisesRegex(ContractError, "active"):
            self.refresh()

    def test_crash_replays_refresh_journal_without_new_child_or_deadline(self):
        self.prepare(); self.integrate_first()
        before = self.service._data(self.team_id)
        with patch.object(self.service.team.snapshots, "apply_prepared", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.refresh()
        pending = self.service._data(self.team_id)["native_host"]["refresh_pending"]
        self.assertEqual(self.service.progress(self.team_id)["next_action"]["kind"], "recover_draft_refresh")
        result = self.refresh()
        self.assertEqual(result["workbench"]["id"], pending["id"])
        after = self.service._data(self.team_id)
        self.assertEqual(before["records"]["docs"]["child_run_id"], after["records"]["docs"]["child_run_id"])
        self.assertEqual(before["native_host"]["workers"]["docs"]["deadline_epoch"], after["native_host"]["workers"]["docs"]["deadline_epoch"])

    def test_pause_after_refresh_context_keeps_original_journal_and_request(self):
        self.prepare(); self.integrate_first()
        save = self.service.team.teams.save
        def crash(data, event):
            save(data, event)
            if event == "host.refresh_context_prepared":
                raise RuntimeError("crash after context")
        with patch.object(self.service.team.teams, "save", side_effect=crash):
            with self.assertRaises(RuntimeError): self.refresh()
        request = self.service._data(self.team_id)["records"]["docs"]["request"]
        self.service.control(self.team_id, "pause")
        self.service.control(self.team_id, "resume")
        result = self.refresh()
        self.assertEqual(result["workbench"]["request_id"], request["id"])
        self.assertEqual(self.service._data(self.team_id)["records"]["docs"]["request"], request)
        self.service.workbench_submit(self.team_id, "docs", result["workbench"]["id"], "Recovered inspected draft")
        self.assertEqual(self.service.progress(self.team_id)["tasks"]["docs"]["status"], "HANDOFF")

    def test_direct_proposal_cannot_bypass_parallel_file_ownership(self):
        self.prepare()
        step = self.owner.respond(self.a)
        step["changes"][0]["path"] = "notes.txt"
        with self.assertRaisesRegex(ContractError, "ownership"):
            self.service.task_submit(self.team_id, "implement", self.a["team_assignment"]["request_id"], step)
        self.assertFalse((self.root / "notes.txt").exists())
