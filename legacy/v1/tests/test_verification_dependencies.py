"""Dependency-bound observation reuse keeps provenance and original acceptance."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, load, validate
from loop_engineering.verification_dependencies import bindings
from tests import test_native_host as fixture
from tests.test_native_verification import SCENARIOS


class DependencyObservationTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start

    def prepare(self):
        for name in ("mobile.txt", "backend.txt", "shared.txt"):
            (self.root / name).write_text("original")
        task_path = self.root / ".loop/tasks/implement.json"
        self.task = load(task_path)
        snapshot = self.service.team.snapshots.capture(self.root, load(self.root / ".loop/project.json"))
        common = [f["path"] for f in snapshot["manifest"]["files"] if f["path"] not in {"mobile.txt", "backend.txt"}]
        graph = {"schema_version": "1.0", "modules": [
            {"id": "shared", "paths": common, "depends_on": []},
            {"id": "backend", "paths": ["backend.txt"], "depends_on": ["shared"]},
            {"id": "mobile", "paths": ["mobile.txt"], "depends_on": ["backend"]}]}
        scenarios = [dict(SCENARIOS[0], id=key, depends_on=[], input_modules=[key],
                          check_id=self.task["checks"][0]["id"]) for key in ("backend", "mobile")]
        self.task["extensions"].update(verification_dependencies=graph, verification_scenarios=scenarios)
        task_path.write_text(json.dumps(self.task))
        self.start()
        self.workbench = self.service.workbench_prepare(self.team_id, "implement")["workbench"]
        self.args = dict(team_id=self.team_id, task_id="implement", workbench_id=self.workbench["id"],
                         owner="actual-tester", environment="Toolchain fixture v1 and isolated synthetic dataset v1")
        self.session = self.service.verification_begin(**self.args)
        for identity in ("backend", "mobile"):
            row = self.service.verification_update(self.team_id, self.session["id"], identity, "actual-tester", "start")
            artifact = Path(row["artifacts_directory"]) / (identity + ".txt")
            artifact.write_text("Actual offline observation")
            self.service.verification_update(self.team_id, self.session["id"], identity, "actual-tester", "pass",
                attempt_id=row["unresolved_attempt"]["id"], observation="Read fixture", artifacts=[artifact.name])

    def test_mobile_leaf_preserves_backend_but_shared_change_invalidates_both(self):
        self.prepare()
        root = Path(self.workbench["directory"])
        (root / "mobile.txt").write_text("mobile fix")
        current = self.service.verification_begin(**self.args)
        self.assertEqual(current["reused_scenarios"], ["backend"])
        backend = current["scenarios"][0]
        self.assertEqual(backend["attempt"]["reused_from"]["candidate"], self.session["candidate"])
        self.assertEqual(backend["attempts_remaining"], 1)
        with self.assertRaisesRegex(ContractError, "already passed"):
            self.service.verification_update(self.team_id, current["id"], "backend", "actual-tester", "start")
        (root / "shared.txt").write_text("API changed")
        final = self.service.verification_begin(**self.args)
        self.assertEqual(final["reused_scenarios"], [])
        child = self.service.team._child(self.service._data(self.team_id), "implement")
        self.assertEqual(child["selected_evidence"], [])
        self.assertEqual(child["state"]["status"], "PLANNING")

    def test_unknown_file_environment_change_and_artifact_corruption_refuse_reuse(self):
        self.prepare()
        root = Path(self.workbench["directory"])
        (root / "unknown.txt").write_text("undeclared")
        session = self.service.verification_begin(**self.args)
        self.assertEqual(session["reused_scenarios"], [])
        self.assertIn("coverage", session["reuse_problem"])
        (root / "unknown.txt").unlink(); (root / "mobile.txt").write_text("fix")
        session = self.service.verification_begin(**{**self.args, "environment": "different toolchain or dataset"})
        self.assertEqual(session["reused_scenarios"], [])
        original = self.service.verification_progress(self.team_id, self.session["id"])["sessions"][0]
        digest = original["scenarios"][0]["attempt"]["artifacts"][0]["sha256"]
        read = self.service.team.snapshots.read
        def corrupt(identity):
            if identity == digest: raise ContractError("Artifact hash mismatch")
            return read(identity)
        (root / "mobile.txt").write_text("another mobile fix")
        with patch.object(self.service.team.snapshots, "read", side_effect=corrupt):
            with self.assertRaisesRegex(ContractError, "Artifact hash"):
                self.service.verification_begin(**self.args)

    def test_missing_duplicate_and_cyclic_graphs_rejected_before_freeze(self):
        self.prepare()
        for mutation in ("missing", "cycle", "duplicate", "absent_graph"):
            task = deepcopy(self.task)
            nodes = task["extensions"]["verification_dependencies"]["modules"]
            if mutation == "missing": nodes[1]["depends_on"] = ["absent"]
            if mutation == "cycle": nodes[0]["depends_on"] = ["mobile"]
            if mutation == "duplicate": nodes.append(deepcopy(nodes[0]))
            if mutation == "absent_graph": del task["extensions"]["verification_dependencies"]
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                validate("task", task)
        snapshot = self.service.team.snapshots.capture(Path(self.workbench["directory"]), self.service.team.profile(self.service._data(self.team_id)))
        task = deepcopy(self.task); task["extensions"]["verification_dependencies"]["modules"][0]["paths"].append("mobile.txt")
        result, problem = bindings(task, snapshot, "env", "runtime")
        self.assertFalse(result); self.assertIn("ambiguous", problem)

    def test_same_source_new_environment_gets_fresh_session_without_reuse(self):
        self.prepare()
        session = self.service.verification_begin(**{**self.args, "environment": "new toolchain and data"})
        self.assertNotEqual(session["id"], self.session["id"])
        self.assertEqual(session["observed_pass"], 0)
        self.assertFalse(self.service.verification_progress(self.team_id, self.session["id"])["sessions"][0]["current"])
