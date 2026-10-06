"""Real owned subprocess/authority/cache integration with deterministic workers."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlparse, unquote

from test_native import NativeCase
from test_execution_tools import browser_plan, load_plan
from loop_engineering.contracts import ContractError, load, ROOT
from loop_engineering.native_engine import NativeController


FIXTURE_WORKER = ROOT / "tests/executor_fixture.py"


class ExternalExecutionTests(NativeCase):
    def prepare(self, plan=None):
        plan = plan or browser_plan()
        config = load(ROOT / "templates/scenarios/development/execution-tools.json")
        config["executors"] = [plan]
        (self.root / ".loop/execution-tools.json").write_text(json.dumps(config))
        (self.root / "ui.html").write_text('<div id="status">done</div>')
        profile = load(self.profile_path); profile["execution_tools"] = ".loop/execution-tools.json"
        self.profile_path.write_text(json.dumps(profile))
        task = load(self.task_path)
        check_type = "interaction" if plan["kind"] == "browser" else "artifact"
        task["checks"].append({"id": plan["check_id"], "type": check_type, "description": "Actual registered test procedure",
                              "procedure": ["Execute the exact frozen tool plan and retain its observations"]})
        task["criteria"].append({"id": "external", "description": "The registered tool procedure passes", "check_ids": [plan["check_id"]]})
        self.write_task(task)
        self.store.authorities.create("fixture-executor", check_type)
        self.config["evaluator_keys"] = [{"key_id": "fixture-executor", "role": check_type, "check_ids": [plan["check_id"]]}]
        self.configure(); self.start()
        self.engine.submit(self.run_id, self.step())
        return plan

    def fixture_launch(self, variant=None):
        original = self.engine.launch
        def launch(data, argv, cwd, logs, **kwargs):
            if kwargs["kind"] == "evaluator":
                argv = [sys.executable, "-B", str(FIXTURE_WORKER), *([variant] if variant else [])]
            return original(data, argv, cwd, logs, **kwargs)
        return patch.object(self.engine, "launch", side_effect=launch)

    def test_registered_browser_protocol_retains_separate_signed_artifacts(self):
        self.prepare()
        with self.fixture_launch() as launch:
            record = self.engine.execute_evaluation(self.run_id, "ui")
            again = self.engine.execute_evaluation(self.run_id, "ui")
        self.assertEqual(record, again)
        self.assertEqual(record["result"], "pass")
        self.assertEqual(sum(c.kwargs["kind"] == "evaluator" for c in launch.call_args_list), 1)
        self.assertGreaterEqual(len(record["artifacts"]), 4)
        self.engine.resume(self.run_id)
        self.assertEqual(self.engine.verify(self.run_id)["state"]["status"], "SUCCEEDED")
        self.assertEqual(self.store.replay(self.run_id), self.store.get(self.run_id))

    def test_registered_http_protocol_uses_raw_workload_and_no_model_call(self):
        self.prepare(load_plan())
        with self.fixture_launch(), patch("loop_engineering.models.HTTPTransport.post", side_effect=AssertionError("No model dispatch")):
            record = self.engine.execute_evaluation(self.run_id, "load")
        self.assertEqual(record["result"], "pass")
        report_path = next(Path(unquote(urlparse(a["uri"]).path)) for a in record["artifacts"] if a["uri"].endswith("report.json"))
        report = json.loads(report_path.read_text())
        self.assertEqual(len(report["samples"]), 3)
        self.assertEqual(report["metrics"]["successes"], 3)

    def test_missing_browser_runtime_is_inconclusive_and_cannot_succeed(self):
        self.prepare()
        record = self.engine.execute_evaluation(self.run_id, "ui")
        self.assertEqual(record["result"], "inconclusive")
        self.engine.resume(self.run_id)
        self.assertNotEqual(self.engine.verify(self.run_id)["state"]["status"], "SUCCEEDED")

    def test_evaluation_run_cli_dispatches_only_registered_local_check(self):
        self.prepare()
        code, result, error = self.cli(["evaluation-run", self.run_id, "--check-id", "ui", "--state-dir", str(self.store.directory)])
        self.assertEqual(code, 0, error)
        self.assertEqual(result["result"], "inconclusive")
        self.assertIn("unavailable", result["summary"])

    def invalid_executor(self, variant):
        self.prepare()
        with self.fixture_launch(variant), self.assertRaises(ContractError):
            self.engine.execute_evaluation(self.run_id, "ui")
        data = self.store.get(self.run_id)
        self.assertEqual(data["native"]["external_by_check"], {})
        row = next(iter(data["native"]["execution_attempts"].values()))
        self.assertEqual(row["status"], "RUNNING")
        self.engine.resume(self.run_id)
        with self.fixture_launch(), self.assertRaisesRegex(ContractError, "unresolved"):
            self.engine.execute_evaluation(self.run_id, "ui")
        self.engine.reconcile_process(self.run_id, row["action_id"], "Inspected failed fixture output and confirmed no owned process remains")

    def test_executor_cannot_forge_candidate(self):
        self.invalid_executor("wrong_candidate")

    def test_executor_cannot_escape_artifacts(self):
        self.invalid_executor("escape_artifact")

    def test_executor_cannot_mutate_input(self):
        self.invalid_executor("mutate_source")

    def test_cached_result_after_import_crash_does_not_repeat_external_effect(self):
        self.prepare(load_plan())
        with self.fixture_launch() as launch, patch.object(self.engine, "import_evaluation", side_effect=OSError("Crash before import")):
            with self.assertRaises(OSError): self.engine.execute_evaluation(self.run_id, "load")
        self.assertEqual(sum(c.kwargs["kind"] == "evaluator" for c in launch.call_args_list), 1)
        self.engine.resume(self.run_id)
        with patch.object(self.engine, "launch", side_effect=AssertionError("Cached result must not dispatch")):
            record = self.engine.execute_evaluation(self.run_id, "load")
        self.assertEqual(record["result"], "pass")

    def test_mutated_cached_artifact_is_rejected_before_admission(self):
        self.prepare()
        with self.fixture_launch(), patch.object(self.engine, "import_evaluation", side_effect=OSError("Crash before import")):
            with self.assertRaises(OSError): self.engine.execute_evaluation(self.run_id, "ui")
        data = self.store.get(self.run_id)
        row = next(iter(data["native"]["execution_attempts"].values()))
        path = Path(unquote(urlparse(row["envelope"]["payload"]["artifacts"][0]["uri"]).path))
        path.write_text("changed after signing")
        with patch.object(self.engine, "launch", side_effect=AssertionError("No redispatch")), self.assertRaisesRegex(ContractError, "bytes changed"):
            self.engine.execute_evaluation(self.run_id, "ui")

    def test_unregistered_check_and_nonlocal_runtime_cannot_dispatch(self):
        self.prepare()
        with self.fixture_launch(), self.assertRaisesRegex(ContractError, "matching registered"):
            self.engine.execute_evaluation(self.run_id, "other-check")
        with self.store.writer(self.run_id) as data:
            data["native"]["config"]["runtime"]["backend"] = "docker"
            self.store.save(data, "test.fixture_backend_unavailable")
        with self.fixture_launch(), self.assertRaisesRegex(ContractError, "supervised local"):
            self.engine.execute_evaluation(self.run_id, "ui")

    def test_cancelled_run_does_not_dispatch_an_executor(self):
        self.prepare()
        self.store.cancel(self.run_id)
        with self.fixture_launch() as launch, self.assertRaises(ContractError):
            self.engine.execute_evaluation(self.run_id, "ui")
        self.assertEqual(launch.call_count, 0)

    def test_configuration_change_invalidates_the_accepted_run(self):
        self.prepare()
        path = self.root / ".loop/execution-tools.json"
        cfg = load(path); cfg["executors"][0]["timeout_seconds"] += 1; path.write_text(json.dumps(cfg))
        with self.fixture_launch() as launch, self.assertRaisesRegex(ContractError, "configuration changed"):
            self.engine.execute_evaluation(self.run_id, "ui")
        self.assertEqual(launch.call_count, 0)

    def test_revoked_executor_cannot_reuse_a_cached_passing_record(self):
        self.prepare()
        with self.fixture_launch(): self.engine.execute_evaluation(self.run_id, "ui")
        self.store.authorities.revoke("fixture-executor")
        with patch.object(self.engine, "launch", side_effect=AssertionError("No dispatch")), self.assertRaises(ContractError):
            self.engine.execute_evaluation(self.run_id, "ui")
