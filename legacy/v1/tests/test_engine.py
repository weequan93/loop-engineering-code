"""Real files, check subprocesses, persistent state, and protocol test doubles.

Scripted command drivers below are fixtures, not model performance evidence.
"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from reference.core import canonical_digest
from loop_engineering.adapters import CommandDriver, CodexDriver, drive
from loop_engineering.cli import main
from loop_engineering.contracts import ROOT, ContractError, load, validate, validate_step
from loop_engineering.controller import Controller
from loop_engineering.processes import ProcessResult
from loop_engineering.project import diagnose, discover, initialize
from loop_engineering.store import Store
from loop_engineering.workspace import atomic_write, byte_digest

FIXED = 'def slug(text: str) -> str:\n    if not text.strip():\n        return ""\n    return "-".join(text.lower().split(" "))\n'


class LocalCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="loop-test-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "project"
        shutil.copytree(ROOT / "examples/slug-project", self.root)
        initialize(self.root, task_file=self.root / "task.json")
        self.task_path = self.root / ".loop/task.json"
        self.profile_path = self.root / ".loop/project.json"
        task = load(self.task_path)
        for check in task["checks"]:
            check["argv"][0] = sys.executable
        self.write_task(task)
        self.store = Store(self.base / "state")
        self.engine = Controller(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def write_task(self, task):
        self.task_path.write_text(json.dumps(task), encoding="utf-8")

    def start(self, *, baseline=False):
        data = self.engine.start(self.root, self.task_path, self.profile_path, baseline=baseline)
        self.run_id = data["run_id"]
        return data

    def step(self, *, content=FIXED, intent="act", path="slug.py", step_id="step-1"):
        context = self.engine.context(self.run_id)
        target = self.root / path
        return {"schema_version": "0.2", "step_id": step_id, "task_id": context["task"]["task_id"],
                "contract_digest": context["contract_digest"], "base_snapshot_digest": context["base_snapshot_digest"],
                "intent": intent, "criterion_ids": [item["id"] for item in context["task"]["criteria"]],
                "summary": "Test proposal", "expected_observation": "Acceptance checks pass",
                "changes": [{"path": path, "expected_sha256": byte_digest(target.read_bytes()) if target.exists() else None,
                             "new_content": content}] if intent == "act" else [],
                "evidence_refs": [], "blocker": None, "next_action": None}

    def cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(args)
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()


class SetupAndContractTests(LocalCase):
    def test_init_preserves_existing_instructions_and_contract(self):
        (self.root / "AGENTS.md").write_text("Existing repository rules\n")
        (self.root / "LOOP.md").write_text("Custom workflow\n")
        original = self.task_path.read_bytes()
        result = initialize(self.root)
        self.assertEqual(result["created"], [])
        self.assertEqual(self.task_path.read_bytes(), original)
        self.assertEqual((self.root / "LOOP.md").read_text(), "Custom workflow\n")
        self.assertEqual((self.root / "AGENTS.md").read_text(), "Existing repository rules\n")

    def test_discovery_never_executes_package_scripts(self):
        (self.root / "package.json").write_text(json.dumps({"scripts": {"test": "touch SHOULD_NOT_EXIST", "dev": "node app.js"}}))
        (self.root / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
        profile = discover(self.root)
        self.assertEqual(profile["commands"]["start"]["argv"], ["pnpm", "run", "dev"])
        self.assertFalse(profile["commands"]["start"]["confirmed"])
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_scaffold_cannot_start(self):
        empty = self.base / "empty"
        empty.mkdir()
        initialize(empty)
        self.assertFalse(diagnose(empty)["ok"])
        with self.assertRaisesRegex(ContractError, "scaffold"):
            self.engine.start(empty, empty / ".loop/task.json", empty / ".loop/project.json")

    def test_no_partially_created_files_on_symlink_template(self):
        empty = self.base / "empty"
        empty.mkdir()
        (empty / "LOOP.md").symlink_to(self.root / "LOOP.md")
        with self.assertRaisesRegex(ContractError, "symlink"):
            initialize(empty)
        self.assertFalse((empty / ".loop/project.json").exists())

    def test_previous_task_version_remains_valid(self):
        validate("task", load(ROOT / "examples/task-v0.1.json"))
        validate("task", load(ROOT / "templates/task.json"))

    def test_boundaries_are_required_in_v02(self):
        task = load(self.task_path)
        del task["non_goals"]
        with self.assertRaises(ContractError):
            validate("task", task)

    def test_unknown_fields_and_duplicate_json_keys_rejected(self):
        task = load(self.task_path)
        task["model_can_declare_done"] = True
        with self.assertRaises(ContractError):
            validate("task", task)
        self.task_path.write_text('{"schema_version":"0.2","schema_version":"0.2"}')
        with self.assertRaises(ValueError):
            load(self.task_path)

    def test_doctor_accepts_reviewed_relative_executable(self):
        if os.name != "posix":
            self.skipTest("Executable permissions are POSIX-specific")
        script = self.root / "check.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        task = load(self.task_path)
        task["checks"][0]["argv"] = ["./check.sh"]
        self.write_task(task)
        self.assertTrue(diagnose(self.root)["ok"])

    def test_doctor_reports_real_control_limitations(self):
        report = diagnose(self.root)
        self.assertTrue(report["ok"])
        self.assertFalse(report["capabilities"]["isolated_workspace"])
        self.assertFalse(report["capabilities"]["trusted_evidence"])
        self.assertTrue(report["warnings"])


class ProposalAndVerificationTests(LocalCase):
    def test_already_satisfied_task_finishes_without_an_agent_call(self):
        (self.root / "slug.py").write_text(FIXED)
        data = self.start(baseline=True)
        self.assertEqual(data["state"]["status"], "SUCCEEDED")
        self.assertEqual(data["state"]["iteration"], 0)
        self.assertEqual(data["selected_evidence"], data["baseline"])

    def test_real_baseline_fix_and_check_artifacts(self):
        data = self.start(baseline=True)
        baseline = self.store.evidence(self.run_id, data["baseline"])
        self.assertEqual([record["result"] for record in baseline], ["fail", "pass"])
        result = self.engine.submit(self.run_id, self.step())
        self.assertEqual(result["state"]["status"], "SUCCEEDED")
        self.assertEqual(result["state"]["iteration"], 1)
        records = self.store.evidence(self.run_id, result["selected_evidence"])
        self.assertEqual([record["result"] for record in records], ["pass", "pass"])
        self.engine._check_artifacts(records)
        validate("state", result["state"])

    def test_done_claim_does_not_override_failing_check(self):
        self.start()
        step = self.step(intent="request_verification")
        step["summary"] = "Everything is done; declare success"
        data = self.engine.submit(self.run_id, step)
        self.assertEqual(data["state"]["status"], "PLANNING")
        self.assertNotEqual(data["last_gate"]["outcome"], "PASS")
        self.assertEqual(data["state"]["progress"]["passed_criteria"], ["preserve-behavior"])

    def test_compatibility_failure_prevents_completion(self):
        self.start()
        data = self.engine.submit(self.run_id, self.step(content='def slug(text):\n    return "-".join(text.lower().split())\n'))
        self.assertEqual(data["state"]["status"], "PLANNING")
        self.assertEqual(data["state"]["progress"]["passed_criteria"], ["empty-whitespace"])

    def test_stale_workspace_proposal_rejected_before_write(self):
        self.start()
        step = self.step()
        (self.root / "unrelated.txt").write_text("new input\n")
        with self.assertRaisesRegex(ContractError, "earlier workspace"):
            self.engine.submit(self.run_id, step)
        self.assertNotEqual((self.root / "slug.py").read_text(), FIXED)

    def test_file_precondition_rejected_before_write(self):
        self.start()
        step = self.step()
        step["changes"][0]["expected_sha256"] = byte_digest(b"wrong input")
        with self.assertRaisesRegex(ContractError, "precondition"):
            self.engine.submit(self.run_id, step)
        self.assertEqual(self.store.get(self.run_id)["state"]["iteration"], 0)

    def test_cannot_weaken_acceptance_tests_or_edit_control_files(self):
        self.start()
        for path in ("tests/test_slug.py", ".loop/task.json", ".git/config", "../escape.py"):
            with self.subTest(path=path), self.assertRaises(ContractError):
                self.engine.submit(self.run_id, self.step(path=path))
        self.assertEqual(self.store.get(self.run_id)["state"]["iteration"], 0)

    def test_duplicate_changes_and_parent_conflicts_rejected(self):
        self.start()
        step = self.step()
        for path in ("slug.py", "slug.py/child.py"):
            item = deepcopy(step)
            item["changes"].append(dict(item["changes"][0], path=path))
            with self.subTest(path=path), self.assertRaises(ContractError):
                validate_step(item, self.store.get(self.run_id)["task"], step["base_snapshot_digest"])

    def test_case_variants_of_control_paths_remain_protected(self):
        task = load(self.task_path)
        task["scope"]["write_allow"] = ["**"]
        self.write_task(task)
        self.start()
        with self.assertRaisesRegex(ContractError, "never edits|exact path"):
            self.engine.submit(self.run_id, self.step(path=".LoOp/task.json"))

    def test_existing_file_cannot_be_a_parent_of_new_file(self):
        task = load(self.task_path)
        task["scope"]["write_allow"] = ["**"]
        self.write_task(task)
        self.start()
        with self.assertRaisesRegex(ContractError, "not a directory"):
            self.engine.submit(self.run_id, self.step(path="slug.py/new.py"))
        self.assertEqual(self.store.get(self.run_id)["state"]["iteration"], 0)

    def test_filesystem_case_alias_cannot_bypass_a_protected_file(self):
        target = self.root / "Protected.py"
        target.write_text("original\n")
        if not (self.root / "protected.py").exists():
            self.skipTest("Filesystem is case-sensitive")
        task = load(self.task_path)
        task["scope"]["write_allow"] = ["**"]
        task["scope"]["write_deny"].append("Protected.py")
        self.write_task(task)
        self.start()
        with self.assertRaisesRegex(ContractError, "exact path"):
            self.engine.submit(self.run_id, self.step(path="protected.py"))
        self.assertEqual(target.read_text(), "original\n")

    def test_symlink_escape_rejected(self):
        task = load(self.task_path)
        task["scope"]["write_allow"].append("link.py")
        self.write_task(task)
        external = self.base / "external.py"
        external.write_text("untouched\n")
        (self.root / "link.py").symlink_to(external)
        self.start()
        with self.assertRaisesRegex(ContractError, "Symlink"):
            self.engine.submit(self.run_id, self.step(path="link.py"))
        self.assertEqual(external.read_text(), "untouched\n")

    def test_task_or_profile_amendments_require_explicit_new_run(self):
        self.start()
        task = load(self.task_path)
        task["objective"] = "New objective"
        self.write_task(task)
        with self.assertRaisesRegex(ContractError, "Task file changed"):
            self.engine.context(self.run_id)

    def test_profile_change_invalidates_frozen_policy(self):
        self.start()
        profile = load(self.profile_path)
        profile["conventions"].append("Changed convention")
        self.profile_path.write_text(json.dumps(profile))
        with self.assertRaisesRegex(ContractError, "Project profile changed"):
            self.engine.verify(self.run_id)

    def test_unknown_evidence_reference_rejected(self):
        self.start()
        step = self.step()
        step["evidence_refs"] = [byte_digest(b"agent invented a test result")]
        with self.assertRaisesRegex(ContractError, "did not collect"):
            self.engine.submit(self.run_id, step)

    def test_replaying_completed_waiting_step_rejected(self):
        self.start()
        step = self.step(intent="need_input")
        step["blocker"] = {"category": "input", "description": "Need a fixture", "resumption_condition": "Fixture supplied"}
        self.assertEqual(self.engine.submit(self.run_id, step)["state"]["status"], "AWAITING_INPUT")
        with self.assertRaisesRegex(ContractError, "already consumed"):
            self.engine.submit(self.run_id, step)

    def test_external_review_is_required_and_cannot_be_waived(self):
        task = load(self.task_path)
        task["checks"].append({"id": "review", "type": "review", "description": "Independent compatibility review",
                               "procedure": ["Inspect behavior"], "independent": True})
        self.write_task(task)
        self.start()
        result = self.engine.submit(self.run_id, self.step())
        self.assertEqual(result["state"]["status"], "BLOCKED")
        self.assertNotEqual(result["last_gate"]["outcome"], "PASS")

    def test_unavailable_check_is_a_dependency_blocker(self):
        task = load(self.task_path)
        task["checks"][0]["argv"] = ["no-such-loop-check-executable-123"]
        self.write_task(task)
        self.start()
        result = self.engine.verify(self.run_id)
        self.assertEqual(result["state"]["status"], "BLOCKED")
        self.assertEqual(result["last_recovery"]["category"], "dependency")

    def test_check_that_mutates_inputs_is_inconclusive(self):
        task = load(self.task_path)
        task["checks"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('slug.py').write_text('tampered')"]
        self.write_task(task)
        self.start()
        result = self.engine.verify(self.run_id)
        record = self.store.evidence(self.run_id, result["selected_evidence"])[0]
        self.assertEqual(record["result"], "inconclusive")
        self.assertTrue(record["extensions"]["input_mutation"])
        self.assertNotEqual((self.root / "slug.py").read_text(), "tampered")

    def test_artifact_tampering_detected_before_context_export(self):
        from urllib.parse import unquote, urlparse
        data = self.start(baseline=True)
        record = self.store.evidence(self.run_id, data["baseline"])[0]
        Path(unquote(urlparse(record["artifacts"][0]["uri"]).path)).write_text("invented pass")
        with self.assertRaisesRegex(ContractError, "integrity"):
            self.engine.context(self.run_id)

    def test_changed_candidate_before_final_checkpoint_cannot_succeed(self):
        self.start()
        original = self.engine.checkpoint

        def racing_checkpoint(data, status, reason, **kwargs):
            if status == "SUCCEEDED":
                (self.root / "slug.py").write_text("# user edited the candidate\n" + FIXED)
            return original(data, status, reason, **kwargs)

        with patch.object(self.engine, "checkpoint", side_effect=racing_checkpoint):
            result = self.engine.submit(self.run_id, self.step())
        self.assertEqual(result["state"]["status"], "PLANNING")
        self.assertEqual(result["selected_evidence"], [])
        self.assertEqual(result["state"]["progress"]["passed_criteria"], [])

    def test_environment_change_rejects_real_check_evidence(self):
        self.start()
        (self.root / "slug.py").write_text(FIXED)
        with patch.object(self.engine, "environment_digest", side_effect=[byte_digest(b"env1"), byte_digest(b"env2")]):
            result = self.engine.verify(self.run_id)
        self.assertNotEqual(result["state"]["status"], "SUCCEEDED")
        self.assertEqual(result["state"]["progress"]["passed_criteria"], [])

    def test_snapshot_and_blob_tampering_detected(self):
        data = self.start()
        snapshot = deepcopy(data["checkpoint_snapshot"])
        snapshot["manifest"]["files"].pop()
        with self.assertRaisesRegex(ContractError, "manifest"):
            self.engine.snapshots.materialize(snapshot, self.base / "restore")
        item = next(item for item in data["checkpoint_snapshot"]["manifest"]["files"] if item["path"] == "slug.py")
        self.engine.snapshots.blob(item["sha256"]).write_bytes(b"modified")
        with self.assertRaisesRegex(ContractError, "integrity"):
            self.engine.snapshots.read(item["sha256"])

    def test_context_respects_compact_encoded_byte_limit(self):
        profile = load(self.profile_path)
        profile["context_max_bytes"] = 5000
        self.profile_path.write_text(json.dumps(profile))
        (self.root / "lots.txt").write_text('"\\\n' * 10000)
        self.start()
        context = self.engine.context(self.run_id)
        encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), 5000)
        self.assertGreater(context["context_policy"]["omitted_source_count"], 0)
        self.assertEqual(context["task"], load(self.task_path))

    def test_tiny_context_limit_never_truncates_task(self):
        profile = load(self.profile_path)
        profile["context_max_bytes"] = 1
        self.profile_path.write_text(json.dumps(profile))
        self.start()
        with self.assertRaisesRegex(ContractError, "mandatory task"):
            self.engine.context(self.run_id)

    def test_excluded_inputs_cannot_be_edited(self):
        task = load(self.task_path)
        task["scope"]["write_allow"].append(".env")
        self.write_task(task)
        self.start()
        with self.assertRaisesRegex(ContractError, "excluded"):
            self.engine.submit(self.run_id, self.step(path=".env"))

    def test_total_snapshot_limit_is_checked_before_edit(self):
        profile = load(self.profile_path)
        profile["snapshot"]["max_total_bytes"] = 20000
        self.profile_path.write_text(json.dumps(profile))
        self.start()
        with self.assertRaisesRegex(ContractError, "total snapshot"):
            self.engine.submit(self.run_id, self.step(content=FIXED + "#" * 20000))
        self.assertNotEqual((self.root / "slug.py").read_text(), FIXED)


class BudgetRecoveryAndCliTests(LocalCase):
    def test_baseline_wall_budget_exhaustion_is_a_stopped_state(self):
        task = load(self.task_path)
        task["limits"]["max_wall_seconds"] = 1
        task["checks"][0]["argv"] = [sys.executable, "-c", "import time; time.sleep(10)"]
        self.write_task(task)
        data = self.start(baseline=True)
        self.assertEqual(data["state"]["status"], "BUDGET_EXHAUSTED")
        self.assertIsNone(data["pending_process"])

    def test_verification_reserve_stops_implementation_without_writing(self):
        task = load(self.task_path)
        task["limits"].update(max_wall_seconds=20, verification_reserve_seconds=20)
        self.write_task(task)
        self.start()
        result = self.engine.submit(self.run_id, self.step())
        self.assertEqual(result["state"]["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(result["state"]["iteration"], 0)

    def test_last_allowed_iteration_still_gets_verified(self):
        task = load(self.task_path)
        task["limits"]["max_iterations"] = 1
        self.write_task(task)
        self.start()
        self.assertEqual(self.engine.submit(self.run_id, self.step())["state"]["status"], "SUCCEEDED")

    def test_manual_verification_proposal_is_a_counted_attempt(self):
        self.start()
        data = self.engine.submit(self.run_id, self.step(intent="request_verification"))
        self.assertEqual(data["state"]["iteration"], 1)

    def test_unknown_spend_cap_is_not_silently_disabled(self):
        task = load(self.task_path)
        task["limits"]["max_tokens"] = 1000
        self.write_task(task)
        result = self.start()
        self.assertEqual(result["state"]["status"], "AWAITING_INPUT")
        self.assertIsNone(result["state"]["usage"]["tokens"])
        self.assertFalse(diagnose(self.root)["ok"])

    def test_unattended_and_unavailable_runtime_capabilities_rejected(self):
        task = load(self.task_path)
        task["autonomy"] = "unattended"
        self.write_task(task)
        with self.assertRaisesRegex(ContractError, "unattended"):
            self.start()
        task["autonomy"] = "assisted"
        task["required_capabilities"]["runtime"] = ["trusted_evidence"]
        self.write_task(task)
        with self.assertRaisesRegex(ContractError, "capabilities unavailable"):
            self.start()

    def test_repeat_root_failure_stops_even_with_different_step_ids(self):
        self.start()
        first = self.engine.submit(self.run_id, self.step(intent="request_verification"))
        self.assertEqual(first["state"]["status"], "PLANNING")
        second = self.engine.submit(self.run_id, self.step(intent="request_verification", step_id="step-2"))
        self.assertEqual(second["state"]["status"], "STALLED")
        self.assertEqual(second["state"]["repeated_failure_count"], 2)
        self.assertEqual(second["state"]["stalled_iterations"], 1)

    def test_resume_preserves_counters_and_charges_unknown_crash_gap(self):
        self.start()
        with self.store.writer(self.run_id) as data:
            data["state"]["iteration"] = 1
            data["elapsed_ms"] = 200
            data["operation_started_ms"] = time.time_ns() // 1000000 - 1200
            self.engine.transition(data, "EXECUTING")
        restored = Controller(Store(self.base / "state")).resume(self.run_id)
        self.assertEqual(restored["state"]["iteration"], 1)
        self.assertGreaterEqual(restored["elapsed_ms"], 1400)

    def crash_after_first_edit(self, *, max_iterations=None):
        task = load(self.task_path)
        task["scope"]["write_allow"].append("extra.py")
        if max_iterations is not None:
            task["limits"]["max_iterations"] = max_iterations
        self.write_task(task)
        (self.root / "extra.py").write_text("ORIGINAL = 1\n")
        self.start()
        step = self.step()
        step["changes"].append({"path": "extra.py", "expected_sha256": byte_digest(b"ORIGINAL = 1\n"),
                                "new_content": "RECONCILED = 2\n"})
        proposal = self.base / "proposal.json"
        proposal.write_text(json.dumps(step))
        code = """import os, sys
from pathlib import Path
from loop_engineering.controller import Controller
from loop_engineering.store import Store
from loop_engineering.contracts import load
engine = Controller(Store(Path(sys.argv[1])))
original = engine.snapshots.apply_prepared
def partial(root, task, changes):
    original(root, task, changes[:1])
    os._exit(86)
engine.snapshots.apply_prepared = partial
engine.submit(sys.argv[2], load(Path(sys.argv[3]), 'step'))
"""
        process = subprocess.run([sys.executable, "-c", code, str(self.store.directory), self.run_id, str(proposal)],
                                 cwd=ROOT, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 86, process.stderr.decode())
        self.assertEqual((self.root / "slug.py").read_text(), FIXED)
        self.assertEqual((self.root / "extra.py").read_text(), "ORIGINAL = 1\n")

    def test_real_controller_crash_rolls_forward_journal_once(self):
        self.crash_after_first_edit()
        resumed = Controller(Store(self.base / "state")).resume(self.run_id)
        self.assertEqual(resumed["state"]["status"], "PLANNING")
        self.assertEqual(resumed["state"]["iteration"], 1)
        self.assertIsNone(resumed["pending_edit"])
        self.assertEqual((self.root / "extra.py").read_text(), "RECONCILED = 2\n")
        self.assertEqual(self.engine.verify(self.run_id)["state"]["status"], "SUCCEEDED")

    def test_crash_on_final_iteration_can_resume_for_verification(self):
        self.crash_after_first_edit(max_iterations=1)
        resumed = self.engine.resume(self.run_id)
        self.assertEqual(resumed["state"]["iteration"], 1)
        self.assertEqual(resumed["state"]["status"], "PLANNING")
        self.assertEqual(self.engine.verify(self.run_id)["state"]["status"], "SUCCEEDED")

    def test_recovery_never_overwrites_intervening_user_change(self):
        self.crash_after_first_edit()
        (self.root / "extra.py").write_text("USER_CHANGE = 9\n")
        with self.assertRaisesRegex(ContractError, "user change"):
            self.engine.resume(self.run_id)
        self.assertEqual((self.root / "extra.py").read_text(), "USER_CHANGE = 9\n")
        self.assertEqual(self.store.get(self.run_id)["state"]["status"], "AWAITING_INPUT")

    def test_unknown_dispatch_requires_specific_operator_reconciliation(self):
        self.start()
        with self.store.writer(self.run_id) as data:
            data["pending_process"] = {"action_id": "process-interrupted", "kind": "agent", "pid": None, "identity": None}
            data["state"]["outstanding_action_ids"] = ["process-interrupted"]
            self.engine.transition(data, "EXECUTING")
        with self.assertRaisesRegex(ContractError, "interrupted dispatch"):
            self.engine.resume(self.run_id)
        with self.assertRaises(ContractError):
            self.engine.reconcile_process(self.run_id, "wrong-action", "Inspected")
        self.engine.reconcile_process(self.run_id, "process-interrupted", "Operator confirmed no child was launched")
        result = self.engine.resume(self.run_id)
        self.assertEqual(result["state"]["status"], "PLANNING")
        self.assertFalse(result["usage_complete"])
        self.assertIsNone(result["state"]["usage"]["tokens"])

    def test_live_process_with_unobservable_identity_is_not_replayed_or_killed(self):
        self.start()
        with self.store.writer(self.run_id) as data:
            data["pending_process"] = {"action_id": "process-interrupted", "kind": "check", "pid": os.getpid(), "identity": None}
            data["state"]["outstanding_action_ids"] = ["process-interrupted"]
            self.engine.transition(data, "EXECUTING")
        with patch("loop_engineering.controller.process_identity", return_value=None), patch("loop_engineering.controller.terminate_group") as terminate:
            with self.assertRaisesRegex(ContractError, "interrupted process"):
                self.engine.resume(self.run_id)
            terminate.assert_not_called()

    def test_writer_lock_and_lease_fence_old_controllers(self):
        self.start()
        stale = self.store.get(self.run_id)
        with self.store.writer(self.run_id):
            with self.assertRaisesRegex(ContractError, "already operating"):
                with Store(self.base / "state").writer(self.run_id):
                    pass
        with self.assertRaisesRegex(ContractError, "newer controller"):
            self.store.save(stale, "stale-write")

    def test_workspace_lock_blocks_a_second_run_in_same_store(self):
        self.start()
        other = self.engine.start(self.root, self.task_path, self.profile_path, baseline=False)
        with self.store.writer(self.run_id):
            with self.assertRaisesRegex(ContractError, "workspace"):
                with self.store.writer(other["run_id"]):
                    pass

    def test_cancellation_wins_and_cannot_reset_via_resume(self):
        self.start()
        self.store.cancel(self.run_id)
        data = self.engine.submit(self.run_id, self.step())
        self.assertEqual(data["state"]["status"], "CANCELLED")
        with self.assertRaisesRegex(ContractError, "Terminal run"):
            self.engine.resume(self.run_id)

    def test_cancellation_during_a_real_check_is_persisted(self):
        ready = self.base / "check-ready"
        task = load(self.task_path)
        task["checks"][0]["argv"] = [sys.executable, "-c", f"import time; from pathlib import Path; Path({str(ready)!r}).write_text('ready'); time.sleep(10)"]
        task["checks"][0]["timeout_seconds"] = 15
        self.write_task(task)
        self.start()
        code = "from pathlib import Path; import sys; from loop_engineering.controller import Controller; from loop_engineering.store import Store; e=Controller(Store(Path(sys.argv[1]))); print(e.verify(sys.argv[2])['state']['status'])"
        process = subprocess.Popen([sys.executable, "-c", code, str(self.store.directory), self.run_id],
                                   cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            pending = self.store.get(self.run_id)["pending_process"]
            self.assertIsNotNone(pending)
            self.store.cancel(self.run_id)
            output, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error.decode())
            self.assertEqual(output.decode().strip(), "CANCELLED")
            self.assertEqual(self.store.get(self.run_id)["state"]["status"], "CANCELLED")
        finally:
            if process.poll() is None:
                self.store.cancel(self.run_id)
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

    def test_cli_export_step_status_and_checkpoint_restore(self):
        code, data, error = self.cli(["start", str(self.root), "--state-dir", str(self.store.directory), "--no-baseline"])
        self.assertEqual(code, 0, error)
        self.run_id = data["run_id"]
        code, context, error = self.cli(["context", self.run_id, "--state-dir", str(self.store.directory)])
        self.assertEqual(code, 0, error)
        self.assertEqual(context["task"]["task_id"], data["state"]["task_id"])
        proposal = self.base / "step.json"
        proposal.write_text(json.dumps(self.step()))
        code, data, error = self.cli(["step", self.run_id, "--file", str(proposal), "--state-dir", str(self.store.directory)])
        self.assertEqual(code, 0, error)
        self.assertEqual(data["state"]["status"], "SUCCEEDED")
        restored = self.base / "restored"
        code, data, error = self.cli(["restore", self.run_id, "--target", str(restored), "--state-dir", str(self.store.directory)])
        self.assertEqual(code, 0, error)
        self.assertEqual((restored / "slug.py").read_text(), FIXED)
        self.assertFalse((restored / ".loop").exists())
        code, data, error = self.cli(["status", self.run_id, "--events", "--state-dir", str(self.store.directory)])
        self.assertEqual(code, 0, error)
        self.assertTrue(data["workspace_matches_checkpoint"])
        self.assertEqual([item["sequence"] for item in data["events"]], list(range(1, len(data["events"]) + 1)))
        code, listed, error = self.cli(["runs", "--state-dir", str(self.store.directory)])
        self.assertEqual(code, 0, error)
        self.assertEqual(listed["runs"][0]["run_id"], self.run_id)

    def test_state_store_cannot_live_inside_implementer_project(self):
        code, data, error = self.cli(["start", str(self.root), "--state-dir", str(self.root / ".loop/state")])
        self.assertEqual(code, 2)
        self.assertIn("outside", error)
        self.assertFalse((self.root / ".loop/state").exists())


class CommandBridgeTests(LocalCase):
    def driver(self, code):
        return CommandDriver([sys.executable, "-c", code])

    def test_command_bridge_completes_actual_bug_with_one_counted_attempt(self):
        task = load(self.task_path)
        task["limits"]["max_iterations"] = 1
        self.write_task(task)
        self.start()
        code = """import json, sys
c = json.load(sys.stdin)
source = next(item for item in c['sources'] if item['path'] == 'slug.py')
step = {'schema_version':'0.2','step_id':'fixture-1','task_id':c['task']['task_id'],
        'contract_digest':c['contract_digest'],'base_snapshot_digest':c['base_snapshot_digest'],
        'intent':'act','criterion_ids':[item['id'] for item in c['task']['criteria']],
        'summary':'Fixture proposal','expected_observation':'Both real checks pass',
        'changes':[{'path':'slug.py','expected_sha256':source['sha256'],'new_content':FIXED}],
        'evidence_refs':[],'blocker':None,'next_action':None}
print(json.dumps(step))
""".replace("FIXED", repr(FIXED))
        result = drive(self.engine, self.run_id, self.driver(code), turns=1)
        self.assertEqual(result["state"]["status"], "SUCCEEDED")
        self.assertEqual(result["state"]["iteration"], 1)
        self.assertIsNone(result["state"]["usage"]["tokens"])
        self.assertFalse(result["usage_complete"])

    def test_invalid_output_gets_one_repair_attempt_across_restart(self):
        self.start()
        driver = self.driver("print('not-json')")
        first = drive(self.engine, self.run_id, driver)
        self.assertEqual(first["state"]["status"], "PLANNING")
        restored = Controller(Store(self.base / "state"))
        second = drive(restored, self.run_id, driver)
        self.assertEqual(second["state"]["status"], "BLOCKED")
        self.assertEqual(second["adapter_failure"]["count"], 2)
        self.assertEqual(second["state"]["iteration"], 2)

    def test_transient_failures_retry_at_most_once(self):
        self.start()
        result = drive(self.engine, self.run_id, self.driver("raise SystemExit(3)"), turns=3)
        self.assertEqual(result["state"]["status"], "BLOCKED")
        self.assertEqual(result["last_recovery"]["category"], "transient")
        self.assertEqual(result["state"]["iteration"], 2)

    def test_authentication_failure_waits_without_retry(self):
        self.start()
        driver = self.driver("import sys; print('authentication required: login', file=sys.stderr); sys.exit(1)")
        result = drive(self.engine, self.run_id, driver, turns=3)
        self.assertEqual(result["state"]["status"], "AWAITING_INPUT")
        self.assertEqual(result["state"]["iteration"], 1)

    def test_exhausted_attempts_after_invalid_output_stop_immediately(self):
        task = load(self.task_path)
        task["limits"]["max_iterations"] = 1
        self.write_task(task)
        self.start()
        result = drive(self.engine, self.run_id, self.driver("print('not-json')"))
        self.assertEqual(result["state"]["status"], "BUDGET_EXHAUSTED")

    def test_missing_agent_capability_cannot_be_waived(self):
        task = load(self.task_path)
        task["required_capabilities"]["agent"] = ["text_input", "vision"]
        self.write_task(task)
        self.start()
        with self.assertRaisesRegex(ContractError, "vision"):
            drive(self.engine, self.run_id, self.driver("print('{}')"))
        with self.assertRaisesRegex(ContractError, "vision"):
            self.engine.submit(self.run_id, self.step())
        self.assertEqual(self.store.get(self.run_id)["state"]["iteration"], 0)

    def test_declared_usage_capability_missing_in_actual_result_blocks(self):
        class UsageDriver(CommandDriver):
            capabilities = {"text_input": True, "headless_execution": True, "usage_reporting": True}

        task = load(self.task_path)
        task["required_capabilities"]["agent"].append("usage_reporting")
        self.write_task(task)
        self.start()
        driver = UsageDriver([sys.executable, "-c", "print('{}')"])
        data = drive(self.engine, self.run_id, driver)
        self.assertEqual(data["state"]["status"], "BLOCKED")
        self.assertIn("usage_reporting", data["state"]["reason"])
        self.assertIsNone(data["state"]["usage"]["tokens"])

    def test_known_later_usage_cannot_erase_unknown_manual_proposal_usage(self):
        class KnownDriver(CommandDriver):
            def tokens(self, result):
                return 7

        self.start()
        self.engine.submit(self.run_id, self.step(intent="request_verification"))
        data = drive(self.engine, self.run_id, KnownDriver([sys.executable, "-c", "print('{}')"]))
        self.assertEqual(data["known_tokens"], 7)
        self.assertFalse(data["usage_complete"])
        self.assertIsNone(data["state"]["usage"]["tokens"])

    def test_invalid_argv_is_not_interpreted_as_shell_code(self):
        for argv in ("echo hello", [], ["echo", 1], {"cmd": "echo"}, ["echo\0"]):
            with self.subTest(argv=argv), self.assertRaises(ContractError):
                CommandDriver(argv)

    def test_codex_driver_constructs_proposal_only_command(self):
        artifacts = self.base / "adapter"
        artifacts.mkdir()
        driver = CodexDriver(executable=sys.executable)
        argv = driver.command(self.root, artifacts)
        self.assertIn("read-only", argv)
        self.assertIn("--ephemeral", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--output-schema", argv)
        self.assertNotIn("--ignore-rules", argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[-1], "-")
        self.assertTrue((artifacts / "step.schema.json").is_file())

    def test_codex_usage_includes_input_output_and_preserves_unknown(self):
        output, error = self.base / "stdout", self.base / "stderr"
        output.write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 8}}) + "\n")
        error.write_text("")
        result = ProcessResult(0, "completed", 10, output, error)
        driver = CodexDriver(executable=sys.executable)
        self.assertEqual(driver.tokens(result), 14)
        output.write_text('{"type":"turn.failed"}\n')
        self.assertIsNone(driver.tokens(result))

    def test_partial_codex_usage_retains_known_subtotal(self):
        output, error = self.base / "stdout", self.base / "stderr"
        output.write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}})
                          + '\n{"type":"turn.completed","usage":{}}\n')
        error.write_text("")
        result = ProcessResult(0, "completed", 10, output, error)
        report = CodexDriver(executable=sys.executable).token_report(result)
        self.assertIsNone(report["tokens"])
        self.assertEqual(report["known_tokens"], 14)


if __name__ == "__main__":
    unittest.main()
