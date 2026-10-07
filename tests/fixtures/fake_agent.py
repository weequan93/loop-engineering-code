"""A scripted coding agent for offline runner tests. It speaks MCP like a real host.

Usage (as a command adapter): python fake_agent.py {mcp_config}
The prompt arrives on stdin. Behaviour is controlled by FAKE_* environment variables:
  FAKE_MODE=fix      plan one task, fix calc.py, claim done, finish (default)
  FAKE_MODE=idle     do nothing (stagnation)
  FAKE_MODE=sleep    sleep for a long time (timeouts / stop)
  FAKE_REVIEW=fail-once   first review fails, later reviews pass
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time


class Client:
    def __init__(self, config_path):
        spec = json.loads(Path(config_path).read_text())["mcpServers"]["loop"]
        env = {**os.environ, **spec.get("env", {})}
        self.proc = subprocess.Popen([spec["command"], *spec["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, env=env)
        self.n = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "fake", "version": "1"}})
        self.tools = {t["name"] for t in self.request("tools/list", {})["tools"]}

    def request(self, method, params):
        self.n += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def call(self, name, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        value = json.loads(result["content"][0]["text"])
        if result.get("isError"):
            raise RuntimeError(f"{name}: {value}")
        return value

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def main():
    sys.stdin.read()
    mode = os.environ.get("FAKE_MODE", "fix")
    if mode == "sleep":
        time.sleep(600)
        return
    client = Client(sys.argv[1])
    try:
        if "loop_goal_draft" in client.tools and "loop_next" not in client.tools:  # planner session
            (Path(os.environ["FAKE_STATE"]) / "calls.log").open("a").write("propose\n")
            for n in (1, 2, 3):
                try:
                    client.call("loop_goal_draft", id=f"next-{n}", kind="develop", title=f"Next {n}",
                                objective="follow-up increment",
                                acceptance=[{"id": "tests", "run": "python3 test_calc.py"}],
                                agent={"adapter": "fake"})
                except RuntimeError as exc:  # the session limit is part of the test
                    print(json.dumps({"type": "result", "result": f"stopped: {exc}"}))
            try:
                client.call("loop_goal_draft", id="first", title="overwrite", objective="x")
            except RuntimeError:
                pass
            print(json.dumps({"type": "result", "result": "drafted next-1, next-2"}))
            return
        nxt = client.call("loop_next")
        kind = nxt["action"]["kind"]
        state_dir = Path(os.environ["FAKE_STATE"])  # outside the project, so it never changes the fingerprint
        log = state_dir / "calls.log"
        with log.open("a") as handle:
            handle.write(kind + "\n")
        if mode == "idle":
            print("did nothing")
            return
        full = client.call("loop_status").get("pipeline") is not None
        if kind in {"intake", "solution", "test_design", "resources"}:
            out = Path(nxt["action"]["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f"# {kind}\n\nDocument written by the fake {kind} specialist for the test goal.\n")
            client.call("loop_stage_done", stage=kind, path=str(out), summary=f"{kind} written")
        elif kind == "requirements":
            client.call("loop_requirements", requirements=[
                {"id": "R1", "text": "add(a, b) returns the sum", "source": "README#add", "verify": "unit test"},
                {"id": "R2", "text": "the test suite passes", "source": "README#quality", "verify": "acceptance"}],
                final=True)
        elif kind == "plan" and full:
            revised = bool(nxt["action"].get("revise_after"))
            client.call("loop_plan", final=True, tasks=[
                {"id": "fix", "title": "Fix add", "role": "backend", "covers": ["R1", "R2"],
                 "detail": "add must add" + (" (revised after plan review)" if revised else ""),
                 "checks": [{"id": "unit", "run": "python3 test_calc.py"}]}])
        elif kind == "review" and nxt["action"]["review"] == "plan_review":
            flag = Path(os.environ["FAKE_STATE"]) / "plan-review-failed"
            if os.environ.get("FAKE_PLAN_REVIEW") == "fail-once" and not flag.exists():
                flag.write_text("x")
                client.call("loop_review", review_id="plan_review", verdict="fail", findings="R2 has no real test")
            else:
                client.call("loop_review", review_id="plan_review", verdict="pass", findings="")
        elif kind == "plan":
            client.call("loop_plan", tasks=[{"id": "fix", "title": "Fix add", "detail": "add must add",
                                              "checks": [{"id": "unit", "run": "python3 test_calc.py"}]}])
        elif kind in {"work", "repair"}:
            Path("calc.py").write_text("def add(a, b):\n    return a + b\n")
            if kind == "work":
                client.call("loop_task", action="done", task_id=nxt["action"]["task"], summary="fixed operator",
                            wait_seconds=30)
            else:
                Path("NOTES.txt").write_text("repaired after review\n")
        elif kind == "review":
            flag = state_dir / "review-failed"
            if os.environ.get("FAKE_REVIEW") == "fail-once" and not flag.exists():
                flag.write_text("x")
                client.call("loop_review", review_id=nxt["action"]["review"], verdict="fail",
                            findings="calc.py:2 lacks a docstring")
            else:
                client.call("loop_review", review_id=nxt["action"]["review"], verdict="pass", findings="")
        elif kind == "remediate":
            Path("healthy").write_text("ok\n")
        state = client.call("loop_status")
        if kind in {"work"} and state["tasks"]["total"] == state["tasks"]["done"]:
            client.call("loop_note", kind="handoff", text="All tasks done; acceptance is next.")
        else:
            client.call("loop_note", kind="handoff", text=f"Finished {kind} step.")
        print(json.dumps({"type": "result", "result": f"did {kind}"}))
    finally:
        client.close()


if __name__ == "__main__":
    main()
