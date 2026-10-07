"""A minimal stdio MCP server (JSON-RPC 2.0, newline-delimited) over the loop service.

Works with any MCP host (Claude Code, Codex, Cursor, Gemini CLI, ...). The
``--autonomous`` flag (used by the runner) removes the human-only tools.
"""

from __future__ import annotations

import json
import sys
import traceback

from . import __version__
from .api import Service
from .store import Project
from .util import LoopError

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

S = {"type": "string"}
CHECK = {"type": "object", "properties": {
    "id": S, "run": {"type": "string", "description": "Shell command"},
    "argv": {"type": "array", "items": S}, "timeout": {"type": "integer"}, "cwd": S,
    "stage": {"type": "string", "enum": ["unit", "integration", "e2e", "performance", "security", "regression",
                                         "acceptance"]},
    "description": S}, "required": ["id"]}
TASK = {"type": "object", "properties": {
    "id": S, "title": S, "detail": S, "role": S, "depends_on": {"type": "array", "items": S},
    "covers": {"type": "array", "items": S, "description": "Requirement ids this task delivers (full pipeline)"},
    "team": {"type": "array", "description": "Specialist seats coordinated as sub-agents, e.g. "
                                             "[{role: backend, count: 2, focus: ...}, {role: tester, count: 1}]",
             "items": {"type": "object", "properties": {"role": S, "count": {"type": "integer"}, "focus": S},
                       "required": ["role"]}},
    "checks": {"type": "array", "items": CHECK}}, "required": ["id"]}
REQUIREMENT = {"type": "object", "properties": {
    "id": S, "text": S, "source": {"type": "string", "description": "doc path and section or line"},
    "verify": {"type": "string", "description": "how it will be proven (test type and check)"},
    "kind": S, "priority": S}, "required": ["id", "text", "source", "verify"]}
GOAL = {"type": "string", "description": "Goal id (default: current goal)"}


def tool(name, description, properties, required=(), human=False, readonly=False):
    return {"name": name, "description": description, "human": human, "readonly": readonly,
            "inputSchema": {"type": "object", "properties": {**properties, "goal_id": GOAL},
                            "required": list(required), "additionalProperties": False}}


TOOLS = [
    tool("loop_status", "Current goal status: state, next action, tasks, checks, questions, blocker, budget, runner.",
         {"verbose": {"type": "boolean", "description": "Include STATUS.md text and an integrity audit"}}, readonly=True),
    tool("loop_next", "Your current assignment with full context. Call at the start of every turn. "
         "Starting a ready task marks it active.", {}),
    tool("loop_goal_draft", "Create or update a DRAFT goal from the user's request (intake). kind: develop | operate "
         "| investigate. acceptance: checks the controller runs to decide completion (develop/operate need at least "
         "one). operate needs schedule.interval_minutes. Show the result to the user before approving.",
         {"kind": {"type": "string", "enum": ["develop", "operate", "investigate"]}, "title": S, "objective": S,
          "context": {"type": "array", "items": S}, "constraints": {"type": "array", "items": S},
          "acceptance": {"type": "array", "items": CHECK}, "policy": {"type": "object"},
          "schedule": {"type": "object", "properties": {"interval_minutes": {"type": "integer"}}},
          "agent": {"type": "object", "properties": {"adapter": S, "model": S, "permission_mode": S,
                                                      "extra_args": {"type": "array", "items": S}}},
          "pipeline": {"type": ["string", "null"], "enum": ["full", None],
                       "description": "full (default for develop): requirements → plan → plan review → develop → "
                                      "staged tests → reviews → final acceptance"},
          "id": S, "enqueue": {"type": "boolean", "description": "Append to the project goal queue and keep the "
                                                                 "current goal (draft follow-up increments)"}},
         human=True),
    tool("loop_goal_approve", "Freeze the goal contract after the user explicitly confirmed it in this conversation.",
         {"confirmed_by_user": {"type": "boolean"}}, ["confirmed_by_user"], human=True),
    tool("loop_requirements", "Full pipeline, stage 1: record requirements extracted from the documents (id, text, "
         "source, verify). Reuse the documents' own IDs. final=true when every requirement is recorded.",
         {"requirements": {"type": "array", "items": REQUIREMENT}, "drop": {"type": "array", "items": S},
          "final": {"type": "boolean"}}),
    tool("loop_plan", "Add or update tasks (id, title, detail, depends_on, covers, checks, role) and drop tasks with a "
         "reason. Each task should have fast real checks. Full pipeline: every requirement must be covered; "
         "final=true submits the plan for independent review.",
         {"tasks": {"type": "array", "items": TASK},
          "drop": {"type": "array", "items": {"type": "object", "properties": {"id": S, "reason": S},
                                              "required": ["id", "reason"]}},
          "final": {"type": "boolean"}}),
    tool("loop_task", "start | done a task. 'done' makes the controller run the task's checks "
         "(waits up to wait_seconds, then poll loop_check with job_id).",
         {"action": {"type": "string", "enum": ["start", "done"]}, "task_id": S,
          "summary": {"type": "string", "description": "What you changed and how you verified it"},
          "wait_seconds": {"type": "number"}}, ["action", "task_id"]),
    tool("loop_check", "Ask the controller to run checks: scope 'acceptance' (default), a task id, or 'all'; or ids "
         "like 'acceptance.tests'. Runs in the background; poll with job_id.",
         {"scope": S, "ids": {"type": "array", "items": S}, "job_id": S, "wait_seconds": {"type": "number"}}),
    tool("loop_note", "Record a note. kinds: progress, handoff (end of every turn), decision, risk, hypothesis, "
         "evidence, root_cause. Update a finding with finding_id + finding_status (open|confirmed|rejected).",
         {"kind": {"type": "string", "enum": ["progress", "handoff", "decision", "risk", "hypothesis", "evidence",
                                              "root_cause"]},
          "text": S, "task_id": S, "finding_id": S,
          "finding_status": {"type": "string", "enum": ["open", "confirmed", "rejected", "recorded"]}},
         ["kind", "text"]),
    tool("loop_ask", "Ask the human a question, or request approval (kind='approval', action=the exact operation) "
         "before any action that needs approval. Blocking questions pause dependent work.",
         {"question": S, "options": {"type": "array", "items": S}, "blocking": {"type": "boolean"},
          "kind": {"type": "string", "enum": ["question", "approval"]}, "action": S}, ["question"]),
    tool("loop_answer", "Record the user's answer to an open question (only what the user actually said).",
         {"question_id": S, "answer": S}, ["question_id", "answer"], human=True),
    tool("loop_block", "Stop the goal for a human. permission/environment/external need the exact reproduction "
         "(evidence) and what you already tried (attempted). Try workarounds first.",
         {"category": {"type": "string", "enum": ["decision", "permission", "external", "environment", "scope",
                                                  "other"]},
          "reason": S, "evidence": S, "attempted": S}, ["category", "reason"]),
    tool("loop_finish", "All tasks are finished: submit the final report. The controller then runs acceptance.",
         {"report": S, "wait_seconds": {"type": "number"}}, ["report"]),
    tool("loop_review", "Reviewer sessions only: record your independent verdict for the assigned review. For "
         "pre-development gates, back_to may name the earlier stage that must be redone.",
         {"review_id": S, "verdict": {"type": "string", "enum": ["pass", "fail"]},
          "findings": {"type": "string", "description": "Concrete findings (file:line, problem, impact)"},
          "back_to": {"type": "string", "description": "e.g. requirements, solution, test_design, plan, resources"}},
         ["review_id", "verdict"]),
    tool("loop_stage_done", "Full pipeline: the document for your stage (intake, solution, test_design, resources) "
         "is written at path; the controller records it and moves on.",
         {"stage": {"type": "string", "enum": ["intake", "solution", "test_design", "resources"]}, "path": S,
          "summary": S}, ["stage", "path"]),
    tool("loop_control", "pause | resume | stop the goal (the user's request only).",
         {"command": {"type": "string", "enum": ["pause", "resume", "stop"]}}, ["command"], human=True),
]


REVIEWER_TOOLS = {"loop_status", "loop_next", "loop_note", "loop_review"}
PLANNER_TOOLS = {"loop_status", "loop_goal_draft"}


class Server:
    def __init__(self, project: Project, autonomous: bool, review_token: str | None = None,
                 planner_token: str | None = None):
        actor = "planner" if planner_token else "reviewer" if review_token else ("agent" if autonomous else "chat")
        self.service = Service(project, actor=actor, interactive=not autonomous and not review_token and not planner_token,
                               review_token=review_token, planner_token=planner_token)
        self.autonomous = autonomous
        if planner_token:
            self.tools = [t for t in TOOLS if t["name"] in PLANNER_TOOLS]
        elif review_token:
            self.tools = [t for t in TOOLS if t["name"] in REVIEWER_TOOLS]
        else:
            self.tools = [t for t in TOOLS if t["name"] != "loop_review" and not (autonomous and t["human"])]

    def handle(self, message: dict) -> dict | None:
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            return None  # notification
        try:
            if method == "initialize":
                requested = (message.get("params") or {}).get("protocolVersion")
                return self._ok(request_id, {
                    "protocolVersion": requested if requested in VERSIONS else VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "loop", "version": __version__},
                    "instructions": INSTRUCTIONS})
            if method == "ping":
                return self._ok(request_id, {})
            if method == "tools/list":
                return self._ok(request_id, {"tools": [{
                    "name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"],
                    "annotations": {"readOnlyHint": t["readonly"]}} for t in self.tools]})
            if method == "tools/call":
                params = message.get("params") or {}
                return self._ok(request_id, self.call(params.get("name"), params.get("arguments") or {}))
            if method in {"resources/list", "prompts/list"}:
                return self._ok(request_id, {method.split("/")[0]: []})
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method {method}"}}
        except Exception as exc:  # protocol-level failure
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}

    def call(self, name: str, args: dict) -> dict:
        if name not in {t["name"] for t in self.tools}:
            return _result({"error": f"Unknown or unavailable tool {name}"}, error=True)
        s = self.service
        goal = args.pop("goal_id", None)
        try:
            handlers = {
                "loop_status": lambda: s.status(goal, verbose=bool(args.get("verbose"))),
                "loop_next": lambda: s.next(goal),
                "loop_goal_draft": lambda: s.goal_draft({k: v for k, v in args.items()}, goal),
                "loop_goal_approve": lambda: s.goal_approve(args.get("confirmed_by_user"), goal),
                "loop_plan": lambda: s.plan(args.get("tasks"), args.get("drop"), goal, args.get("final", False)),
                "loop_requirements": lambda: s.requirements(args.get("requirements"), args.get("drop"),
                                                            args.get("final", False), goal),
                "loop_task": lambda: s.task(args["action"], args["task_id"], args.get("summary"),
                                            float(args.get("wait_seconds", 45)), goal),
                "loop_check": lambda: s.check(args.get("scope"), args.get("ids"), args.get("job_id"),
                                              float(args.get("wait_seconds", 45)), goal),
                "loop_note": lambda: s.note(args["kind"], args["text"], args.get("task_id"), args.get("finding_id"),
                                            args.get("finding_status"), goal),
                "loop_ask": lambda: s.ask(args["question"], args.get("options"), args.get("blocking", True),
                                          args.get("kind", "question"), args.get("action"), goal),
                "loop_answer": lambda: s.answer(args["question_id"], args["answer"], goal),
                "loop_block": lambda: s.block(args["category"], args["reason"], args.get("evidence", ""),
                                              args.get("attempted", ""), goal),
                "loop_finish": lambda: s.finish(args["report"], float(args.get("wait_seconds", 45)), goal),
                "loop_control": lambda: s.control(args["command"], goal),
                "loop_review": lambda: s.review(args["review_id"], args["verdict"], args.get("findings", ""), goal,
                                                args.get("back_to")),
                "loop_stage_done": lambda: s.stage_done(args["stage"], args["path"], args.get("summary", ""), goal),
            }
            return _result(handlers[name]())
        except LoopError as exc:
            return _result({"error": str(exc)}, error=True)
        except KeyError as exc:
            return _result({"error": f"Missing argument {exc}"}, error=True)
        except Exception as exc:
            return _result({"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-2000:]},
                           error=True)

    @staticmethod
    def _ok(request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


INSTRUCTIONS = (
    "Loop Engineering controller for this project. Start every turn with loop_next. The controller owns "
    "completion: it runs task checks on loop_task done and acceptance on loop_finish. Record a handoff note "
    "before you stop. Ask (loop_ask kind=approval) before any action in the goal's approval list. Never edit "
    ".loop/ files directly. Status for humans: .loop/STATUS.md.")


def _result(value, error=False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=1, default=str)}],
            "isError": error}


def serve(project: Project, autonomous: bool, stdin=None, stdout=None, review_token: str | None = None,
          planner_token: str | None = None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    server = Server(project, autonomous, review_token, planner_token)
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if isinstance(message, list):
                replies = [r for r in (server.handle(m) for m in message) if r]
                if replies:
                    stdout.write(json.dumps(replies) + "\n")
                    stdout.flush()
                continue
            reply = server.handle(message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
