"""Native stdio/client and owned-waiter tests; never launch a live model."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from loop_engineering.adapters import CommandDriver
from loop_engineering.budgets import BudgetError
from loop_engineering.contracts import ROOT, ContractError
from loop_engineering.desktop_bridge import MAX_MESSAGE_BYTES, DesktopService, desktop_config
from loop_engineering.desktop_mcp import MCPServer
from loop_engineering.team_automation import TeamAutomation, RECEIPT_RESPONSE
from loop_engineering.team_host import TeamHost
from tests import test_team_automation as automation_fixture


class OfflineReviewDriver(CommandDriver):
    name = "codex-cli"
    capabilities = {"text_input": True, "headless_execution": True, "structured_output": True,
                    "structured_events": True, "usage_reporting": True}

    def __init__(self, *args, **kwargs):
        pass

    def command(self, work, artifacts):
        code = "import sys,json; raw=sys.stdin.read(); assert raw.startswith('Return one JSON response'); print(json.dumps({'result':'pass','summary':'Explicit offline independent judgment fixture','findings':[]}))"
        return [sys.executable, "-c", code]

    def token_report(self, result):
        return {"tokens": 20, "known_tokens": 20}


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.fixture = automation_fixture.AutomationTests()
        self.fixture.setUp()
        self.fixture.policy["timeout_seconds"] = 6
        self.fixture.write_policy()
        self.addCleanup(self.fixture.doCleanups)
        self.team, self.root = self.fixture.team, self.fixture.root
        self.service = DesktopService(self.root, self.team.store)
        # Inject an observable birth identity; the outer macOS test sandbox
        # denies ps. Process creation, liveness and cancellation remain real.
        for name in ("loop_engineering.team_host.process_identity", "loop_engineering.desktop_bridge.process_identity"):
            probe = patch(name, side_effect=lambda pid: "offline-owned-pid:" + str(pid))
            probe.start()
            self.addCleanup(probe.stop)
        self.host = TeamHost(self.team, desktop=True)
        self.auto = TeamAutomation(self.team, self.host)
        self.team_id = None

    def start(self):
        self.team_id = self.auto.start(self.root, self.fixture.policy_path)["team_id"]
        self.addCleanup(lambda: self.team.stop(self.team_id, cancel=True)
                        if self.team.teams.get(self.team_id)["status"] not in {"COMPLETE", "CANCELLED"} else None)

    def wait_pending(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            values = self.service.pending()["requests"]
            if values:
                return values[0]
            time.sleep(.03)
        self.fail("No actual owned Desktop request was admitted")

    def receipt(self):
        return {"kind": "team-recipient-request", "role": "coordinator", "request_id": "fixture-request"}

    def rpc(self, process, request):
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        if "id" not in request:
            return None
        ready, _, _ = select.select([process.stdout], [], [], 5)
        self.assertTrue(ready, "MCP stdio response timed out")
        result = json.loads(process.stdout.readline())
        self.assertEqual(result["id"], request["id"])
        self.assertNotIn("error", result)
        return result["result"]

    def test_actual_stdio_desktop_completes_local_work_with_unknown_spend(self):
        self.start()
        code = "import runpy,sys; import loop_engineering.desktop_bridge as bridge; bridge.process_identity=lambda pid:'offline-owned-pid:'+str(pid); script=sys.argv.pop(1); runpy.run_path(script,run_name='__main__')"
        process = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "scripts/desktop_mcp.py"),
                                    "--project", str(self.root), "--state-dir", str(self.team.store.directory)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, bufsize=1)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.addCleanup(cleanup)
        initialized = self.rpc(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                                   "clientInfo": {"name": "offline-client", "version": "1"}}})
        self.assertEqual(initialized["protocolVersion"], "2025-11-25")
        self.rpc(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(len(self.rpc(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["tools"]), 5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.auto.run, self.team_id)
            number = 3
            deadline = time.monotonic() + 25
            while not future.done() and time.monotonic() < deadline:
                result = self.rpc(process, {"jsonrpc": "2.0", "id": number, "method": "tools/call",
                                          "params": {"name": "loop_pending_requests", "arguments": {}}})
                number += 1
                self.assertFalse(result["isError"])
                for job in json.loads(result["content"][0]["text"])["requests"]:
                    args = {key: job[key] for key in ("team_id", "operation_id")}
                    claimed = self.rpc(process, {"jsonrpc": "2.0", "id": number, "method": "tools/call",
                                                 "params": {"name": "loop_claim_request", "arguments": args}})
                    number += 1
                    self.assertFalse(claimed["isError"])
                    packet = json.loads(claimed["content"][0]["text"])
                    response = self.fixture.respond(packet["request"])
                    args.update(lease_id=packet["lease_id"], response=response)
                    submitted = self.rpc(process, {"jsonrpc": "2.0", "id": number, "method": "tools/call",
                                                   "params": {"name": "loop_submit_response", "arguments": args}})
                    number += 1
                    self.assertFalse(submitted["isError"], submitted)
                time.sleep(.03)
            if not future.done():
                self.team.stop(self.team_id, cancel=True)
            report = future.result(timeout=5)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertTrue(report["final_candidate_current"])
        self.assertIsNone(report["team_budget"]["actual_tokens"])
        self.assertEqual(report["team_budget"]["unknown_requests"], 4)
        self.team.teams.audit(self.team_id)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertEqual(process.stderr.read(), "")
        process.stdout.close(); process.stderr.close()

    def test_lease_invalid_response_replay_and_cross_connection_conflict(self):
        self.start()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.host.invoke, self.team_id, self.receipt(), RECEIPT_RESPONSE)
            job = self.wait_pending()
            packet = self.service.claim(self.team_id, job["operation_id"])
            other = DesktopService(self.root, self.team.store)
            with self.assertRaisesRegex(ContractError, "Another Desktop"):
                other.claim(self.team_id, job["operation_id"])
            with self.assertRaisesRegex(ContractError, "lease"):
                self.service.submit(self.team_id, job["operation_id"], "wrong", {"accept": True, "note": "actual fixture"})
            with self.assertRaisesRegex(ContractError, "schema"):
                self.service.submit(self.team_id, job["operation_id"], packet["lease_id"], {"accept": "yes"})
            reply = {"accept": True, "note": "Explicit offline fixture"}
            first = self.service.submit(self.team_id, job["operation_id"], packet["lease_id"], reply)
            self.assertFalse(first["replayed"])
            self.assertEqual(future.result(timeout=5), reply)
            self.assertTrue(self.service.submit(self.team_id, job["operation_id"], packet["lease_id"], reply)["replayed"])
            with self.assertRaisesRegex(ContractError, "Conflicting"):
                self.service.submit(self.team_id, job["operation_id"], packet["lease_id"], {"accept": False, "note": "different"})

    def test_other_projects_stale_inputs_and_orphaned_waiters_are_refused(self):
        self.start()
        other_root = self.fixture.base / "another-project"
        other_root.mkdir()
        with self.assertRaisesRegex(ContractError, "outside"):
            DesktopService(other_root, self.team.store).status(self.team_id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.host.invoke, self.team_id, self.receipt(), RECEIPT_RESPONSE)
            job = self.wait_pending()
            with patch("loop_engineering.desktop_bridge.process_identity", return_value="wrong process birth"):
                with self.assertRaisesRegex(ContractError, "live owned"):
                    self.service.claim(self.team_id, job["operation_id"])
            spec = self.root / ".loop/spec.md"
            original = spec.read_bytes()
            spec.write_bytes(original + b"\nChanged task\n")
            with self.assertRaises(ContractError):
                self.service.claim(self.team_id, job["operation_id"])
            spec.write_bytes(original)
            self.team.stop(self.team_id, cancel=True)
            with self.assertRaises(ContractError):
                future.result(timeout=5)
        self.assertEqual(self.service.pending()["requests"], [])
        self.assertEqual(self.team.status(self.team_id)["team_budget"]["unknown_requests"], 1)

    def test_timeout_cancels_waiter_and_preserves_unknown_accounting(self):
        self.fixture.policy["timeout_seconds"] = 1
        self.fixture.write_policy()
        self.start()
        with self.assertRaises(ContractError):
            self.host.invoke(self.team_id, self.receipt(), RECEIPT_RESPONSE)
        data = self.team.teams.get(self.team_id)
        operation = next(iter(data["automation"]["operations"].values()))
        self.assertIsNone(operation["pid"])
        self.assertEqual(self.service.pending()["requests"], [])
        self.assertEqual(self.team.status(self.team_id)["team_budget"]["unknown_requests"], 1)

    def test_hard_spend_refuses_before_any_waiter_or_desktop_data_export(self):
        self.fixture.policy["max_tokens"] = 100
        self.fixture.write_policy()
        self.start()
        with patch("subprocess.Popen", side_effect=AssertionError("No waiter allowed")):
            with self.assertRaises(BudgetError):
                self.host.invoke(self.team_id, self.receipt(), RECEIPT_RESPONSE)
        self.assertEqual(self.service.pending()["requests"], [])

    def test_desktop_cannot_issue_independent_verdict_without_separate_host(self):
        self.start()
        with self.assertRaisesRegex(ContractError, "fresh independent review"):
            self.host.invoke(self.team_id, {"kind": "team-evaluator-request"}, {})
        self.assertEqual(self.team.status(self.team_id)["team_budget"]["dispatches"], 0)

    def test_missing_process_identity_refuses_before_any_request(self):
        with patch("loop_engineering.team_host.process_identity", return_value=None):
            with self.assertRaisesRegex(ContractError, "birth identity"):
                TeamHost(self.team, desktop=True)
        self.assertEqual(self.service.pending()["requests"], [])

    def test_native_question_answer_is_recorded_without_model_dispatch(self):
        self.fixture.coordinator_response()
        self.fixture.plan_response["questions"] = [{"id":"compatibility", "question":"Preserve nonempty behavior?", "reason":"Define compatibility", "blocking":True}]
        self.auto = self.fixture.auto
        self.start()
        self.assertEqual(self.auto.run(self.team_id)["status"], "AWAITING_INPUT")
        dispatches = self.team.status(self.team_id)["team_budget"]["dispatches"]
        with patch("subprocess.Popen", side_effect=AssertionError("Answer cannot launch a model")):
            self.service.answer(self.team_id, "compatibility", "Yes, preserve the existing behavior.")
        answers = json.loads((self.root / ".loop/questions.json").read_text())["questions"][0]["answers"]
        self.assertEqual(answers, ["Yes, preserve the existing behavior."])
        self.assertEqual(self.team.status(self.team_id)["team_budget"]["dispatches"], dispatches)

    def test_desktop_work_and_fresh_review_import_reach_verified_native_acceptance(self):
        self.fixture.native_acceptance()
        with patch("loop_engineering.team_host.CodexDriver", OfflineReviewDriver), patch("loop_engineering.team_host.TeamCodexDriver", OfflineReviewDriver):
            self.host = TeamHost(self.team, desktop=True, review_codex=True)
            self.auto = TeamAutomation(self.team, self.host)
            self.start()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.auto.run, self.team_id)
                deadline = time.monotonic() + 25
                while not future.done() and time.monotonic() < deadline:
                    for job in self.service.pending()["requests"]:
                        packet = self.service.claim(self.team_id, job["operation_id"])
                        self.assertNotEqual(packet["request"]["kind"], "team-evaluator-request")
                        self.service.submit(self.team_id, job["operation_id"], packet["lease_id"], self.fixture.respond(packet["request"]))
                    time.sleep(.03)
                if not future.done():
                    self.team.stop(self.team_id, cancel=True)
                report = future.result(timeout=5)
        self.assertEqual(report["status"], "COMPLETE", report["reason"])
        self.assertTrue(report["final_candidate_current"])
        data = self.team.teams.audit(self.team_id)
        reviews = [op for op in data["automation"]["operations"].values() if op["request"]["kind"]=="team-evaluator-request"]
        self.assertEqual(len(reviews),2)
        self.assertTrue(all(op["host"]["adapter_id"]=="codex-cli" for op in reviews))
        self.assertNotEqual(reviews[0]["request"]["request"]["context_id"], reviews[1]["request"]["request"]["context_id"])
        child = self.team.store.replay(report["tasks"]["verify"]["integration_run_id"])
        self.assertTrue(child["native"]["external_origins"])
        self.assertEqual(report["team_budget"]["unknown_requests"],4)

    def test_separate_reviewer_routes_through_fresh_owned_process_and_actual_host_record(self):
        self.start()
        schema = {"type": "object", "properties": {"result": {"enum": ["pass"]}, "summary": {"type": "string"},
                   "findings": {"type": "array"}}, "required": ["result", "summary", "findings"], "additionalProperties": False}
        with patch("loop_engineering.team_host.CodexDriver", OfflineReviewDriver), patch("loop_engineering.team_host.TeamCodexDriver", OfflineReviewDriver):
            host = TeamHost(self.team, desktop=True, review_codex=True)
            result = host.invoke(self.team_id, {"kind": "team-evaluator-request", "role": "reviewer"}, schema)
        self.assertEqual(result["result"], "pass")
        operation = next(iter(self.team.teams.get(self.team_id)["automation"]["operations"].values()))
        self.assertEqual(operation["host"]["adapter_id"], "codex-cli")
        self.assertNotIn("desktop", operation)
        self.assertEqual(self.team.status(self.team_id)["team_budget"]["known_tokens"], 20)

    def test_config_is_scoped_and_preserves_explicit_python_without_settings_writes(self):
        config = desktop_config(self.root, self.team.store.directory)
        entry = config["mcpServers"]["loop-development"]
        self.assertEqual(entry["command"], str(Path(sys.executable).absolute()))
        self.assertIn(str(self.root.resolve()), entry["args"])
        with self.assertRaises(ContractError):
            desktop_config(self.root, self.root / ".loop/state")

    def test_protocol_frames_errors_initialization_and_no_arbitrary_tools(self):
        server = MCPServer(self.service)
        result = server.message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(result["error"]["code"], -32000)
        frames = [b"not-json\n", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name":"offline-client","version":"1"}}}).encode()+b"\n",
                  b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
                  b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"shell","arguments":{}}}\n',
                  b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"loop_pending_requests","arguments":{"path":"/etc/passwd"}}}\n']
        output = io.StringIO()
        server.serve(io.BytesIO(b"".join(frames)), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[2]["error"]["code"], -32602)
        self.assertEqual(responses[3]["error"]["code"], -32602)

    def test_wire_size_bounds_resynchronize_and_reject_oversize_result(self):
        server = MCPServer(self.service)
        output = io.StringIO()
        server.serve(io.BytesIO(b'x'*(MAX_MESSAGE_BYTES+10)+b'\n'+b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'),output)
        responses=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"],-32600)
        self.assertEqual(responses[1]["result"],{})
        self.assertEqual(server.message({"jsonrpc":"2.0","id":2,"method":"server/discover"})["error"]["code"],-32601)
        server.message({"jsonrpc":"2.0","id":3,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"offline","version":"1"}}})
        server.message({"jsonrpc":"2.0","method":"notifications/initialized"})
        output=io.StringIO()
        with patch.object(self.service,'status',return_value={'large':'x'*MAX_MESSAGE_BYTES}):
            server.serve(io.BytesIO(b'{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"loop_team_status","arguments":{}}}\n'),output)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"],-32603)
