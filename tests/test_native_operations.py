"""Real local checks and interrupted journals, with no model dispatch."""

import json
from pathlib import Path
import sys
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from loop_engineering.contracts import ContractError, ROOT, load
from loop_engineering.native_host import NativeHostService, render_progress
from loop_engineering.native_mcp import NativeMCPServer
from tests import test_native_host as fixture


class NativeOperationTests(unittest.TestCase):
    setUp = fixture.NativeHostTests.setUp
    start = fixture.NativeHostTests.start
    receive = fixture.NativeHostTests.receive

    def proposal(self):
        packet = self.service.task_request(self.team_id, "implement")
        return dict(team_id=self.team_id, task_id="implement", request_id=packet["team_assignment"]["request_id"],
                    step=self.owner.respond(packet))

    def wait(self, result):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = self.service.operations.status(self.team_id, result["operation_id"], 1)["operations"][0]
            if row["status"] != "RUNNING":
                return row
        self.fail("Offline operation did not finish")

    def seed_interruption(self, task_id="implement"):
        with self.service.team.teams.writer(self.team_id) as data:
            data["native_host"]["operations"] = {"operation-interrupted": {
                "id": "operation-interrupted", "key": "fixture", "kind": "task_submit", "task_id": task_id,
                "status": "RUNNING", "started_epoch": int(self.service.clock()) - 10,
                "finished_epoch": None, "error": None, "payload": self.service.team.snapshots.put(b'{}')}}
            self.service.team.teams.save(data, "fixture.interrupted_admission")
        return "operation-interrupted"

    def test_mcp_submission_is_responsive_idempotent_and_continues_to_handoff(self):
        self.start()
        args = self.proposal()
        entered, release = threading.Event(), threading.Event()
        original = self.service.task_submit
        def held(**arguments):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Fixture not released")
            return original(**arguments)
        self.addCleanup(release.set)
        server = NativeMCPServer(self.service)
        with patch.object(self.service, "task_submit", side_effect=held):
            result = server.handlers()["loop_task_submit"](**args)
            self.assertTrue(entered.wait(2))
            again = server.handlers()["loop_task_submit"](**args)
            self.assertEqual(again["operation_id"], result["operation_id"])
            state = server.handlers()["loop_progress"](team_id=self.team_id)
            self.assertEqual(state["next_action"]["tool"], "loop_operation_status")
            self.assertTrue(state["next_action"]["continue_work"])
            self.assertIn("后台检查与恢复", render_progress(state))
            with self.assertRaisesRegex(ContractError, "operation pending"):
                self.service.workbench_prepare(self.team_id, "implement")
            with self.assertRaisesRegex(ContractError, "still owns"):
                self.service.operations.recover(self.team_id, result["operation_id"])
            release.set()
            self.assertEqual(self.wait(result)["status"], "FINISHED")
        self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "receive_handoff")
        self.assertEqual(self.service.task_submit_background(**args)["operation_id"], result["operation_id"])
        self.assertEqual(len(self.service._data(self.team_id)["native_host"]["operations"]), 1)
        self.assertEqual(self.owner.transport.requests, [])
        self.service.team.teams.audit(self.team_id)

    def test_workbench_submission_freezes_content_before_background_execution(self):
        self.start()
        packet = self.service.workbench_prepare(self.team_id, "implement")
        draft = Path(packet["workbench"]["directory"])
        change = self.owner.respond(packet)["changes"][0]
        (draft / change["path"]).write_text(change["new_content"])
        original = self.service.task_submit
        entered, release = threading.Event(), threading.Event()
        def held(**arguments):
            entered.set(); release.wait(10)
            return original(**arguments)
        self.addCleanup(release.set)
        with patch.object(self.service, "task_submit", side_effect=held):
            result = self.service.workbench_submit_background(self.team_id, "implement", packet["workbench"]["id"], "Stable draft")
            self.assertTrue(entered.wait(2))
            (draft / change["path"]).write_text("raise Exception('late writer')\n")
            release.set()
            self.assertEqual(self.wait(result)["status"], "FINISHED")
        self.assertEqual((self.root / change["path"]).read_text(), change["new_content"])
        self.assertEqual(self.service.progress(self.team_id)["tasks"]["implement"]["status"], "HANDOFF")

    def test_real_long_subprocess_keeps_progress_and_cancel_available(self):
        path = self.root / ".loop/tasks/implement.json"
        task = load(path)
        task["checks"][0].update(argv=[sys.executable, "-c", "import time; time.sleep(20)"], timeout_seconds=30)
        path.write_text(json.dumps(task))
        self.start()
        result = self.service.task_submit_background(**self.proposal())
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = self.service.progress(self.team_id)
            if state["tasks"]["implement"].get("pending_process"):
                break
            time.sleep(.05)
        self.assertIsNotNone(state["tasks"]["implement"].get("pending_process"))
        started = time.monotonic()
        stopped = self.service.control(self.team_id, "cancel")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(stopped["status"], "CANCELLED")
        self.wait(result)
        child_id = self.service._data(self.team_id)["records"]["implement"]["child_run_id"]
        child = self.store.get(child_id)
        self.assertIsNone(child["pending_process"])
        self.assertEqual(child["state"]["status"], "CANCELLED")

    def test_reconnect_observes_original_runner_and_cannot_duplicate_it(self):
        self.start()
        args = self.proposal()
        entered, release = threading.Event(), threading.Event()
        original = self.service.task_submit
        def held(**arguments):
            entered.set(); release.wait(10)
            return original(**arguments)
        self.addCleanup(release.set)
        second = NativeHostService(self.root, self.store)
        self.addCleanup(second.close)
        with patch.object(self.service, "task_submit", side_effect=held):
            result = self.service.task_submit_background(**args)
            self.assertTrue(entered.wait(2))
            row = second.operations.status(self.team_id)["operations"][0]
            self.assertTrue(row["runner_alive"])
            self.assertFalse(row["recovery_required"])
            self.assertEqual(second.task_submit_background(**args)["operation_id"], result["operation_id"])
            with self.assertRaisesRegex(ContractError, "still owns"):
                second.operations.recover(self.team_id, result["operation_id"])
            release.set()
            self.wait(result)

    def test_orphan_admission_recovers_same_child_without_replaying_or_resetting_limits(self):
        self.start()
        self.proposal()
        data = self.service._data(self.team_id)
        child_id = data["records"]["implement"]["child_run_id"]
        before = self.store.get(child_id)
        identity = self.seed_interruption()
        self.assertEqual(self.service.next(self.team_id)["next_action"]["kind"], "recover_operation")
        recovered = self.service.operations.recover(self.team_id, identity)
        self.assertEqual(recovered["next_action"]["kind"], "prepare_task")
        after = self.store.get(child_id)
        self.assertEqual(after["task"], before["task"])
        self.assertEqual(after["steps"], before["steps"])
        self.assertGreaterEqual(after["elapsed_ms"], before["elapsed_ms"])
        self.assertEqual(self.service._data(self.team_id)["records"]["implement"]["child_run_id"], child_id)

    def test_orphan_recovery_preserves_user_pause(self):
        self.start(); self.proposal()
        identity = self.seed_interruption()
        self.service.control(self.team_id, "pause")
        with self.assertRaisesRegex(ContractError, "resume instruction"):
            self.service.operations.recover(self.team_id, identity)
        self.assertEqual(self.service.progress(self.team_id)["status"], "PAUSED")

    def test_server_crash_reconciles_owned_process_without_replaying_proposal(self):
        path = self.root / ".loop/tasks/implement.json"
        task = load(path)
        task["checks"][0].update(argv=[sys.executable, "-c", "import time; time.sleep(20)"], timeout_seconds=30)
        path.write_text(json.dumps(task))
        self.start()
        args = self.proposal()
        request_file = self.store.directory / "offline-operation-args.json"
        request_file.write_text(json.dumps(args))
        script = ("import json,sys,time\nfrom loop_engineering.native_host import NativeHostService\n"
                  "from loop_engineering.store import Store\nfrom pathlib import Path\n"
                  "s=NativeHostService(Path(sys.argv[1]),Store(Path(sys.argv[2])))\n"
                  "s.task_submit_background(**json.loads(Path(sys.argv[3]).read_text()))\n"
                  "while True: time.sleep(1)\n")
        process = subprocess.Popen([sys.executable, "-c", script, str(self.root), str(self.store.directory), str(request_file)],
                                   cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            data = self.service._data(self.team_id)
            child_id = data["records"]["implement"]["child_run_id"]
            child = self.store.get(child_id)
            if (child.get("pending_process") or {}).get("pid"):
                break
            if process.poll() is not None:
                self.fail("Fixture server exited: " + process.communicate()[1].decode())
            time.sleep(.05)
        self.assertTrue(child.get("pending_process"))
        self.assertTrue(child["steps"])
        iteration = child["state"]["iteration"]
        process.terminate(); process.communicate(timeout=5)
        identity = self.service.operations.status(self.team_id)["operations"][0]["id"]
        result = self.service.operations.recover(self.team_id, identity)
        recovered = self.store.get(child_id)
        self.assertIsNone(recovered["pending_process"])
        self.assertEqual(recovered["steps"], child["steps"])
        self.assertEqual(recovered["state"]["iteration"], iteration)
        self.assertEqual(result["tasks"]["implement"]["child_run_id"], child_id)
        self.assertEqual(result["next_action"]["kind"], "prepare_task")

    def test_completed_child_survives_missing_operation_completion_journal(self):
        self.start()
        self.service.task_submit(**self.proposal())
        child_id = self.service._data(self.team_id)["records"]["implement"]["child_run_id"]
        before = self.store.get(child_id)
        identity = self.seed_interruption()
        result = self.service.operations.recover(self.team_id, identity)
        self.assertEqual(result["next_action"]["kind"], "receive_handoff")
        self.assertEqual(self.store.get(child_id)["steps"], before["steps"])

    def test_failed_operation_retains_error_and_never_claims_completion(self):
        self.start()
        args = self.proposal()
        args["step"]["base_snapshot_digest"] = "sha256:" + "0" * 64
        result = self.service.task_submit_background(**args)
        row = self.wait(result)
        self.assertEqual(row["status"], "FAILED")
        self.assertTrue(row["error"])
        self.assertNotEqual(self.service.progress(self.team_id)["status"], "COMPLETE")

    def test_bounded_host_driver_reaches_completion_through_failure_repair_and_receipts(self):
        self.start()
        submitted, implement_children = 0, set()
        for _ in range(8):
            state = self.service.progress(self.team_id)
            action = state["next_action"]
            if action["kind"] == "complete":
                break
            self.assertTrue(action["continue_work"], action)
            if action["kind"] == "prepare_task":
                task_id = action["arguments"]["task_id"]
                request = self.service.task_request(self.team_id, task_id)
                step = self.owner.respond(request)
                if submitted == 0:
                    step["changes"][0]["new_content"] = "def slug(value): return 'incorrect'\n"
                result = self.service.task_submit_background(self.team_id, task_id,
                    request["team_assignment"]["request_id"], step)
                self.assertEqual(self.wait(result)["status"], "FINISHED")
                submitted += 1
                if task_id == "implement":
                    implement_children.add(self.service._data(self.team_id)["records"][task_id]["child_run_id"])
            elif action["kind"] == "receive_handoff":
                self.receive(action["arguments"]["task_id"])
            else:
                self.fail("Unexpected continuation: " + str(action))
        else:
            self.fail("Host driver failed to reach the agreed batch boundary")
        self.assertEqual(submitted, 3)
        self.assertEqual(len(implement_children), 1)
        self.assertTrue(state["final_candidate_current"])
        self.assertEqual(state["task_counts"]["COMPLETE"], 2)
        self.assertEqual(self.owner.transport.requests, [])

    def test_wait_validation_and_unknown_operations_do_not_dispatch(self):
        self.start()
        for invalid in (-1, 6, True, float('nan')):
            with self.assertRaises(ContractError):
                self.service.operations.status(self.team_id, wait_seconds=invalid)
        with self.assertRaisesRegex(ContractError, "Unknown"):
            self.service.operations.status(self.team_id, "absent")
        self.assertEqual(self.owner.transport.requests, [])
