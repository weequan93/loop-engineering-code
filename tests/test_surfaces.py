import io
import json
from pathlib import Path
import shutil
import subprocess

from loop_engineering import adapters, engine
from loop_engineering.install import install, remove_toml_table
from loop_engineering.mcp_server import Server, serve
from loop_engineering.migrate import build_goal, convert_check, plan_drafts
from loop_engineering.util import file_digest

from tests.helpers import ProjectCase


def call(server, name, **args):
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})["result"]
    return reply["isError"], json.loads(reply["content"][0]["text"])


class MCPTests(ProjectCase):
    def test_protocol_and_tool_sets(self):
        interactive = Server(self.project, autonomous=False)
        init = interactive.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18"}})["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIsNone(interactive.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = {t["name"] for t in interactive.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                 ["result"]["tools"]}
        self.assertIn("loop_goal_approve", names)
        self.assertNotIn("loop_review", names)
        auto = {t["name"] for t in Server(self.project, autonomous=True).tools}
        self.assertNotIn("loop_goal_approve", auto)
        self.assertNotIn("loop_answer", auto)
        reviewer = {t["name"] for t in Server(self.project, autonomous=True, review_token="t").tools}
        self.assertEqual(reviewer, {"loop_status", "loop_next", "loop_note", "loop_review"})
        self.assertLessEqual(len(names), 16)  # keep the tool surface small

    def test_chat_intake_and_errors_are_tool_errors(self):
        server = Server(self.project, autonomous=False)
        error, value = call(server, "loop_status")
        self.assertTrue(error)
        self.assertIn("No goal", value["error"])
        error, value = call(server, "loop_goal_draft", kind="develop", title="T", objective="O",
                            acceptance=[{"id": "tests", "run": "python3 test_calc.py"}])
        self.assertFalse(error)
        error, value = call(server, "loop_goal_approve", confirmed_by_user=True)
        self.assertEqual(value["status"], "ready")
        error, value = call(server, "loop_next")
        self.assertEqual(value["action"]["kind"], "intake")
        self.assertIn("Role: product manager", value["assignment"])
        error, value = call(server, "loop_stage_done", stage="intake", path=value["action"]["output"])
        self.assertTrue(error)  # the document must exist first
        target = self.root / call(server, "loop_next")[1]["action"]["output"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Product brief\n\nProblem, users, journeys, success metrics and scope for the test.\n")
        error, value = call(server, "loop_stage_done", stage="intake", path=str(target.relative_to(self.root)))
        self.assertFalse(error, value)
        self.assertEqual(value["action"]["kind"], "requirements")
        error, value = call(server, "loop_requirements", requirements=[
            {"id": "R1", "text": "t", "source": "README", "verify": "unit"}], final=True)
        self.assertFalse(error, value)
        error, value = call(server, "loop_next")
        self.assertEqual((value["action"]["kind"], value["action"]["review"]), ("review", "requirements_review"))
        error, value = call(server, "loop_plan", tasks=[{"id": "BAD ID", "title": "x"}])
        self.assertTrue(error)

    def test_stdio_loop(self):
        lines = "\n".join(json.dumps(m) for m in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"}, "not json"]) + "\n"
        out = io.StringIO()
        serve(self.project, False, stdin=io.StringIO(lines.replace('"not json"', "not json")), stdout=out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "loop")
        self.assertEqual(replies[2]["error"]["code"], -32700)


class InstallTests(ProjectCase):
    def test_install_merges_and_removes_legacy_entries(self):
        (self.root / ".codex").mkdir()
        (self.root / ".codex" / "config.toml").write_text(
            'model = "x"\n\n[mcp_servers.loop-native]\ncommand = "old"\n\n[mcp_servers.loop-native.env]\nA = "1"\n'
            '\n[mcp_servers.other]\ncommand = "keep"\n')
        (self.root / ".mcp.json").write_text(json.dumps({"mcpServers": {"loop-native": {}, "mine": {"command": "a"}}}))
        result = install(self.project, ["claude", "codex", "generic"])
        config = (self.root / ".codex" / "config.toml").read_text()
        self.assertNotIn("loop-native", config)
        self.assertIn("[mcp_servers.other]", config)
        self.assertIn("[mcp_servers.loop]", config)
        self.assertIn('default_tools_approval_mode = "approve"', config)
        mcp = json.loads((self.root / ".mcp.json").read_text())
        self.assertEqual(set(mcp["mcpServers"]), {"mine", "loop"})
        self.assertTrue((self.root / ".claude/skills/loop/SKILL.md").is_file())
        self.assertTrue((self.root / ".agents/skills/loop/SKILL.md").is_file())
        self.assertIn("loop-engineering (managed)", (self.root / "AGENTS.md").read_text())
        self.assertTrue(result["changed"])
        again = install(self.project, ["claude", "codex", "generic"])
        self.assertEqual(again["changed"], [])
        wrapper = self.root / ".loop" / "bin" / "loop"
        out = subprocess.run([str(wrapper), "list"], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        if shutil.which("codex"):
            # The generated TOML must parse.
            import tomllib
            tomllib.loads(config)

    def test_remove_toml_table_keeps_other_tables(self):
        text = "[a]\nx=1\n[a.b]\ny=2\n[ab]\nz=3\n"
        self.assertEqual(remove_toml_table(text, "a"), "[ab]\nz=3\n")


class AdapterTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.store = self.goal()
        self.goal_data = self.store.goal()

    def test_claude_build_and_parse(self):
        adapter = adapters.ClaudeAdapter({})
        work = self.root / ".loop" / "w"
        work.mkdir()
        turn = adapter.build("hi", self.root, work, {**self.goal_data, "policy": {**self.goal_data["policy"],
                                                                                   "max_cost_usd": 2.5}}, "sess")
        self.assertIn("--mcp-config", turn.argv)
        self.assertIn("--max-budget-usd", turn.argv)
        self.assertEqual(turn.argv[turn.argv.index("--resume") + 1], "sess")
        self.assertEqual(turn.stdin, "hi")
        review = adapter.build("r", self.root, work, self.goal_data, None, mcp_args=["--review-token", "t"],
                               readonly=True)
        self.assertIn("Edit,Write,NotebookEdit", review.argv)
        spec = json.loads((work / "mcp.json").read_text())["mcpServers"]["loop"]
        self.assertIn("--review-token", spec["args"])
        out = work / "out.jsonl"
        out.write_text("\n".join(json.dumps(x) for x in [
            {"type": "system", "subtype": "init", "session_id": "s1"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}},
            {"type": "result", "session_id": "s1", "total_cost_usd": 0.42, "result": "done it",
             "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7}}]))
        report = adapter.parse(out, work / "none", work)
        self.assertEqual((report.session_id, report.cost_usd, report.input_tokens, report.output_tokens,
                          report.summary), ("s1", 0.42, 15, 7, "done it"))

    def test_codex_build_and_parse(self):
        adapter = adapters.CodexAdapter({})
        adapter._help = "--no-daemon"
        work = self.root / ".loop" / "w2"
        work.mkdir()
        fresh = adapter.build("p", self.root, work, self.goal_data, None)
        self.assertEqual(fresh.argv[1:7], ["--no-daemon", "-a", "never", "-s", "workspace-write", "exec"])
        self.assertIn("-C", fresh.argv)
        self.assertEqual(fresh.argv[-1], "-")
        self.assertTrue(any(a.startswith("mcp_servers.loop.args=") for a in fresh.argv))
        self.assertTrue(any(a == "sandbox_workspace_write.network_access=true" for a in fresh.argv))
        # Regression: without this, `-a never` makes Codex reject every non-read-only loop tool call.
        self.assertIn('mcp_servers.loop.default_tools_approval_mode="approve"', fresh.argv)
        resumed = adapter.build("p", self.root, work, self.goal_data, "abc")
        self.assertEqual(resumed.argv[resumed.argv.index("exec") + 1:resumed.argv.index("exec") + 3], ["resume", "abc"])
        self.assertNotIn("-C", resumed.argv)
        review = adapter.build("p", self.root, work, self.goal_data, None, readonly=True)
        self.assertEqual(review.argv[review.argv.index("-s") + 1], "read-only")
        out = work / "events.jsonl"
        out.write_text("\n".join(json.dumps(x) for x in [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}]))
        report = adapter.parse(out, work / "none", work)
        self.assertEqual((report.session_id, report.input_tokens, report.output_tokens, report.cost_usd),
                         ("t1", 100, 20, None))

    def test_unknown_adapter_and_command_template(self):
        from loop_engineering.util import LoopError
        with self.assertRaises(LoopError):
            adapters.get("nope", self.root)
        adapter = adapters.get("fake", self.root)
        work = self.root / ".loop" / "w3"
        work.mkdir()
        turn = adapter.build("prompt", self.root, work, self.goal_data, None)
        self.assertTrue(turn.argv[-1].endswith("mcp.json"))
        self.assertEqual(turn.stdin, "prompt")


class MigrationTests(ProjectCase):
    def export(self):
        return {
            "workflow": {"workflow_id": "r1", "final_task": "final", "tasks": [
                {"id": "build", "phase": "implement", "role": "backend", "depends_on": []},
                {"id": "final", "phase": "accept", "role": "tester", "depends_on": ["build"]}]},
            "selected_team": {"team_id": "team-1", "final_task": "final", "scope": "Ship R1",
                              "records": {"build": {"status": "COMPLETE"}, "final": {"status": "RUNNING",
                                                                                     "reason": "blocked on EPERM"}},
                              "tasks": {"build": {"phase": "implement", "role": "backend", "depends_on": []},
                                        "final": {"phase": "accept", "role": "tester", "depends_on": ["build"]}}},
            "task_contracts": {
                "build": {"objective": "Build it", "criteria": ["compiles"],
                          "checks": [{"id": "unit", "type": "command", "argv": ["python3", "test_calc.py"],
                                      "timeout_seconds": 60}]},
                "final": {"objective": "Accept", "checks": [
                    {"id": "all", "type": "command", "argv": ["python3", "test_calc.py"], "timeout_seconds": 120},
                    {"id": "independent", "type": "review", "description": "Review", "procedure": "Read it"},
                    {"id": "gui", "type": "interaction", "procedure": "Click"}]}},
            "questions": [{"id": "scope", "question": "R1 only?", "answers": ["yes"]}],
            "supervisors": [], "drafts": []}

    def test_convert_and_build_goal(self):
        command, gate = convert_check({"id": "X_1", "type": "command", "argv": ["a"], "timeout_seconds": 5})
        self.assertEqual((command["id"], command["timeout"], gate), ("x_1", 5, None))
        _, gate = convert_check({"id": "gui", "type": "interaction", "procedure": "click"})
        self.assertEqual(gate["by"], "human")
        goal, tasks, integrate = build_goal(self.project, self.export(), [])
        self.assertEqual([c["id"] for c in goal["acceptance"]], ["all"])
        self.assertEqual([(r["id"], r["by"]) for r in goal["reviews"]], [("independent", "agent"), ("gui", "human")])
        self.assertEqual([(t["values"]["id"], t["status"]) for t in tasks], [("build", "done"), ("final", "active")])
        self.assertEqual(integrate, [])
        from loop_engineering import model
        model.goal(goal)  # valid v2 contract

    def test_draft_planning_detects_conflicts(self):
        bundle = self.root.parent / "bundle"
        files = bundle / "drafts" / "wb1"
        files.mkdir(parents=True)
        (files / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (files / "new.py").write_text("x = 1\n")
        base = file_digest(self.root / "calc.py")
        export = self.export()
        export["drafts"] = [{"id": "wb1", "task_id": "final", "changes": [
            {"path": "calc.py", "base_sha256": base, "new_sha256": file_digest(files / "calc.py"),
             "operation": "modify", "kind": "file"},
            {"path": "new.py", "base_sha256": None, "new_sha256": file_digest(files / "new.py"),
             "operation": "add", "kind": "file"},
            {"path": "test_calc.py", "base_sha256": "sha256:other", "new_sha256": "sha256:x",
             "operation": "modify", "kind": "file"}]}]
        plans = plan_drafts(self.project, export, bundle)
        self.assertEqual([c["path"] for c in plans[0]["apply"]], ["calc.py", "new.py"])
        self.assertEqual([c["path"] for c in plans[0]["conflicts"]], ["test_calc.py"])
        goal, tasks, integrate = build_goal(self.project, export, plans)
        self.assertEqual(tasks[0]["values"]["id"], "integrate-v1-drafts")
        self.assertIn("integrate-v1-drafts", tasks[2]["values"]["depends_on"])
        self.assertEqual(integrate, ["wb1"])
        from loop_engineering.migrate import apply_draft
        plans[0]["conflicts"] = []
        self.assertEqual(apply_draft(self.project, plans[0]), 2)
        self.assertIn("a + b", (self.root / "calc.py").read_text())
        engine  # imported for parity with other suites


class DashboardTests(ProjectCase):
    def test_busy_port_falls_back_and_serves_status(self):
        import socket, threading, time, urllib.request
        from loop_engineering import dashboard
        self.goal()
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        busy = blocker.getsockname()[1]
        servers = []
        original = dashboard.ThreadingHTTPServer

        class Capture(original):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                servers.append(self)
        dashboard.ThreadingHTTPServer = Capture
        try:
            thread = threading.Thread(target=dashboard.serve, args=(self.project, busy), daemon=True)
            thread.start()
            deadline = time.time() + 10
            while not servers and time.time() < deadline:
                time.sleep(0.05)
            port = servers[0].server_address[1]
            self.assertNotEqual(port, busy)
            data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5).read())
            self.assertEqual(data["summary"]["title"], "Fix add")
        finally:
            dashboard.ThreadingHTTPServer = original
            for server in servers:
                server.shutdown()
            blocker.close()


class TranscriptTests(ProjectCase):
    def write(self, name, events):
        path = self.root.parent / name
        path.write_text("\n".join(json.dumps(e) for e in events) + "\nplain trailing text\n")
        return path

    def test_codex_stream(self):
        from loop_engineering.transcript import parse
        path = self.write("codex.jsonl", [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.started", "item": {"id": "a", "type": "command_execution", "command": "npm test",
                                              "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "a", "type": "command_execution", "command": "npm test",
                                                "aggregated_output": "1 failed", "exit_code": 1}},
            {"type": "item.completed", "item": {"id": "b", "type": "mcp_tool_call", "server": "loop", "tool": "loop_next",
                                                "arguments": {}, "result": {"content": [{"type": "text",
                                                                                         "text": "{\"ok\": 1}"}]}}},
            {"type": "item.completed", "item": {"id": "c", "type": "agent_message", "text": "Fixed it"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}])
        rows = parse(path)
        kinds = [(r["kind"], r["status"]) for r in rows]
        self.assertEqual(kinds[:5], [("other", None), ("command", "failed"), ("tool", "ok"), ("message", "ok"),
                                     ("usage", "ok")])
        self.assertIn("[exit 1]", rows[1]["body"])
        self.assertIn('"ok": 1', rows[2]["body"])
        self.assertEqual(rows[-1]["title"], "Other output")

    def test_claude_stream(self):
        from loop_engineering.transcript import parse
        path = self.write("claude.jsonl", [
            {"type": "system", "subtype": "init", "session_id": "s", "model": "m", "tools": []},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking"},
                                                          {"type": "tool_use", "id": "u1", "name": "Bash",
                                                           "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1",
                                                      "content": "a.py"}]}},
            {"type": "result", "total_cost_usd": 0.1, "usage": {"input_tokens": 5}}])
        rows = parse(path)
        self.assertEqual(rows[2]["title"], "$ ls")
        self.assertEqual(rows[2]["status"], "ok")
        self.assertIn("a.py", rows[2]["body"])
        self.assertIn("cost=$0.1000", rows[3]["body"])
        self.assertIn("input_tokens=5", rows[3]["body"])


class DashboardActivityTests(ProjectCase):
    def test_turns_transcripts_and_check_logs(self):
        from loop_engineering import dashboard
        from loop_engineering.runner import Runner
        store = self.goal()
        store.mutate("a", lambda g, s: engine.approve(g, s, "t"))
        Runner(self.project, store.id, log=lambda m: None).run()
        listing = dashboard.turns(store)["turns"]
        self.assertEqual([t["n"] for t in listing], [2, 1])
        detail = dashboard.turn(store, 1)
        self.assertIn("Loop protocol", detail["prompt"])
        self.assertTrue(detail["entries"])
        checks = dashboard.check_rows(store)["checks"]
        self.assertTrue(any(c["id"] == "acceptance.tests" for c in checks))
        log = dashboard.check_log(store, "acceptance.tests")["text"]
        self.assertIn("ok", log)
        from loop_engineering.util import LoopError
        with self.assertRaises(LoopError):
            dashboard.turn(store, 99)


class SubagentTests(ProjectCase):
    def test_codex_subagent_rollouts_are_found_and_parsed(self):
        import os, time
        from loop_engineering import transcript
        sessions = self.root.parent / "codex" / "sessions"
        day = sessions / time.strftime("%Y/%m/%d")
        day.mkdir(parents=True)

        def rollout(name, thread, parent, records):
            meta = {"type": "session_meta", "payload": {"id": thread, "source": {"subagent": {"thread_spawn": {
                "parent_thread_id": parent, "depth": 1, "agent_path": f"/root/{name}", "agent_nickname": name}}}}}
            (day / f"rollout-x-{thread}.jsonl").write_text("\n".join(json.dumps(r) for r in [meta, *records]))
        rollout("tester", "child", "main", [
            {"type": "response_item", "payload": {"type": "agent_message", "author": "/root", "recipient": "/root/tester",
                                                  "content": [{"type": "input_text", "text": "NEW_TASK "},
                                                              {"type": "encrypted_content", "encrypted_content": "gAAAA"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text",
                                                                                    "text": "Check tests first"}]}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1",
                                                  "input": "tools.exec_command({cmd:'npm test'})"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                                                  "output": [{"type": "input_text", "text": "3 passed"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "All good"}]}}])
        rollout("nested", "grandchild", "child", [])
        rollout("other", "x", "unrelated", [])
        found = transcript.find_codex_subagents("main", time.time() - 60, sessions)
        self.assertEqual([m["name"] for m in found], ["tester", "nested"])
        rows = transcript.parse_codex_rollout(found[0]["path"])
        self.assertEqual([r["kind"] for r in rows], ["instruction", "reasoning", "command", "message"])
        self.assertIn(transcript.ENCRYPTED, rows[0]["body"])
        self.assertIn("3 passed", rows[2]["body"])

    def test_claude_subagent_events_are_grouped(self):
        from loop_engineering import transcript
        path = self.root.parent / "claude.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Task",
                                                           "input": {"description": "Write tests"}}]}},
            {"type": "assistant", "parent_tool_use_id": "t1",
             "message": {"content": [{"type": "text", "text": "sub working"}]}}]))
        groups = transcript.claude_subagents(path)
        self.assertEqual(groups[0]["name"], "Write tests")
        self.assertEqual(groups[0]["entries"][0]["body"], "sub working")
        main = transcript.parse(path)
        self.assertFalse(any("sub working" in r["body"] for r in main))
