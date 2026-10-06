"""Owned deterministic installers; no package registry or model dispatch."""

from copy import deepcopy
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_engine import LocalCase, FIXED
from loop_engineering.contracts import ContractError, load, ROOT
from loop_engineering.execution_tools import output_identity
from loop_engineering.provision import copy_dependencies
from loop_engineering.processes import execute
from loop_engineering.workspace import byte_digest


class ProvisionTests(LocalCase):
    def configure_tools(self, program=None):
        tools = load(ROOT / "templates/scenarios/development/execution-tools.json")
        tools["provision"].update(inputs=["deps.lock"], output_dirs=[".deps"], steps=[{
            "id": "install", "argv": [sys.executable, "-c", program or
                "from pathlib import Path; p=Path('.deps'); p.mkdir(exist_ok=True); (p/'value').write_text('installed')"],
            "cwd": ".", "timeout_seconds": 2}])
        (self.root / "deps.lock").write_text("pinned dependency")
        (self.root / ".loop/execution-tools.json").write_text(json.dumps(tools))
        profile = load(self.profile_path)
        profile.update(execution_tools=".loop/execution-tools.json")
        profile["snapshot"]["exclude"].append(".deps/**")
        profile["snapshot"]["exclude"].append(".deps")
        self.profile_path.write_text(json.dumps(profile))
        return tools, profile

    def test_dependency_setup_is_logged_copied_and_bound_to_environment(self):
        self.configure_tools()
        data = self.start(baseline=True)
        self.assertEqual(data["provisioning"]["status"], "COMPLETE")
        self.assertEqual(data["provisioning"]["steps"][0]["outcome"], "completed")
        roots = list(self.store.run_dir(self.run_id).glob("baseline-*/.deps/value"))
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].read_text(), "installed")
        before = self.engine.environment_digest(data)
        (self.root / ".deps/value").write_text("tampered")
        self.assertNotEqual(before, self.engine.environment_digest(data))
        with self.assertRaisesRegex(ContractError, "dependency bytes changed"):
            self.engine.verify(self.run_id)

    def test_existing_dependencies_do_not_run_installer_again(self):
        self.configure_tools(); self.start()
        with patch.object(self.engine, "launch", wraps=self.engine.launch) as launch:
            self.engine.verify(self.run_id)
        self.assertTrue(launch.call_args_list)
        self.assertTrue(all(call.kwargs["kind"] == "check" for call in launch.call_args_list))

    def test_failure_requires_operator_reconciliation_before_retry(self):
        self.configure_tools("raise SystemExit(9)")
        with self.assertRaisesRegex(ContractError, "did not complete"):
            self.start()
        run_id = self.store.runs()[0]["run_id"]
        record = self.store.get(run_id)["provisioning"]
        self.assertEqual(record["status"], "FAILED")
        self.engine.resume(run_id)
        with self.assertRaisesRegex(ContractError, "explicit reconciliation"):
            self.engine.verify(run_id)
        data = self.engine.reconcile_process(run_id, record["action_id"], "Inspected retained exit 9 and stopped process group")
        self.assertEqual(data["provisioning"]["status"], "RETRY_ALLOWED")
        self.engine.resume(run_id)
        with self.assertRaisesRegex(ContractError, "did not complete"):
            self.engine.verify(run_id)
        self.assertEqual(len(self.store.get(run_id)["provision_history"]), 1)

    def test_installer_cannot_change_business_source_or_lock(self):
        self.configure_tools("from pathlib import Path; Path('slug.py').write_text('changed')")
        with self.assertRaisesRegex(ContractError, "modified source"):
            self.start()
        run_id = self.store.runs()[0]["run_id"]
        action = self.store.get(run_id)["provisioning"]["action_id"]
        with self.assertRaisesRegex(ContractError, "Restore the source"):
            self.engine.reconcile_process(run_id, action, "I inspected the mutation")

    def test_installer_timeout_is_retained_and_not_a_pass(self):
        self.configure_tools("import time; time.sleep(10)")
        with self.assertRaises(ContractError):
            self.start()
        data = self.store.get(self.store.runs()[0]["run_id"])
        self.assertEqual(data["provisioning"]["steps"][0]["outcome"], "timeout")
        self.assertEqual(data["baseline"], [])

    def test_escaping_dependency_symlinks_are_refused(self):
        tools, profile = self.configure_tools()
        (self.root / ".deps").mkdir()
        (self.root / ".deps/escape").symlink_to(sys.executable)
        with self.assertRaisesRegex(ContractError, "escapes"):
            output_identity(self.root, tools["provision"], profile)

    def test_absolute_owned_links_are_relocated_and_modes_preserved(self):
        tools, profile = self.configure_tools()
        deps = self.root / ".deps"; deps.mkdir()
        (deps / "tool").write_text("tool bytes"); (deps / "tool").chmod(0o755)
        (deps / "alias").symlink_to(deps / "tool")
        target = self.base / "copy"; target.mkdir()
        digest = copy_dependencies(self.root, target, tools, profile)
        self.assertEqual(digest, output_identity(target, tools["provision"], profile))
        self.assertEqual((target / ".deps/alias").resolve(), (target / ".deps/tool").resolve())
        (target / ".deps/tool").chmod(0o644)
        self.assertNotEqual(digest, output_identity(target, tools["provision"], profile))

    def test_dependency_copy_mutation_makes_check_inconclusive(self):
        self.configure_tools()
        task = load(self.task_path)
        task["checks"][0]["argv"] = [sys.executable, "-c", "from pathlib import Path; Path('.deps/value').write_text('check mutation')"]
        self.write_task(task)
        data = self.start(baseline=True)
        evidence = self.store.evidence(self.run_id, data["baseline"])
        self.assertEqual(evidence[0]["result"], "inconclusive")
        self.assertTrue(evidence[0]["extensions"]["input_mutation"])
        self.assertEqual((self.root / ".deps/value").read_text(), "installed")

    def test_process_output_limit_also_applies_to_fast_exit(self):
        result = execute([sys.executable, "-c", "print('x' * 10000)"], self.root, self.base / "logs", timeout=2, max_output_bytes=100)
        self.assertEqual(result.outcome, "output_limit")

    def test_setup_crash_keeps_unknown_effect_until_explicit_reconciliation(self):
        self.configure_tools()
        with patch("loop_engineering.controller.execute", side_effect=RuntimeError("Crash at dispatch")), self.assertRaises(RuntimeError):
            self.start()
        run_id = self.store.runs()[0]["run_id"]
        data = self.store.get(run_id)
        self.assertEqual(data["provisioning"]["status"], "RUNNING")
        self.assertEqual(data["provisioning"]["action_id"], data["pending_process"]["action_id"])
        with self.assertRaises(ContractError): self.engine.resume(run_id)
        self.engine.reconcile_process(run_id, data["pending_process"]["action_id"], "Inspected injected pre-launch crash; no process exists")
        self.engine.resume(run_id)
        self.engine.verify(run_id)
        self.assertEqual(self.store.get(run_id)["provisioning"]["status"], "COMPLETE")
        self.assertEqual(len(self.store.get(run_id)["provision_history"]), 1)

    def test_cancellation_stops_owned_installer_group_and_retains_attempt(self):
        self.configure_tools("import time; time.sleep(10)")
        run_id = "run-" + uuid4().hex
        def cancel():
            for _ in range(100):
                try:
                    if self.store.get(run_id).get("pending_process"):
                        self.store.cancel(run_id); return
                except (FileNotFoundError, ContractError): pass
                time.sleep(0.01)
        worker = threading.Thread(target=cancel); worker.start()
        try:
            with self.assertRaisesRegex(ContractError, "did not complete|stopped"):
                self.engine.start(self.root, self.task_path, self.profile_path, baseline=False, run_id=run_id)
        finally:
            worker.join(timeout=2)
        data = self.store.get(run_id)
        self.assertEqual(data["provisioning"]["status"], "CANCELLED")
        self.assertEqual(data["provisioning"]["steps"][0]["outcome"], "cancelled")
        self.assertIsNone(data["pending_process"])

    def test_new_lock_input_reprovisions_without_discarding_previous_history(self):
        self.configure_tools("from pathlib import Path; p=Path('.deps'); p.mkdir(exist_ok=True); (p/'value').write_text(Path('deps.lock').read_text())")
        task = load(self.task_path); task["scope"]["write_allow"].append("deps.lock"); self.write_task(task)
        self.start()
        step = self.step(); step["changes"].append({"path": "deps.lock", "expected_sha256":
            byte_digest(b"pinned dependency"), "new_content": "next pinned version"})
        data = self.engine.submit(self.run_id, step)
        self.assertEqual(data["state"]["status"], "SUCCEEDED")
        self.assertEqual((self.root / ".deps/value").read_text(), "next pinned version")
        self.assertEqual(len(data["provision_history"]), 1)
        self.assertEqual(data["provision_history"][0]["status"], "COMPLETE")
