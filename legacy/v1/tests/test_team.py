"""Team batches with real local checks and scripted hosts; no live inference."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from tests import test_scenarios as fixture
from loop_engineering.adapters import CommandDriver
from loop_engineering.contracts import ContractError, ROOT, load, validate
from loop_engineering.controller import Controller
from loop_engineering.evaluators import sign_result
from loop_engineering.native_contracts import validate_context
from loop_engineering.native_engine import NativeController
from loop_engineering.store import Store
from loop_engineering.team_engine import TeamController
from loop_engineering.workspace import byte_digest


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ScenarioTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.initialize()
        original = self.fixture.prepare_task()
        self.root, self.base = self.fixture.root, self.fixture.base
        self.store = Store(self.base / "team-state")
        self.team = TeamController(self.store)
        (self.root / ".loop/tasks").mkdir()
        for task_id in ("implement", "verify"):
            contract = deepcopy(original)
            contract["task_id"] = task_id
            self.fixture.write(f".loop/tasks/{task_id}.json", contract)
        self.plan = {"schema_version": "1.0", "workflow_id": "slug-batch", "revision": 1,
                     "scope": "Implement and functionally verify a library fix; project acceptance is separate.",
                     "final_task": "verify", "tasks": [
                         {"id": "implement", "phase": "implement", "role": "backend",
                          "task_file": ".loop/tasks/implement.json", "engine_file": None,
                          "depends_on": [], "outputs": ["slug.py"], "handoff_to": ["coordinator"]},
                         {"id": "verify", "phase": "verify", "role": "tester",
                          "task_file": ".loop/tasks/verify.json", "engine_file": None,
                          "depends_on": [], "outputs": ["slug.py"], "handoff_to": ["coordinator"]}]}
        self.save_plan()

    def save_plan(self):
        self.fixture.write(".loop/workflow-tasks.json", self.plan)

    def start(self):
        result = self.team.start(self.root)
        self.team_id = result["team_id"]
        return result

    def step(self, context, *, change=True):
        request_id = context["team_assignment"]["request_id"]
        context = context["task_context"]
        bundle = context.get("bundle", context)
        return {"schema_version": "0.2", "step_id": request_id,
                "task_id": bundle["task"]["task_id"], "contract_digest": context["contract_digest"],
                "base_snapshot_digest": context["base_snapshot_digest"], "intent": "act" if change else "request_verification",
                "criterion_ids": [c["id"] for c in bundle["task"]["criteria"]],
                "summary": "Deterministic offline specialist fixture", "expected_observation": "Real slug checks pass",
                "changes": [{"path": "slug.py", "expected_sha256": byte_digest((self.root / "slug.py").read_bytes()),
                             "new_content": fixture.FIXED}] if change else [],
                "evidence_refs": [], "blocker": None, "next_action": None}

    def complete(self, task_id, *, change=True):
        context = self.team.request(self.team_id, task_id)
        return self.team.submit(self.team_id, task_id, context["team_assignment"]["request_id"], self.step(context, change=change))

    def receive(self, task_id, *, role="coordinator", accept=True):
        handoff = self.team.status(self.team_id)["tasks"][task_id]["handoff"]
        return self.team.receive(self.team_id, task_id, handoff["id"], role, accept=accept,
                                 note="Inspected the candidate, actual test results and required output.")

    def driver(self, code):
        return CommandDriver([sys.executable, "-c", code])

    def test_start_is_offline_and_does_not_create_a_child_or_dispatch(self):
        self.assertTrue((self.root / ".loop/execution.md").is_file())
        self.assertTrue((self.root / ".loop/schemas/team-workflow.schema.json").is_file())
        with patch("subprocess.Popen", side_effect=AssertionError("No dispatch during team admission")):
            report = self.start()
        self.assertEqual(report["ready_tasks"], ["implement"])
        self.assertEqual(self.store.runs(), [])
        self.assertEqual(self.team.teams.runs()[0]["team_id"], self.team_id)
        with self.assertRaisesRegex(ContractError, "unfinished team"):
            self.team.start(self.root)

    def test_scaffold_and_invalid_plan_contracts_fail_before_admission(self):
        original = deepcopy(self.plan)
        changes = [
            lambda x: x["tasks"].append(deepcopy(x["tasks"][0])),
            lambda x: x["tasks"][0].update(depends_on=["verify"]),
            lambda x: x["tasks"][0].update(depends_on=["missing"]),
            lambda x: x.update(final_task="implement"),
            lambda x: x["tasks"][0].update(role="frontend"),
            lambda x: x["tasks"][0].update(phase="review"),
            lambda x: x["tasks"][0].update(task_file="../escape.json"),
            lambda x: x["tasks"][0].update(outputs=[".loop/task.json"]),
            lambda x: x["tasks"][0].update(outputs=[".env"]),
            lambda x: x["tasks"][0].update(outputs=["Integrated candidate changing only slug.py"]),
            lambda x: x["tasks"][0].update(outputs=["tests"]),
            lambda x: x["tasks"][0].update(handoff_to=["tester"]),
            lambda x: x.update(tasks=[], final_task=None),
        ]
        for change in changes:
            self.plan = deepcopy(original)
            change(self.plan)
            self.save_plan()
            with self.subTest(plan=self.plan), self.assertRaises(ContractError):
                self.team.start(self.root)
            self.assertEqual(self.team.teams.runs(), [])
        self.assertEqual(self.store.runs(), [])

    def native_staged_task(self):
        task = load(self.root / ".loop/tasks/implement.json")
        task["workflow"] = "staged"
        self.fixture.write(".loop/tasks/implement.json", task)
        criteria = [c["id"] for c in task["criteria"]]
        graph = [{"id": "source", "depends_on": [], "criterion_ids": [criteria[0]],
                  "write_allow": deepcopy(task["scope"]["write_allow"]), "write_deny": deepcopy(task["scope"]["write_deny"])},
                 {"id": "compatibility", "depends_on": ["source"], "criterion_ids": criteria[1:],
                  "write_allow": deepcopy(task["scope"]["write_allow"]), "write_deny": deepcopy(task["scope"]["write_deny"])}]
        config = load(ROOT / "templates/engine.json")
        config["stages"] = graph
        self.plan["tasks"][0]["engine_file"] = ".loop/tasks/implement-engine.json"
        self.save_plan()
        return task, config

    def test_native_stage_graph_is_validated_during_build_before_any_child_is_reserved(self):
        task, config = self.native_staged_task()
        original = deepcopy(config)
        mutations = [
            (lambda stages: stages.clear(), "stage dependency graph"),
            (lambda stages: stages[0]["depends_on"].append("compatibility"), "cycle"),
            (lambda stages: stages[1]["depends_on"].append("missing"), "unknown dependency"),
            (lambda stages: stages.pop(), "criterion must have exactly one stage owner"),
            (lambda stages: stages[1]["criterion_ids"].append(task["criteria"][0]["id"]),
             "criterion must have exactly one stage owner"),
            (lambda stages: stages[1]["criterion_ids"].append("unknown-criterion"),
             "criterion must have exactly one stage owner"),
        ]
        for mutate, message in mutations:
            config = deepcopy(original)
            mutate(config["stages"])
            self.fixture.write(".loop/tasks/implement-engine.json", config)
            before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
            with self.subTest(reason=message), \
                    patch("subprocess.Popen", side_effect=AssertionError("Build must be offline")), \
                    self.assertRaisesRegex(ContractError, "implement:.*" + message):
                self.team.build(self.root)
            self.assertEqual(self.store.runs(), [])
            self.assertEqual(self.team.teams.runs(), [])
            self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_native_stage_graph_for_a_standard_task_is_rejected_during_build(self):
        task, config = self.native_staged_task()
        task["workflow"] = "standard"
        self.fixture.write(".loop/tasks/implement.json", task)
        self.fixture.write(".loop/tasks/implement-engine.json", config)
        with self.assertRaisesRegex(ContractError, "implement:.*workflow=staged"):
            self.team.build(self.root)
        self.assertEqual(self.store.runs(), [])
        self.assertEqual(self.team.teams.runs(), [])

    def test_valid_native_stage_graph_survives_build_and_real_native_child_initialization(self):
        task, config = self.native_staged_task()
        self.fixture.write(".loop/tasks/implement-engine.json", config)
        with patch("subprocess.Popen", side_effect=AssertionError("Build must be offline")):
            built = self.team.build(self.root)
        self.assertEqual(load(self.root / built["tasks"]["implement"]["engine_file"])["stages"], config["stages"])
        self.assertEqual(self.store.runs(), [])
        self.start()
        request = self.team.request(self.team_id, "implement")
        child = self.store.get(self.team.teams.get(self.team_id)["records"]["implement"]["child_run_id"])
        self.assertEqual(request["team_assignment"]["task_id"], "implement")
        self.assertEqual(child["task"], task)
        self.assertEqual(child["native"]["config"]["stages"], config["stages"])

    def test_portable_staged_task_without_an_engine_keeps_its_existing_admission_path(self):
        task = load(self.root / ".loop/tasks/implement.json")
        task["workflow"] = "staged"
        self.fixture.write(".loop/tasks/implement.json", task)
        self.assertIsNone(self.team.build(self.root)["tasks"]["implement"]["engine_file"])
        self.start()
        request = self.team.request(self.team_id, "implement")
        child = self.store.get(self.team.teams.get(self.team_id)["records"]["implement"]["child_run_id"])
        self.assertEqual(request["team_assignment"]["task_id"], "implement")
        self.assertNotIn("native", child)

    def test_future_output_can_be_produced_by_a_dependency_with_write_scope(self):
        path = self.root / ".loop/tasks/implement.json"
        contract = load(path); contract["scope"]["write_allow"].append("receipt.txt")
        self.fixture.write(".loop/tasks/implement.json", contract)
        contract = load(self.root / ".loop/tasks/verify.json")
        contract["scope"]["write_deny"].append("**")
        self.fixture.write(".loop/tasks/verify.json", contract)
        for task in self.plan["tasks"]:
            task["outputs"] = ["receipt.txt"]
        self.save_plan()
        report = self.start()
        self.assertEqual(report["ready_tasks"], ["implement"])
        self.assertFalse((self.root / "receipt.txt").exists())
        self.assertEqual(self.store.runs(), [])

    def test_existing_output_with_spaces_is_valid_for_a_read_only_task(self):
        (self.root / "review notes.txt").write_text("Existing actual output\n")
        for task in self.plan["tasks"]:
            task["outputs"] = ["review notes.txt"]
            contract = load(self.root / task["task_file"])
            contract["scope"]["write_deny"].append("**")
            self.fixture.write(task["task_file"], contract)
        self.save_plan()
        report = self.start()
        self.assertEqual(report["ready_tasks"], ["implement"])
        self.assertEqual(self.store.runs(), [])

    def test_file_bridge_verification_handoffs_phase_gate_and_final_candidate(self):
        self.start()
        with self.assertRaisesRegex(ContractError, "dependencies"):
            self.team.request(self.team_id, "verify")
        first = self.team.request(self.team_id, "implement")
        self.assertEqual(self.team.request(self.team_id, "implement"), first)
        report = self.team.submit(self.team_id, "implement", first["team_assignment"]["request_id"], self.step(first))
        self.assertEqual(report["tasks"]["implement"]["status"], "HANDOFF")
        self.assertEqual(report["tasks"]["implement"]["controller_status"], "SUCCEEDED")
        self.assertEqual(report["ready_tasks"], [])
        with self.assertRaisesRegex(ContractError, "dependencies"):
            self.team.request(self.team_id, "verify")
        self.assertEqual(self.receive("implement")["ready_tasks"], ["verify"])
        last = self.team.request(self.team_id, "verify")
        self.assertEqual(last["team_assignment"]["owner"], "coordinator")
        instructions = {d["path"] for d in last["team_assignment"]["instructions"]}
        self.assertTrue({".loop/agents/tester.md", ".loop/agents/coordinator.md", "LOOP.md"}.issubset(instructions))
        self.assertEqual(last["team_assignment"]["dependencies"][0]["outputs"][0]["sha256"], byte_digest(fixture.FIXED.encode()))
        self.team.submit(self.team_id, "verify", last["team_assignment"]["request_id"], self.step(last, change=False))
        self.assertEqual(self.receive("verify")["status"], "COMPLETE")
        self.assertTrue(self.team.status(self.team_id)["final_candidate_current"])
        self.assertEqual(self.team.teams.audit(self.team_id)["status"], "COMPLETE")
        (self.root / "slug.py").write_text("A later user change\n")
        self.assertFalse(self.team.status(self.team_id)["final_candidate_current"])

    def test_missing_output_and_unverified_child_cannot_make_a_handoff(self):
        self.plan["tasks"][0]["outputs"].append("missing-report.md")
        contract = load(self.root / ".loop/tasks/implement.json")
        contract["scope"]["write_allow"].append("missing-report.md")
        self.fixture.write(".loop/tasks/implement.json", contract)
        self.save_plan(); self.start()
        with self.assertRaisesRegex(ContractError, "running task"):
            self.team.collect(self.team_id, "implement")
        context = self.team.request(self.team_id, "implement")
        self.team.collect(self.team_id, "implement")
        self.assertEqual(self.team.status(self.team_id)["tasks"]["implement"]["status"], "RUNNING")
        with self.assertRaisesRegex(ContractError, "output is missing"):
            self.team.submit(self.team_id, "implement", context["team_assignment"]["request_id"], self.step(context))
        self.assertEqual(self.team.status(self.team_id)["ready_tasks"], [])

    def test_stale_wrong_and_replayed_host_responses_are_rejected(self):
        self.start(); context = self.team.request(self.team_id, "implement")
        proposal = self.step(context)
        with self.assertRaisesRegex(ContractError, "different host request"):
            self.team.submit(self.team_id, "implement", "wrong", proposal)
        path = self.root / "slug.py"; original = path.read_bytes(); path.write_text("Changed externally\n")
        with self.assertRaisesRegex(ContractError, "stale"):
            self.team.submit(self.team_id, "implement", context["team_assignment"]["request_id"], proposal)
        path.write_bytes(original)
        self.team.submit(self.team_id, "implement", context["team_assignment"]["request_id"], proposal)
        with self.assertRaises(ContractError):
            self.team.submit(self.team_id, "implement", context["team_assignment"]["request_id"], proposal)

    def test_receipts_require_all_named_recipients_and_current_candidate(self):
        selected = load(self.root / ".loop/team.json")
        next(d for d in selected["decisions"] if d["role"] == "backend")["handoff_to"].append("acceptance")
        self.fixture.write(".loop/team.json", selected)
        self.plan["tasks"][0]["handoff_to"].append("acceptance")
        self.save_plan(); self.start(); self.complete("implement")
        record = self.team.status(self.team_id)["tasks"]["implement"]
        for role, identity in (("backend", record["handoff"]["id"]), ("coordinator", "wrong")):
            with self.assertRaises(ContractError):
                self.team.receive(self.team_id, "implement", identity, role, accept=True, note="Inspected")
        self.assertEqual(self.receive("implement")["tasks"]["implement"]["status"], "HANDOFF")
        with self.assertRaisesRegex(ContractError, "duplicate"):
            self.receive("implement")
        path = self.root / "slug.py"; before = path.read_bytes(); path.write_text("Changed\n")
        with self.assertRaisesRegex(ContractError, "candidate"):
            self.receive("implement", role="acceptance")
        path.write_bytes(before)
        self.assertEqual(self.receive("implement", role="acceptance")["ready_tasks"], ["verify"])

    def test_rejected_handoff_blocks_progress_and_requires_a_reviewed_revision(self):
        self.start(); self.complete("implement")
        result = self.receive("implement", accept=False)
        self.assertEqual(result["status"], "BLOCKED")
        with self.assertRaises(ContractError): self.team.request(self.team_id, "verify")
        with self.assertRaises(ContractError): self.team.resume(self.team_id)
        self.assertEqual(len(self.store.runs()), 1)

    def test_frozen_plan_contract_selection_and_profile_reject_changes(self):
        self.start()
        for name in (".loop/workflow-tasks.json", ".loop/tasks/implement.json", ".loop/team.json", ".loop/project.json"):
            path = self.root / name; original = path.read_bytes(); path.write_bytes(original + b"\n")
            with self.subTest(path=name):
                self.assertFalse(self.team.status(self.team_id)["inputs_current"])
                with self.assertRaisesRegex(ContractError, "changed"):
                    self.team.request(self.team_id, "implement")
                self.assertEqual(self.store.runs(), [])
            path.write_bytes(original)

    def test_context_retains_nested_instructions_and_refuses_oversize(self):
        (self.root / "nested").mkdir()
        (self.root / "nested/AGENTS.md").write_text("Mandatory nested convention\n")
        self.start(); context = self.team.request(self.team_id, "implement")
        self.assertIn("nested/AGENTS.md", {d["path"] for d in context["team_assignment"]["instructions"]})
        self.assertLessEqual(len(json.dumps(context,ensure_ascii=False,separators=(",",":")).encode()), 60000)
        self.team.stop(self.team_id, cancel=True)
        profile = load(self.root / ".loop/project.json"); profile["context_max_bytes"] = 15000
        self.fixture.write(".loop/project.json", profile)
        self.start()
        with self.assertRaisesRegex(ContractError, "context_max_bytes"):
            self.team.request(self.team_id, "implement")

    def test_local_command_receives_assignment_and_returns_real_verified_proposal(self):
        self.start()
        code = """import json,sys
request=json.load(sys.stdin); c=request['task_context']
assert request['team_assignment']['role']=='backend'
assert request['team_assignment']['authority'].startswith('Proposal only')
assert any(d['path']=='.loop/spec.md' for d in request['team_assignment']['instructions'])
source=next(s for s in c['sources'] if s['path']=='slug.py')
step={'schema_version':'0.2','step_id':'offline-command','task_id':c['task']['task_id'],
 'contract_digest':c['contract_digest'],'base_snapshot_digest':c['base_snapshot_digest'],
 'intent':'act','criterion_ids':[x['id'] for x in c['task']['criteria']],
 'summary':'Deterministic fixture','expected_observation':'Actual tests pass',
 'changes':[{'path':'slug.py','expected_sha256':source['sha256'],'new_content':FIXTURE}],
 'evidence_refs':[],'blocker':None,'next_action':None}
print(json.dumps(step))
""".replace("FIXTURE", repr(fixture.FIXED))
        result = self.team.drive(self.team_id, "implement", self.driver(code))
        self.assertEqual(result["tasks"]["implement"]["status"], "HANDOFF")
        child = self.store.get(result["tasks"]["implement"]["child_run_id"])
        self.assertFalse(child["usage_complete"])
        self.assertEqual(child["state"]["iteration"], 1)
        self.assertEqual((self.root / "slug.py").read_text(), fixture.FIXED)

    def test_failed_host_pause_resume_preserves_child_identity_and_budget(self):
        self.start()
        first = self.team.drive(self.team_id, "implement", self.driver("raise SystemExit(3)"))
        run_id = first["tasks"]["implement"]["child_run_id"]
        spent = self.store.get(run_id)["elapsed_ms"]
        context = self.team.request(self.team_id, "implement")
        self.assertEqual(self.team.stop(self.team_id)["status"], "PAUSED")
        with self.assertRaises(ContractError): self.team.request(self.team_id, "verify")
        result = self.team.resume(self.team_id)
        self.assertEqual(result["tasks"]["implement"]["child_run_id"], run_id)
        self.assertEqual(self.store.get(run_id)["state"]["iteration"], 1)
        self.assertGreaterEqual(self.store.get(run_id)["elapsed_ms"], spent)
        fresh = self.team.request(self.team_id, "implement")
        self.assertNotEqual(fresh["team_assignment"]["request_id"], context["team_assignment"]["request_id"])
        with self.assertRaises(ContractError):
            self.team.submit(self.team_id, "implement", context["team_assignment"]["request_id"], self.step(fresh))

    def test_timeout_does_not_advance_or_start_a_new_child(self):
        self.start()
        report = self.team.drive(self.team_id, "implement", self.driver("import time; time.sleep(10)"), timeout=1)
        self.assertEqual(report["tasks"]["implement"]["status"], "RUNNING")
        self.assertEqual(report["ready_tasks"], [])
        self.assertEqual(len(self.store.runs()), 1)
        child = self.store.get(report["tasks"]["implement"]["child_run_id"])
        self.assertIsNone(child["pending_process"])
        self.assertIn("timeout", child["state"]["reason"])

    def test_cancellation_stops_the_owned_process_and_blocks_late_results(self):
        self.start()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.team.drive, self.team_id, "implement", self.driver("import time; time.sleep(10)"), timeout=5)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                record = self.team.status(self.team_id)["tasks"]["implement"]
                if record["child_run_id"] and self.team._exists(record["child_run_id"]):
                    process = self.store.get(record["child_run_id"])["pending_process"]
                    if process and process["pid"]: break
                time.sleep(.01)
            else: self.fail("Fixture process did not start")
            self.team.stop(self.team_id, cancel=True)
            report = future.result(timeout=3)
        self.assertEqual(report["status"], "CANCELLED")
        self.assertEqual(report["tasks"]["implement"]["controller_status"], "CANCELLED")
        with self.assertRaises(ContractError): self.team.resume(self.team_id)

    def test_crash_after_child_creation_reuses_reserved_identity(self):
        self.start()
        original = Controller.start
        def crash(controller, *args, **kwargs):
            original(controller, *args, **kwargs)
            raise RuntimeError("crash after durable child creation")
        with patch.object(Controller, "start", crash), self.assertRaisesRegex(RuntimeError, "crash"):
            self.team.request(self.team_id, "implement")
        run_id = self.store.runs()[0]["run_id"]
        restarted = TeamController(Store(self.base / "team-state"))
        context = restarted.request(self.team_id, "implement")
        self.assertEqual(context["task_context"]["run_id"], run_id)
        self.assertEqual(len(self.store.runs()), 1)

    def test_unknown_dispatch_effect_requires_reconciliation_before_retry(self):
        self.start()
        with patch("loop_engineering.controller.execute", side_effect=RuntimeError("dispatch boundary crash")):
            with self.assertRaises(RuntimeError):
                self.team.drive(self.team_id, "implement", self.driver("print('{}')"))
        run_id = self.team.status(self.team_id)["tasks"]["implement"]["child_run_id"]
        with self.assertRaisesRegex(ContractError, "Reconcile the interrupted dispatch"):
            self.team.resume(self.team_id)
        child = self.store.get(run_id)
        self.assertEqual(child["state"]["iteration"], 1)
        Controller(self.store).reconcile_process(run_id, child["pending_process"]["action_id"],
                                                "Offline injected crash occurred before process creation; no worker exists.")
        self.team.resume(self.team_id)
        self.assertEqual(len(self.store.runs()), 1)
        self.assertFalse(self.store.get(run_id)["usage_complete"])

    def test_team_store_rejects_stale_updates_tampering_and_deleted_history(self):
        self.start(); before = self.team.teams.get(self.team_id)
        current = deepcopy(before); self.team.teams.save(current, "fixture.updated")
        with self.assertRaisesRegex(ContractError, "Stale"):
            self.team.teams.save(before, "fixture.stale")
        with self.store.connect() as connection:
            connection.execute("DELETE FROM team_events WHERE id=? AND revision=1", (self.team_id,))
        with self.assertRaisesRegex(ContractError, "incomplete"):
            self.team.teams.audit(self.team_id)
        with self.store.connect() as connection:
            forged = deepcopy(current); forged["status"] = "COMPLETE"
            connection.execute("UPDATE teams SET data=? WHERE id=?", (json.dumps(forged), self.team_id))
        with self.assertRaisesRegex(ContractError, "projection"):
            self.team.status(self.team_id)
        with self.assertRaisesRegex(ContractError, "projection"):
            self.team.teams.save(current, "fixture.overwrite_tampering")
        with self.assertRaisesRegex(ContractError, "projection"):
            self.team.start(self.root)

    def test_native_independent_check_cannot_be_replaced_by_a_handoff_receipt(self):
        self.plan["tasks"][1].update(phase="acceptance", role="acceptance", engine_file=".loop/review-engine.json")
        contract = load(self.root / ".loop/tasks/verify.json")
        contract["checks"].append({"id": "external", "type": "review", "independent": True,
                                   "description": "Independent acceptance fixture", "procedure": ["Inspect the candidate and actual slug results."]})
        contract["criteria"].append({"id": "reviewed", "description": "Independent review passes", "check_ids": ["external"]})
        self.fixture.write(".loop/tasks/verify.json", contract)
        self.store.authorities.create("team-reviewer", "reviewer")
        config = load(ROOT / "templates/engine.json")
        config["evaluator_keys"] = [{"key_id": "team-reviewer", "role": "reviewer", "check_ids": ["external"]}]
        self.fixture.write(".loop/review-engine.json", config)
        self.save_plan(); self.start(); self.complete("implement"); self.receive("implement")
        context = self.team.request(self.team_id, "verify")
        validate_context(context["task_context"])
        report = self.team.submit(self.team_id, "verify", context["team_assignment"]["request_id"], self.step(context, change=False))
        self.assertEqual(report["tasks"]["verify"]["status"], "RUNNING")
        run_id = report["tasks"]["verify"]["child_run_id"]
        controller = NativeController(self.store, self.root / ".loop/review-engine.json")
        request = controller.evaluation_request(run_id, "external")
        artifact = self.base / "review.txt"; artifact.write_text("Scripted offline review fixture; real tests checked the implementation.\n")
        signed = sign_result(self.store.authorities, request, "team-reviewer", result="pass",
                             summary="Offline independent review fixture", artifacts=[artifact], findings=[])
        controller.import_evaluation(run_id, signed)
        self.team.resume(self.team_id)
        controller.verify(run_id)
        self.assertEqual(self.team.collect(self.team_id, "verify")["tasks"]["verify"]["status"], "HANDOFF")
        self.assertEqual(self.receive("verify")["status"], "COMPLETE")

    def test_excluded_nested_instructions_are_kept_and_changes_block_submission(self):
        (self.root / "nested").mkdir()
        # No existing source in this directory: the prospective output still
        # makes its excluded repository instructions mandatory.
        self.plan["tasks"][0]["outputs"].append("nested/new-report.md")
        contract = load(self.root / ".loop/tasks/implement.json")
        contract["scope"]["write_allow"].append("nested/new-report.md")
        self.fixture.write(".loop/tasks/implement.json", contract)
        self.save_plan()
        path = self.root / "nested/AGENTS.md"; path.write_text("Keep this convention\n")
        profile = load(self.root / ".loop/project.json")
        profile["snapshot"]["exclude"].append("nested/AGENTS.md")
        self.fixture.write(".loop/project.json", profile)
        self.start(); context = self.team.request(self.team_id, "implement")
        self.assertIn("nested/AGENTS.md", {d["path"] for d in context["team_assignment"]["instructions"]})
        path.write_text("A changed mandatory convention\n")
        with self.assertRaisesRegex(ContractError, "instructions changed"):
            self.team.submit(self.team_id, "implement", context["team_assignment"]["request_id"], self.step(context))
        self.assertNotEqual((self.root / "slug.py").read_text(), fixture.FIXED)

    def test_cancellation_signal_cannot_be_overwritten_by_late_pause(self):
        self.start()
        self.team.teams.signal(self.team_id, "CANCELLED")
        self.assertEqual(self.team.teams.signal(self.team_id, "PAUSED"), "CANCELLED")
        self.assertEqual(self.team.teams.signal(self.team_id, ""), "CANCELLED")
        with self.assertRaises(ContractError): self.team.resume(self.team_id)

    def test_unknown_agent_capability_refuses_command_dispatch(self):
        contract = load(self.root / ".loop/tasks/implement.json")
        contract["required_capabilities"]["agent"].append("vision")
        self.fixture.write(".loop/tasks/implement.json", contract)
        self.start()
        with patch("subprocess.Popen", side_effect=AssertionError("Unsupported agent must not start")):
            with self.assertRaisesRegex(ContractError, "capabilities unavailable"):
                self.team.drive(self.team_id, "implement", self.driver("print('{}')"))

    def test_reserved_child_before_creation_survives_crash_without_duplicate(self):
        self.start()
        with patch.object(Controller, "start", side_effect=RuntimeError("before child creation")):
            with self.assertRaises(RuntimeError): self.team.request(self.team_id, "implement")
        reserved = self.team.status(self.team_id)["tasks"]["implement"]["child_run_id"]
        self.assertEqual(self.store.runs(), [])
        self.assertEqual(self.team.request(self.team_id, "implement")["task_context"]["run_id"], reserved)
        self.assertEqual(len(self.store.runs()), 1)

    def test_native_command_cannot_bypass_accounting_or_start_a_child(self):
        self.plan["tasks"][0]["engine_file"] = ".loop/native-worker.json"
        self.fixture.write(".loop/native-worker.json", load(ROOT / "templates/engine.json"))
        self.save_plan(); self.start()
        with patch("subprocess.Popen", side_effect=AssertionError("No native legacy command")):
            with self.assertRaisesRegex(ContractError, "managed accounting"):
                self.team.drive(self.team_id, "implement", self.driver("print('{}')"))
        self.assertEqual(self.store.runs(), [])

    def test_concurrent_team_writers_are_rejected(self):
        self.start()
        with self.team.teams.writer(self.team_id):
            with self.assertRaisesRegex(ContractError, "Another operation"):
                self.team.request(self.team_id, "implement")
        self.assertEqual(self.store.runs(), [])

    def test_cli_roundtrip_and_output_preservation(self):
        cli = self.fixture.cli
        common = ["--state-dir", str(self.base / "cli-team-state")]
        code, result, _ = cli(["team-start", str(self.root), *common]); self.assertEqual(code, 0)
        team_id = result["team_id"]
        self.assertEqual(cli(["team-runs", *common])[1]["teams"][0]["team_id"], team_id)
        target = self.base / "request.json"
        args = ["team-request", team_id, "--task", "implement", "--output", str(target), *common]
        self.assertEqual(cli(args)[0], 0)
        before = target.read_bytes(); self.assertEqual(cli(args)[0], 2); self.assertEqual(target.read_bytes(), before)
        context = json.loads(before); step_file = self.base / "step.json"; step_file.write_text(json.dumps(self.step(context)))
        code, result, _ = cli(["team-submit", team_id, "--task", "implement", "--request-id", context["team_assignment"]["request_id"], "--file", str(step_file), *common])
        self.assertEqual(code, 0)
        handoff_id = result["tasks"]["implement"]["handoff"]["id"]
        code, result, _ = cli(["team-receive", team_id, "--task", "implement", "--handoff-id", handoff_id,
                              "--role", "coordinator", "--decision", "accept", "--note", "Inspected fixture results", *common])
        self.assertEqual(code, 0); self.assertEqual(result["ready_tasks"], ["verify"])
        self.assertTrue(cli(["team-audit", team_id, *common])[1]["authenticated"])


if __name__ == "__main__":
    unittest.main()
