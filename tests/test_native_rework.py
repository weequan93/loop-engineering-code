"""Offline review -> repair -> fresh review, preserving original child budgets."""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError
from tests import test_native_preflight as fixture


class NativeReworkTests(unittest.TestCase):
    setUp = fixture.NativePreflightTests.setUp
    start = fixture.NativePreflightTests.start
    complete = fixture.NativePreflightTests.complete
    receive = fixture.NativePreflightTests.receive
    prepare_review = fixture.NativePreflightTests.prepare_review
    signed_outcome = fixture.NativePreflightTests.signed_outcome

    def failed_review(self, *, enabled=True, findings=None):
        if enabled:
            self.owner.plan["native_rework"] = {"max_rounds": 2}
        self.owner.plan["tasks"][1]["repair_task"] = "implement"
        self.prepare_review()
        result = self.signed_outcome("fail", findings=findings or [{"id": "gap", "criterion_id": "reviewed",
            "severity": "blocking", "description": "Actual fixture requires a documented repair."}])
        failure = next(e for e in result["pending_evaluations"] if e.get("result") == "fail")
        self.args = dict(team_id=self.team_id, **{k: failure[k] for k in ("task_id", "check_id", "evidence_digest")})
        return result

    def repair_and_accept(self):
        request = self.service.workbench_prepare(self.team_id, "implement")
        path = Path(request["workbench"]["directory"]) / "slug.py"
        path.write_text(path.read_text() + "\n# Reviewed compatibility repair fixture.\n")
        self.service.workbench_submit(self.team_id, "implement", request["workbench"]["id"], "Actual scoped repair")
        self.receive("implement")
        self.complete("verify")
        result = self.signed_outcome("pass")
        self.assertEqual(result["tasks"]["verify"]["status"], "HANDOFF")
        return self.receive("verify")

    def test_failed_review_reopens_same_children_and_reaches_fresh_acceptance(self):
        result = self.failed_review()
        self.assertEqual(result["next_action"]["tool"], "loop_rework", result["next_action"])
        before = deepcopy(self.service._data(self.team_id))
        children = {k: deepcopy(self.store.get(r["child_run_id"])) for k, r in before["records"].items()}
        with patch("subprocess.Popen", side_effect=AssertionError("Rework cannot run models/checks")):
            result = self.service.rework(**self.args)
        self.assertEqual(result["ready_tasks"], ["implement"])
        self.assertEqual(result["rework_rounds"], 1)
        for key, child in children.items():
            after = self.store.get(result["tasks"][key]["child_run_id"])
            self.assertEqual(after["run_id"], child["run_id"])
            for field in ("iteration", "usage", "stalled_iterations", "repeated_failure_count"):
                self.assertEqual(after["state"][field], child["state"][field])
            self.assertEqual(after["elapsed_ms"], child["elapsed_ms"])
            self.assertEqual(after["task"], child["task"])
            self.assertEqual(after["selected_evidence"], [])
        self.assertEqual(self.repair_and_accept()["status"], "COMPLETE")
        self.assertTrue(self.store.evidence(children["verify"]["run_id"], [self.args["evidence_digest"]]))
        self.service.team.teams.audit(self.team_id)
        self.assertEqual(self.owner.transport.requests, [])

    def test_absent_policy_keeps_supervised_stop(self):
        result = self.failed_review(enabled=False)
        self.assertFalse(result["next_action"]["continue_work"])
        with self.assertRaisesRegex(ContractError, "not enabled"):
            self.service.rework(**self.args)

    def test_crash_replays_same_journal_and_repeated_request_is_idempotent(self):
        self.failed_review()
        save = self.store.save
        def crash(data, event, *args, **kwargs):
            result = save(data, event, *args, **kwargs)
            if event == "team.rework_reopened":
                raise RuntimeError("Crash after durable child update")
            return result
        with patch.object(self.store, "save", side_effect=crash), self.assertRaises(RuntimeError):
            self.service.rework(**self.args)
        self.assertEqual(self.service.progress(self.team_id)["next_action"]["kind"], "recover_rework")
        with self.assertRaisesRegex(ContractError, "original native rework"):
            self.service.task_request(self.team_id, "implement")
        self.service.rework(**self.args)
        before = deepcopy(self.service._data(self.team_id))
        self.service.rework(**self.args)
        self.assertEqual(before, self.service._data(self.team_id))
        self.assertEqual(len(before["native_host"]["rework_history"]), 1)

    def test_pause_and_candidate_conflict_preserve_pending_journal(self):
        self.failed_review()
        with patch("loop_engineering.native_rework._apply", side_effect=RuntimeError("crash")), self.assertRaises(RuntimeError):
            self.service.rework(**self.args)
        self.service.control(self.team_id, "pause")
        with self.assertRaisesRegex(ContractError, "Stopped"):
            self.service.rework(**self.args)
        self.service.control(self.team_id, "resume")
        self.service.rework(**self.args)
        self.assertEqual(self.service.progress(self.team_id)["ready_tasks"], ["implement"])

    def test_pending_journal_refuses_source_conflict_and_preserves_original_records(self):
        self.failed_review()
        with patch("loop_engineering.native_rework._apply", side_effect=RuntimeError("crash")), self.assertRaises(RuntimeError):
            self.service.rework(**self.args)
        before = deepcopy(self.service._data(self.team_id))
        path = self.root / "slug.py"; path.write_text(path.read_text() + "\n# user change\n")
        with self.assertRaisesRegex(ContractError, "Candidate changed"):
            self.service.rework(**self.args)
        self.assertEqual(before, self.service._data(self.team_id))

    def test_round_limit_and_active_writer_refuse_recovery(self):
        self.failed_review()
        with self.service.team.teams.writer(self.team_id) as data:
            data["native_host"]["specialists"] = {"fixture": {"status": "RUNNING"}}
            self.service.team.teams.save(data, "fixture.writer_active")
        with self.assertRaisesRegex(ContractError, "specialist writers"):
            self.service.rework(**self.args)
        with self.service.team.teams.writer(self.team_id) as data:
            del data["native_host"]["specialists"]
            data["native_host"]["rework_history"] = [{"id": "consumed-1"}, {"id": "consumed-2"}]
            self.service.team.teams.save(data, "fixture.rounds_consumed")
        with self.assertRaisesRegex(ContractError, "round allowance is exhausted"):
            self.service.rework(**self.args)

    def test_unchanged_candidate_cannot_be_handed_off_as_repair(self):
        self.failed_review(); self.service.rework(**self.args)
        with self.assertRaisesRegex(ContractError, "changed candidate"):
            self.complete("implement")
        self.assertEqual(self.service._data(self.team_id)["records"]["implement"]["status"], "RUNNING")
        run_id = self.service._data(self.team_id)["records"]["implement"]["child_run_id"]
        self.assertEqual(self.store.get(run_id)["state"]["status"], "PLANNING")

    def test_cancelled_child_and_pending_process_never_get_new_allowances(self):
        self.failed_review()
        run_id = self.service._data(self.team_id)["records"]["implement"]["child_run_id"]
        original = deepcopy(self.store.get(run_id))
        with self.store.writer(run_id) as child:
            child["state"]["outstanding_action_ids"] = ["unknown-effect"]
            self.store.save(child, "fixture.unknown_effect")
        with self.assertRaisesRegex(ContractError, "Reconcile child effects"):
            self.service.rework(**self.args)
        with self.store.writer(run_id) as child:
            child["state"]["outstanding_action_ids"] = []
            child["state"]["status"] = "CANCELLED"
            self.store.save(child, "fixture.cancelled")
        with self.assertRaisesRegex(ContractError, "terminal child"):
            self.service.rework(**self.args)
        self.assertEqual(self.store.get(run_id)["elapsed_ms"], original["elapsed_ms"])

    def test_pause_after_partial_child_reopen_recovers_only_after_explicit_resume(self):
        self.failed_review()
        save = self.store.save
        def crash(data, event, *args, **kwargs):
            result = save(data, event, *args, **kwargs)
            if event == "team.rework_reopened":
                raise RuntimeError("Partial rework")
            return result
        with patch.object(self.store, "save", side_effect=crash), self.assertRaises(RuntimeError):
            self.service.rework(**self.args)
        self.service.control(self.team_id, "pause")
        self.assertEqual(self.service.progress(self.team_id)["next_action"]["kind"], "paused")
        with self.assertRaisesRegex(ContractError, "Stopped"):
            self.service.rework(**self.args)
        self.service.control(self.team_id, "resume")
        self.service.rework(**self.args)
        data = self.service._data(self.team_id)
        self.assertEqual(len(data["native_host"]["rework_history"]), 1)
        for record in data["records"].values():
            child = self.store.get(record["child_run_id"])
            self.assertEqual(len(child["team_rework_ids"]), 1)

    def test_all_run_locks_are_acquired_before_any_child_reopens(self):
        self.failed_review()
        data = self.service._data(self.team_id)
        held = data["records"]["verify"]["child_run_id"]
        with self.store._lock(self.store.run_dir(held) / "writer.lock", "fixture owned run"), \
                self.assertRaisesRegex(ContractError, "Another controller"):
            self.service.rework(**self.args)
        for record in data["records"].values():
            self.assertNotIn("team_rework_ids", self.store.get(record["child_run_id"]))
        self.service.rework(**self.args)
        self.assertEqual(self.service.progress(self.team_id)["ready_tasks"], ["implement"])

    def test_large_review_findings_remain_available_without_overflowing_task_context(self):
        findings = [{"id": "gap-" + str(i), "criterion_id": "reviewed", "severity": "blocking",
                     "description": "Actual fixture finding " + "x" * 2048} for i in range(100)]
        self.failed_review(findings=findings); self.service.rework(**self.args)
        packet = self.service.task_request(self.team_id, "implement")
        feedback = packet["team_assignment"]["repair_feedback"][0]
        from loop_engineering.team_memory import byte_size
        from loop_engineering.workspace import byte_digest
        import json
        actual = Path(feedback["findings_path"]).read_bytes()
        self.assertEqual(byte_digest(actual), feedback["findings_sha256"])
        self.assertEqual(json.loads(actual)["findings"], findings)
        self.assertEqual(feedback["findings_count"], 100)
        self.assertLess(byte_size(feedback), 4096)
        self.assertLessEqual(byte_size(packet), self.service.team.profile(self.service._data(self.team_id))["context_max_bytes"])

    def test_stale_failure_and_exhausted_child_allowance_refuse_without_reset(self):
        self.failed_review()
        before = deepcopy(self.service._data(self.team_id))
        path = self.root / "slug.py"
        original = path.read_bytes(); path.write_bytes(original + b"\n# changed\n")
        with self.assertRaisesRegex(ContractError, "current authenticated"):
            self.service.rework(**self.args)
        path.write_bytes(original)
        run_id = before["records"]["implement"]["child_run_id"]
        with self.store.writer(run_id) as child:
            child["elapsed_ms"] = child["task"]["limits"]["max_wall_seconds"] * 1000
            self.store.save(child, "fixture.exhausted")
        with self.assertRaisesRegex(ContractError, "allowance is exhausted"):
            self.service.rework(**self.args)
        self.assertEqual(before, self.service._data(self.team_id))


if __name__ == "__main__":
    unittest.main()
