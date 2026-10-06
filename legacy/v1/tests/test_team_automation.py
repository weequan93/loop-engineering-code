"""Actual local effects and injected hosts; never dispatch a live model."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_team as fixture
from loop_engineering.adapters import CommandDriver
from loop_engineering.budgets import BudgetError
from loop_engineering.contracts import ROOT, ContractError, load
from loop_engineering.team_automation import TeamAutomation, RECEIPT_RESPONSE
from loop_engineering.team_host import TeamHost
from loop_engineering.processes import execute, process_alive
from loop_engineering.workspace import byte_digest


class FakeTransport:
    def __init__(self, owner):
        self.owner, self.requests = owner, []
        self.reject_once = False
        self.rejected = False
        self.delay = 0
        self.unknown = False
        self.over_bound = False
        self.review_reject_once = False
        self.review_rejected = False
        self.lock = threading.Lock()
        self.active = self.peak = 0

    def post(self, operation, payload, **kwargs):
        if operation == "count":
            return {"input_tokens": 10}
        request = json.loads(payload["input"])
        self.requests.append(request)
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            response = self.owner.respond(request)
        finally:
            with self.lock:
                self.active -= 1
        if request["kind"] == "team-recipient-request" and self.reject_once and not self.rejected:
            response = {"accept": False, "note": "Offline rejection fixture: repeat implementation and acceptance."}
            self.rejected = True
        if request["kind"] == "team-evaluator-request" and self.review_reject_once and not self.review_rejected:
            response = {"result": "fail", "summary": "Independent fixture requests an implementation revision", "findings": [
                {"id":"revise", "severity":"blocking", "description":"Exercise bounded repair and re-review", "criterion_id":None}]}
            self.review_rejected = True
        return {"status": "completed", "usage": None if self.unknown else
                {"input_tokens": 10000 if self.over_bound else 10, "output_tokens": 10},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(response)}]}]}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.TeamTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.base, self.team = self.fixture.root, self.fixture.base, self.fixture.team
        self.plan = self.fixture.plan
        self.config = load(ROOT / "templates/engine.json")
        self.config["model"]["model"] = "offline-fixture"
        self.config["model"]["pricing"] = {"price_id": "offline-fixture", "currency": "USD", "input_microunits_per_million": 1000000,
                                               "output_microunits_per_million": 1000000}
        self.config["response_limits"]["max_output_tokens"] = 100
        self.engine = self.base / "provider.json"
        self.engine.write_text(json.dumps(self.config))
        self.transport = FakeTransport(self)
        self.host = TeamHost(self.team, engine=self.engine, transport=self.transport)
        self.auto = TeamAutomation(self.team, self.host)
        self.policy = load(ROOT / "templates/scenarios/development/team-policy.json", "team-policy")
        self.policy_path = self.base / "policy.json"
        self.write_policy()
        self.plan_response = None

    def write_policy(self):
        self.policy_path.write_text(json.dumps(self.policy))

    def start(self):
        self.write_policy()
        report = self.auto.start(self.root, self.policy_path)
        self.team_id = report["team_id"]
        return report

    def respond(self, request):
        if request["kind"] == "team-coordinator-request":
            return self.plan_response
        if request["kind"] == "team-recipient-request":
            return {"accept": True, "note": "Offline recipient inspected actual outputs and check records."}
        if request["kind"] == "team-evaluator-request":
            return {"result": "pass", "summary": "Independent offline review fixture", "findings": []}
        self.assertEqual(request["kind"], "team-host-request")
        context = request["task_context"]
        bundle = context.get("bundle", context)
        task_id = bundle["task"]["task_id"]
        changes = []
        if task_id == "implement":
            source = next(s for s in bundle["sources"] if s["path"] == "slug.py")
            changes = [{"path": "slug.py", "expected_sha256": source["sha256"], "new_content": fixture.fixture.FIXED}]
        elif task_id.startswith("write-"):
            changes = [{"path": task_id + ".txt", "expected_sha256": None, "new_content": task_id + "\n"}]
        return {"schema_version": "0.2", "step_id": request["team_assignment"]["request_id"], "task_id": task_id,
                "contract_digest": context["contract_digest"], "base_snapshot_digest": context["base_snapshot_digest"],
                "intent": "act" if changes else "request_verification", "criterion_ids": [c["id"] for c in bundle["task"]["criteria"]],
                "summary": "Offline fixture proposes a bounded change", "expected_observation": "Actual checks pass",
                "changes": changes, "evidence_refs": [], "blocker": None, "next_action": None}

    def test_continuous_team_reaches_verified_completion_without_manual_handoffs(self):
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertTrue(report["final_candidate_current"])
        self.assertEqual(report["team_budget"]["dispatches"], 4)
        self.assertEqual(report["team_budget"]["known_tokens"], 80)
        self.assertNotEqual(report["tasks"]["implement"]["workspace"], str(self.root))
        self.assertEqual((self.root / "slug.py").read_text(), fixture.fixture.FIXED)
        self.team.teams.audit(self.team_id)

    def test_recipient_receives_actual_bound_checks_outputs_and_phase_scope(self):
        respond = self.respond
        def inspect_actual_receipt(request):
            if request["kind"] != "team-recipient-request":
                return respond(request)
            scope, proof = request["phase_scope"], request["verification"]
            task = scope["task_contract"]
            self.assertEqual(scope["task_id"], task["task_id"])
            self.assertEqual(proof["candidate"], request["handoff"]["candidate"])
            self.assertEqual({c["id"] for c in task["checks"]}, {e["check_id"] for e in proof["checks"]})
            for evidence in proof["checks"]:
                self.assertEqual(evidence["result"], "pass")
                self.assertEqual(evidence["snapshot_digest"], proof["candidate"])
                self.assertEqual(evidence["environment_digest"], proof["environment"])
            self.assertTrue(any("OK" in item["output_tail"] for item in proof["observations"]))
            if task["task_id"] == "implement":
                self.assertEqual(proof["observed_changed_paths"], ["slug.py"])
                self.assertEqual(scope["downstream_tasks"][0]["id"], "verify")
                self.assertEqual(scope["downstream_tasks"][0]["status"], "PENDING")
            else:
                self.assertEqual(proof["observed_changed_paths"], [])
                self.assertEqual(scope["downstream_tasks"], [])
            return {"accept": True, "note": "Actual phase-scoped check records and output excerpts inspected"}
        self.respond = inspect_actual_receipt
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(report["team_budget"]["reworks"], 0)

    def test_rejection_automatically_repairs_and_invalidates_downstream_receipts(self):
        self.plan["tasks"][1]["repair_task"] = "implement"
        self.fixture.save_plan()
        self.transport.reject_once = True
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(report["team_budget"]["reworks"], 1)
        self.assertGreater(report["team_budget"]["dispatches"], 4)
        data = self.team.teams.get(self.team_id)
        self.assertEqual(len(data["automation"]["history"]), 2)
        steps = [r for r in self.transport.requests if r["kind"] == "team-host-request"]
        self.assertTrue(any(r["team_assignment"]["repair_feedback"] for r in steps))
        self.assertEqual(load(self.root / ".loop/tasks/implement.json")["checks"], load(self.root / ".loop/tasks/verify.json")["checks"])

    def test_shared_spend_stops_before_an_unaffordable_response(self):
        self.policy.update(max_tokens=150, max_cost_microunits=150)
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED", report["reason"])
        self.assertEqual(len(self.transport.requests), 3)
        self.assertEqual(report["team_budget"]["known_tokens"], 60)
        self.assertNotEqual(report["tasks"]["verify"]["status"], "COMPLETE")

    def test_missing_feasible_handoff_output_repairs_then_rechecks_downstream(self):
        path = self.root / ".loop/tasks/implement.json"
        contract = load(path); contract["scope"]["write_allow"].append("report.md")
        path.write_text(json.dumps(contract))
        for task in self.fixture.plan["tasks"]:
            task["outputs"].append("report.md")
        self.fixture.save_plan()
        respond = self.respond
        def produce_report_on_repair(request):
            response = respond(request)
            if (request["kind"] == "team-host-request" and response["task_id"] == "implement"
                    and request["team_assignment"]["repair_feedback"]):
                response["changes"].append({"path": "report.md", "expected_sha256": None,
                                            "new_content": "Actual offline fixture output\n"})
            return response
        self.respond = produce_report_on_repair
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(report["team_budget"]["reworks"], 1)
        self.assertTrue(report["final_candidate_current"])
        self.assertEqual((self.root / "report.md").read_text(), "Actual offline fixture output\n")
        requests = [r for r in self.transport.requests if r["kind"] == "team-host-request"
                    and r["team_assignment"]["task_id"] == "implement"]
        self.assertEqual(len(requests), 2)
        self.assertIn("output is missing", requests[1]["team_assignment"]["repair_feedback"][-1]["finding"])
        self.assertEqual(report["tasks"]["verify"]["status"], "COMPLETE")

    def invalid_proposals(self, *, once=False):
        respond = self.respond
        rejected = False
        def fixture_response(request):
            nonlocal rejected
            response = respond(request)
            if (request["kind"] == "team-host-request" and response["changes"]
                    and (not once or not rejected)):
                response["intent"] = "request_verification"
                rejected = True
            return response
        self.respond = fixture_response

    def test_invalid_proposal_gets_fresh_feedback_without_effects_or_child_reset(self):
        self.invalid_proposals(once=True)
        original = (self.root / "slug.py").read_bytes()
        self.start()
        first = self.auto.run(self.team_id, max_cycles=1)
        self.assertEqual(first["status"], "ACTIVE", first["reason"])
        self.assertEqual((self.root / "slug.py").read_bytes(), original)
        child_id = first["tasks"]["implement"]["child_run_id"]
        self.assertEqual(self.team.store.get(child_id)["state"]["iteration"], 1)
        final = self.auto.run(self.team_id)
        self.assertEqual(final["status"], "COMPLETE", final["reason"])
        self.assertEqual(final["tasks"]["implement"]["child_run_id"], child_id)
        self.assertEqual(self.team.store.get(child_id)["state"]["iteration"], 2)
        self.assertEqual(final["team_budget"]["reworks"], 1)
        proposals = [r for r in self.transport.requests if r["kind"] == "team-host-request"
                     and r["team_assignment"]["task_id"] == "implement"]
        self.assertEqual(len(proposals), 2)
        self.assertNotEqual(proposals[0]["team_assignment"]["request_id"], proposals[1]["team_assignment"]["request_id"])
        self.assertIn("Only an act", proposals[1]["team_assignment"]["repair_feedback"][-1]["finding"])
        data = self.team.teams.get(self.team_id)
        self.assertEqual(len(data["records"]["implement"]["rejected_proposals"]), 1)
        self.team.teams.audit(self.team_id)

    def test_repeated_invalid_proposals_exhaust_shared_repair_before_effects(self):
        self.policy["max_reworks"] = 1
        self.invalid_proposals()
        original = (self.root / "slug.py").read_bytes()
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED", report["reason"])
        self.assertEqual(report["team_budget"]["dispatches"], 2)
        self.assertEqual(report["team_budget"]["reworks"], 1)
        self.assertEqual((self.root / "slug.py").read_bytes(), original)

    def test_invalid_proposal_cannot_reset_its_child_iteration_limit(self):
        path = self.root / ".loop/tasks/implement.json"
        task = load(path); task["limits"]["max_iterations"] = 1
        path.write_text(json.dumps(task))
        self.invalid_proposals()
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED", report["reason"])
        self.assertEqual(len(self.transport.requests), 1)
        child = self.team.store.get(report["tasks"]["implement"]["child_run_id"])
        self.assertEqual(child["state"]["iteration"], 1)

    def test_unknown_usage_holds_reservations_and_resume_does_not_reset_budget(self):
        self.policy.update(max_tokens=140)
        self.transport.unknown = True
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(report["team_budget"]["tokens_upper"], 110)
        self.assertEqual(report["team_budget"]["unknown_requests"], 1)
        before = deepcopy(report["team_budget"])
        self.auto.run(self.team_id)
        self.assertEqual(self.team.status(self.team_id)["team_budget"]["dispatches"], before["dispatches"])

    def test_unbounded_command_refuses_hard_spend_before_process_creation(self):
        self.policy["max_tokens"] = 1000
        self.start()
        host = TeamHost(self.team, driver=CommandDriver([sys.executable, "-c", "print('{}')"]))
        with patch("subprocess.Popen", side_effect=AssertionError("No unsupported hard-cap command")):
            report = TeamAutomation(self.team, host).run(self.team_id)
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")
        self.assertIsNone(report["team_budget"]["actual_tokens"] if report["team_budget"]["unknown_requests"] else None)

    def test_dispatch_and_rework_limits_are_cumulative(self):
        self.policy.update(max_dispatches=2)
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(report["team_budget"]["dispatches"], 2)

    def test_bound_violation_blocks_effects_and_later_dispatch(self):
        self.transport.over_bound = True
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")
        self.assertTrue(report["team_budget"]["violated_bound"])
        self.assertNotEqual((self.root / "slug.py").read_text(), fixture.fixture.FIXED)

    def parallel_workflow(self):
        original = load(self.root / ".loop/tasks/implement.json")
        tasks = []
        for suffix in ("a", "b"):
            task_id = "write-" + suffix
            task = deepcopy(original)
            task["task_id"] = task_id
            task["scope"]["write_allow"] = [task_id + ".txt"]
            task["checks"] = [{"id": "content", "type": "command", "description": "Check actual artifact content",
                "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('" + task_id + ".txt').read_text() == '" + task_id + "\\n'"], "timeout_seconds": 5}]
            task["criteria"] = [{"id": "written", "description": "Required artifact exists with exact content", "check_ids": ["content"]}]
            self.fixture.fixture.write(f".loop/tasks/{task_id}.json", task)
            tasks.append({"id": task_id, "phase": "implement", "role": "backend", "task_file": f".loop/tasks/{task_id}.json",
                          "engine_file": None, "depends_on": [], "outputs": [task_id + ".txt"], "handoff_to": ["coordinator"]})
        final = deepcopy(original)
        final.update(task_id="verify", checks=[load(self.root / f'.loop/tasks/{task["id"]}.json')["checks"][0] | {"id": task["id"]} for task in tasks],
                     criteria=[{"id":"both","description":"Integrated outputs both pass", "check_ids":[t["id"] for t in tasks]}])
        self.fixture.fixture.write(".loop/tasks/verify.json", final)
        self.plan["tasks"] = tasks + [self.plan["tasks"][1]]
        self.plan["tasks"][-1]["outputs"] = ["write-a.txt", "write-b.txt"]
        self.fixture.save_plan()

    def test_parallel_workers_use_distinct_roots_and_integrate_real_checks(self):
        self.parallel_workflow()
        # Require actual overlapping dispatch without assuming a fast machine.
        rendezvous = threading.Barrier(2)
        respond = self.respond
        def parallel_response(request):
            if request["kind"] == "team-host-request" and request["team_assignment"]["task_id"] in {"write-a", "write-b"}:
                rendezvous.wait(timeout=10)
            return respond(request)
        self.respond = parallel_response
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertNotEqual(report["tasks"]["write-a"]["workspace"], report["tasks"]["write-b"]["workspace"])
        self.assertEqual((self.root / "write-a.txt").read_text(), "write-a\n")
        self.assertEqual((self.root / "write-b.txt").read_text(), "write-b\n")
        self.assertGreaterEqual(self.transport.peak, 2)

    def test_integrated_regression_repairs_a_parallel_result_and_rechecks_acceptance(self):
        self.parallel_workflow()
        contract = load(self.root / ".loop/tasks/write-b.json")
        contract["checks"][0]["argv"] = [sys.executable, "-c",
            "from pathlib import Path; assert Path('write-b.txt').read_text() == ('paired\\n' if Path('write-a.txt').exists() else 'write-b\\n')"]
        self.fixture.fixture.write(".loop/tasks/write-b.json", contract)
        final = load(self.root / ".loop/tasks/verify.json")
        final["checks"][1] = contract["checks"][0] | {"id": "write-b"}
        self.fixture.fixture.write(".loop/tasks/verify.json", final)
        original = self.respond
        def respond(request):
            response = original(request)
            if request["kind"] == "team-host-request" and request["team_assignment"]["task_id"] == "write-b":
                bundle = request["task_context"].get("bundle", request["task_context"])
                source = next((s for s in bundle["sources"] if s["path"] == "write-b.txt"), None)
                response["changes"][0]["expected_sha256"] = source["sha256"] if source else None
                if request["team_assignment"]["repair_feedback"]:
                    response["changes"][0]["new_content"] = "paired\n"
            return response
        self.respond = respond
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(report["team_budget"]["reworks"], 1)
        self.assertEqual((self.root / "write-b.txt").read_text(), "paired\n")

    def coordinator_response(self):
        selected = load(self.root / ".loop/team.json")
        contracts = [{"path": t["task_file"], "task_json": json.dumps(load(self.root / t["task_file"])),
                      "engine_path": t["engine_file"], "engine_json": None} for t in self.plan["tasks"]]
        self.plan_response = {"selection_json": json.dumps(selected), "workflow_json": json.dumps(self.plan),
                              "summary": "Prepared scoped library workflow in an offline fixture", "contracts": contracts, "questions": []}
        self.fixture.fixture.write(".loop/workflow-tasks.json", load(ROOT / "templates/scenarios/development/workflow-tasks.json"))

    def test_coordinator_prepares_validated_contracts_from_spec_then_runs_the_team(self):
        self.coordinator_response()
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(self.transport.requests[0]["kind"], "team-coordinator-request")
        self.assertEqual(report["team_budget"]["dispatches"], 5)

    def test_coordinator_clarifications_wait_for_actual_human_answers(self):
        self.coordinator_response()
        self.plan_response["questions"] = [{"id":"compatibility", "question":"Preserve nonempty behavior?", "reason":"Define compatibility", "blocking":True}]
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "AWAITING_INPUT", report["reason"])
        questions = load(self.root / ".loop/questions.json")
        self.assertEqual(questions["questions"][0]["answers"], [])
        self.auto.answer(self.team_id, "compatibility", "Yes, preserve current supported behavior.")
        self.plan_response["questions"] = []
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(load(self.root / ".loop/questions.json")["questions"][0]["answers"], ["Yes, preserve current supported behavior."])

    def test_invalid_coordinator_plan_does_not_write_partial_contracts(self):
        self.policy["max_reworks"] = 0
        self.coordinator_response()
        value = json.loads(self.plan_response["workflow_json"])
        value["tasks"][0]["role"] = "frontend"
        self.plan_response["workflow_json"] = json.dumps(value)
        original = (self.root / ".loop/team.json").read_bytes()
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")
        self.assertEqual((self.root / ".loop/team.json").read_bytes(), original)
        self.assertEqual(load(self.root / ".loop/workflow-tasks.json")["tasks"], [])

    def test_coordinator_corrects_invalid_preparation_with_feedback_and_shared_budget(self):
        self.coordinator_response()
        valid = deepcopy(self.plan_response)
        selection = json.loads(self.plan_response["selection_json"])
        next(role for role in selection["decisions"] if role["role"] == "reviewer").update(state="pending", assignee=None, handoff_to=[])
        self.plan_response["selection_json"] = json.dumps(selection)
        original = self.respond
        def response(request):
            if request["kind"] == "team-coordinator-request" and request.get("preparation_feedback"):
                self.assertIn("reviewer", request["preparation_feedback"][-1]["error"])
                self.assertIn("separate fresh requests", request["execution_contract"])
                return valid
            return original(request)
        self.respond = response
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(report["team_budget"]["reworks"], 1)
        self.assertEqual(report["team_budget"]["dispatches"], 6)
        self.assertEqual(len([r for r in self.transport.requests if r["kind"] == "team-coordinator-request"]), 2)

    def test_preparation_supplies_actual_independent_review_plumbing_without_engine_json(self):
        self.plan["tasks"][1].update(role="acceptance", phase="acceptance", repair_task="implement")
        self.fixture.save_plan()
        original = load(self.root / ".loop/tasks/verify.json")
        self.coordinator_response()
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        prepared = load(self.root / ".loop/tasks/verify.json")
        self.assertEqual(prepared["checks"][:-1], original["checks"])
        self.assertEqual(prepared["criteria"][:-1], original["criteria"])
        self.assertTrue(prepared["checks"][-1]["independent"])
        config = load(self.root / report["tasks"]["verify"].get("engine_file", ".loop/tasks/verify-engine.json"))
        self.assertTrue(config["evaluator_keys"])
        self.assertEqual(len([r for r in self.transport.requests if r["kind"] == "team-evaluator-request"]), 2)

    def test_preparation_preserves_required_human_checks_when_hydrating_review(self):
        self.plan["tasks"][1].update(role="acceptance", phase="acceptance", repair_task="implement")
        self.fixture.save_plan()
        task = load(self.root / ".loop/tasks/verify.json")
        task["checks"].append({"id":"human", "type":"human", "description":"Actual required human assessment", "procedure":["Inspect the candidate"]})
        task["criteria"].append({"id":"human", "description":"Actual human acceptance required", "check_ids":["human"]})
        self.fixture.fixture.write(".loop/tasks/verify.json", task)
        self.coordinator_response()
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "AWAITING_INPUT", report["reason"])
        self.assertIn("human evaluator", report["reason"])
        self.assertIn(task["checks"][-1], load(self.root / ".loop/tasks/verify.json")["checks"])

    def native_acceptance(self, *, human=False):
        self.plan["tasks"][1].update(phase="acceptance", role="acceptance", engine_file=".loop/tasks/acceptance-engine.json", repair_task="implement")
        contract = load(self.root / ".loop/tasks/verify.json")
        check = {"id":"independent", "type":"human" if human else "review", "description":"Actual required assessment", "procedure":["Inspect actual slug checks and implementation"]}
        if not human:
            check["independent"] = True
        contract["checks"].append(check)
        contract["criteria"].append({"id":"assessment","description":"Independent required assessment passes", "check_ids":["independent"]})
        self.fixture.fixture.write(".loop/tasks/verify.json", contract)
        config = load(ROOT / "templates/engine.json")
        config["evaluator_keys"] = [{"key_id":"acceptance-fixture", "role":"human" if human else "reviewer", "check_ids":["independent"]}]
        self.team.store.authorities.create("acceptance-fixture", "human" if human else "reviewer")
        self.fixture.fixture.write(".loop/tasks/acceptance-engine.json", config)
        self.fixture.save_plan()

    def test_independent_review_is_performed_and_signed_for_branch_and_integrated_candidate(self):
        self.native_acceptance()
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        evaluations = [r for r in self.transport.requests if r["kind"] == "team-evaluator-request"]
        self.assertEqual(len(evaluations), 2)
        self.assertNotEqual(evaluations[0]["request"]["context_id"], evaluations[1]["request"]["context_id"])
        child = self.team.store.get(report["tasks"]["verify"]["integration_run_id"])
        self.assertTrue(child["native"]["external_origins"])
        self.assertTrue(report["final_candidate_current"])

    def test_review_only_task_receives_original_code_and_actual_dependency_checks(self):
        self.native_acceptance()
        path = self.root / ".loop/tasks/verify.json"
        task = load(path)
        task["checks"] = [c for c in task["checks"] if c["id"] == "independent"]
        for criterion in task["criteria"]:
            criterion["check_ids"] = ["independent"]
        path.write_text(json.dumps(task))
        original = (self.root / "slug.py").read_text()
        respond = self.respond
        def inspect_comparison(request):
            if request["kind"] == "team-evaluator-request":
                material = request["comparison_and_checks"]
                self.assertEqual(material["observed_changed_paths"], ["slug.py"])
                source = next(s for s in material["original_changed_sources"] if s["path"] == "slug.py")
                self.assertEqual(source["content"], original)
                self.assertFalse(source["truncated"])
                predecessor = next(p for p in material["verified_dependencies"] if p["task_id"] == "implement")
                self.assertEqual(predecessor["candidate"], request["request"]["snapshot_digest"])
                self.assertTrue(all(e["result"] == "pass" and e["snapshot_digest"] == predecessor["candidate"]
                                    for e in predecessor["checks"]))
                self.assertTrue(any("OK" in o["output_tail"] for o in predecessor["observations"]))
            return respond(request)
        self.respond = inspect_comparison
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(len([r for r in self.transport.requests if r["kind"] == "team-evaluator-request"]), 2)

    def test_independent_rejection_dispatches_repair_then_fresh_acceptance(self):
        self.native_acceptance()
        self.transport.review_reject_once = True
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(report["team_budget"]["reworks"], 1)
        self.assertGreater(len([r for r in self.transport.requests if r["kind"] == "team-evaluator-request"]), 2)

    def test_review_response_survives_crash_before_import_without_another_dispatch(self):
        from loop_engineering.native_engine import NativeController
        self.native_acceptance()
        self.start()
        with patch.object(NativeController, "import_evaluation", side_effect=RuntimeError("Injected import gap")), self.assertRaises(RuntimeError):
            self.auto.run(self.team_id)
        self.assertEqual(len([r for r in self.transport.requests if r["kind"] == "team-evaluator-request"]), 1)
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(len([r for r in self.transport.requests if r["kind"] == "team-evaluator-request"]), 2)

    def test_required_human_evidence_stays_pending(self):
        # Independent role also requires a review check; retain it alongside
        # the human check so admission cannot be weakened for this fixture.
        self.native_acceptance(human=True)
        contract = load(self.root / ".loop/tasks/verify.json")
        contract["checks"].append({"id":"review","type":"review","independent":True,"description":"Separate review", "procedure":["Inspect requirements"]})
        contract["criteria"].append({"id":"review","description":"Review passes", "check_ids":["review"]})
        self.fixture.fixture.write(".loop/tasks/verify.json", contract)
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "AWAITING_INPUT", report["reason"])
        self.assertIn("human evaluator", report["reason"])
        self.assertNotEqual(report["tasks"]["verify"]["status"], "COMPLETE")

    def test_ambiguous_dispatch_cannot_be_retried_until_reconciliation(self):
        self.start()
        original = self.transport.post
        def crash(operation, payload, **kwargs):
            if operation == "respond":
                raise RuntimeError("Injected ambiguous response crash")
            return original(operation, payload, **kwargs)
        with patch.object(self.transport, "post", crash), self.assertRaises(RuntimeError):
            self.auto.run(self.team_id)
        before = self.team.status(self.team_id)["team_budget"]["dispatches"]
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "AWAITING_INPUT")
        self.assertEqual(report["team_budget"]["dispatches"], before)
        data = self.team.teams.get(self.team_id)
        operation_id = next(iter(data["automation"]["operations"]))
        self.auto.reconcile(self.team_id, operation_id, "Offline injected transport crashed before any real provider or child process existed.")
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(report["team_budget"]["unknown_requests"], 1)

    def test_crash_after_merge_application_reuses_integration_identity_and_budget(self):
        self.start()
        original = self.team.snapshots.apply_prepared
        crashed = False
        def crash(*args, **kwargs):
            nonlocal crashed
            original(*args, **kwargs)
            if not crashed:
                crashed = True
                raise RuntimeError("Injected crash after merge write")
        with patch.object(self.team.snapshots, "apply_prepared", crash), self.assertRaises(RuntimeError):
            self.auto.run(self.team_id)
        data = self.team.teams.get(self.team_id)
        run_id = data["records"]["implement"]["integration_run_id"]
        spent = len(data["automation"]["operations"])
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertEqual(report["tasks"]["implement"]["integration_run_id"], run_id)
        self.assertGreaterEqual(report["team_budget"]["dispatches"], spent)

    def test_same_path_parallel_merge_conflict_preserves_the_integrated_candidate(self):
        self.start()
        context = self.team.request(self.team_id, "implement", managed=True)
        data = self.team.teams.get(self.team_id)
        step = self.respond(context)
        self.team.submit(self.team_id, "implement", context["team_assignment"]["request_id"], step, collect=False, managed=True)
        (self.root / "slug.py").write_text("User's concurrent edit\n")
        with self.assertRaisesRegex(ContractError, "precondition"):
            self.team.collect(self.team_id, "implement")
        self.assertEqual((self.root / "slug.py").read_text(), "User's concurrent edit\n")
        self.assertEqual(self.team.status(self.team_id)["tasks"]["implement"]["status"], "RUNNING")

    def test_command_host_runs_actual_receipt_process_and_records_unknown_usage(self):
        self.start()
        code = "import json,sys; r=json.load(sys.stdin); assert r['kind']=='team-recipient-request'; print(json.dumps({'accept':True,'note':'Actual offline subprocess inspected the supplied fixture.'}))"
        host = TeamHost(self.team, driver=CommandDriver([sys.executable, "-c", code]))
        response = host.invoke(self.team_id, {"kind":"team-recipient-request"}, RECEIPT_RESPONSE)
        self.assertTrue(response["accept"])
        self.assertEqual(self.team.status(self.team_id)["team_budget"]["unknown_requests"], 1)

    def test_invalid_host_response_retains_actual_spend_and_allows_inspected_recovery(self):
        self.start()
        self.respond = lambda request: {"accept": True}
        with self.assertRaisesRegex(ContractError, "invalid structured response"):
            self.host.invoke(self.team_id, {"kind": "team-recipient-request"}, RECEIPT_RESPONSE)
        data = self.team.teams.get(self.team_id)
        self.assertEqual(next(iter(data["automation"]["operations"].values()))["status"], "FAILED")
        self.assertEqual(self.team.status(self.team_id)["team_budget"]["known_tokens"], 20)
        self.assertEqual(self.team.status(self.team_id)["team_budget"]["unknown_requests"], 0)

    def test_paused_and_cancelled_teams_do_not_dispatch_new_workers(self):
        self.start()
        self.team.stop(self.team_id)
        self.auto.run(self.team_id)
        self.assertEqual(self.transport.requests, [])
        self.team.resume(self.team_id)
        self.team.stop(self.team_id, cancel=True)
        self.auto.run(self.team_id)
        self.assertEqual(self.transport.requests, [])

    def test_manual_worker_routes_cannot_bypass_automatic_team_accounting(self):
        self.start()
        with self.assertRaisesRegex(ContractError, "shared budget"):
            self.team.request(self.team_id, "implement")
        with self.assertRaisesRegex(ContractError, "accounted"):
            self.team.drive(self.team_id, "implement", CommandDriver([sys.executable, "-c", "print('{}')"]))

    def test_missing_host_capability_refuses_before_any_actual_process(self):
        task = load(self.root / ".loop/tasks/implement.json")
        task["required_capabilities"]["agent"].append("structured_output")
        self.fixture.fixture.write(".loop/tasks/implement.json", task)
        self.start()
        host = TeamHost(self.team, driver=CommandDriver([sys.executable, "-c", "print('{}')"]))
        with patch("loop_engineering.processes.subprocess.Popen", side_effect=AssertionError("Dispatch must be refused")):
            report = TeamAutomation(self.team, host).run(self.team_id)
        self.assertEqual(report["status"], "AWAITING_INPUT")
        self.assertIn("lacks required capabilities", report["reason"])

    def test_child_iteration_limit_blocks_another_response_before_dispatch(self):
        task = load(self.root / ".loop/tasks/implement.json")
        task["limits"]["max_iterations"] = 1
        self.fixture.fixture.write(".loop/tasks/implement.json", task)
        original = self.respond
        def no_fix(request):
            result = original(request)
            if request["kind"] == "team-host-request":
                result.update(changes=[], intent="request_verification")
            return result
        self.respond = no_fix
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED", report["reason"])
        self.assertEqual(len(self.transport.requests), 1)

    def test_rework_limit_preserves_rejected_state_and_existing_contracts(self):
        self.policy["max_reworks"] = 0
        self.transport.reject_once = True
        before = (self.root / ".loop/tasks/implement.json").read_bytes()
        self.start()
        report = self.auto.run(self.team_id)
        self.assertEqual(report["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(report["tasks"]["implement"]["status"], "REJECTED")
        self.assertEqual((self.root / ".loop/tasks/implement.json").read_bytes(), before)

    def test_team_deadline_cancels_a_running_actual_process(self):
        self.policy.update(max_wall_seconds=60, timeout_seconds=10)
        self.start()
        host = TeamHost(self.team, driver=CommandDriver([sys.executable, "-c", "import time; time.sleep(20)"]))
        started = []
        def expire_after_start(*args, **kwargs):
            callback = kwargs['on_started']
            def expire(pid):
                callback(pid)
                started.append(pid)
                with self.team.teams.writer(self.team_id, wait_seconds=5) as data:
                    # Deterministic expiry after actual process admission; CPU
                    # load must not turn this cancellation case into preflight.
                    data['automation']['started_ms'] = time.time_ns()//1000000-61000
                    self.team.teams.save(data, 'fixture.deadline_elapsed_after_process_start')
            kwargs['on_started'] = expire
            return execute(*args, **kwargs)
        before = time.monotonic()
        with patch('loop_engineering.team_host.execute', side_effect=expire_after_start):
            report = TeamAutomation(self.team, host).run(self.team_id)
        self.assertLess(time.monotonic() - before, 8)
        self.assertEqual(len(started), 1)
        self.assertFalse(process_alive(started[0]))
        self.assertEqual(report["status"], "CANCELLED")
        self.assertEqual(report["team_budget"]["remaining_wall_seconds"], 0)
        self.assertTrue(all(row.get("pid") is None for row in self.team.teams.get(self.team_id)["automation"]["operations"].values()))

    def test_cli_team_run_continues_same_batch_without_implicit_live_dispatch(self):
        self.start()
        fixture_code = '''import json,sys
r=json.load(sys.stdin)
if r['kind']=='team-recipient-request':
 print(json.dumps({'accept':True,'note':'Offline command fixture inspected handoff'}))
else:
 c=r['task_context']; t=c.get('bundle',c); changes=[]
 if t['task']['task_id']=='implement':
  s=next(s for s in t['sources'] if s['path']=='slug.py')
  changes=[{'path':'slug.py','expected_sha256':s['sha256'],'new_content':FIXED}]
 print(json.dumps({'schema_version':'0.2','step_id':r['team_assignment']['request_id'],'task_id':t['task']['task_id'],
  'contract_digest':c['contract_digest'],'base_snapshot_digest':c['base_snapshot_digest'],'intent':'act' if changes else 'request_verification',
  'criterion_ids':[v['id'] for v in t['task']['criteria']],'summary':'Offline proposal','expected_observation':'Actual slug checks pass',
  'changes':changes,'evidence_refs':[],'blocker':None,'next_action':None}))
'''.replace('FIXED', repr(fixture.fixture.FIXED))
        code, report, errors = self.fixture.fixture.cli(['team-run','--team-id',self.team_id,'--state-dir',str(self.team.store.directory),
            '--adapter','command','--argv',json.dumps([sys.executable,'-c',fixture_code])])
        self.assertEqual(code, 0, errors)
        self.assertEqual(report['status'], 'COMPLETE', report['reason'])
        self.assertEqual(report['team_id'], self.team_id)


if __name__ == "__main__":
    unittest.main()
