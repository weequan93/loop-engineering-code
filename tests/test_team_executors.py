"""The real scheduler executes registered tools; model fixture cannot attest UI."""

from copy import deepcopy
import json
import sys
import unittest
from unittest.mock import patch

import test_team_automation as automation_fixture
from test_execution_tools import browser_plan, load_plan
from test_external_execution import FIXTURE_WORKER
from loop_engineering.contracts import ROOT, load
from loop_engineering.native_engine import NativeController
from loop_engineering.file_changes import change_from_bytes
from loop_engineering.workspace import byte_digest


class TeamExecutorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = automation_fixture.AutomationTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.team, self.auto = self.fixture.root, self.fixture.team, self.fixture.auto

    def prepare(self, plan, register=True):
        tools = load(self.root / ".loop/execution-tools.json")
        tools["executors"] = [plan] if register else []
        self.fixture.fixture.fixture.write(".loop/execution-tools.json", tools)
        self.fixture.fixture.fixture.select_team()
        (self.root / "ui.html").write_text('<div id="status">done</div>')
        path = self.root / ".loop/tasks/verify.json"
        task = load(path)
        kind = "interaction" if plan["kind"] == "browser" else "artifact"
        task["checks"].append({"id": plan["check_id"], "type": kind, "description": "The registered fixture procedure", "procedure": ["Run the exact registered plan"]})
        task["criteria"].append({"id": "tool", "description": "Registered procedure passes", "check_ids": [plan["check_id"]]})
        path.write_text(json.dumps(task))
        key = "team-fixture-" + kind
        self.team.store.authorities.create(key, kind)
        config = deepcopy(self.fixture.config)
        config["evaluator_keys"] = [{"key_id": key, "role": kind, "check_ids": [plan["check_id"]]}]
        engine = self.root / ".loop/tasks/verify-engine.json"; engine.write_text(json.dumps(config))
        self.fixture.plan["tasks"][1]["engine_file"] = ".loop/tasks/verify-engine.json"
        self.fixture.fixture.save_plan()

    def run_with_worker(self):
        original = NativeController.launch
        def launch(controller, data, argv, cwd, logs, **kwargs):
            if kwargs["kind"] == "evaluator":
                argv = [sys.executable, "-B", str(FIXTURE_WORKER)]
            return original(controller, data, argv, cwd, logs, **kwargs)
        self.fixture.start()
        with patch.object(NativeController, "launch", launch):
            return self.auto.run(self.fixture.team_id)

    def test_browser_check_runs_on_branch_and_delivered_candidate(self):
        self.prepare(browser_plan())
        report = self.run_with_worker()
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        child = self.team.store.get(report["tasks"]["verify"]["child_run_id"])
        integrated = self.team.store.get(report["tasks"]["verify"]["integration_run_id"])
        for run in (child, integrated):
            record = self.team.store.evidence(run["run_id"], [run["native"]["external_by_check"]["ui"]])[0]
            self.assertEqual(record["result"], "pass")
            self.assertEqual(record["snapshot_digest"], run["state"]["snapshot_digest"])
        self.assertFalse(any(request["kind"] == "team-evaluator-request" for request in self.fixture.transport.requests))

    def test_http_workload_executes_without_replacing_actual_command_checks(self):
        self.prepare(load_plan())
        report = self.run_with_worker()
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        run = self.team.store.get(report["tasks"]["verify"]["integration_run_id"])
        evidence = self.team.store.evidence(run["run_id"], run["selected_evidence"])
        self.assertEqual({e["check_id"] for e in evidence}, {c["id"] for c in run["task"]["checks"]})
        self.assertTrue(all(e["result"] == "pass" for e in evidence))

    def test_missing_executor_leaves_team_awaiting_input(self):
        self.prepare(browser_plan(), register=False)
        report = self.run_with_worker()
        self.assertEqual(report["status"], "AWAITING_INPUT")
        self.assertIn("registered", report["reason"])

    def test_coordinator_receives_actual_registered_check_ids_and_types(self):
        self.prepare(load_plan())
        self.fixture.coordinator_response()
        self.fixture.start()
        self.auto.prepare(self.fixture.team_id)
        request = self.fixture.transport.requests[0]
        self.assertEqual(request["kind"], "team-coordinator-request")
        self.assertEqual(request["registered_execution_tools"], [{"check_id": "load", "kind": "http_load", "check_type": "artifact"}])

    def test_v3_parallel_merge_delivers_binary_and_deletion_before_final_checks(self):
        (self.root / "obsolete.txt").write_text("old")
        for task_id in ("implement", "verify"):
            path = self.root / (".loop/tasks/" + task_id + ".json")
            task = load(path); task.setdefault("extensions", {})["agent_step_version"] = "0.3"
            task["scope"]["write_allow"] += ["obsolete.txt", "asset.bin"]
            path.write_text(json.dumps(task))
        respond = self.fixture.respond
        def v3_response(request):
            response = respond(request)
            if request["kind"] == "team-host-request":
                response["schema_version"] = "0.3"
                response["changes"] = [change_from_bytes(c["path"], c["expected_sha256"], c["new_content"].encode(), "0.3") for c in response["changes"]]
                if request["task_context"].get("bundle", request["task_context"])["task"]["task_id"] == "implement":
                    response["changes"] += [change_from_bytes("obsolete.txt", byte_digest(b"old"), None, "0.3"),
                                            change_from_bytes("asset.bin", None, b"\xff\x00", "0.3")]
            return response
        self.fixture.respond = v3_response
        self.fixture.start()
        report = self.auto.run(self.fixture.team_id)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertFalse((self.root / "obsolete.txt").exists())
        self.assertEqual((self.root / "asset.bin").read_bytes(), b"\xff\x00")
