"""Actual receipt rejection -> retained-budget repair -> new checks and receipts."""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlparse

from loop_engineering.contracts import ContractError, load
from loop_engineering.native_mcp import NativeMCPServer
from loop_engineering.workspace import byte_digest
from tests import test_native_preflight as fixture


class NativeHandoffReworkTests(unittest.TestCase):
    setUp = fixture.NativePreflightTests.setUp
    start = fixture.NativePreflightTests.start
    complete = fixture.NativePreflightTests.complete
    receive = fixture.NativePreflightTests.receive
    prepare_review = fixture.NativePreflightTests.prepare_review
    signed_outcome = fixture.NativePreflightTests.signed_outcome

    def rejected(self, *, enabled=True, task_id="implement"):
        if enabled:
            self.owner.plan["native_rework"] = {"max_rounds": 2}
        if task_id == "verify":
            self.owner.plan["tasks"][1]["repair_task"] = "implement"
            self.prepare_review()
            self.signed_outcome("pass")
        else:
            self.owner.fixture.save_plan()
            self.start(); self.complete("implement")
        before = self.service._data(self.team_id)
        handoff_id = before["records"][task_id]["handoff"]["id"]
        self.args = dict(team_id=self.team_id, task_id=task_id, handoff_id=handoff_id)
        result = self.service.receive(**self.args, role="coordinator", accept=False,
            note="Actual recipient requests a scoped compatibility repair; previous passing commands are retained.")
        self.assertEqual(result["status"], "BLOCKED")
        return result

    def repair(self, *, broken=False):
        packet = self.service.workbench_prepare(self.team_id, "implement")
        path = Path(packet["workbench"]["directory"]) / "slug.py"
        path.write_text("def slug(text):\n    return 'broken'\n" if broken else
                        path.read_text() + "\n# Actual recipient compatibility repair fixture.\n")
        result = self.service.workbench_submit(self.team_id, "implement", packet["workbench"]["id"],
                                             "Actual scoped repair files and original command checks")
        return packet, result

    def test_rejected_producer_reopens_original_child_and_reaches_fresh_completion(self):
        state = self.rejected()
        self.assertEqual(state["next_action"]["tool"], "loop_handoff_rework")
        self.assertTrue(state["next_action"]["continue_work"])
        self.assertFalse(state["turn_boundary"]["may_end_turn"])
        before = deepcopy(self.service._data(self.team_id))
        original = deepcopy(self.store.get(before["records"]["implement"]["child_run_id"]))
        with patch("subprocess.Popen", side_effect=AssertionError("Recovery cannot run a model or check")):
            state = self.service.handoff_rework(**self.args)
        after = self.store.get(original["run_id"])
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["ready_tasks"], ["implement"])
        self.assertEqual(state["rework_rounds"], 1)
        self.assertEqual(after["task"], original["task"])
        self.assertEqual(after["elapsed_ms"], original["elapsed_ms"])
        for key in ("iteration", "usage", "stalled_iterations", "repeated_failure_count"):
            self.assertEqual(after["state"][key], original["state"][key])
        self.assertEqual(after["selected_evidence"], [])
        self.assertEqual(after["state"]["progress"]["passed_criteria"], [])
        _, state = self.repair()
        new_handoff = state["tasks"]["implement"]["handoff"]
        self.assertNotEqual(new_handoff["id"], self.args["handoff_id"])
        self.assertNotEqual(new_handoff["candidate"], before["records"]["implement"]["handoff"]["candidate"])
        self.receive("implement"); self.complete("verify")
        self.assertEqual(self.receive("verify")["status"], "COMPLETE")
        history = self.service._data(self.team_id)["native_host"]["rework_history"]
        self.assertEqual(history[0]["records"]["implement"]["receipts"], before["records"]["implement"]["receipts"])
        self.assertTrue(self.store.evidence(original["run_id"], original["selected_evidence"]))
        self.service.team.teams.audit(self.team_id)
        self.assertEqual(self.owner.transport.requests, [])

    def test_rejected_readonly_review_reopens_explicit_ancestor_and_requires_new_independent_pass(self):
        self.rejected(task_id="verify")
        old = deepcopy(self.service._data(self.team_id))
        children = {key: self.store.get(r["child_run_id"]) for key, r in old["records"].items()}
        state = self.service.handoff_rework(**self.args)
        self.assertEqual(state["ready_tasks"], ["implement"])
        for key, child in children.items():
            current = self.store.get(state["tasks"][key]["child_run_id"])
            self.assertEqual(current["run_id"], child["run_id"])
            self.assertEqual(current["elapsed_ms"], child["elapsed_ms"])
            if "native" in current:
                self.assertEqual(current["native"]["external_by_check"], {})
        self.repair(); self.receive("implement")
        state = self.complete("verify")
        self.assertEqual(state["tasks"]["verify"]["status"], "RUNNING")
        self.assertTrue(state["pending_evaluations"])
        self.signed_outcome("pass")
        self.assertEqual(self.receive("verify")["status"], "COMPLETE")

    def test_dry_run_and_mcp_route_preserve_records_and_usage_without_dispatch(self):
        self.rejected()
        old = deepcopy(self.service._data(self.team_id))
        child_id = old["records"]["implement"]["child_run_id"]
        child = deepcopy(self.store.get(child_id))
        with patch("subprocess.Popen", side_effect=AssertionError("Inspection cannot dispatch")):
            report = NativeMCPServer(self.service).handlers()["loop_handoff_rework"](**self.args, dry_run=True)
        self.assertTrue(report["eligible"])
        self.assertEqual(old, self.service._data(self.team_id))
        self.assertEqual(child, self.store.get(child_id))
        self.assertNotIn("rework_pending", old["native_host"])

    def test_absent_policy_and_shared_round_exhaustion_keep_original_blocked_state(self):
        self.rejected(enabled=False)
        old = deepcopy(self.service._data(self.team_id))
        with self.assertRaisesRegex(ContractError, "not enabled"):
            self.service.handoff_rework(**self.args)
        self.assertEqual(old, self.service._data(self.team_id))
        self.assertFalse(self.service.progress(self.team_id)["next_action"]["continue_work"])

    def test_preparation_repairs_and_review_reworks_consume_the_same_frozen_limit(self):
        self.rejected()
        with self.service.team.teams.writer(self.team_id) as data:
            data["native_host"]["repair_rounds"] = load(self.root / ".loop/team-policy.json")["max_reworks"]
            self.service.team.teams.save(data, "fixture.original_rounds_consumed")
        before = deepcopy(self.service._data(self.team_id))
        self.assertEqual(self.service.handoff_rework(**self.args, dry_run=True)["remaining_rounds"], 0)
        with self.assertRaisesRegex(ContractError, "allowance is exhausted"):
            self.service.handoff_rework(**self.args)
        self.assertEqual(before, self.service._data(self.team_id))

    def test_stale_handoff_and_foreign_task_refuse_without_new_journal(self):
        self.rejected()
        before = deepcopy(self.service._data(self.team_id))
        for args in ({**self.args, "handoff_id": "handoff-stale"}, {**self.args, "task_id": "verify"}):
            with self.subTest(args=args), self.assertRaisesRegex(ContractError, "exact current rejected"):
                self.service.handoff_rework(**args)
        self.assertEqual(before, self.service._data(self.team_id))

    def test_source_change_and_corrupt_command_artifact_prevent_reopening(self):
        self.rejected()
        before = deepcopy(self.service._data(self.team_id))
        path = self.root / "slug.py"; original = path.read_bytes()
        path.write_bytes(original + b"\n# Concurrent actual user change\n")
        with self.assertRaisesRegex(ContractError, "candidate or environment changed"):
            self.service.handoff_rework(**self.args)
        path.write_bytes(original)
        child = self.store.get(before["records"]["implement"]["child_run_id"])
        evidence = self.store.evidence(child["run_id"], child["selected_evidence"])[0]
        artifact = Path(unquote(urlparse(evidence["artifacts"][0]["uri"]).path))
        artifact.write_bytes(artifact.read_bytes() + b"changed")
        with self.assertRaises(ContractError):
            self.service.handoff_rework(**self.args)
        self.assertEqual(before, self.service._data(self.team_id))

    def test_uncollected_writers_and_pending_child_effects_refuse_recovery(self):
        self.rejected()
        with self.service.team.teams.writer(self.team_id) as data:
            data["native_host"]["specialists"] = {"active": {"task_id": "implement", "status": "RUNNING"}}
            self.service.team.teams.save(data, "fixture.actual_writer")
        with self.assertRaisesRegex(ContractError, "specialist writers"):
            self.service.handoff_rework(**self.args)
        with self.service.team.teams.writer(self.team_id) as data:
            del data["native_host"]["specialists"]
            self.service.team.teams.save(data, "fixture.writer_collected")
        child_id = data["records"]["implement"]["child_run_id"]
        with self.store.writer(child_id) as child:
            child["state"]["outstanding_action_ids"] = ["actual-unresolved-effect"]
            self.store.save(child, "fixture.pending_effect")
        with self.assertRaisesRegex(ContractError, "Reconcile child effects"):
            self.service.handoff_rework(**self.args)

    def test_crash_after_child_reopen_recovers_same_round_and_original_receipt(self):
        self.rejected()
        save = self.store.save
        def crash(data, event, *args, **kwargs):
            result = save(data, event, *args, **kwargs)
            if event == "team.rework_reopened":
                raise RuntimeError("Crash after durable child reopen")
            return result
        with patch.object(self.store, "save", side_effect=crash), self.assertRaises(RuntimeError):
            self.service.handoff_rework(**self.args)
        state = self.service.progress(self.team_id)
        self.assertEqual(state["status"], "BLOCKED")
        self.assertEqual(state["next_action"]["kind"], "recover_rework")
        self.assertEqual(state["next_action"]["tool"], "loop_handoff_rework")
        self.service.handoff_rework(**self.args)
        before = deepcopy(self.service._data(self.team_id))
        self.service.handoff_rework(**self.args)
        self.assertEqual(before, self.service._data(self.team_id))
        child = self.store.get(before["records"]["implement"]["child_run_id"])
        self.assertEqual(len(child["team_rework_ids"]), 1)
        self.assertEqual(len(before["native_host"]["rework_history"]), 1)

    def test_pause_after_partial_reopen_requires_explicit_resume_and_keeps_journal(self):
        self.rejected()
        with patch("loop_engineering.native_handoff_rework._apply", side_effect=RuntimeError("crash")), self.assertRaises(RuntimeError):
            self.service.handoff_rework(**self.args)
        self.service.control(self.team_id, "pause")
        self.assertEqual(self.service.progress(self.team_id)["next_action"]["kind"], "paused")
        with self.assertRaisesRegex(ContractError, "Stopped"):
            self.service.handoff_rework(**self.args)
        self.service.control(self.team_id, "resume")
        self.service.handoff_rework(**self.args)
        self.assertEqual(self.service.progress(self.team_id)["ready_tasks"], ["implement"])

    def test_cancelled_team_or_exhausted_child_never_gets_new_allowance(self):
        self.rejected()
        child_id = self.service._data(self.team_id)["records"]["implement"]["child_run_id"]
        with self.store.writer(child_id) as child:
            child["elapsed_ms"] = child["task"]["limits"]["max_wall_seconds"] * 1000
            self.store.save(child, "fixture.original_allowance_exhausted")
        with self.assertRaisesRegex(ContractError, "allowance is exhausted"):
            self.service.handoff_rework(**self.args)
        self.service.control(self.team_id, "cancel")
        with self.assertRaisesRegex(ContractError, "blocked native team"):
            self.service.handoff_rework(**self.args)

    def test_findings_remain_full_and_source_typed_in_bounded_repair_memory(self):
        self.rejected(); self.service.handoff_rework(**self.args)
        packet = self.service.task_request(self.team_id, "implement")
        source = packet["team_assignment"]["repair_feedback"][0]
        self.assertEqual(source["kind"], "handoff")
        self.assertNotIn("evidence_digest", source)
        contents = Path(source["findings_path"]).read_bytes()
        self.assertEqual(byte_digest(contents), source["findings_sha256"])
        record = json.loads(contents)
        self.assertFalse(record["receipts"]["coordinator"]["accepted"])
        self.assertEqual(record["findings"][0]["source"], "host-declared rejected receipt")
        self.assertIn("native_repair_finding", json.dumps(packet["team_assignment"]["memory"]))
        memory = self.service.memory(self.team_id, role="reviewer", task_id="implement")
        self.assertNotIn(source["findings_path"], json.dumps(memory))
        self.assertNotIn("native_repair_finding", json.dumps(memory))

    def test_all_child_locks_precede_reopening_rejected_review_and_owner(self):
        self.rejected(task_id="verify")
        original = deepcopy(self.service._data(self.team_id))
        locked = original["records"]["verify"]["child_run_id"]
        with self.store._lock(self.store.run_dir(locked) / "writer.lock", "Actual fixture owner"), \
                self.assertRaisesRegex(ContractError, "Another controller"):
            self.service.handoff_rework(**self.args)
        for record in original["records"].values():
            self.assertNotIn("team_rework_ids", self.store.get(record["child_run_id"]))
        self.service.handoff_rework(**self.args)
        self.assertEqual(self.service.progress(self.team_id)["ready_tasks"], ["implement"])

    def test_new_rejection_uses_new_identity_but_no_new_round_allowance(self):
        self.rejected(); self.service.handoff_rework(**self.args)
        self.repair()
        state = self.receive("implement", accept=False)
        second = dict(self.args, handoff_id=state["tasks"]["implement"]["handoff"]["id"])
        self.assertNotEqual(second["handoff_id"], self.args["handoff_id"])
        self.service.handoff_rework(**second)
        before = deepcopy(self.service._data(self.team_id))
        self.service.handoff_rework(**self.args)
        self.service.handoff_rework(**second)
        self.assertEqual(before, self.service._data(self.team_id))
        self.assertEqual(len(before["native_host"]["rework_history"]), 2)

    def test_project_stop_remains_gate_during_prepare_and_pending_replay(self):
        self.rejected()
        with patch("loop_engineering.native_project.assert_running", side_effect=ContractError("Actual project is paused")), \
                self.assertRaisesRegex(ContractError, "project is paused"):
            self.service.handoff_rework(**self.args)
        with patch("loop_engineering.native_handoff_rework._apply", side_effect=RuntimeError("crash")), self.assertRaises(RuntimeError):
            self.service.handoff_rework(**self.args)
        before = deepcopy(self.service._data(self.team_id))
        with patch("loop_engineering.native_project.assert_running", side_effect=ContractError("Actual project is paused")), \
                self.assertRaisesRegex(ContractError, "project is paused"):
            self.service.handoff_rework(**self.args)
        self.assertEqual(before, self.service._data(self.team_id))

    def test_invalid_recipient_receipt_is_not_reopening_authority(self):
        self.rejected()
        with self.service.team.teams.writer(self.team_id) as data:
            data["records"]["implement"]["receipts"] = {"unregistered": {"accepted": False, "note": "Unregistered recipient"}}
            self.service.team.teams.save(data, "fixture.invalid_recipient")
        before = deepcopy(self.service._data(self.team_id))
        with self.assertRaisesRegex(ContractError, "invalid recipient receipt"):
            self.service.handoff_rework(**self.args)
        self.assertEqual(before, self.service._data(self.team_id))

    def test_unchanged_candidate_and_broken_repair_cannot_be_handed_off(self):
        self.rejected(); self.service.handoff_rework(**self.args)
        with self.assertRaisesRegex(ContractError, "changed candidate"):
            self.complete("implement")
        _, state = self.repair(broken=True)
        self.assertEqual(state["tasks"]["implement"]["status"], "RUNNING")
        self.assertIsNone(state["tasks"]["implement"]["handoff"])
        self.assertTrue(any(check["result"] == "fail" for check in state["check_results"]))

    def test_corrupt_findings_and_other_rework_entry_cannot_replay_pending_journal(self):
        self.rejected()
        with patch("loop_engineering.native_handoff_rework._apply", side_effect=RuntimeError("crash")), self.assertRaises(RuntimeError):
            self.service.handoff_rework(**self.args)
        before = deepcopy(self.service._data(self.team_id))
        with self.assertRaisesRegex(ContractError, "original rework journal"):
            self.service.rework(self.team_id, "implement", "unknown", "sha256:" + "0" * 64)
        path = Path(before["native_host"]["rework_pending"]["findings_path"])
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ContractError, "findings changed"):
            self.service.handoff_rework(**self.args)
        self.assertEqual(before, self.service._data(self.team_id))

    def test_owner_without_edit_authority_has_no_automatic_reopening(self):
        path = self.root / ".loop/tasks/implement.json"
        task = load(path); task["authorization"]["allowed_actions"].remove("edit_workspace")
        path.write_text(json.dumps(task))
        from tests.test_scenarios import FIXED
        (self.root / "slug.py").write_text(FIXED)
        self.owner.plan["native_rework"] = {"max_rounds": 2}
        self.owner.fixture.save_plan()
        self.start()
        packet = self.service.workbench_prepare(self.team_id, "implement")
        state = self.service.workbench_submit(self.team_id, "implement", packet["workbench"]["id"], "Original actual checks only")
        args = dict(team_id=self.team_id, task_id="implement", handoff_id=state["tasks"]["implement"]["handoff"]["id"])
        self.service.receive(**args, role="coordinator", accept=False, note="Actual required repair has no authorized writer")
        self.assertFalse(self.service.handoff_rework(**args, dry_run=True)["eligible"])
        with self.assertRaisesRegex(ContractError, "no authorized source edits"):
            self.service.handoff_rework(**args)


if __name__ == "__main__":
    unittest.main()
