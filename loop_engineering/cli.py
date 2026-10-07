"""`loop` command line. Every MCP tool has a CLI equivalent, plus runner and setup commands."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from . import __version__
from .util import LoopError, read_json


def _check_arg(value: str) -> dict:
    """--check id=command"""
    if "=" not in value:
        raise argparse.ArgumentTypeError("use id=command, e.g. tests='pytest -q'")
    key, command = value.split("=", 1)
    return {"id": key.strip(), "run": command.strip()}


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS, help="Project root (default: search upward from cwd)")
    common.add_argument("--goal", default=argparse.SUPPRESS, help="Goal id (default: current goal)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Print JSON")

    p = argparse.ArgumentParser(prog="loop", description="Loop Engineering 2: autonomous goal loops with verified "
                                "completion and visible status.", parents=[common])
    p.add_argument("--version", action="version", version=f"loop-engineering {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[common])

    x = add("install", "Connect this project to Claude Code / Codex / other agents")
    x.add_argument("--host", default="claude,codex,generic", help="Comma list of claude, codex, generic")
    x.add_argument("--statusline", action="store_true", help="Also add a Claude Code status line")

    x = add("goal", "Create a goal (or edit .loop/goals/<id>/goal.json and run `loop approve`)")
    x.add_argument("--kind", choices=["develop", "operate", "investigate"], default="develop")
    x.add_argument("--title")
    x.add_argument("--objective", help="Text, or @path to read it from a file")
    x.add_argument("--from-file", type=Path, help="Full goal JSON")
    x.add_argument("--check", dest="checks", action="append", type=_check_arg, default=[],
                   help="Acceptance check id=command (repeatable)")
    x.add_argument("--context", action="append", default=[])
    x.add_argument("--constraint", action="append", default=[])
    x.add_argument("--adapter", help="claude | codex | configured command adapter")
    x.add_argument("--model")
    x.add_argument("--every", type=int, help="operate: health check interval in minutes")
    x.add_argument("--pipeline", choices=["full", "none"], help="develop goals default to the full pipeline")
    x.add_argument("--max-iterations", type=int)
    x.add_argument("--max-hours", type=float)
    x.add_argument("--max-cost", type=float)

    x.add_argument("--enqueue", action="store_true", help="Append to the goal queue; keep the current goal")
    add("approve", "Approve the current goal contract (you, the human); --goal <id> for a queued one")
    x = add("queue", "Show or edit the ordered goal queue (the runner continues with approved queued goals)")
    x.add_argument("action", nargs="?", choices=["list", "add", "remove", "clear"], default="list")
    x.add_argument("ids", nargs="*")
    x.add_argument("--at", type=int, help="1-based position for add")
    add("list", "List goals")
    x = add("use", "Make a goal the current one")
    x.add_argument("goal_id")
    x = add("status", "Show status")
    x.add_argument("--watch", action="store_true")
    x.add_argument("--interval", type=float, default=2.0)
    add("next", "Show the next assignment")
    x = add("plan", "Add/update tasks from JSON (list of tasks) or one --add")
    x.add_argument("--file", type=Path)
    x.add_argument("--add", nargs=2, metavar=("ID", "TITLE"))
    x.add_argument("--detail", default="")
    x.add_argument("--after", action="append", default=[], help="dependency task id")
    x.add_argument("--check", dest="checks", action="append", type=_check_arg, default=[])
    x.add_argument("--drop", nargs=2, metavar=("ID", "REASON"))
    x.add_argument("--covers", action="append", default=[], help="requirement id the --add task delivers")
    x.add_argument("--final", action="store_true", help="Full pipeline: submit the plan for review")
    x = add("stage-done", "Full pipeline: record the document of a stage (intake, solution, test_design, resources)")
    x.add_argument("stage", choices=["intake", "solution", "test_design", "resources"])
    x.add_argument("path")
    x.add_argument("--summary", default="")
    x = add("requirements", "Full pipeline: record requirements from a JSON list, or show them")
    x.add_argument("--file", type=Path)
    x.add_argument("--final", action="store_true")
    x = add("task", "start or finish a task")
    x.add_argument("action", choices=["start", "done"])
    x.add_argument("task_id")
    x.add_argument("--summary", default="")
    x.add_argument("--wait", type=float, default=600)
    x = add("check", "Run checks through the controller")
    x.add_argument("--scope", default=None, help="acceptance (default), a task id, or all")
    x.add_argument("--id", dest="ids", action="append")
    x.add_argument("--wait", type=float, default=3600)
    add("verify", "Controller runs acceptance now and applies the result")
    x = add("note", "Record a note")
    x.add_argument("kind", choices=["progress", "handoff", "decision", "risk", "hypothesis", "evidence", "root_cause"])
    x.add_argument("text")
    x.add_argument("--task")
    x.add_argument("--finding")
    x.add_argument("--finding-status", choices=["open", "confirmed", "rejected", "recorded"])
    x = add("ask", "Ask the human (or request approval)")
    x.add_argument("question")
    x.add_argument("--option", action="append")
    x.add_argument("--non-blocking", action="store_true")
    x.add_argument("--approval", metavar="ACTION", help="Request approval for this exact action")
    x = add("answer", "Answer an open question")
    x.add_argument("question_id")
    x.add_argument("answer")
    x = add("block", "Block the goal for a human")
    x.add_argument("category", choices=["decision", "permission", "external", "environment", "scope", "other"])
    x.add_argument("reason")
    x.add_argument("--evidence", default="")
    x.add_argument("--attempted", default="")
    x = add("unblock", "Clear a blocker after resolving it")
    x.add_argument("--note", required=True)
    x = add("finish", "Submit the final report; the controller verifies acceptance")
    x.add_argument("--report", required=True, help="Text or @path")
    x.add_argument("--wait", type=float, default=3600)
    for name in ("pause", "resume", "stop"):
        add(name, f"{name.capitalize()} the goal")
    x = add("start", "Start the autonomous runner in the background")
    x.add_argument("--adapter")
    x = add("run", "Run the autonomous runner in the foreground")
    x.add_argument("--adapter")
    x.add_argument("--max-turns", type=int)
    x = add("dashboard", "Serve a read-only local status page")
    x.add_argument("--port", type=int, default=8765)
    add("statusline", "One status line (Claude Code statusLine command)")
    add("roles", "List specialist roles and where each guide comes from (.loop/roles/<role>.md overrides)")
    x = add("notify", "Send a test notification; --progress on|off toggles per-turn progress messages")
    x.add_argument("--progress", choices=["on", "off"])
    x = add("telegram-setup", "Connect Telegram notifications (asks for the bot token in this terminal)")
    x.add_argument("--chat-id", help="Skip discovery if you know the chat id")
    x.add_argument("--wait", type=int, default=180)
    x = add("log", "Show recent events")
    x.add_argument("-n", type=int, default=30)
    add("audit", "Verify the event hash chain and state integrity")
    x = add("repair", "Accept an inspected external state edit")
    x.add_argument("--note", required=True)
    x = add("mcp", "Run the MCP server on stdio")
    x.add_argument("--autonomous", action="store_true")
    x.add_argument("--review-token", help=argparse.SUPPRESS)
    x.add_argument("--planner-token", help=argparse.SUPPRESS)
    x = add("request", "Submit a new requirement: the planning agent drafts it as a queued goal for your approval")
    x.add_argument("text", help="The requirement in your own words (or @file)")
    x.add_argument("--doc", action="append", default=[], help="Document(s) that describe it")
    x.add_argument("--adapter")
    x = add("propose", "Run the planning agent: it drafts and queues the next goals for your approval")
    x.add_argument("--count", type=int, help="Maximum goals to draft (default 5)")
    x.add_argument("--adapter", help="Agent for the planning turn (default: the current goal's adapter)")
    x.add_argument("--instructions", help="Extra guidance, e.g. 'only G2 identity work'")
    x = add("review", "Record a review verdict yourself (human override)")
    x.add_argument("review_id")
    x.add_argument("verdict", choices=["pass", "fail"])
    x.add_argument("--findings", default="")
    x = add("migrate-legacy", "Migrate a Loop Engineering v1 project and state into this version")
    x.add_argument("--state-dir", type=Path, required=True)
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--host", default="claude,codex,generic")
    x = add("_job", argparse.SUPPRESS)
    x.add_argument("--job", required=True)
    return p


def _text_arg(value: str | None) -> str | None:
    if value and value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    project_arg = getattr(args, "project", None)
    goal_id = getattr(args, "goal", None)
    as_json = getattr(args, "json", False)
    from .store import Project
    try:
        if args.command == "mcp":
            from .mcp_server import serve
            serve(Project(project_arg) if project_arg else Project.find(), args.autonomous,
                  review_token=args.review_token, planner_token=args.planner_token)
            return 0
        if args.command == "statusline":
            from .install import statusline_from_stdin
            if project_arg:
                from .status import line
                print(line(Project(project_arg).goal(goal_id)))
            else:
                print(statusline_from_stdin(sys.stdin))
            return 0
        project = Project(project_arg) if project_arg else Project.find()
        result = dispatch(args, project, goal_id)
    except LoopError as exc:
        _print({"error": str(exc)}, True) if as_json else print(f"loop: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    if result is not None:
        _print(result, as_json)
    return 0


def dispatch(args, project, goal_id):
    from .api import Service
    autonomous = os.environ.get("LOOP_AUTONOMOUS") == "1"
    service = Service(project, actor="agent" if autonomous else "human", interactive=not autonomous)
    c = args.command
    if c == "install":
        from .install import install
        return install(project, [h.strip() for h in args.host.split(",") if h.strip()], args.statusline)
    if c == "goal":
        if args.from_file:
            values = read_json(args.from_file)
        else:
            if not args.title or not args.objective:
                raise LoopError("Give --title and --objective (or --from-file)")
            values = {"kind": args.kind, "title": args.title, "objective": _text_arg(args.objective),
                      "acceptance": args.checks, "context": args.context, "constraints": args.constraint}
            policy = {k: v for k, v in (("max_iterations", args.max_iterations), ("max_hours", args.max_hours),
                                        ("max_cost_usd", args.max_cost)) if v is not None}
            if policy:
                values["policy"] = policy
            if args.adapter or args.model:
                values["agent"] = {k: v for k, v in (("adapter", args.adapter), ("model", args.model)) if v}
            if args.every:
                values["schedule"] = {"interval_minutes": args.every}
            if args.pipeline:
                values["pipeline"] = None if args.pipeline == "none" else "full"
        result = service.goal_draft(values, enqueue=args.enqueue)
        return {"created": result["goal"]["id"], "path": result["path"], "problems": result["problems"],
                "queued": args.enqueue,
                "next": f"Review the goal file, then `loop approve --goal {result['goal']['id']}`."}
    if c == "request":
        from .planner import propose
        text_ = _text_arg(args.text)
        docs = "".join(f"\n- {d}" for d in args.doc)
        guidance = ("A NEW REQUEST from the user. Draft goal(s) that deliver exactly this request, and nothing else. "
                    "Use the full pipeline, so a product manager writes the brief first and the requirements are "
                    "extracted from the request and the documents. Choose acceptance checks the project can really "
                    "run (existing test/build commands plus a behavior check the goal creates).\n\nRequest:\n"
                    + text_ + (f"\n\nDocuments for this request:{docs}" if docs else ""))
        entry = propose(project, count=2, instructions=guidance, adapter=args.adapter,
                        log=lambda m: print(m, file=sys.stderr, flush=True))
        from . import queue as queue_mod
        return {**entry, "queue": queue_mod.view(project),
                "next": "Review the drafted goal.json, then `loop approve --goal <id>` and `loop start`."}
    if c == "propose":
        from .planner import propose
        entry = propose(project, count=args.count, instructions=args.instructions, adapter=args.adapter,
                        log=lambda m: print(m, file=sys.stderr, flush=True))
        from . import queue as queue_mod
        return {**entry, "queue": queue_mod.view(project),
                "next": "Review each drafted goal.json, then `loop approve --goal <id>` and `loop start`."}
    if c == "queue":
        from . import queue as queue_mod
        if args.action == "add":
            queue_mod.add(project, args.ids, None if args.at is None else args.at - 1)
        elif args.action == "remove":
            for goal in args.ids:
                queue_mod.remove(project, goal)
        elif args.action == "clear":
            queue_mod.save(project, [])
        return {"queue": queue_mod.view(project)}
    if c == "approve":
        return service.goal_approve(True, goal_id, by="human")
    if c == "list":
        rows = []
        for gid in project.goal_ids():
            store = project.goal(gid)
            state = store.state()
            rows.append({"id": gid, "title": store.goal()["title"], "kind": store.goal()["kind"],
                         "status": state["status"], "current": gid == project.current_id()})
        return {"goals": rows}
    if c == "use":
        return service.select(args.goal_id)
    if c == "status":
        if args.watch:
            return _watch(project, goal_id, args.interval)
        if getattr(args, "json", False):
            return service.status(goal_id, verbose=True)
        from .status import markdown
        print(markdown(project.goal(goal_id)), end="")
        return None
    if c == "next":
        result = service.next(goal_id)
        if getattr(args, "json", False):
            return result
        print(result.get("context", "") + "\n\n" + result["assignment"] if result.get("context") else result["assignment"])
        return None
    if c == "plan":
        tasks = read_json(args.file) if args.file else []
        if args.add:
            tasks.append({"id": args.add[0], "title": args.add[1], "detail": args.detail, "depends_on": args.after,
                          "checks": args.checks, "covers": args.covers})
        drop = [{"id": args.drop[0], "reason": args.drop[1]}] if args.drop else None
        return service.plan(tasks, drop, goal_id, args.final)
    if c == "stage-done":
        return service.stage_done(args.stage, args.path, args.summary, goal_id)
    if c == "requirements":
        if not args.file and not args.final:
            return {"requirements": project.goal(goal_id).state().get("requirements", [])}
        return service.requirements(read_json(args.file) if args.file else None, None, args.final, goal_id)
    if c == "task":
        return service.task(args.action, args.task_id, args.summary, args.wait, goal_id)
    if c == "check":
        return service.check(args.scope, args.ids, None, args.wait, goal_id)
    if c == "verify":
        from . import engine
        engine.verify_now(project.goal(goal_id), actor="human")
        return service.status(goal_id)
    if c == "note":
        return service.note(args.kind, args.text, args.task, args.finding, args.finding_status, goal_id)
    if c == "ask":
        return service.ask(args.question, args.option, not args.non_blocking,
                           "approval" if args.approval else "question", args.approval, goal_id)
    if c == "answer":
        return service.answer(args.question_id, args.answer, goal_id, by="human")
    if c == "block":
        return service.block(args.category, args.reason, args.evidence, args.attempted, goal_id)
    if c == "unblock":
        return service.unblock(args.note, goal_id, by="human")
    if c == "finish":
        return service.finish(_text_arg(args.report), args.wait, goal_id)
    if c in {"pause", "resume", "stop"}:
        result = service.control(c, goal_id, by="human")
        if c == "stop":
            _signal_runner(project, goal_id)
        return result
    if c == "start":
        from .runner import start_background
        _require_runnable(project, goal_id, None if autonomous else service)
        return start_background(project, goal_id, args.adapter)
    if c == "run":
        from .runner import Runner
        _require_runnable(project, goal_id, None if autonomous else service)
        return Runner(project, goal_id, adapter=args.adapter, max_turns=args.max_turns).run()
    if c == "dashboard":
        from .dashboard import serve
        serve(project, args.port)
        return None
    if c == "telegram-setup":
        import getpass
        from .notify import telegram_setup
        token = os.environ.get("LOOP_TELEGRAM_BOT_TOKEN") or getpass.getpass(
            "Telegram bot token from @BotFather (input hidden): ").strip()
        if not token:
            raise LoopError("No token given")
        try:
            return telegram_setup(token, args.chat_id, args.wait)
        except (OSError, ValueError) as exc:
            raise LoopError(f"Telegram setup failed: {exc}")
    if c == "roles":
        from .roles import available
        return {"roles": available(project.root),
                "customize": "Copy a guide to .loop/roles/<role>.md and edit it; add new roles the same way."}
    if c == "notify":
        from .notify import config, send, set_progress
        if args.progress:
            path = set_progress(args.progress == "on")
            return {"progress_messages": args.progress, "config": str(path),
                    "note": "Applies to runners started after this change."}
        settings = config(project.root)
        ok = send(settings, {"title": "Loop notifications work", "message": f"{project.root.name}: you will be told "
                             "here when a question, approval, blocker or result needs you.", "command": "loop status",
                             "project": str(project.root)})
        return {"delivered": ok, "channels": {"desktop": settings.get("desktop", True),
                                              "telegram": bool((settings.get("telegram") or {}).get("chat_id")),
                                              "progress": bool(settings.get("progress")),
                                              "command": bool(settings.get("command"))}}
    if c == "log":
        return {"events": project.goal(goal_id).events(args.n)}
    if c == "audit":
        problems = project.goal(goal_id).verify()
        return {"ok": not problems, "problems": problems}
    if c == "review":
        return service.review(args.review_id, args.verdict, args.findings, goal_id)
    if c == "repair":
        return service.repair(args.note, goal_id, by="human")
    if c == "migrate-legacy":
        from .migrate import migrate
        return migrate(project, args.state_dir, dry_run=args.dry_run,
                       hosts=[h.strip() for h in args.host.split(",") if h.strip()])
    if c == "_job":
        from .checks import run_job
        run_job(project.goal(goal_id), args.job)
        return None
    raise LoopError(f"Unhandled command {c}")


def _require_runnable(project, goal_id, service=None):
    from . import engine
    from .checks import fingerprint
    store = project.goal(goal_id)
    state = store.state()
    if service and (state.get("control") == "pause" or state["status"] == "paused"):
        # An explicit start by a person means "resume"; blockers and limits still need their own action.
        service.control("resume", goal_id, by="human")
    act = engine.next_action(store.goal(), store.state(), fingerprint(project.root))
    if act["kind"] in {"approve_goal", "repair_state"}:
        raise LoopError(act["summary"])


def _signal_runner(project, goal_id):
    import signal as signals
    from . import procs
    runner = project.goal(goal_id).runner()
    if runner and not runner.get("exited_at") and procs.alive(runner.get("pid"), runner.get("identity")):
        try:
            os.kill(runner["pid"], signals.SIGTERM)
        except OSError:
            pass


def _watch(project, goal_id, interval):
    from .status import markdown
    last = None
    try:
        while True:
            text = markdown(project.goal(goal_id))
            if text != last:
                sys.stdout.write("\x1b[2J\x1b[H" + text)
                sys.stdout.flush()
                last = text
            time.sleep(interval)
    except KeyboardInterrupt:
        return None


def _print(value, as_json):
    if as_json or not isinstance(value, dict):
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
        return
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
