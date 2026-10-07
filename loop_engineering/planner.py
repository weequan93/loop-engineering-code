"""The planning agent: proposes the next goals when the queue runs dry.

One read-only agent turn (fresh session) reads the project's docs, the finished goals
and their reports, and the queue, then drafts the next goals through a planner-only MCP
session (``loop_goal_draft``, always queued, never approved). A person approves.

- ``loop propose`` runs it on demand.
- The runner runs it automatically after the last queued goal is done, unless
  ``.loop/settings.json`` sets ``"propose_next": false``. It proposes at most once per
  finished goal, so an unanswered proposal never loops.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from . import adapters as adapters_mod
from . import procs
from . import queue as queue_mod
from .util import LoopError, digest, now, read_json, write_json

DEFAULT_COUNT = 5


def settings(project) -> dict:
    path = project.loop / "settings.json"
    data = read_json(path, {}) if path.is_file() else {}
    return {"propose_next": True, "propose_count": DEFAULT_COUNT, **(data if isinstance(data, dict) else {})}


def session_path(project) -> Path:
    return project.loop / "planner.json"


def check_token(project, token: str) -> dict:
    data = read_json(session_path(project), {}) if session_path(project).is_file() else {}
    session = data.get("active")
    if not session or session.get("token") != digest(token):
        raise LoopError("This session is not the active planning session")
    return session


def record_draft(project, goal_id: str) -> None:
    data = read_json(session_path(project), {})
    session = data.get("active") or {}
    if goal_id not in session.setdefault("created", []):
        if len(session["created"]) >= session.get("max_goals", DEFAULT_COUNT):
            raise LoopError(f"This planning session may draft at most {session.get('max_goals')} goals")
        session["created"].append(goal_id)
    data["active"] = session
    write_json(session_path(project), data)


def _goal_digest_lines(project) -> list[str]:
    lines = []
    queued = set(queue_mod.load(project))
    for goal_id in project.goal_ids():
        store = project.goal(goal_id)
        goal, state = store.goal(), store.state()
        report = (state.get("report") or {}).get("text", "")
        handoff = next((n["text"] for n in reversed(state["notes"]) if n["kind"] == "handoff"), "")
        lines += [f"### `{goal_id}` — {goal['title']} [{state['status']}{', queued' if goal_id in queued else ''}]",
                  goal["objective"][:1500],
                  f"Acceptance: {', '.join(c['id'] + '=' + (c.get('run') or ' '.join(c['argv'])) for c in goal['acceptance'])}",
                  f"Reviews: {', '.join(r['id'] + '(' + r['by'] + ')' for r in goal['reviews']) or 'none'}"]
        if report:
            lines.append("Final report: " + report[:1500])
        elif handoff and state["status"] == "done":
            lines.append("Last handoff: " + handoff[:1000])
        lines.append("")
    return lines


def prompt(project, count: int, instructions: str | None) -> str:
    context = []
    for goal_id in project.goal_ids():
        for item in project.goal(goal_id).goal()["context"]:
            if item not in context:
                context.append(item)
    return "\n".join([
        f"You are the planning agent for the project at {project.root}. Propose the NEXT goals for the autonomous",
        "Loop Engineering controller. You do not implement anything and you must not modify any file.",
        "",
        "## Existing goals (oldest first)", "", *_goal_digest_lines(project),
        "## Documents to read first", "", *[f"- {c}" for c in context or ["README.md", "docs/"]],
        "Also look for roadmaps, plans, requirement ledgers and specifications in the repository.", "",
        "## Your job",
        f"1. Work out which increments of the product are not done yet, in dependency order. Draft at most {count} goals.",
        "2. Draft each goal with `loop_goal_draft` (it is queued automatically; you cannot approve it). Each goal needs:",
        "   - `kind` (usually develop), a short `title`, and an `objective` with scope, the requirement IDs it covers,",
        "     concrete deliverables, and what stays out of scope.",
        "   - `acceptance`: real commands the controller runs. Use a behavior check the goal itself creates (name its path),",
        "     plus the previous goals' acceptance commands as regression checks so accepted work cannot break.",
        "   - `reviews`: independent agent reviews (security, code, requirement coverage) with `timeout_minutes`. Add a",
        "     `by: human` review where real devices, real environments or human judgement are needed.",
        "   - `constraints` carried over from previous goals where they still apply. Mark external dependencies (paid",
        "     services, credentials, remote deployments, real devices) so the agent must ask for approval first.",
        "   - `context` (the documents that define it), `policy` similar to the previous goals, and the same `agent`",
        "     settings (adapter, model, stage/role profiles) as the previous goals.",
        "   - `\"pipeline\": \"full\"` for develop goals: requirements → plan → plan review → develop → staged tests →",
        "     reviews → final acceptance. Give each acceptance check a `stage` (unit, integration, e2e, performance,",
        "     security, regression, acceptance).",
        "   - If the documents call for specialists, keep or extend the `agent.roles` profiles (for example a stronger",
        "     model for architect or analyst work). Tasks and their teams are planned later, inside each goal.",
        "3. Do not duplicate finished work. Respect explicit stop instructions in the documents (for example \"stop after",
        "   R1\"). If nothing should be proposed, draft nothing and explain why.",
        "4. Finish with a short summary: the drafted goal IDs, the order, and any decision the human must make.",
        "If the extra instructions contain a NEW REQUEST, draft goals for that request only, placed before other",
        "queued work only if the request says it is urgent.",
        *(["", "## Extra instructions from the user", instructions] if instructions else []),
    ])


def propose(project, *, count: int | None = None, instructions: str | None = None, adapter: str | None = None,
            after_goal: str | None = None, log=lambda message: None, cancelled=lambda: False) -> dict:
    ids = project.goal_ids()
    if not ids:
        raise LoopError("Create the first goal yourself (or in chat); the planner extends an existing plan")
    count = count or settings(project)["propose_count"]
    latest = project.goal(project.current_id() or ids[-1]).goal()
    planner_profile = (latest["agent"].get("stages") or {}).get("planner") or {}
    name = adapter or planner_profile.get("adapter") or latest["agent"]["adapter"]
    agent = adapters_mod.get(name, project.root)
    problem = agent.available()
    if problem:
        raise LoopError(f"Planner adapter {name} unavailable: {problem}")
    data = read_json(session_path(project), {}) if session_path(project).is_file() else {}
    active = data.get("active")
    if active and procs.alive(active.get("pid"), active.get("identity")):
        raise LoopError("A planning session is already running")
    token = secrets.token_hex(16)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    workdir = project.loop / "proposals" / stamp
    workdir.mkdir(parents=True, exist_ok=True)
    text = prompt(project, count, instructions)
    (workdir / "prompt.md").write_text(text, encoding="utf-8")
    data["active"] = {"token": digest(token), "started_at": now(), "max_goals": count, "created": [],
                      "after_goal": after_goal, "dir": str(workdir.relative_to(project.root)),
                      "pid": os.getpid(), "identity": procs.identity(os.getpid())}
    write_json(session_path(project), data)
    turn_goal = {**latest, "policy": {**latest["policy"], "max_cost_usd": None},
                 "agent": {**latest["agent"], "model": planner_profile.get("model") or latest["agent"].get("model"),
                           "effort": planner_profile.get("effort") or latest["agent"].get("effort") or "high"}}
    turn = agent.build(text, project.root, workdir, turn_goal, None, mcp_args=["--planner-token", token],
                       readonly=True)
    log(f"planner: proposing up to {count} goals via {agent.name}")
    result = procs.run(turn.argv, cwd=project.root, logs=workdir, timeout=latest["policy"]["turn_timeout_minutes"] * 60,
                       stdin_text=turn.stdin, env={**turn.env, "LOOP_AUTONOMOUS": "1"}, cancelled=cancelled, poll=0.5)
    report = agent.parse(result.stdout, result.stderr, workdir)
    data = read_json(session_path(project), {})
    session = data.pop("active", {}) or {}
    created = session.get("created", [])
    entry = {**{k: session.get(k) for k in ("started_at", "after_goal", "dir")}, "finished_at": now(),
             "outcome": result.outcome, "exit_code": result.exit_code, "created": created,
             "summary": (report.error or report.summary or "")[:4000]}
    data["history"] = (data.get("history", []) + [entry])[-50:]
    write_json(session_path(project), data)
    _notify(project, entry)
    log(f"planner: drafted {len(created)} goal(s): {', '.join(created) or 'none'}")
    return entry


def already_proposed_after(project, goal_id: str) -> bool:
    data = read_json(session_path(project), {}) if session_path(project).is_file() else {}
    return any(h.get("after_goal") == goal_id for h in data.get("history", []))


def _notify(project, entry: dict) -> None:
    if os.environ.get("LOOP_NOTIFY") == "0":
        return
    from .notify import config, send
    created = entry["created"]
    title = (f"{len(created)} new goal(s) proposed · {project.root.name}" if created else
             f"Planner proposed no new goals · {project.root.name}")
    message = (", ".join(created) + "\n" + entry["summary"][:500]) if created else entry["summary"][:600]
    try:
        send(config(project.root), {"title": title, "message": message, "project": str(project.root),
                                    "command": "loop queue  →  loop approve --goal <id>  →  loop start"})
    except Exception:
        pass
