"""Native engine conformance without live provider calls or Docker execution."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_engine import LocalCase, FIXED
from reference.core import canonical_digest
from loop_engineering.authority import Authorities
from loop_engineering.broker import Broker
from loop_engineering.budgets import BudgetError, Ledger
from loop_engineering.contracts import ContractError, ROOT, load, validate
from loop_engineering.evaluators import sign_result
from loop_engineering.models import ModelDriver, ModelError
from loop_engineering.native_contracts import validate_context, validate_native
from loop_engineering.native_engine import NativeController
from loop_engineering.runtime import DockerRuntime
from loop_engineering.stages import active_stage, statuses, validate_stages
from loop_engineering.store import Store
from loop_engineering.workspace import byte_digest


class FixtureTransport:
    def __init__(self, engine, run_id, *, provider="openai", content=FIXED):
        self.engine, self.run_id, self.provider, self.content = engine, run_id, provider, content
        self.calls, self.responses = [], []
        self.input_tokens, self.output_tokens = 100, 20

    def post(self, operation, payload, **limits):
        self.calls.append((operation, deepcopy(payload), limits))
        if operation == "count":
            return {"input_tokens": self.input_tokens}
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        context = json.loads(payload["input"] if self.provider == "openai" else payload["messages"][0]["content"])
        bundle = context["bundle"]
        source = next(s for s in bundle["sources"] if s["path"] == "slug.py")
        stage = context["active_stage"]
        step = {"schema_version": "0.2", "step_id": "fixture-" + str(len(self.calls)),
                "task_id": bundle["task"]["task_id"], "contract_digest": context["contract_digest"],
                "base_snapshot_digest": context["base_snapshot_digest"], "intent": "act",
                "criterion_ids": stage["criterion_ids"] if stage else [c["id"] for c in bundle["task"]["criteria"]],
                "summary": "Scripted native protocol fixture", "expected_observation": "Required checks pass",
                "changes": [{"path": "slug.py", "expected_sha256": source["sha256"], "new_content": self.content}],
                "evidence_refs": [], "blocker": None, "next_action": None}
        usage = {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}
        text = json.dumps(step)
        if self.provider == "openai":
            return {"status": "completed", "usage": usage, "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        return {"stop_reason": "end_turn", "usage": usage, "content": [{"type": "text", "text": text}]}


class NativeCase(LocalCase):
    def setUp(self):
        super().setUp()
        self.config = load(ROOT / "templates/engine.json")
        self.config["model"]["model"] = "fixture-model"
        self.config["model"]["pricing"] = {"price_id": "fixture-only", "currency": "USD",
                "input_microunits_per_million": 1000000, "output_microunits_per_million": 2000000}
        self.config["response_limits"]["max_output_tokens"] = 128
        self.engine_path = self.root / ".loop/engine.json"
        self.configure()

    def configure(self):
        self.engine_path.write_text(json.dumps(self.config))
        self.engine = NativeController(self.store, self.engine_path)

    def step(self, *, content=FIXED, intent="act", path="slug.py", step_id="native-step"):
        context = self.engine.context(self.run_id)
        target = self.root / path
        return {"schema_version": "0.2", "step_id": step_id, "task_id": context["bundle"]["task"]["task_id"],
                "contract_digest": context["contract_digest"], "base_snapshot_digest": context["base_snapshot_digest"],
                "intent": intent, "criterion_ids": [c["id"] for c in context["bundle"]["task"]["criteria"]],
                "summary": "Manual native fixture", "expected_observation": "Checks pass",
                "changes": [{"path": path, "expected_sha256": byte_digest(target.read_bytes()) if target.exists() else None,
                             "new_content": content}] if intent == "act" else [], "evidence_refs": [], "blocker": None, "next_action": None}


class NativeWorkflowTests(NativeCase):
    def test_direct_native_fixture_fixes_real_bug_with_reserved_usage(self):
        self.start(baseline=True)
        transport = FixtureTransport(self.engine, self.run_id)
        data = self.engine.drive(self.run_id, transport=transport)
        self.assertEqual(data["state"]["status"], "SUCCEEDED")
        self.assertEqual(data["state"]["iteration"], 1)
        self.assertEqual(data["state"]["usage"]["tokens"], 120)
        self.assertEqual(data["state"]["usage"]["cost_microunits"], 140)
        self.assertEqual([c[0] for c in transport.calls], ["count", "respond"])
        self.assertEqual(self.store.replay(self.run_id), self.store.get(self.run_id))

    def test_hard_caps_reserve_before_response_and_allow_final_verification(self):
        task = load(self.task_path)
        task["limits"].update(max_tokens=228, max_cost_microunits=356)
        self.write_task(task); self.start()
        result = self.engine.drive(self.run_id, transport=FixtureTransport(self.engine, self.run_id))
        self.assertEqual(result["state"]["status"], "SUCCEEDED")
        self.assertEqual(result["native"]["reservations"][0]["status"], "settled")

    def test_insufficient_budget_never_dispatches_model_response(self):
        task = load(self.task_path); task["limits"]["max_tokens"] = 227
        self.write_task(task); self.start()
        fixture = FixtureTransport(self.engine, self.run_id)
        result = self.engine.drive(self.run_id, transport=fixture)
        self.assertEqual(result["state"]["status"], "BUDGET_EXHAUSTED")
        self.assertEqual([c[0] for c in fixture.calls], ["count"])
        self.assertEqual(result["native"]["reservations"], [])

    def test_final_review_reserve_is_kept_before_dispatch(self):
        self.config["response_limits"]["final_reserve_tokens"] = 100
        self.configure()
        task = load(self.task_path); task["limits"]["max_tokens"] = 300
        self.write_task(task); self.start()
        fixture = FixtureTransport(self.engine, self.run_id)
        result = self.engine.drive(self.run_id, transport=fixture)
        self.assertEqual(result["state"]["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(len(fixture.calls), 1)

    def test_ambiguous_response_keeps_reservation_across_restart(self):
        self.start()
        fixture = FixtureTransport(self.engine, self.run_id)
        fixture.responses = [ModelError("transient", "network disconnected after dispatch")]
        data = self.engine.drive(self.run_id, transport=fixture)
        self.assertEqual(data["native"]["reservations"][0]["status"], "unknown")
        self.assertIsNone(data["state"]["usage"]["tokens"])
        restored = NativeController(self.store, self.engine_path)
        restored.resume(self.run_id)
        balance = Ledger(self.store.get(self.run_id)["native"]["reservations"]).balance()
        self.assertEqual(balance["tokens_upper"], 228)
        self.assertIsNone(balance["actual_tokens"])

    def test_invalid_output_gets_one_repair_across_invocations(self):
        self.start()
        fixture = FixtureTransport(self.engine, self.run_id)
        fixture.responses = [{"status": "completed", "usage": {"input_tokens": 100, "output_tokens": 20}, "output": []}] * 2
        self.assertEqual(self.engine.drive(self.run_id, transport=fixture)["state"]["status"], "PLANNING")
        self.assertEqual(self.engine.drive(self.run_id, transport=fixture)["state"]["status"], "BLOCKED")
        self.assertEqual(self.store.get(self.run_id)["state"]["iteration"], 2)

    def test_authentication_failure_waits_without_response_retry(self):
        self.start()
        fixture = FixtureTransport(self.engine, self.run_id)
        fixture.responses = [ModelError("authentication", "credential expired")]
        data = self.engine.drive(self.run_id, turns=3, transport=fixture)
        self.assertEqual(data["state"]["status"], "AWAITING_INPUT")
        self.assertEqual(len(fixture.calls), 2)

    def test_actual_usage_violating_quote_prevents_acceptance(self):
        self.start()
        fixture = FixtureTransport(self.engine, self.run_id)
        fixture.output_tokens = 129
        data = self.engine.drive(self.run_id, transport=fixture)
        self.assertEqual(data["state"]["status"], "AWAITING_INPUT")
        self.assertTrue(Ledger(data["native"]["reservations"]).balance()["violated_bound"])
        self.assertNotEqual((self.root / "slug.py").read_text(), FIXED)

    def test_model_switch_preserves_unknown_spend_and_iteration(self):
        self.start()
        fixture = FixtureTransport(self.engine, self.run_id)
        fixture.responses = [ModelError("transient", "ambiguous")]
        self.engine.drive(self.run_id, transport=fixture)
        model = deepcopy(self.config["model"]); model.update(provider="anthropic", model="fixture-second-provider", api_key_env="ANTHROPIC_API_KEY")
        self.engine.switch_model(self.run_id, model)
        result = self.engine.drive(self.run_id, transport=FixtureTransport(self.engine, self.run_id, provider="anthropic"))
        self.assertEqual(result["state"]["status"], "SUCCEEDED")
        self.assertEqual(result["state"]["iteration"], 2)
        self.assertIsNone(result["state"]["usage"]["tokens"])
        self.assertEqual(result["known_tokens"], 120)

    def test_model_switch_cannot_drop_a_required_capability(self):
        task = load(self.task_path)
        task["required_capabilities"]["agent"].append("structured_output")
        self.write_task(task); self.start()
        model = deepcopy(self.config["model"]); model["provider"] = "anthropic"
        with self.assertRaisesRegex(ContractError, "required agent capability"):
            self.engine.switch_model(self.run_id, model)
        self.assertEqual(self.store.get(self.run_id)["native"]["current_model"]["provider"], "openai")

    def test_estimated_provider_count_cannot_satisfy_hard_cap(self):
        self.config["model"]["provider"] = "anthropic"; self.configure()
        task = load(self.task_path); task["limits"]["max_tokens"] = 1000
        self.write_task(task); self.start()
        fixture = FixtureTransport(self.engine, self.run_id, provider="anthropic")
        data = self.engine.drive(self.run_id, transport=fixture)
        self.assertEqual(data["state"]["status"], "AWAITING_INPUT")
        self.assertEqual(len(fixture.calls), 1)

    def test_unmanaged_proposals_cannot_bypass_native_hard_cap(self):
        task = load(self.task_path); task["limits"]["max_tokens"] = 1000
        self.write_task(task); self.start()
        with self.assertRaises(ContractError):
            self.engine.submit(self.run_id, self.step())

    def test_required_repository_instructions_survive_context_trimming(self):
        (self.root / "AGENTS.md").write_text("Mandatory project convention: retain compatibility.\n")
        self.start()
        context = self.engine.context(self.run_id)
        validate_native("context", context)
        self.assertIn("AGENTS.md", [s["path"] for s in context["repository_instructions"]])
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode()), load(self.profile_path)["context_max_bytes"])

    def test_context_validation_rejects_mixed_task_or_source_identity(self):
        self.start()
        context = self.engine.context(self.run_id)
        for mutate in (lambda c: c["bundle"]["state"].update(contract_digest="sha256:" + "a" * 64),
                       lambda c: c["bundle"]["sources"][0].update(content="changed"),
                       lambda c: c["bundle"].update(authority="model-written")):
            changed = deepcopy(context); mutate(changed)
            with self.assertRaises(ContractError):
                validate_context(changed)

    def test_config_amendment_cannot_silently_change_runtime(self):
        self.start()
        self.config["runtime"]["memory_mb"] = 1024
        self.engine_path.write_text(json.dumps(self.config))
        with self.assertRaises(ContractError):
            self.engine.context(self.run_id)

    def test_pause_resume_and_revision_preserve_cumulative_counters(self):
        self.start()
        self.engine.pause(self.run_id, "Change approved budget")
        old = self.store.get(self.run_id)
        self.assertEqual(old["state"]["status"], "PAUSED")
        task = load(self.task_path); task["revision"] += 1; task["limits"]["max_iterations"] += 2
        amended_path = self.root / "task-revision.json"; amended_path.write_text(json.dumps(task))
        data = self.engine.amend(self.run_id, amended_path, "Operator increases attempts")
        self.assertEqual(data["state"]["iteration"], old["state"]["iteration"])
        self.assertGreaterEqual(data["elapsed_ms"], old["elapsed_ms"])
        self.assertEqual(data["native"]["contract_history"][0]["revision"], 1)
        self.assertEqual(data["state"]["status"], "PLANNING")

    def test_amendment_cannot_weaken_a_frozen_profile(self):
        self.start(); self.engine.pause(self.run_id, "Review amendment")
        task = load(self.task_path); task["revision"] += 1
        revised = self.base / "revision.json"; revised.write_text(json.dumps(task))
        profile = load(self.profile_path); profile["snapshot"]["exclude"].append("tests/**")
        self.profile_path.write_text(json.dumps(profile))
        with self.assertRaisesRegex(ContractError, "frozen profile"):
            self.engine.amend(self.run_id, revised, "Approved task only")
        self.assertEqual(self.store.get(self.run_id)["task"]["revision"], 1)

    def test_pause_is_recoverable_but_cancellation_wins(self):
        self.start()
        self.engine.pause(self.run_id, "Operator pause")
        self.assertEqual(self.engine.resume(self.run_id)["state"]["status"], "PLANNING")
        self.store.cancel(self.run_id)
        self.engine.pause(self.run_id, "late pause")
        self.assertEqual(self.engine.resume(self.run_id)["state"]["status"], "CANCELLED")

    def test_projection_tampering_is_detected_and_rebuild_is_lossless(self):
        self.start()
        expected = self.store.replay(self.run_id)
        with self.store.connect() as connection:
            changed = deepcopy(expected); changed["state"]["iteration"] = 0; changed["state"]["status"] = "SUCCEEDED"
            connection.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(changed), self.run_id))
        with self.assertRaises(ContractError):
            self.store.get(self.run_id)
        restored = self.store.rebuild(self.run_id)
        self.assertEqual(restored["state"]["iteration"], expected["state"]["iteration"])
        self.assertEqual(restored["state"]["status"], expected["state"]["status"])
        self.assertGreater(restored["state"]["lease_token"], expected["state"]["lease_token"])
        self.assertEqual(self.store.get(self.run_id), restored)

    def test_event_deletion_or_conflicting_replay_is_rejected(self):
        self.start()
        with self.store.connect() as connection:
            connection.execute("DELETE FROM events WHERE run_id=? AND sequence=2", (self.run_id,))
        with self.assertRaises(ContractError):
            self.store.replay(self.run_id)

    def test_cli_native_init_start_context_pause_and_audit(self):
        empty = self.base / "empty"; empty.mkdir()
        code, result, _ = self.cli(["init", str(empty), "--native"])
        self.assertEqual(code, 0); self.assertTrue((empty / ".loop/engine.json").exists())
        self.start()
        shared = [self.run_id, "--state-dir", str(self.store.directory)]
        self.assertEqual(self.cli(["context"] + shared)[0], 0)
        self.assertEqual(self.cli(["pause"] + shared)[1]["state"]["status"], "PAUSED")
        self.assertTrue(self.cli(["audit"] + shared)[1]["authenticated"])


class NativeFaultTests(NativeCase):
    def test_failed_acceptance_transaction_does_not_leave_an_unsigned_run(self):
        with patch.object(self.store.authorities, "sign", side_effect=OSError("Interrupted authority write")):
            with self.assertRaises(OSError):
                self.start()
        self.assertEqual(self.store.runs(), [])

    def test_process_crash_after_reservation_preserves_spend_and_attempt(self):
        self.start()
        script = f'''import os
from pathlib import Path
from loop_engineering.native_engine import NativeController
from loop_engineering.store import Store
class CrashTransport:
    def post(self, operation, payload, **limits):
        if operation == "count": return {{"input_tokens": 100}}
        os._exit(91)
engine = NativeController(Store(Path({str(self.store.directory)!r})), Path({str(self.engine_path)!r}))
engine.drive({self.run_id!r}, transport=CrashTransport())
'''
        process = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 91, process.stderr.decode())
        interrupted = self.store.get(self.run_id)
        self.assertEqual(interrupted["native"]["reservations"][0]["status"], "held")
        self.assertIsNotNone(interrupted["operation_started_ms"])
        recovered = NativeController(self.store, self.engine_path).resume(self.run_id)
        self.assertEqual(recovered["state"]["iteration"], 1)
        self.assertEqual(recovered["native"]["reservations"][0]["status"], "unknown")
        self.assertEqual(Ledger(recovered["native"]["reservations"]).balance()["tokens_upper"], 228)
        self.assertEqual(recovered["state"]["status"], "PLANNING")
        self.assertNotEqual((self.root / "slug.py").read_text(), FIXED)

    def test_cross_store_workspace_lock_leaves_new_run_recoverable(self):
        self.start()
        other_store = Store(self.base / "other-state")
        other = NativeController(other_store, self.engine_path)
        with self.store.writer(self.run_id):
            with self.assertRaisesRegex(ContractError, "Another run"):
                other.start(self.root, self.task_path, self.profile_path, baseline=False)
        interrupted = other_store.runs()[0]
        self.assertEqual(interrupted["status"], "NEW")
        recovered = other.resume(interrupted["run_id"])
        self.assertEqual(recovered["state"]["status"], "PLANNING")

    def test_pause_during_a_real_check_stops_process_and_allows_resume(self):
        task = load(self.task_path)
        task["checks"][0]["argv"] = [sys.executable, "-c", "import time; time.sleep(20)"]
        task["checks"][0]["timeout_seconds"] = 25
        self.write_task(task); self.start()
        results, failures = [], []
        def verify():
            try:
                results.append(self.engine.verify(self.run_id))
            except BaseException as exc:
                failures.append(exc)
        worker = threading.Thread(target=verify)
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = self.store.get(self.run_id).get("pending_process")
            if pending and pending.get("pid"):
                break
            time.sleep(.02)
        else:
            self.store.pause(self.run_id, "Test cleanup")
            worker.join(timeout=5)
            self.fail("Check did not start within the test deadline")
        self.engine.pause(self.run_id, "Operator interrupts a slow check")
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results[0]["state"]["status"], "PAUSED")
        self.assertIsNone(results[0]["pending_process"])
        self.assertEqual(self.engine.resume(self.run_id)["state"]["status"], "PLANNING")

    def test_missing_required_usage_retains_hard_cap_reservation(self):
        task = load(self.task_path)
        task["required_capabilities"]["agent"].append("usage_reporting")
        task["limits"]["max_tokens"] = 500
        self.write_task(task); self.start()
        fixture = FixtureTransport(self.engine, self.run_id)
        fixture.responses = [{"status": "completed", "output": []}]
        result = self.engine.drive(self.run_id, transport=fixture)
        self.assertEqual(result["state"]["status"], "BLOCKED")
        balance = Ledger(result["native"]["reservations"]).balance()
        self.assertIsNone(balance["actual_tokens"])
        self.assertEqual(balance["tokens_upper"], 228)

    def test_excluded_and_nested_repository_instructions_are_mandatory(self):
        (self.root / "AGENTS.md").write_text("Root rules")
        (self.root / "src").mkdir()
        (self.root / "src/item.py").write_text("value = 1\n")
        (self.root / "src/AGENTS.md").write_text("Nested rules")
        profile = load(self.profile_path)
        profile["snapshot"]["exclude"].extend(["AGENTS.md", "src/AGENTS.md"])
        self.profile_path.write_text(json.dumps(profile)); self.start()
        context = self.engine.context(self.run_id)
        self.assertTrue({"AGENTS.md", "src/AGENTS.md"}.issubset({i["path"] for i in context["repository_instructions"]}))
        self.assertNotIn("AGENTS.md", {i["path"] for i in context["bundle"]["sources"]})

    def test_oversized_mandatory_instructions_fail_instead_of_truncating(self):
        (self.root / "AGENTS.md").write_text("Mandatory convention.\n" * 4000)
        self.start()
        with self.assertRaisesRegex(ContractError, "Mandatory"):
            self.engine.context(self.run_id)

    def test_instruction_change_during_dispatch_prevents_file_effects(self):
        (self.root / "AGENTS.md").write_text("Original convention")
        profile = load(self.profile_path); profile["snapshot"]["exclude"].append("AGENTS.md")
        self.profile_path.write_text(json.dumps(profile)); self.start()
        fixture = FixtureTransport(self.engine, self.run_id)
        original = fixture.post
        def changed(operation, payload, **limits):
            response = original(operation, payload, **limits)
            if operation == "respond":
                (self.root / "AGENTS.md").write_text("Updated convention")
            return response
        fixture.post = changed
        result = self.engine.drive(self.run_id, transport=fixture)
        self.assertEqual(result["state"]["status"], "PLANNING")
        self.assertIn("instructions changed", result["state"]["reason"])
        self.assertNotEqual((self.root / "slug.py").read_text(), FIXED)

    def test_malformed_provider_items_are_bounded_repairable_failures(self):
        self.start()
        fixture = FixtureTransport(self.engine, self.run_id)
        fixture.responses = [{"status": "completed", "usage": {"input_tokens": 100, "output_tokens": 20}, "output": ["invalid"]}]
        result = self.engine.drive(self.run_id, transport=fixture)
        self.assertEqual(result["state"]["status"], "PLANNING")
        self.assertEqual(result["native"]["reservations"][0]["status"], "settled")
        self.assertNotEqual((self.root / "slug.py").read_text(), FIXED)

    def test_operator_note_cannot_waive_unknown_container_effect(self):
        self.start()
        with self.store.writer(self.run_id) as data:
            data["pending_process"] = {"action_id": "container-action", "kind": "container", "pid": None,
                                       "identity": None, "container": "loop-test", "owner": "expected-owner"}
            data["state"]["outstanding_action_ids"] = ["container-action"]
            self.store.save(data, "fixture.container_unknown")
        self.engine.runtime = DockerRuntime(self.config["runtime"])
        with patch.object(self.engine.runtime, "reconcile", side_effect=PermissionError("Daemon unavailable")):
            with self.assertRaises(PermissionError):
                self.engine.reconcile_process(self.run_id, "container-action", "Inspected host")
        self.assertIsNotNone(self.store.get(self.run_id)["pending_process"])
        with self.assertRaisesRegex(ContractError, "dispatch"):
            self.engine.switch_model(self.run_id, deepcopy(self.config["model"]))


class EvaluatorTests(NativeCase):
    def setup_review(self, kind="review"):
        self.store.authorities.create("test-evaluator", {"review": "reviewer", "human": "human", "interaction": "interaction", "artifact": "artifact"}[kind])
        task = load(self.task_path)
        task["checks"].append({"id": "external", "type": kind, "description": "Required external procedure",
                              "procedure": ["Inspect compatibility on the requested immutable candidate"],
                              **({"independent": True} if kind == "review" else {})})
        self.write_task(task)
        self.config["evaluator_keys"] = [{"key_id": "test-evaluator", "role": self.store.authorities.key("test-evaluator")["role"], "check_ids": ["external"]}]
        self.configure(); self.start()
        self.engine.drive(self.run_id, transport=FixtureTransport(self.engine, self.run_id))
        self.request = self.engine.evaluation_request(self.run_id, "external")
        self.artifact = self.base / "evaluation.txt"; self.artifact.write_text("Offline evaluator fixture observations\n")

    def signed(self, *, outcome="pass", findings=None):
        return sign_result(self.store.authorities, self.request, "test-evaluator", result=outcome,
                           summary="Scripted external evaluator fixture", artifacts=[self.artifact], findings=findings or [])

    def test_independent_review_import_and_final_real_verification(self):
        self.setup_review()
        self.assertEqual(self.store.get(self.run_id)["state"]["status"], "REVIEWING")
        self.engine.import_evaluation(self.run_id, self.signed())
        self.engine.resume(self.run_id)
        result = self.engine.verify(self.run_id)
        self.assertEqual(result["state"]["status"], "SUCCEEDED")
        self.assertTrue(result["native"]["external_origins"])
        self.artifact.unlink()
        self.assertTrue(self.engine.context(self.run_id))  # Imported copy is durable.

    def test_pending_review_findings_survive_source_trimming_and_context_switch(self):
        self.setup_review()
        description = "Observed compatibility concern. " * 1000
        finding = {"id": "review-blocker", "severity": "blocking", "description": description,
                   "criterion_id": "preserve-behavior"}
        self.engine.import_evaluation(self.run_id, self.signed(outcome="fail", findings=[finding]))
        self.engine.pause(self.run_id, "Fresh review context")
        (self.root / "optional-source.txt").write_text("optional context\n" * 2400)
        model = deepcopy(self.config["model"]); model["model"] = "second-fixture-context"
        self.engine.switch_model(self.run_id, model)
        context = self.engine.context(self.run_id)
        self.assertEqual(context["pending_findings"][0]["finding"], finding)
        self.assertFalse(context["pending_findings"][0]["current"])
        self.assertNotIn("optional-source.txt", {s["path"] for s in context["bundle"]["sources"]})

    def test_all_non_command_services_use_registered_role_and_artifact(self):
        for kind in ("human", "interaction", "artifact"):
            with self.subTest(kind=kind):
                if kind != "human":
                    self.tearDown(); self.setUp()
                self.setup_review(kind)
                self.engine.import_evaluation(self.run_id, self.signed())
                self.engine.resume(self.run_id)
                self.assertEqual(self.engine.verify(self.run_id)["state"]["status"], "SUCCEEDED")

    def test_stale_candidate_review_is_rejected(self):
        self.setup_review(); signed = self.signed()
        (self.root / "slug.py").write_text(FIXED + "\n")
        with self.assertRaises(ContractError):
            self.engine.import_evaluation(self.run_id, signed)

    def test_result_replay_and_artifact_changes_are_rejected(self):
        self.setup_review(); signed = self.signed()
        self.artifact.write_text("changed")
        with self.assertRaises(ContractError):
            self.engine.import_evaluation(self.run_id, signed)
        self.artifact.write_text("Offline evaluator fixture observations\n")
        self.engine.import_evaluation(self.run_id, signed)
        with self.assertRaises(ContractError):
            self.engine.import_evaluation(self.run_id, signed)

    def test_unregistered_or_wrong_role_cannot_attest_independent_review(self):
        self.setup_review()
        self.store.authorities.create("fake-human", "human")
        signed = self.signed()
        forged = self.store.authorities.sign("fake-human", "evidence", signed["payload"])
        with self.assertRaises(ContractError):
            self.engine.import_evaluation(self.run_id, forged)

    def test_forged_mac_and_revoked_key_are_rejected(self):
        self.setup_review(); signed = self.signed()
        tampered = deepcopy(signed); tampered["payload"]["result"] = "fail"
        with self.assertRaises(ContractError):
            self.engine.import_evaluation(self.run_id, tampered)
        self.store.authorities.revoke("test-evaluator")
        with self.assertRaises(ContractError):
            self.engine.import_evaluation(self.run_id, signed)

    def test_blocking_finding_cannot_pass_and_failed_review_requires_revision(self):
        self.setup_review()
        finding = {"id": "finding-1", "severity": "blocking", "description": "Missing compatibility evidence", "criterion_id": None}
        with self.assertRaises(ContractError):
            self.signed(findings=[finding])
        self.engine.import_evaluation(self.run_id, self.signed(outcome="fail", findings=[finding]))
        self.engine.resume(self.run_id)
        data = self.engine.verify(self.run_id)
        self.assertNotEqual(data["state"]["status"], "SUCCEEDED")
        self.assertEqual(data["last_gate"]["outcome"], "REVISE")

    def test_same_implementer_context_is_not_independent(self):
        self.setup_review(); signed = self.signed()
        payload = deepcopy(signed["payload"])
        identity = self.store.get(self.run_id)["native"]["implementer_context_id"]
        payload["executor_run_id"] = identity; payload["extensions"]["context_id"] = identity
        forged = self.store.authorities.sign("test-evaluator", "evidence", payload)
        with self.assertRaises(ContractError):
            self.engine.import_evaluation(self.run_id, forged)


class ReservationAndProtocolTests(unittest.TestCase):
    def setUp(self):
        self.limits = {"max_tokens": 300, "max_cost_microunits": 600, "cost_currency": "USD"}
        self.quote = {"input_tokens": 100, "max_output_tokens": 100, "bound_verified": True,
                      "pricing": {"price_id": "test", "currency": "USD", "input_microunits_per_million": 1000000,
                                  "output_microunits_per_million": 2000000}, "request_digest": "sha256:" + "a" * 64}

    def test_unknown_usage_retains_full_reservation(self):
        ledger = Ledger([]); ledger.reserve("r1", self.quote, self.limits); ledger.settle("r1", None)
        self.assertEqual(ledger.balance()["tokens_upper"], 200)
        self.assertEqual(ledger.balance()["cost_upper_microunits"], 300)
        self.assertIsNone(ledger.balance()["actual_tokens"])
        with self.assertRaises(BudgetError):
            ledger.reserve("r2", self.quote, self.limits)

    def test_known_usage_releases_unused_reserved_capacity_once(self):
        ledger = Ledger([]); ledger.reserve("r1", self.quote, self.limits)
        usage = {"input_tokens": 100, "output_tokens": 10}
        ledger.settle("r1", usage); ledger.settle("r1", usage)
        self.assertEqual(ledger.balance()["tokens_upper"], 110)
        with self.assertRaises(BudgetError):
            ledger.settle("r1", {"input_tokens": 0, "output_tokens": 0})

    def test_currency_and_unverified_bounds_fail_closed(self):
        for mutate in (lambda q: q.update(bound_verified=False), lambda q: q["pricing"].update(currency="EUR"), lambda q: q.update(pricing=None)):
            quote = deepcopy(self.quote); mutate(quote)
            with self.assertRaises(BudgetError):
                Ledger([]).reserve("r", quote, self.limits)

    def test_duplicate_ids_negative_and_boolean_counts_rejected(self):
        ledger = Ledger([]); ledger.reserve("r", self.quote, self.limits)
        with self.assertRaises(BudgetError):
            ledger.reserve("r", self.quote, self.limits)
        for value in (-1, True, None):
            quote = deepcopy(self.quote); quote["input_tokens"] = value
            with self.assertRaises(BudgetError):
                Ledger([]).reserve("x", quote, self.limits)

    def test_accounting_overflow_is_refused_before_admission(self):
        quote = deepcopy(self.quote)
        quote["input_tokens"] = 9007199254740991
        with self.assertRaisesRegex(BudgetError, "integer range"):
            Ledger([]).reserve("x", quote, dict(self.limits, max_tokens=None, max_cost_microunits=None))
        ledger = Ledger([]); ledger.reserve("r", self.quote, self.limits)
        ledger.settle("r", {"input_tokens": 9007199254740991, "output_tokens": 9007199254740991})
        balance = ledger.balance()
        self.assertTrue(balance["violated_bound"])
        self.assertIsNone(balance["tokens_upper"])
        self.assertIsNone(balance["actual_tokens"])

    def test_provider_usage_missing_boolean_cache_and_reasoning_handling(self):
        config = load(ROOT / "templates/engine.json")
        driver = ModelDriver(config["model"], config["response_limits"], None)
        validate("adapter", driver.describe())
        self.assertIsNone(driver.usage({"usage": {"input_tokens": True, "output_tokens": 2}}))
        self.assertEqual(driver.usage({"usage": {"input_tokens": 5, "output_tokens": 8, "output_tokens_details": {"reasoning_tokens": 4}}}), {"input_tokens": 5, "output_tokens": 8})
        config["model"]["provider"] = "anthropic"
        driver = ModelDriver(config["model"], config["response_limits"], None)
        self.assertEqual(driver.usage({"usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3}}), {"input_tokens": 15, "output_tokens": 2})

    def test_provider_refusal_incomplete_and_tools_are_not_proposals(self):
        config = load(ROOT / "templates/engine.json")
        driver = ModelDriver(config["model"], config["response_limits"], None)
        for response in ({"status": "incomplete"}, {"status": "completed", "output": [{"type": "function_call"}]},
                         {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]}):
            with self.assertRaises(ModelError):
                driver.step(response)


class StageAndBrokerTests(NativeCase):
    def stages(self):
        task = load(self.task_path); task["workflow"] = "staged"
        criteria = [c["id"] for c in task["criteria"]]
        return task, [{"id": "first", "depends_on": [], "criterion_ids": [criteria[0]], "write_allow": ["slug.py"], "write_deny": []},
                      {"id": "second", "depends_on": ["first"], "criterion_ids": criteria[1:], "write_allow": ["slug.py"], "write_deny": []}]

    def test_stage_graph_unknown_cycles_and_duplicate_ownership_rejected(self):
        task, graph = self.stages()
        validate_stages(task, graph)
        for mutate in (lambda g: g[0]["depends_on"].append("second"), lambda g: g[1]["depends_on"].append("missing"),
                       lambda g: g[1]["criterion_ids"].append(g[0]["criterion_ids"][0])):
            changed = deepcopy(graph); mutate(changed)
            with self.assertRaises(ContractError):
                validate_stages(task, changed)

    def test_stage_passes_are_invalidated_by_a_new_candidate(self):
        task, graph = self.stages()
        snapshot = "sha256:" + "a" * 64
        state = statuses(graph, set(graph[0]["criterion_ids"]), snapshot)
        self.assertEqual(active_stage(graph, state, snapshot)["id"], "second")
        self.assertEqual(active_stage(graph, state, "sha256:" + "b" * 64)["id"], "first")

    def test_staged_task_requires_integrated_checks_and_owned_criteria(self):
        task, graph = self.stages()
        self.config["stages"] = graph; self.configure(); self.write_task(task); self.start(baseline=True)
        result = self.engine.drive(self.run_id, transport=FixtureTransport(self.engine, self.run_id))
        self.assertEqual(result["state"]["status"], "SUCCEEDED")
        self.assertTrue(all(s["status"] == "passed" for s in result["native"]["stage_status"]))

    def test_broker_rejects_unknown_actor_stale_lease_and_replaced_check(self):
        self.start()
        with self.store.writer(self.run_id) as data:
            broker = Broker()
            check = data["task"]["checks"][0]
            proposal = broker.proposal(data, "check.run", {"check_id": check["id"], "argv": check["argv"], "cwd": "."})
            validate_native("authorized_action", broker.authorize(data, proposal, actor_id="controller"))
            changed = deepcopy(proposal); changed["arguments"]["argv"] = ["sh", "-c", "echo fake"]
            with self.assertRaises(ContractError):
                broker.authorize(data, changed, actor_id="controller")
            with self.assertRaises(ContractError):
                broker.authorize(data, proposal, actor_id="untrusted-agent")
            changed = deepcopy(proposal); changed["candidate_digest"] = "sha256:" + "a" * 64
            with self.assertRaises(ContractError):
                broker.authorize(data, changed, actor_id="controller")

    def test_implementer_cannot_import_review_or_add_authority_fields(self):
        self.start()
        with self.store.writer(self.run_id) as data:
            broker = Broker(); proposal = broker.proposal(data, "evaluator.import", {"request_id": "x", "evidence_digest": "sha256:" + "a" * 64})
            with self.assertRaises(ContractError):
                broker.authorize(data, proposal, actor_id="implementer")
            proposal["policy_decision_id"] = "I authorize myself"
            with self.assertRaises(ContractError):
                broker.authorize(data, proposal, actor_id="operator")


class ProtectedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = load(ROOT / "templates/engine.json")["runtime"]
        self.config.update(backend="docker", image="sha256:" + "a" * 64)
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.runtime = DockerRuntime(self.config, control=self.control)

    def tearDown(self):
        self.temp.cleanup()

    def control(self, argv, timeout):
        if argv[:2] == ["image", "inspect"]:
            return json.dumps([{"Id": self.config["image"], "Config": {"Volumes": None}}])
        return ""

    def container(self):
        return {"Image": self.config["image"], "Config": {"Labels": {"loop.owner": "owner"}, "User": "1000:1000"},
                "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"],
                "CapAdd": None, "SecurityOpt": ["no-new-privileges:true"], "Memory": 512 * 1048576,
                "MemorySwap": 512 * 1048576, "NanoCpus": 1000000000, "PidsLimit": 128, "PidMode": "", "IpcMode": "private",
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=128m,mode=1777"},
                "Devices": [], "Binds": None, "VolumesFrom": None},
                "Mounts": [{"Type": "bind", "Source": str(self.root), "Destination": "/workspace", "RW": False}]}

    def test_container_command_has_no_credentials_socket_network_or_mutable_image(self):
        command = self.runtime.create_command(self.root, ".", ["python3", "-m", "unittest"], "loop-test", "owner")
        for flag in ("--pull=never", "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true"):
            self.assertIn(flag, command)
        self.assertIn("readonly", next(arg for arg in command if arg.startswith("type=bind")))
        self.assertNotIn("docker.sock", " ".join(command))
        self.assertNotIn("OPENAI_API_KEY", " ".join(command))

    def test_actual_inspected_controls_must_match_policy(self):
        self.runtime.verify_container(self.container(), self.root, "owner")
        for field, value in (("NetworkMode", "host"), ("ReadonlyRootfs", False), ("Privileged", True), ("Memory", 0), ("PidsLimit", 0),
                             ("CapAdd", ["SYS_ADMIN"]), ("Tmpfs", {}), ("SecurityOpt", ["seccomp=unconfined"]), ("UtsMode", "host")):
            container = self.container(); container["HostConfig"][field] = value
            with self.assertRaises(ContractError):
                self.runtime.verify_container(container, self.root, "owner")

    def test_only_the_configured_non_host_temporary_mount_is_permitted(self):
        container = self.container()
        container["Mounts"].append({"Type": "tmpfs", "Source": "", "Destination": "/tmp", "RW": True})
        self.runtime.verify_container(container, self.root, "owner")
        container["Mounts"][-1]["Destination"] = "/workspace"
        with self.assertRaises(ContractError):
            self.runtime.verify_container(container, self.root, "owner")

    def test_host_mounts_aliases_and_csv_injection_are_rejected(self):
        container = self.container(); container["Mounts"].append({"Type": "bind", "Source": "/", "Destination": "/host", "RW": True})
        with self.assertRaises(ContractError):
            self.runtime.verify_container(container, self.root, "owner")
        with self.assertRaises(ContractError):
            self.runtime.create_command(self.root / "bad,readonly=false", ".", ["python3"], "loop-test", "owner")

    def test_runtime_permission_failure_is_unavailable_not_verified(self):
        def denied(argv, timeout):
            raise PermissionError("daemon inaccessible")
        runtime = DockerRuntime(self.config, control=denied)
        result = runtime.probe(self.root)
        self.assertFalse(result["verified"])
        self.assertEqual(result["capabilities"], {})

    def test_reconciliation_never_removes_a_different_owner(self):
        called = []
        self.runtime.control = lambda argv, timeout: called.append(argv) or ""
        with patch.object(self.runtime, "inspect", return_value={"Config": {"Labels": {"loop.owner": "other"}}}):
            with self.assertRaises(ContractError):
                self.runtime.reconcile("loop-test", "owner")
        self.assertEqual(called, [])
