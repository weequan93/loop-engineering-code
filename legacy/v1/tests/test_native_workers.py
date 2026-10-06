"""Professional native work is parallel, observable and collected through real checks.

All agents in this module are declared offline host fixtures. No model, provider
or actual native coding agent is started by the service or by these tests.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, ROOT, load
from loop_engineering.controller import Controller
from loop_engineering.evaluators import sign_result
from loop_engineering.native_contracts import validate_context
from loop_engineering.native_engine import NativeController
from loop_engineering.native_host import NativeHostService, render_progress
from loop_engineering.team_memory import byte_size
from loop_engineering.workspace import byte_digest
from tests import test_native_workbench as fixture


class NativeWorkerTests(unittest.TestCase):
    start = fixture.NativeWorkbenchTests.start
    prepare = fixture.NativeWorkbenchTests.prepare
    edit = fixture.NativeWorkbenchTests.edit
    submit = fixture.NativeWorkbenchTests.submit
    receive = fixture.NativeWorkbenchTests.receive
    complete = fixture.NativeWorkbenchTests.complete

    def setUp(self):
        fixture.NativeWorkbenchTests.setUp(self)
        self.addCleanup(lambda: self.assertEqual(self.owner.transport.requests, []))

    def allow_second_file(self):
        path = self.root / ".loop/tasks/implement.json"
        task = load(path)
        task["scope"]["write_allow"].append("worker-result.txt")
        task["checks"].append({"id": "worker-artifact", "type": "command",
            "description": "The actual second professional worker artifact is integrated.",
            "argv": [sys.executable, "-c", "from pathlib import Path; assert Path('worker-result.txt').read_text() == 'actual parallel worker\\n'"],
            "timeout_seconds": 5})
        task["criteria"].append({"id": "parallel-artifact", "description": "Retain the second worker result.",
            "check_ids": ["worker-artifact"]})
        path.write_text(json.dumps(task))

    def assign(self, assignment_id="backend-implementation", *, role="backend", write_paths=None,
               title="Implement the scoped library fix", goal="Return an empty slug for whitespace.", depends_on=None):
        return self.service.worker_assign(self.team_id, "implement", self.workbench["id"], assignment_id,
            role, title, goal, ["slug.py"] if write_paths is None else write_paths,
            [] if depends_on is None else depends_on)

    def row(self, worker, service=None):
        return next(r for r in (service or self.service).worker_board(self.team_id)["workers"] if r["id"] == worker["id"])

    def bind(self, worker, agent_id=None):
        return self.service.worker_update(self.team_id, worker["id"], agent_id or "fixture:" + worker["id"],
            "RUNNING", "Offline host reports the actual worker ID after dispatch.")

    def mark(self, worker, status, detail="Offline host collected an actual stable result."):
        return self.service.worker_update(self.team_id, worker["id"], self.row(worker).get("agent_id"), status, detail)

    def collect(self, worker):
        return self.service.worker_collect(self.team_id, worker["id"], "Inspected actual scoped files in the stopped worker copy.")

    def edit_worker(self, worker, packet):
        change = self.owner.respond(packet)["changes"][0]
        (Path(worker["directory"]) / change["path"]).write_text(change["new_content"])

    def native_context_fixture(self, *, many_sources=False):
        """An actual native child and signed current finding, with no model call."""
        engine_path = ".loop/native-context-engine.json"
        config = load(ROOT / "templates/engine.json")
        config["model"]["model"] = "offline-native-context-fixture"
        config["evaluator_keys"] = [{"key_id": "native-context-fixture-reviewer", "role": "reviewer", "check_ids": ["context-review"]}]
        self.store.authorities.create("native-context-fixture-reviewer", "reviewer")
        task_path = self.root / ".loop/tasks/implement.json"
        task = load(task_path)
        task["checks"].append({"id": "context-review", "type": "review", "independent": True,
            "description": "Keep the actual current fixture review finding in every native briefing.",
            "procedure": ["Inspect the exact candidate and the real local slug results."]})
        task["criteria"].append({"id": "context-reviewed", "description": "The independent fixture review is satisfied.",
            "check_ids": ["context-review"]})
        task_path.write_text(json.dumps(task))
        (self.root / engine_path).write_text(json.dumps(config))
        self.owner.plan["tasks"][0]["engine_file"] = engine_path
        self.owner.fixture.save_plan()
        if many_sources:
            folder = self.root / "optional_sources"
            folder.mkdir()
            for n in range(40):
                (folder / f"source_{n:02}.py").write_text("# Actual optional source fixture.\n" + "# " + "optional source text " * 280 + "\n")
        self.start()
        packet = self.service.task_request(self.team_id, "implement")
        self.service.task_submit(self.team_id, "implement", packet["team_assignment"]["request_id"], self.owner.respond(packet))
        data = self.service.team.teams.get(self.team_id)
        run_id = data["records"]["implement"]["child_run_id"]
        controller = self.service.team._controller(data, "implement")
        self.assertIsInstance(controller, NativeController)
        request = controller.evaluation_request(run_id, "context-review")
        artifact = self.owner.base / "native-context-review.txt"
        artifact.write_text("The deterministic reviewer inspected this exact candidate and real local results.\n")
        finding = {"id": "native-current-finding", "severity": "blocking",
            "description": "Actual fixture review requests a compatibility correction; preserve this current finding." * (220 if many_sources else 1),
            "criterion_id": "preserve-behavior"}
        signed = sign_result(self.store.authorities, request, "native-context-fixture-reviewer", result="fail",
            summary="Deterministic independent fixture finding", artifacts=[artifact], findings=[finding])
        controller.import_evaluation(run_id, signed)
        controller.resume(run_id)
        context = controller.context(run_id)
        self.assertTrue(context["pending_findings"][0]["current"])
        self.assertEqual(context["pending_findings"][0]["finding"], finding)
        validate_context(context)
        return context

    def nested_near_limit(self, packet, *, reserve=64):
        padded = deepcopy(packet)
        bundle = padded["task_context"]["bundle"]
        source = {"path": "large-optional-nested-source.txt", "sha256": "sha256:" + "a" * 64, "content": ""}
        bundle["sources"].append(source)
        limit = load(self.root / ".loop/project.json")["context_max_bytes"]
        remaining = limit - reserve - byte_size(padded)
        self.assertGreater(remaining, 0)
        source["content"] = "n" * remaining
        source["sha256"] = byte_digest(source["content"].encode())
        self.assertEqual(byte_size(padded), limit - reserve)
        validate_context(padded["task_context"])
        return padded, source, limit

    def assert_native_mandatory_preserved(self, before, after):
        for key in ("contract_digest", "base_snapshot_digest", "repository_instructions", "pending_findings",
                    "active_stage", "runtime", "budget"):
            self.assertEqual(after[key], before[key], key)
        for key in ("task", "state", "contract_digest", "base_snapshot_digest", "instructions", "failure_policy"):
            self.assertEqual(after["bundle"][key], before["bundle"][key], key)
        self.assertTrue(after["pending_findings"][0]["current"])
        validate_context(after)

    def assert_nested_source_omissions(self, before, after):
        old, new = before["bundle"], after["bundle"]
        removed = {s["path"] for s in old["sources"]} - {s["path"] for s in new["sources"]}
        self.assertTrue(removed)
        self.assertEqual(new["context_policy"]["omitted_source_count"], old["context_policy"]["omitted_source_count"] + len(removed))
        self.assertTrue(removed.issubset(set(new["omitted_paths"])))

    def assert_current_failure_has_authorized_repair_packet(self, packet, context):
        action = packet["next_action"]
        task = context["bundle"]["task"]
        self.assertEqual(action["kind"], "review_failed")
        self.assertEqual(action["check_id"], "context-review")
        self.assertEqual(action["repair_task"], "implement")
        self.assertEqual(action["repair_owner"], "backend")
        self.assertEqual(action["repair_write_allow"], task["scope"]["write_allow"])
        self.assertEqual(action["repair_write_deny"], task["scope"]["write_deny"])
        self.assertFalse(action["repair_requires_reviewed_plan"])
        self.assertTrue(action["continue_work"])
        self.assertEqual(action["tool"], "loop_workbench_prepare")
        self.assertEqual(action["arguments"], {"team_id": self.team_id, "task_id": "implement"})
        self.assertEqual(packet["team_assignment"]["task_id"], "implement")
        self.assertEqual(packet["team_assignment"]["role"], "backend")
        data = self.service.team.teams.get(self.team_id)
        record = data["records"]["implement"]
        self.assertEqual(record["status"], "RUNNING")
        self.assertIsNone(record["handoff"])
        child = self.store.get(record["child_run_id"])
        self.assertEqual(child["state"]["iteration"], context["bundle"]["state"]["iteration"])
        self.assertEqual(child["task"], task)
        self.assertFalse(child["native"].get("execution_attempts", {}))
        digest = child["native"]["external_by_check"]["context-review"]
        evidence = self.store.evidence(child["run_id"], [digest])[0]
        self.assertEqual(evidence["result"], "fail")
        self.assertEqual(action["evidence_digest"], digest)
        self.assertEqual(action["findings"], evidence["extensions"]["findings"])
        self.assertEqual(action["findings"], [f["finding"] for f in context["pending_findings"] if f["current"]])
        self.assertEqual(evidence["snapshot_digest"], context["base_snapshot_digest"])
        self.assertTrue(self.service.team._controller(data, "implement")._origin(child, digest, evidence))

    def test_native_nested_actual_sources_fit_team_and_loop_next_without_losing_current_findings(self):
        self.native_context_fixture(many_sources=True)
        core_contexts, native_contexts = [], []
        original_core, original_native = Controller.context, NativeController.context
        def capture_core(controller, run_id):
            result = original_core(controller, run_id)
            if isinstance(controller, NativeController):
                core_contexts.append(deepcopy(result))
            return result
        def capture_native(controller, run_id):
            result = original_native(controller, run_id)
            native_contexts.append(deepcopy(result))
            return result
        with patch.object(Controller, "context", new=capture_core), patch.object(NativeController, "context", new=capture_native):
            packet = self.service.next(self.team_id)
        self.assertEqual(len(native_contexts), 1)
        native = native_contexts[0]
        self.assert_current_failure_has_authorized_repair_packet(packet, native)
        limit = load(self.root / ".loop/project.json")["context_max_bytes"]
        self.assertLessEqual(byte_size(packet), limit)
        self.assertTrue(any(s["path"].startswith("optional_sources/") for s in native["bundle"]["sources"]))
        self.assert_native_mandatory_preserved(native, packet["task_context"])
        self.assert_nested_source_omissions(native, packet["task_context"])
        stored = json.loads(Path(packet["workbench"]["context_path"]).read_bytes())
        self.assertLessEqual(Path(packet["workbench"]["context_path"]).stat().st_size, limit)
        self.assert_native_mandatory_preserved(native, stored["task_context"])
        # Sources promoted to mandatory repository instructions are retained,
        # while native-engine size trimming must record its own dropped paths.
        core = core_contexts[0]
        instruction_paths = {s["path"] for s in native["repository_instructions"]}
        dropped = ({s["path"] for s in core["sources"]} - instruction_paths -
                   {s["path"] for s in native["bundle"]["sources"]})
        self.assertTrue(dropped, "The actual native envelope must trim optional sources to retain its current finding.")
        self.assertEqual(native["bundle"]["context_policy"]["omitted_source_count"],
                         core["context_policy"]["omitted_source_count"] + len(dropped))
        self.assertTrue(dropped.issubset(set(native["bundle"]["omitted_paths"])))
        self.assertEqual(self.service.progress(self.team_id)["task_counts"]["COMPLETE"], 0)

    def test_native_nested_proposal_schema_and_final_host_wait_fit_frozen_limit(self):
        self.native_context_fixture()
        actual = self.service.team.request(self.team_id, "implement")
        padded, source, limit = self.nested_near_limit(actual)
        before = deepcopy(padded["task_context"])
        with patch.object(self.service.team, "request", return_value=padded):
            packet = self.service.task_request(self.team_id, "implement")
        self.assertLessEqual(byte_size(packet), limit)
        self.assertEqual(packet["response_schema"], load(ROOT / "schemas/step-v0.2.schema.json"))
        self.assertEqual(packet["host_wait"]["status"], "WAITING")
        self.assert_native_mandatory_preserved(before, packet["task_context"])
        self.assert_nested_source_omissions(before, packet["task_context"])
        self.assertIn(source["path"], packet["task_context"]["bundle"]["omitted_paths"])

    def test_native_nested_professional_briefing_trims_sources_without_dropping_native_rules(self):
        self.native_context_fixture()
        packet = self.service.workbench_prepare(self.team_id, "implement")
        self.workbench, self.draft = packet["workbench"], Path(packet["workbench"]["directory"])
        padded, source, limit = self.nested_near_limit(self.service.task_request(self.team_id, "implement"))
        before = deepcopy(padded["task_context"])
        with patch.object(self.service, "task_request", return_value=padded):
            worker = self.assign("native-architecture-advice", role="architect", write_paths=[])
        raw = Path(worker["context_path"]).read_bytes()
        context = json.loads(raw)
        self.assertLessEqual(len(raw), limit)
        self.assert_native_mandatory_preserved(before, context["task_context"])
        self.assert_nested_source_omissions(before, context["task_context"])
        self.assertIn(source["path"], context["task_context"]["bundle"]["omitted_paths"])
        self.assertEqual(context["professional"]["role"]["id"], "architect")
        for instruction in context["professional"]["instructions"]:
            self.assertEqual(instruction["content"], (self.root / instruction["path"]).read_text())

    def test_native_nested_workbench_and_loop_next_final_metadata_respect_the_same_frozen_limit(self):
        self.native_context_fixture()
        actual = self.service.workbench_prepare(self.team_id, "implement")
        padded, source, limit = self.nested_near_limit(actual)
        before = deepcopy(padded["task_context"])
        with patch.object(self.service, "task_request", return_value=padded):
            packet = self.service.next(self.team_id)
        self.assert_current_failure_has_authorized_repair_packet(packet, before)
        self.assertLessEqual(byte_size(packet), limit)
        self.assertIn("host_wait", packet)
        self.assertIn("workbench", packet)
        self.assertIn("next_action", packet)
        self.assertIn("dashboard_url", packet)
        self.assertEqual(packet["workbench"]["id"], actual["workbench"]["id"])
        self.assertEqual(packet["host_wait"]["deadline_epoch"], actual["host_wait"]["deadline_epoch"])
        self.assert_native_mandatory_preserved(before, packet["task_context"])
        self.assert_nested_source_omissions(before, packet["task_context"])
        self.assertIn(source["path"], packet["task_context"]["bundle"]["omitted_paths"])
        context_path = Path(packet["workbench"]["context_path"])
        self.assertLessEqual(context_path.stat().st_size, limit)
        self.assert_native_mandatory_preserved(before, json.loads(context_path.read_bytes())["task_context"])

    def test_native_nested_mandatory_overflow_fails_closed_at_every_enrichment_layer(self):
        self.native_context_fixture()
        packet = self.service.workbench_prepare(self.team_id, "implement")
        self.workbench, self.draft = packet["workbench"], Path(packet["workbench"]["directory"])
        oversized = deepcopy(self.service.task_request(self.team_id, "implement"))
        limit = load(self.root / ".loop/project.json")["context_max_bytes"]
        mandatory = "Mandatory repository rules. " * (limit // 20)
        oversized["task_context"]["repository_instructions"].append({"path": "mandatory-context-rules.md",
            "content": mandatory, "sha256": byte_digest(mandatory.encode())})
        validate_context(oversized["task_context"])
        data = self.service.team.teams.get(self.team_id)
        record = deepcopy(data["records"]["implement"])
        before = (self.root / "slug.py").read_bytes()
        with self.subTest(layer="team"), self.service.team.teams.writer(self.team_id) as current:
            with self.assertRaises(ContractError):
                self.service.team._decorate(current, "implement", deepcopy(oversized["task_context"]))
        with self.subTest(layer="proposal"), patch.object(self.service.team, "request", return_value=deepcopy(oversized)):
            with self.assertRaises(ContractError):
                self.service.task_request(self.team_id, "implement")
        with self.subTest(layer="professional"), patch.object(self.service, "task_request", return_value=deepcopy(oversized)):
            with self.assertRaises(ContractError):
                self.assign("impossible-mandatory-context", role="architect", write_paths=[])
        with self.subTest(layer="workbench"), patch.object(self.service, "task_request", return_value=deepcopy(oversized)):
            with self.assertRaises(ContractError):
                self.service.workbench_prepare(self.team_id, "implement")
        after = self.service.team.teams.get(self.team_id)["records"]["implement"]
        self.assertEqual(after["request"]["id"], record["request"]["id"])
        self.assertEqual(after["child_run_id"], record["child_run_id"])
        self.assertEqual((self.root / "slug.py").read_bytes(), before)
        self.assertEqual(self.service.worker_board(self.team_id)["workers"], [])

    def test_parallel_professionals_collect_actual_files_before_original_checks_and_final_completion(self):
        self.allow_second_file()
        packet = self.prepare()
        before = (self.root / "slug.py").read_bytes()
        first = self.assign()
        second = self.assign("tester-artifact", role="tester", write_paths=["worker-result.txt"],
            title="Prepare the second concrete delivery", goal="Write the agreed parallel worker artifact.")
        self.assertEqual(first["status"], "PREPARED")
        self.assertEqual(second["status"], "PREPARED")
        self.assertNotEqual(first["directory"], second["directory"])
        self.assertNotEqual(first["directory"], self.workbench["directory"])
        for worker in (first, second):
            self.assertTrue(Path(worker["context_path"]).is_file())
            self.bind(worker)
        barrier = threading.Barrier(2)
        def write(worker, content, name):
            barrier.wait(timeout=5)
            (Path(worker["directory"]) / name).write_text(content)
        change = self.owner.respond(packet)["changes"][0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, first, change["new_content"], "slug.py"),
                       pool.submit(write, second, "actual parallel worker\n", "worker-result.txt")]
            for future in futures:
                future.result(timeout=5)
        board = self.service.worker_board(self.team_id)
        self.assertEqual(board["occupied_slots"], 2)
        self.assertEqual(board["available_slots"], 0)
        self.assertEqual(self.row(first)["changed_files"], ["slug.py"])
        self.assertEqual(self.row(second)["changed_files"], ["worker-result.txt"])
        self.assertEqual(self.row(first)["changed_file_count"], 1)
        self.assertEqual((self.root / "slug.py").read_bytes(), before)
        self.assertFalse((self.root / "worker-result.txt").exists())
        self.assertEqual((self.draft / "slug.py").read_bytes(), before)
        for worker in (first, second):
            self.mark(worker, "DRAFT_READY")
            self.collect(worker)
            self.assertEqual(self.row(worker)["status"], "COLLECTED")
        self.assertEqual((self.draft / "worker-result.txt").read_text(), "actual parallel worker\n")
        self.assertEqual((self.root / "slug.py").read_bytes(), before)
        self.assertFalse(self.service.progress(self.team_id)["check_results"])
        result = self.submit()
        self.assertEqual(result["tasks"]["implement"]["status"], "HANDOFF")
        artifact = next(c for c in result["check_results"] if c["check_id"] == "worker-artifact")
        self.assertEqual(artifact["result"], "pass")
        self.receive("implement")
        verify = self.service.workbench_prepare(self.team_id, "verify")
        self.service.workbench_submit(self.team_id, "verify", verify["workbench"]["id"], "Verify the integrated actual worker outputs.")
        result = self.receive("verify")
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["task_counts"]["COMPLETE"], 2)
        self.assertTrue(result["final_candidate_current"])

    def test_prepared_reservation_has_no_fabricated_actual_agent_and_assign_is_idempotent(self):
        self.prepare()
        first = self.assign()
        deadline = first["deadline_epoch"]
        self.assertIsNone(self.row(first).get("agent_id"))
        self.tick += 20
        again = self.assign()
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["directory"], first["directory"])
        self.assertEqual(again["deadline_epoch"], deadline)
        self.assertEqual(len(self.service.worker_board(self.team_id)["workers"]), 1)
        with self.assertRaises(ContractError):
            self.assign(goal="A conflicting new goal under an already admitted assignment.")
        self.assertIsNone(self.row(first).get("agent_id"))

    def test_near_limit_professional_context_trims_optional_sources_and_keeps_mandatory_instructions(self):
        self.prepare()
        packet = self.service.task_request(self.team_id, "implement")
        padded = deepcopy(packet)
        limit = load(self.root / ".loop/project.json")["context_max_bytes"]
        source = {"path": "large-optional-source.txt", "sha256": "sha256:" + "a" * 64, "content": ""}
        padded["task_context"]["sources"].append(source)
        source["content"] = "x" * (limit - 128 - byte_size(padded))
        source["sha256"] = "sha256:" + hashlib.sha256(source["content"].encode()).hexdigest()
        self.assertEqual(byte_size(padded), limit - 128)
        omitted = padded["task_context"]["context_policy"]["omitted_source_count"]
        with patch.object(self.service, "task_request", return_value=padded):
            worker = self.assign("bounded-architecture-advice", role="architect", write_paths=[],
                title="Inspect architecture within the current context budget", goal="Preserve compatibility and the existing task contract.")
        path = Path(worker["context_path"])
        raw = path.read_bytes()
        context = json.loads(raw)
        self.assertLessEqual(len(raw), limit)
        self.assertLessEqual(byte_size(context), limit)
        task_context = context["task_context"]
        self.assertIn(source["path"], task_context["omitted_paths"])
        self.assertEqual(task_context["context_policy"]["omitted_source_count"], omitted + 1)
        self.assertNotIn(source["path"], [s["path"] for s in task_context["sources"]])
        for name in ("task", "contract_digest", "instructions", "failure_policy"):
            self.assertEqual(task_context[name], packet["task_context"][name])
        professional = context["professional"]
        self.assertEqual(professional["role"]["id"], "architect")
        self.assertEqual(professional["owner"], "coordinator")
        self.assertEqual({i["path"] for i in professional["instructions"]},
                         {".loop/agents/architect.md", ".loop/agents/coordinator.md"})
        for instruction in professional["instructions"]:
            self.assertEqual(instruction["content"], (self.root / instruction["path"]).read_text())
        self.assertIn(source["path"], [s["path"] for s in padded["task_context"]["sources"]])
        self.assertEqual(self.row(worker)["status"], "PREPARED")
        self.assertFalse(self.service.progress(self.team_id)["check_results"])

    def test_running_updates_and_board_reads_do_not_renew_worker_deadline(self):
        self.prepare()
        worker = self.assign()
        deadline = worker["deadline_epoch"]
        self.bind(worker)
        self.tick = deadline - 1
        self.mark(worker, "RUNNING", "One actual update; still the same assigned deadline.")
        self.assertEqual(self.row(worker)["deadline_epoch"], deadline)
        self.tick = deadline
        for _ in range(3):
            row = self.row(worker)
            self.assertEqual(row["deadline_epoch"], deadline)
            self.assertTrue(row["expired"])
        self.assertEqual(self.row(worker)["status"], "RUNNING")
        self.assertEqual(self.service.worker_board(self.team_id)["occupied_slots"], 1)

    def test_max_parallel_includes_prepared_blocked_and_draft_ready_until_collected_or_stopped(self):
        self.prepare()
        first = self.assign()
        second = self.assign("architecture-advice", role="architect", write_paths=[], goal="Inspect compatibility interfaces.")
        self.assertEqual(self.service.worker_board(self.team_id)["max_parallel"], 2)
        self.bind(first)
        self.bind(second)
        self.mark(first, "DRAFT_READY")
        self.mark(second, "BLOCKED", "Need a coordinator decision within the existing scope.")
        with self.assertRaises(ContractError):
            self.assign("third-worker", role="tester", write_paths=[])
        self.assertEqual(self.service.worker_board(self.team_id)["occupied_slots"], 2)
        self.mark(second, "STOPPED")
        third = self.assign("third-worker", role="tester", write_paths=[])
        self.assertEqual(third["status"], "PREPARED")
        self.assertEqual(self.service.worker_board(self.team_id)["occupied_slots"], 2)

    def test_prepared_reservation_can_be_stopped_without_a_native_agent_dispatch(self):
        self.prepare()
        worker = self.assign()
        self.mark(worker, "STOPPED", "Reserved worker was never dispatched.")
        self.assertIsNone(self.row(worker).get("agent_id"))
        self.assertEqual(self.service.worker_board(self.team_id)["occupied_slots"], 0)
        self.assertEqual(self.service.worker_board(self.team_id)["available_slots"], 2)

    def test_dependencies_require_collected_same_workbench_assignments(self):
        self.prepare()
        first = self.assign("architecture-first", role="architect", write_paths=[])
        for dependencies in (["architecture-first"], ["unknown-assignment"], ["architecture-first", "architecture-first"]):
            with self.subTest(dependencies=dependencies), self.assertRaises(ContractError):
                self.assign("backend-after-design", depends_on=dependencies)
        self.bind(first)
        self.mark(first, "DRAFT_READY")
        with self.assertRaises(ContractError):
            self.assign("backend-after-design", depends_on=["architecture-first"])
        self.collect(first)
        worker = self.assign("backend-after-design", depends_on=["architecture-first"])
        self.assertEqual(worker["status"], "PREPARED")
        context = json.loads(Path(worker["context_path"]).read_bytes())
        dependency = context["dependency_results"][0]
        self.assertEqual(dependency["assignment_id"], "architecture-first")
        self.assertEqual(dependency["role"], "architect")
        self.assertEqual(dependency["summary"], self.row(first)["summary"])
        self.assertEqual(dependency["files"], self.row(first)["changed_files"])
        self.assertEqual(dependency["candidate"], self.row(first)["draft_candidate"])

    def test_collected_historical_assignment_does_not_satisfy_a_new_workbench_dependency(self):
        self.prepare()
        first = self.assign("architecture-first", role="architect", write_paths=[])
        self.bind(first)
        self.mark(first, "DRAFT_READY")
        self.collect(first)
        self.service.control(self.team_id, "resume")
        fresh = self.service.workbench_prepare(self.team_id, "implement")
        self.workbench, self.draft = fresh["workbench"], Path(fresh["workbench"]["directory"])
        with self.assertRaises(ContractError):
            self.assign("backend-after-old-design", depends_on=["architecture-first"])
        self.assertFalse(self.row(first)["current"])
        self.assertEqual(self.service.worker_board(self.team_id)["occupied_slots"], 0)

    def test_unknown_inactive_roles_and_independent_write_ownership_are_refused_before_reservation(self):
        self.prepare()
        for role in ("missing-role", "designer", "reviewer", "acceptance"):
            with self.subTest(role=role), self.assertRaises(ContractError):
                self.assign(role=role)
        self.assertEqual(self.service.worker_board(self.team_id)["workers"], [])
        worker = self.assign("covered-architect", role="architect", write_paths=[])
        self.assertEqual(worker["role"], "architect")

    def test_only_explicit_original_task_paths_can_be_assigned(self):
        self.prepare()
        for names in (["../slug.py"], [str(self.root / "slug.py")], ["*.py"], ["slug.py/../slug.py"],
                      ["tests/test_slug.py"], ["README.md"], [".loop/task.json"], [""], ["slug.py", "slug.py"]):
            with self.subTest(paths=names), self.assertRaises(ContractError):
                self.assign(write_paths=names)
        self.assertEqual(self.service.worker_board(self.team_id)["workers"], [])
        self.assertEqual(self.service.worker_board(self.team_id)["occupied_slots"], 0)

    def test_active_write_ownership_cannot_overlap_and_stopping_empty_worker_releases_ownership(self):
        self.prepare()
        first = self.assign()
        with self.assertRaises(ContractError):
            self.assign("second-backend", role="architect", write_paths=["slug.py"])
        self.assertEqual(len(self.service.worker_board(self.team_id)["workers"]), 1)
        self.mark(first, "STOPPED")
        second = self.assign("second-backend")
        self.assertEqual(second["status"], "PREPARED")
        self.bind(second)
        self.mark(second, "STOPPED", "Host stopped an actual worker which produced no draft files.")
        self.assertEqual(self.row(second)["changed_file_count"], 0)
        self.assertEqual(self.assign("third-backend")["status"], "PREPARED")

    def test_real_agent_identity_cannot_be_shared_by_two_active_assignments_or_changed_mid_work(self):
        self.prepare()
        first = self.assign()
        second = self.assign("architect-consult", role="architect", write_paths=[])
        self.bind(first, "actual-host-agent-1")
        with self.assertRaises(ContractError):
            self.bind(second, "actual-host-agent-1")
        self.assertIsNone(self.row(second).get("agent_id"))
        with self.assertRaises(ContractError):
            self.service.worker_update(self.team_id, first["id"], "other-agent", "RUNNING", "Cannot swap actual identity silently.")
        self.assertEqual(self.row(first)["agent_id"], "actual-host-agent-1")
        self.assertIn("host", self.service.worker_board(self.team_id)["assurance"].lower())

    def test_unknown_worker_and_unbound_running_status_never_create_an_activity(self):
        self.prepare()
        worker = self.assign()
        with self.assertRaises(ContractError):
            self.service.worker_update(self.team_id, "unknown-worker", "actual-id", "RUNNING", "Unknown identity.")
        with self.assertRaises(ContractError):
            self.service.worker_update(self.team_id, worker["id"], None, "RUNNING", "No actual dispatch ID.")
        self.assertEqual(self.row(worker)["status"], "PREPARED")
        self.assertIsNone(self.row(worker).get("agent_id"))

    def test_host_updates_cannot_invent_collected_or_formally_complete_status(self):
        self.prepare()
        worker = self.assign()
        self.bind(worker)
        for status in ("COLLECTED", "COMPLETE", "SUCCEEDED", "UNKNOWN"):
            with self.subTest(status=status), self.assertRaises(ContractError):
                self.mark(worker, status, "Host text cannot bypass actual collection or authoritative checks.")
        self.assertEqual(self.row(worker)["status"], "RUNNING")
        self.assertEqual(self.service.progress(self.team_id)["task_counts"]["COMPLETE"], 0)
        self.assertFalse(self.service.progress(self.team_id)["check_results"])

    def test_collect_requires_stable_ready_or_stopped_worker_and_is_idempotent(self):
        packet = self.prepare()
        worker = self.assign()
        self.bind(worker)
        self.edit_worker(worker, packet)
        before = (self.draft / "slug.py").read_bytes()
        with self.assertRaises(ContractError):
            self.collect(worker)
        self.assertEqual((self.draft / "slug.py").read_bytes(), before)
        self.mark(worker, "DRAFT_READY")
        self.collect(worker)
        imported = (self.draft / "slug.py").read_bytes()
        self.collect(worker)
        self.assertEqual((self.draft / "slug.py").read_bytes(), imported)
        self.assertEqual(self.row(worker)["status"], "COLLECTED")
        self.assertFalse(self.service.progress(self.team_id)["check_results"])
        self.assertEqual(self.service.progress(self.team_id)["task_counts"]["COMPLETE"], 0)

    def test_uncollected_worker_blocks_formal_submission_including_stopped_worker_with_files(self):
        packet = self.prepare()
        worker = self.assign()
        self.bind(worker)
        self.edit_worker(worker, packet)
        for status in ("RUNNING", "DRAFT_READY", "STOPPED"):
            self.mark(worker, status)
            with self.subTest(status=status), self.assertRaises(ContractError):
                self.submit()
        self.assertEqual(self.service.progress(self.team_id)["tasks"]["implement"]["controller_status"], "PLANNING")
        self.collect(worker)
        self.assertEqual(self.submit()["tasks"]["implement"]["status"], "HANDOFF")

    def test_direct_collect_cannot_handoff_successful_child_until_actual_worker_stops(self):
        packet = self.prepare()
        worker = self.assign("unfinished-architecture-advice", role="architect", write_paths=[])
        self.bind(worker)
        draft = (Path(worker["directory"]) / "slug.py").read_bytes()
        data = self.service.team.teams.get(self.team_id)
        run_id = data["records"]["implement"]["child_run_id"]
        controller = self.service.team._controller(data, "implement")
        # Exercise the lower-level controller path with a real fix and actual
        # local checks; a synthetic SUCCEEDED status would miss this bypass.
        child = controller.submit(run_id, self.owner.respond(packet))
        self.assertEqual(child["state"]["status"], "SUCCEEDED")
        self.assertEqual(child["state"]["iteration"], 1)
        evidence = self.store.evidence(run_id, child["selected_evidence"])
        self.assertTrue(evidence)
        self.assertTrue(all(e["result"] == "pass" for e in evidence))
        record = deepcopy(self.service.team.teams.get(self.team_id)["records"]["implement"])
        with patch("subprocess.Popen", side_effect=AssertionError("Collection cannot dispatch another process")):
            with self.assertRaisesRegex(ContractError, "uncollected|occupied"):
                self.service.team.collect(self.team_id, "implement")
        self.assertEqual(self.service.team.teams.get(self.team_id)["records"]["implement"], record)
        self.assertIsNone(record["handoff"])
        self.assertEqual(record["status"], "RUNNING")
        self.assertEqual(self.row(worker)["status"], "RUNNING")
        self.assertEqual((Path(worker["directory"]) / "slug.py").read_bytes(), draft)
        self.mark(worker, "STOPPED", "The actual offline consultation has stopped and produced no files.")
        with patch("subprocess.Popen", side_effect=AssertionError("Stopping a worker must not rerun checks")):
            result = self.service.team.collect(self.team_id, "implement")
        after = self.store.get(run_id)
        self.assertEqual(result["tasks"]["implement"]["status"], "HANDOFF")
        self.assertEqual(result["tasks"]["implement"]["child_run_id"], run_id)
        self.assertEqual(after["state"]["iteration"], child["state"]["iteration"])
        self.assertEqual(after["task"]["checks"], child["task"]["checks"])
        self.assertEqual(after["selected_evidence"], child["selected_evidence"])
        self.assertEqual(after["elapsed_ms"], child["elapsed_ms"])
        self.assertEqual(self.store.evidence(run_id, after["selected_evidence"]), evidence)

    def test_stopped_uncollected_draft_blocks_formal_evaluation_before_request_import_or_dispatch(self):
        self.native_context_fixture()
        packet = self.service.workbench_prepare(self.team_id, "implement")
        self.workbench, self.draft = packet["workbench"], Path(packet["workbench"]["directory"])
        worker = self.assign()
        self.bind(worker)
        path = Path(worker["directory"]) / "slug.py"
        path.write_text(path.read_text() + "\n# Retain this actual stopped professional draft.\n")
        self.mark(worker, "STOPPED", "Actual offline worker stopped with a scoped draft still to collect.")
        data = self.service.team.teams.get(self.team_id)
        run_id = data["records"]["implement"]["child_run_id"]
        controller = self.service.team._controller(data, "implement")
        child = controller.submit(run_id, self.owner.respond(packet))
        command_ids = {c["id"] for c in child["task"]["checks"] if c["type"] == "command"}
        passed = {e["check_id"] for e in self.store.evidence(run_id, child["selected_evidence"])
                  if e["result"] == "pass" and e["snapshot_digest"] == child["state"]["snapshot_digest"]}
        self.assertTrue(command_ids)
        self.assertTrue(command_ids.issubset(passed))
        self.assertEqual(child["state"]["iteration"], 2)
        # Prepare a legitimate signed fixture result for this unchanged main
        # candidate; even that result cannot consume an uncollected draft.
        request = controller.evaluation_request(run_id, "context-review")
        artifact = self.owner.base / "stopped-draft-review.txt"
        artifact.write_text("Offline reviewer inspected the actual current candidate and local checks.\n")
        envelope = sign_result(self.store.authorities, request, "native-context-fixture-reviewer", result="pass",
            summary="Actual deterministic review remains bound to this candidate.", artifacts=[artifact], findings=[])
        from loop_engineering.native_review import register
        register(self.store, self.root, executable=sys.executable)
        before = self.store.get(run_id)
        record = deepcopy(self.service.team.teams.get(self.team_id)["records"]["implement"])
        main, stopped_draft = (self.root / "slug.py").read_bytes(), path.read_bytes()
        with patch.object(NativeController, "evaluation_request", side_effect=AssertionError("Draft gate must precede request creation")), \
                patch.object(NativeController, "import_evaluation", side_effect=AssertionError("Draft gate must precede signed import")), \
                patch("loop_engineering.native_review.run", side_effect=AssertionError("Draft gate must precede paid review")), \
                patch("subprocess.Popen", side_effect=AssertionError("No evaluator process may start")):
            for operation in (
                lambda: self.service.evaluation(self.team_id, "implement", "context-review"),
                lambda: self.service.evaluation(self.team_id, "implement", envelope=envelope),
                lambda: self.service.execute_evaluation(self.team_id, "implement", "context-review"),
            ):
                with self.subTest(operation=operation), self.assertRaisesRegex(ContractError, "uncollected draft"):
                    operation()
        self.assertEqual(self.store.get(run_id), before)
        self.assertEqual(self.service.team.teams.get(self.team_id)["records"]["implement"], record)
        self.assertEqual(self.row(worker)["status"], "STOPPED")
        self.assertEqual(self.row(worker)["changed_file_count"], 1)
        self.assertEqual((self.root / "slug.py").read_bytes(), main)
        self.assertEqual(path.read_bytes(), stopped_draft)
        self.collect(worker)
        self.assertEqual((self.draft / "slug.py").read_bytes(), stopped_draft)
        self.assertEqual((self.root / "slug.py").read_bytes(), main)
        result = self.service.evaluation(self.team_id, "implement", envelope=envelope)
        after = self.store.get(run_id)
        self.assertEqual(result["tasks"]["implement"]["status"], "HANDOFF")
        self.assertEqual(result["tasks"]["implement"]["child_run_id"], run_id)
        self.assertEqual(after["state"]["iteration"], before["state"]["iteration"])
        self.assertEqual(after["task"]["checks"], before["task"]["checks"])
        self.assertGreaterEqual(after["elapsed_ms"], before["elapsed_ms"])
        self.assertTrue(all(e["result"] == "pass" for e in self.store.evidence(run_id, after["selected_evidence"])))
        self.assertEqual(self.row(worker)["status"], "COLLECTED")

    def test_stopped_empty_reservation_does_not_block_a_real_coordinator_draft(self):
        packet = self.prepare()
        worker = self.assign()
        self.mark(worker, "STOPPED")
        self.edit(packet)
        result = self.submit()
        self.assertEqual(result["tasks"]["implement"]["status"], "HANDOFF")
        self.assertEqual(self.row(worker)["status"], "STOPPED")

    def test_wrong_scope_edits_are_visible_as_problems_and_cannot_touch_either_coordinator_or_main(self):
        self.allow_second_file()
        packet = self.prepare()
        worker = self.assign()
        self.bind(worker)
        self.edit_worker(worker, packet)
        (Path(worker["directory"]) / "worker-result.txt").write_text("Worker exceeds its actual assigned file scope.\n")
        before = (self.root / "slug.py").read_bytes()
        self.assertTrue(self.row(worker)["problem"])
        self.mark(worker, "DRAFT_READY")
        with self.assertRaises(ContractError):
            self.collect(worker)
        self.assertEqual((self.draft / "slug.py").read_bytes(), before)
        self.assertFalse((self.draft / "worker-result.txt").exists())
        self.assertEqual((self.root / "slug.py").read_bytes(), before)

    def test_changed_coordinator_file_is_preserved_when_worker_collect_precondition_is_stale(self):
        packet = self.prepare()
        worker = self.assign()
        self.bind(worker)
        self.edit_worker(worker, packet)
        self.mark(worker, "DRAFT_READY")
        (self.draft / "slug.py").write_text("Actual coordinator revision made after assignment\n")
        with self.assertRaises(ContractError):
            self.collect(worker)
        self.assertEqual((self.draft / "slug.py").read_text(), "Actual coordinator revision made after assignment\n")
        self.assertNotEqual(self.row(worker)["status"], "COLLECTED")

    def test_paused_cancelled_team_cannot_assign_or_collect_but_preserves_draft_files(self):
        packet = self.prepare()
        worker = self.assign()
        self.bind(worker)
        self.edit_worker(worker, packet)
        self.mark(worker, "DRAFT_READY")
        before = (self.draft / "slug.py").read_bytes()
        for action in ("pause", "cancel"):
            self.service.control(self.team_id, action)
            with self.subTest(action=action), self.assertRaises(ContractError):
                self.collect(worker)
            with self.subTest(action=action), self.assertRaises(ContractError):
                self.assign("later-worker", role="architect", write_paths=[])
            self.assertEqual((self.draft / "slug.py").read_bytes(), before)
            self.assertTrue((Path(worker["directory"]) / "slug.py").is_file())

    def test_stale_request_preserves_old_worker_and_slots_until_host_reports_actual_stop(self):
        packet = self.prepare()
        worker = self.assign()
        self.bind(worker)
        self.edit_worker(worker, packet)
        self.service.control(self.team_id, "resume")
        fresh = self.service.workbench_prepare(self.team_id, "implement")
        self.workbench, self.draft = fresh["workbench"], Path(fresh["workbench"]["directory"])
        row = self.row(worker)
        self.assertFalse(row["current"])
        self.assertEqual(self.service.worker_board(self.team_id)["occupied_slots"], 1)
        with self.assertRaises(ContractError):
            self.collect(worker)
        self.assertTrue((Path(worker["directory"]) / "slug.py").is_file())
        self.mark(worker, "STOPPED", "Host interrupted the old actual worker before dispatching a new one.")
        self.assertEqual(self.service.worker_board(self.team_id)["occupied_slots"], 0)
        self.assertEqual(self.assign("fresh-backend")["status"], "PREPARED")

    def test_main_project_change_refuses_worker_collection_without_replacing_the_user_revision(self):
        packet = self.prepare()
        worker = self.assign()
        self.bind(worker)
        self.edit_worker(worker, packet)
        self.mark(worker, "DRAFT_READY")
        (self.root / "slug.py").write_text("Actual user revision in the main project\n")
        before = (self.draft / "slug.py").read_bytes()
        with self.assertRaises(ContractError):
            self.collect(worker)
        self.assertEqual((self.root / "slug.py").read_text(), "Actual user revision in the main project\n")
        self.assertEqual((self.draft / "slug.py").read_bytes(), before)

    def test_readonly_consultation_cannot_modify_files_or_replace_the_independent_evaluator_gate(self):
        self.owner.plan["tasks"][1].update(phase="acceptance", role="acceptance", engine_file=".loop/review-engine.json")
        task = load(self.root / ".loop/tasks/verify.json")
        task["checks"].append({"id": "external", "type": "review", "independent": True,
            "description": "Independent actual candidate assessment", "procedure": ["Inspect exact candidate and actual artifacts"]})
        task["criteria"].append({"id": "reviewed", "description": "Registered review passes", "check_ids": ["external"]})
        (self.root / ".loop/tasks/verify.json").write_text(json.dumps(task))
        self.store.authorities.create("worker-fixture-reviewer", "reviewer")
        engine = load(ROOT / "templates/engine.json")
        engine["evaluator_keys"] = [{"key_id": "worker-fixture-reviewer", "role": "reviewer", "check_ids": ["external"]}]
        (self.root / ".loop/review-engine.json").write_text(json.dumps(engine))
        self.owner.fixture.save_plan()
        packet = self.prepare()
        consult = self.assign("independent-review-advice", role="reviewer", write_paths=[],
            title="Consult about review coverage", goal="Inspect the review coverage without attesting acceptance.")
        self.bind(consult)
        self.mark(consult, "DRAFT_READY", "Advisory comments only; this does not sign the independent result.")
        self.collect(consult)
        self.assertEqual(self.row(consult)["changed_file_count"], 0)
        self.assertFalse(self.service.progress(self.team_id)["check_results"])
        self.edit(packet)
        self.submit(); self.receive("implement")
        verify = self.service.workbench_prepare(self.team_id, "verify")
        self.service.workbench_submit(self.team_id, "verify", verify["workbench"]["id"], "Run actual checks; keep independent review pending.")
        result = self.service.next(self.team_id)
        self.assertEqual(result["next_action"]["kind"], "await_evaluator")
        self.assertFalse(result["next_action"]["continue_work"])
        self.assertIsNone(result["final_candidate_current"])

    def test_readonly_consultation_cannot_import_any_file_change(self):
        packet = self.prepare()
        consult = self.assign("review-advice", role="reviewer", write_paths=[])
        self.bind(consult)
        self.edit_worker(consult, packet)
        self.mark(consult, "DRAFT_READY")
        self.assertTrue(self.row(consult)["problem"])
        with self.assertRaises(ContractError):
            self.collect(consult)
        self.assertEqual((self.draft / "slug.py").read_bytes(), (self.root / "slug.py").read_bytes())

    def test_interrupted_collection_replays_only_frozen_files_and_blocks_formal_submission(self):
        packet = self.prepare()
        worker = self.assign()
        self.bind(worker)
        self.edit_worker(worker, packet)
        expected = (Path(worker["directory"]) / "slug.py").read_bytes()
        before = (self.draft / "slug.py").read_bytes()
        self.mark(worker, "DRAFT_READY")
        with patch.object(self.service.team.snapshots, "apply_prepared", side_effect=OSError("Offline interrupted import")):
            with self.assertRaises(OSError):
                self.collect(worker)
        self.assertEqual((self.draft / "slug.py").read_bytes(), before)
        with self.assertRaises(ContractError):
            self.submit()
        (Path(worker["directory"]) / "slug.py").write_text("Late worker write must not replace the frozen import\n")
        self.collect(worker)
        self.assertEqual((self.draft / "slug.py").read_bytes(), expected)
        self.assertEqual((self.root / "slug.py").read_bytes(), before)
        self.assertEqual(self.row(worker)["status"], "COLLECTED")

    def test_interrupted_import_can_stop_pause_and_resume_without_renewing_or_automatically_replaying(self):
        packet = self.prepare()
        worker = self.assign()
        self.bind(worker)
        self.edit_worker(worker, packet)
        frozen = (Path(worker["directory"]) / "slug.py").read_bytes()
        before = (self.draft / "slug.py").read_bytes()
        self.mark(worker, "DRAFT_READY")
        original = self.service.team.teams.get(self.team_id)["records"]["implement"]
        task_deadline = self.service.progress(self.team_id)["tasks"]["implement"]["host_wait"]["deadline_epoch"]
        with patch.object(self.service.team.snapshots, "apply_prepared", side_effect=OSError("Offline interrupted import")):
            with self.assertRaises(OSError):
                self.collect(worker)
        self.assertTrue(self.row(worker)["import_pending"])
        self.service.control(self.team_id, "pause")
        self.mark(worker, "STOPPED", "Host stopped the actual writer while its frozen import is pending.")
        self.assertEqual(self.row(worker)["status"], "STOPPED")
        self.assertTrue(self.row(worker)["import_pending"])
        with self.assertRaises(ContractError):
            self.collect(worker)
        (Path(worker["directory"]) / "slug.py").write_text("A late worker edit is outside the already frozen import\n")
        self.tick = worker["deadline_epoch"] + 20
        with patch.object(self.service.team.snapshots, "apply_prepared", side_effect=AssertionError("Resume must not import files")):
            result = self.service.control(self.team_id, "resume")
        resumed = self.service.team.teams.get(self.team_id)["records"]["implement"]
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(resumed["request"]["id"], original["request"]["id"])
        self.assertEqual(resumed["child_run_id"], original["child_run_id"])
        self.assertEqual(result["tasks"]["implement"]["host_wait"]["deadline_epoch"], task_deadline)
        self.assertEqual(self.row(worker)["deadline_epoch"], worker["deadline_epoch"])
        self.assertTrue(self.row(worker)["current"])
        self.assertTrue(self.row(worker)["import_pending"])
        self.assertEqual((self.draft / "slug.py").read_bytes(), before)
        self.assertEqual((self.root / "slug.py").read_bytes(), before)
        self.assertEqual(self.service.team.store.get(resumed["child_run_id"])["state"]["iteration"], 0)
        self.assertFalse(result["check_results"])
        self.collect(worker)
        self.assertEqual((self.draft / "slug.py").read_bytes(), frozen)
        self.assertEqual((self.root / "slug.py").read_bytes(), before)
        self.assertEqual(self.row(worker)["status"], "COLLECTED")
        self.assertFalse(self.row(worker)["import_pending"])
        self.assertFalse(self.service.progress(self.team_id)["check_results"])

    def test_worker_admitted_after_service_preflight_cannot_race_formal_submission_under_team_writer(self):
        packet = self.prepare()
        step = self.owner.respond(packet)
        before = (self.root / "slug.py").read_bytes()
        original_submit = self.service.team.submit
        assigned = []
        def admit_between_checks(*args, **kwargs):
            assigned.append(self.assign("concurrent-advice", role="architect", write_paths=[]))
            return original_submit(*args, **kwargs)
        with patch.object(self.service.team, "submit", side_effect=admit_between_checks):
            with self.assertRaises(ContractError):
                self.service.task_submit(self.team_id, "implement", packet["team_assignment"]["request_id"], step)
        self.assertEqual(len(assigned), 1)
        self.assertEqual(self.row(assigned[0])["status"], "PREPARED")
        self.assertEqual((self.root / "slug.py").read_bytes(), before)
        result = self.service.progress(self.team_id)
        self.assertFalse(result["check_results"])
        self.assertEqual(result["task_counts"]["COMPLETE"], 0)
        record = self.service.team.teams.get(self.team_id)["records"]["implement"]
        self.assertEqual(record["request"]["id"], packet["team_assignment"]["request_id"])
        self.assertEqual(self.service.team.store.get(record["child_run_id"])["state"]["iteration"], 0)
        self.mark(assigned[0], "STOPPED", "Concurrent reservation was never dispatched.")
        result = self.service.task_submit(self.team_id, "implement", packet["team_assignment"]["request_id"], step)
        self.assertEqual(result["tasks"]["implement"]["status"], "HANDOFF")
        self.assertTrue(result["check_results"])

    def test_native_team_cannot_bypass_serial_submission_with_collect_false(self):
        packet = self.prepare()
        step = self.owner.respond(packet)
        before = (self.root / "slug.py").read_bytes()
        record = self.service.team.teams.get(self.team_id)["records"]["implement"]
        with self.assertRaises(ContractError):
            self.service.team.submit(self.team_id, "implement", packet["team_assignment"]["request_id"], step, collect=False)
        self.assertEqual((self.root / "slug.py").read_bytes(), before)
        self.assertEqual(self.service.team.store.get(record["child_run_id"])["state"]["iteration"], 0)
        self.assertEqual(self.service.team.teams.get(self.team_id)["records"]["implement"]["request"]["id"], record["request"]["id"])
        self.assertFalse(self.service.progress(self.team_id)["check_results"])
        self.assertEqual(self.service.worker_board(self.team_id)["workers"], [])

    def test_board_is_durable_observes_real_files_and_escapes_host_material_in_the_page(self):
        packet = self.prepare()
        worker = self.assign(title="<script>alert('worker')</script>", goal="Inspect actual scoped source.")
        self.bind(worker, "actual-host-worker-1")
        self.edit_worker(worker, packet)
        self.mark(worker, "RUNNING", "<img src=x onerror=alert('detail')>")
        self.tick = worker["deadline_epoch"]
        before = self.row(worker)
        second = NativeHostService(self.root, self.store)
        second.workbench_parent, second.clock = self.store.directory, lambda: self.tick
        self.addCleanup(second.close)
        after = self.row(worker, second)
        for key in ("id", "directory", "agent_id", "status", "changed_files", "changed_file_count", "deadline_epoch", "expired"):
            self.assertEqual(after[key], before[key])
        state = second.progress(self.team_id)
        self.assertEqual(state["task_counts"]["COMPLETE"], 0)
        content = render_progress(state)
        self.assertNotIn("<script>", content)
        self.assertNotIn("<img src=x", content)
        self.assertIn("&lt;script&gt;", content)
        self.assertIn("&lt;img src=x", content)
        self.assertIn("actual-host-worker-1", content)
        self.assertIn("slug.py", content)
