"""Offline host fixture: real native task checks, never model/provider calls."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from loop_engineering.native_host import NativeHostService
from loop_engineering.store import Store
from tests.test_scenarios import FIXED


def main():
    argv = sys.argv[1:]
    if argv == ["--help"]:
        print("Offline CLI fixture: --no-daemon --sandbox workspace-write --ask-for-approval --approve-for-me")
        return
    if "--approve-for-me" in argv and "--sandbox" in argv:
        print("error: --sandbox cannot be used with --approve-for-me", file=sys.stderr)
        raise SystemExit(2)
    arguments = next(json.loads(a.split("=", 1)[1]) for a in argv if a.startswith("mcp_servers.loop-native.args="))
    project = Path(arguments[arguments.index("--project") + 1])
    state_dir = Path(arguments[arguments.index("--state-dir") + 1])
    output = Path(argv[argv.index("--output-last-message") + 1])
    identity = argv[argv.index("resume") + 1] if "resume" in argv else "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    request = json.loads(sys.stdin.read().splitlines()[-1])
    service = NativeHostService(project, Store(state_dir))
    service.workbench_parent = state_dir
    try:
        state = service.progress(request["team_id"])
        action = state["next_action"]
        args = action["arguments"]
        if action["kind"] == "prepare_task":
            packet = service.workbench_prepare(**args)
            if args["task_id"] == "implement":
                data = service._data(request["team_id"])
                suffix = "\n# Retained receipt repair fixture.\n" if data["native_host"].get("rework_history") else ""
                (Path(packet["workbench"]["directory"]) / "slug.py").write_text(FIXED + suffix)
            service.workbench_submit(**args, workbench_id=packet["workbench"]["id"], summary="Actual offline fixture files and checks")
        elif action["kind"] == "receive_handoff":
            service.receive(**args, accept=True, note="Actual offline fixture recipient inspected bound command evidence")
        elif action["kind"] in {"rework_handoff", "recover_rework"} and action["tool"] == "loop_handoff_rework":
            service.handoff_rework(**args)
        else:
            raise ValueError("Unexpected fixture action: " + action["kind"])
    finally:
        service.close()
    output.write_text(json.dumps({"status": "checkpoint", "summary": "One actual offline native action finished",
                                 "blocker": None, "questions": []}))
    print(json.dumps({"type": "thread.started", "thread_id": identity}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}))


if __name__ == "__main__":
    main()
