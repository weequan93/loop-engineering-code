"""Assignments for agents. Each turn is self-contained (fresh session by default),
so the prompt carries the contract, the current step and the handoff notes."""

from __future__ import annotations

from pathlib import Path

from .util import tail

PLAYBOOKS = Path(__file__).parent / "playbooks"

PROTOCOL = """\
## Loop protocol (you are one turn in a controller-run loop)

Use the `loop` MCP tools. If they are unavailable, use the `loop` CLI with the same names, for
example `loop task done <id> --summary ...`.
- `loop_next` shows your assignment; `loop_status` shows everything.
- Full pipeline: `loop_stage_done` records a stage document; `loop_requirements` records requirements;
  `loop_plan` adds or updates tasks (with checks, `covers`, `role`, `team`, and `final=true` to submit the plan).
- `loop_task` starts or finishes a task.
- `loop_check` asks the controller to run checks; only controller-run checks count as evidence.
- `loop_note` records progress, decisions and findings. Before you stop, write one
  `kind="handoff"` note: what changed, what is next, and any traps. The next turn starts fresh
  and depends on it.
- `loop_ask` asks the human. Use `kind="approval"` before any action under "needs approval".
- `loop_block` only when no progress is possible without a person. Include the exact
  reproduction and what you already tried.
- `loop_finish` is for when every task is finished. The controller then runs acceptance.

Rules: the controller decides completion. Never edit `.loop/` files directly. Never weaken,
delete or bypass checks. Stay within the constraints. One meaningful step per turn is fine.
Stop when your assignment is done or you are waiting on the controller or a human.
"""


def playbook(kind: str) -> str:
    return (PLAYBOOKS / f"{kind}.md").read_text(encoding="utf-8")


def _checks_text(checks: list[dict]) -> str:
    return "\n".join(f"  - `{c['id']}`: `{c.get('run') or ' '.join(c['argv'])}`" for c in checks) or "  - (none)"


def assignment(goal: dict, state: dict, act: dict, project_root: Path | None = None) -> str:
    """The concrete instruction for the current action (also returned by loop_next)."""
    text = _assignment(goal, state, act)
    from .engine import pipeline, profile_for
    from .roles import guide, workflow
    chosen = profile_for(goal, state, act, project_root)
    if project_root:
        flow = workflow(project_root, goal["kind"])
        order = flow["order"] if pipeline(goal) or goal["kind"] != "develop" else ["plan", "develop", "repair", "review"]
        line = " → ".join(f"**{s}**" if s == chosen.get("stage") else s for s in order)
        text = (f"## Workflow: {flow['label']} — stage `{chosen.get('stage') or act['kind']}`, role "
                f"`{chosen.get('role') or 'generalist'}`\n\n{line}\n\n" + text)
    task = next((t for t in state["tasks"] if t["id"] == act.get("task")), None) if act["kind"] in {
        "work", "repair"} else None
    if task and task.get("team") and project_root:
        text += "\n\n" + coordination(goal, task, project_root)
    elif chosen.get("role") and project_root:
        text += "\n\n## Your specialist role\n\n" + guide(project_root, chosen["role"],
                                                              (goal["agent"].get("roles") or {}).get(chosen["role"]))
    return text


def coordination(goal: dict, task: dict, project_root: Path) -> str:
    """The turn's agent coordinates the task's specialist team through its host's sub-agents."""
    from .roles import guide
    folder = project_root / ".loop" / "goals" / goal["id"] / "runs" / "roles"
    folder.mkdir(parents=True, exist_ok=True)
    profiles = goal["agent"].get("roles") or {}
    seats, lines = [], []
    for seat in task["team"]:
        path = folder / f"{seat['role']}.md"
        path.write_text(guide(project_root, seat["role"], profiles.get(seat["role"])), encoding="utf-8")
        profile = profiles.get(seat["role"]) or {}
        model = f" · model `{profile['model']}`" if profile.get("model") else ""
        effort = f" · effort `{profile['effort']}`" if profile.get("effort") else ""
        lines.append(f"- {seat['count']}× **{seat['role']}**{(' — ' + seat['focus']) if seat.get('focus') else ''}"
                     f" · guide `{path}`{model}{effort}")
        seats += [seat["role"]] * seat["count"]
    return "\n".join([
        f"## You coordinate a specialist team ({len(seats)} seat{'s' if len(seats) != 1 else ''})", "",
        *lines, "",
        "The plan staffed this task with the specialists above. Work as the coordinator:",
        "1. Split the task into one concrete slice per seat. Give each slice exact file ownership so that no two "
        "sub-agents edit the same file. Contracts or interfaces that several slices share come first.",
        "2. Start one sub-agent per seat with your host's sub-agent feature (Codex: spawn_agent; Claude Code: the "
        "Task tool). This task explicitly asks for delegation. Give each sub-agent its slice, its owned files, the "
        "task checks, and the path of its role guide, which it must read first. If your host lets you choose a "
        "sub-agent model, use the model listed for that role.",
        "3. Wait for all of them. Review and integrate their changes, resolve conflicts, and run the task checks "
        "yourself.",
        "4. Record what each seat delivered in a `loop_note(kind=\"handoff\")`, then call `loop_task(action=\"done\")`. "
        "The controller verifies.",
        "If your host cannot start sub-agents, do the slices yourself one by one, each following its role guide."])


def _assignment(goal: dict, state: dict, act: dict) -> str:
    kind = act["kind"]
    lines = [f"**Assignment: {kind}** — {act['summary']}"]
    from .engine import (FINAL_GATE, GATE_STAGE_IDS, STAGE_BY_ID, artifact_path, find_gate, pipeline,
                         pre_stages, uncovered)
    full = pipeline(goal)
    documents = _stage_documents(goal, state)
    revise = act.get("revise_after")
    if revise:
        record = state.get("stages", {}).get(revise) or {}
        lines.append(f"**{revise} sent this stage back.** Address these findings:\n{tail(record.get('findings', ''), 6000)}")
    if documents and kind not in {"work", "repair"}:
        lines.append("Documents produced by earlier stages (read them):\n" + documents)
    if kind in STAGE_BY_ID and STAGE_BY_ID[kind]["type"] == "artifact":
        lines.append(f"Write the {kind.replace('_', ' ')} document at `{act['output']}` (create the folder), following "
                     "your role guide below. When it is complete, call "
                     f"`loop_stage_done(stage=\"{kind}\", path=\"{act['output']}\", summary=...)`. Do not write production "
                     "code in this stage.")
        if kind == "resources":
            lines.append("Plan to check (tasks and teams):\n" + "\n".join(
                f"- `{t['id']}` [{t.get('role') or '-'}] {t['title']} team "
                f"{[(x['role'], x['count']) for x in t.get('team', [])]}" for t in state["tasks"] if t["status"] != "dropped"))
    elif kind == "requirements":
        lines.append(
            "Stage 1 of the full pipeline. Read every document listed under context, and any specification, plan, "
            "roadmap or ledger they point to. Record each requirement as an atomic, testable item with "
            "`loop_requirements`: `id` (reuse the document's own ID, otherwise R-001…), `text` (what must be true), "
            "`source` (doc path and section or line), `verify` (the test type and how it will be proven: unit, "
            "integration, e2e, performance, security, review or human). Include functional and non-functional "
            "requirements, constraints and explicit out-of-scope notes. Do not invent requirements the documents do "
            "not state; ask with loop_ask when the documents are ambiguous. Call loop_requirements(final=true) when "
            "the list is complete. Do not write code in this stage.")
        if state.get("requirements"):
            lines.append(f"Already recorded: {len(state['requirements'])} requirements "
                         f"({', '.join(r['id'] for r in state['requirements'][:40])}).")
    elif kind == "plan" and full:
        requirements = "\n".join(f"- `{r['id']}` {r['text'][:200]} (verify: {r['verify'][:120]})"
                                  for r in state.get("requirements", [])[:200])
        lines.append(
            "Stage 2: plan. Break the work into tasks with `loop_plan`. Each task needs `covers` (the requirement IDs "
            "it delivers), `depends_on`, a `detail` with the expected outcome, a `role` (the specialist who does it: "
            "architect, backend, frontend, mobile, data, tester, security, devops, or a project role in "
            ".loop/roles/), and real `checks` (tests that prove it). Split work so each task has one lead role. "
            "When a task really benefits from several specialists working in parallel, add `team`, for example "
            "[{\"role\": \"backend\", \"count\": 2, \"focus\": \"API and persistence\"}, {\"role\": \"tester\", \"count\": 1}] "
            f"(at most {goal['agent'].get('team_limit', 4)} seats). The turn's agent then coordinates them as "
            "sub-agents. Leave `team` empty when one specialist is enough. Every requirement must be covered by at "
            "least one task. Order the work: architecture and contracts first, then implementation, then the test "
            "suites (unit, integration, e2e, performance, security). When the plan is complete, call "
            "loop_plan(final=true); an independent reviewer checks it before development starts.\n\n"
            f"Requirements:\n{requirements}\n\nGoal acceptance checks (staged):\n" + _checks_text(goal["acceptance"]))
        missing = uncovered(state)
        if state["tasks"] and missing:
            lines.append(f"Not covered yet: {', '.join(missing[:60])}")
    elif kind == "plan":
        lines.append("Create the task list with `loop_plan`. Each task needs an id, a title, a detail with the "
                     "expected outcome, `depends_on`, and fast `checks` that prove it. Acceptance checks for the "
                     "whole goal already exist:\n" + _checks_text(goal["acceptance"]))
    elif kind == "work":
        task = next(t for t in state["tasks"] if t["id"] == act["task"])
        lines += [f"Task `{task['id']}`: {task['title']}", task.get("detail") or "",
                  "Task checks (the controller runs them when you call loop_task done):\n" + _checks_text(task["checks"])]
        if task.get("role"):
            lines.append(f"Suggested specialist role: {task['role']}.")
        if task.get("last_failure"):
            lines.append(f"Previous attempt {task['attempts']} failed these checks:\n```\n"
                         f"{tail(task['last_failure']['tail'], 3000)}\n```")
        if task["notes"]:
            lines.append("Task notes:\n" + "\n".join(f"- {n['text'][:500]}" for n in task["notes"][-5:]))
    elif kind == "review" and act["review"] == "requirements_review":
        requirements = "\n".join(f"- `{r['id']}` {r['text'][:300]} — source {r['source'][:120]}; verify: "
                                  f"{r['verify'][:150]}" for r in state.get("requirements", []))
        lines += ["Review the product brief and the recorded requirements before any design work: is every "
                  "requirement faithful to the documents and the brief, is anything missing or invented, are "
                  "priorities and non-scope respected, and is each requirement verifiable?",
                  f"Requirements:\n{requirements}",
                  "Do NOT modify files. Finish with loop_review(review_id='requirements_review', verdict='pass'|'fail', "
                  "findings=..., back_to='intake' or 'requirements') with concrete gaps."]
    elif kind == "review" and act["review"] in GATE_STAGE_IDS:
        requirements = "\n".join(f"- `{r['id']}` {r['text'][:300]} — source {r['source'][:120]}; verify: "
                                  f"{r['verify'][:150]}" for r in state.get("requirements", []))
        tasks = "\n".join(f"- `{t['id']}` [{t.get('role') or '-'}] {t['title']} ← {t['depends_on']} covers "
                           f"{t.get('covers', [])} team {[(x['role'], x['count']) for x in t.get('team', [])]} "
                           f"checks {[c['id'] for c in t['checks']]}"
                           for t in state["tasks"] if t["status"] != "dropped")
        lines += ["You are the independent plan reviewer. Read the source documents and the stage documents yourself "
                  "and judge, before any code is written: does the solution satisfy the requirements with sound "
                  "trade-offs? Does the test plan prove every requirement? Do the tasks cover the requirements in a "
                  "sensible order, with the right roles and team sizes, and checks that would really prove them "
                  "(not `echo ok`)? Does the readiness report show that every needed resource is really available?",
                  f"Requirements:\n{requirements}", f"Tasks:\n{tasks}",
                  "Do NOT modify files. Finish with loop_review(review_id='plan_review', verdict='pass'|'fail', "
                  "findings=..., back_to='solution'|'test_design'|'plan'|'resources') listing concrete gaps."]
    elif kind == "review" and act["review"] == FINAL_GATE["id"]:
        matrix = []
        for r in state.get("requirements", []):
            covering = [t for t in state["tasks"] if r["id"] in t.get("covers", [])]
            evidence = []
            for t in covering:
                results = {c["id"]: state["checks"].get(f"{t['id']}.{c['id']}", {}).get("status", "not run")
                           for c in t["checks"]}
                evidence.append(f"{t['id']}={t['status']} checks {results}")
            matrix.append(f"- `{r['id']}` {r['text'][:200]} → {'; '.join(evidence) or 'NO TASK'}")
        staged = "\n".join(f"- [{c.get('stage', 'acceptance')}] `{c['id']}`: "
                            f"{state['checks'].get('acceptance.' + c['id'], {}).get('status', 'not run')}"
                            for c in goal["acceptance"])
        reviews = "\n".join(f"- {k}: {v['verdict']}" for k, v in state["reviews"].items())
        lines += ["You are the final acceptance reviewer. Decide whether the goal is really done: every requirement "
                  "must be proven by real evidence on the current candidate. Open the code and the tests to confirm "
                  "the evidence is meaningful; run read-only commands if needed.",
                  "Traceability matrix (requirement → tasks → task checks):\n" + "\n".join(matrix),
                  "Staged acceptance results:\n" + staged, "Other reviews:\n" + (reviews or "- none"),
                  "Do NOT modify files. Finish with loop_review(review_id='final-acceptance', verdict='pass'|'fail', "
                  "findings=...) naming every requirement that is not proven and why."]
    elif kind == "review":
        gate = find_gate(goal, act["review"])
        base = state.get("base_ref")
        lines += [f"You are the independent reviewer `{gate['id']}`. You did not write this work. Judge it against "
                  "the objective, constraints and acceptance, and look for defects the checks miss: correctness, "
                  "security, data loss, missing requirements, and tests that do not test anything.",
                  "Review instructions:\n" + gate["instructions"],
                  (f"The work since approval is `git diff {base}` plus uncommitted changes." if base else
                   "Inspect the current working tree and recent history."),
                  "Do NOT modify any file (that invalidates the review). Running read-only commands and tests is fine. "
                  f"Finish with loop_review(review_id='{gate['id']}', verdict='pass'|'fail', findings=...). Findings "
                  "must be concrete: file:line, what is wrong, and why it matters."]
    elif kind == "repair" and act.get("review"):
        result = state["reviews"].get(act["review"], {})
        lines.append(f"Review `{act['review']}` failed. Findings:\n{tail(result.get('findings', ''), 6000)}\n"
                     "Fix the real problems. If a finding is wrong, record why with loop_note(kind='decision').")
    elif kind == "repair":
        verification = state.get("verification") or {}
        index = {f"acceptance.{c['id']}": c.get("stage", "acceptance") for c in goal["acceptance"]}
        details = "\n\n".join(f"## [{index.get(k, 'acceptance')} tests] {k}\n"
                              f"{tail(state['checks'].get(k, {}).get('tail', ''), 2000)}"
                              for k in verification.get("failing", []))
        lines.append(f"Repair round {state.get('repair_rounds', 0)} of {goal['policy']['max_repair_rounds']}. "
                     f"Failing output:\n```\n{tail(details, 6000)}\n```")
    elif kind == "remediate":
        incident = next(i for i in state["incidents"] if i["id"] == act["incident"])
        lines.append(f"Incident `{incident['id']}` (round {incident.get('rounds', 0)}). Failing health output:\n"
                     f"```\n{tail(incident['evidence'], 6000)}\n```")
    elif kind == "report":
        open_findings = [f for f in state["findings"] if f["status"] in {"open", "confirmed", "recorded"}]
        lines.append("Findings so far:\n" + ("\n".join(f"- `{f['id']}` [{f['kind']}/{f['status']}] {f['text'][:400]}"
                                                       for f in open_findings[-20:]) or "- (none recorded)"))
    return "\n\n".join(x for x in lines if x)


def context_block(goal: dict, state: dict) -> str:
    parts = [f"# Goal: {goal['title']} ({goal['kind']})", "", goal["objective"]]
    if goal["context"]:
        parts += ["", "Context to read (paths or references):", *[f"- {c}" for c in goal["context"]]]
    if goal["constraints"]:
        parts += ["", "Constraints:", *[f"- {c}" for c in goal["constraints"]]]
    parts += ["", "Needs approval first (`loop_ask kind=approval`):",
              *[f"- {a}" for a in goal["policy"]["approval_required"]]]
    decisions = [q for q in state["questions"] if q["answer"] is not None][-10:]
    if decisions:
        parts += ["", "Human answers so far:", *[f"- {q['text'][:200]} → **{q['answer'][:300]}**" for q in decisions]]
    done = [t for t in state["tasks"] if t["status"] == "done"]
    if state["tasks"]:
        parts += ["", f"Plan: {len(done)}/{len(state['tasks'])} tasks done. " + ", ".join(
            f"{t['id']}={t['status']}" for t in state["tasks"][:40])]
    handoffs = [n for n in state["notes"] if n["kind"] in {"handoff", "decision", "risk"}][-4:]
    if handoffs:
        parts += ["", "Latest handoff notes (newest last):", *[f"- {n['at']}: {n['text'][:1200]}" for n in handoffs]]
    return "\n".join(parts)


REVIEW_PROTOCOL = """\
## Reviewer protocol

You are a read-only reviewer in a controller-run loop. Use only `loop_status`, `loop_next`,
`loop_note` and `loop_review`. Do not edit files and do not start other agents. Record exactly
one verdict with `loop_review`, then stop.
"""


def turn_prompt(goal: dict, state: dict, act: dict, iteration: int, project_root: Path | None = None) -> str:
    if act["kind"] == "review":
        return "\n\n".join([f"You are turn {iteration} of an autonomous Loop Engineering run: an independent review.",
                             context_block(goal, state), assignment(goal, state, act, project_root), REVIEW_PROTOCOL])
    return "\n\n".join([
        f"You are turn {iteration} of an autonomous Loop Engineering run in this repository.",
        context_block(goal, state),
        assignment(goal, state, act, project_root),
        playbook(goal["kind"]),
        PROTOCOL,
    ])


def _stage_documents(goal: dict, state: dict) -> str:
    rows = []
    for sid, record in (state.get("stages") or {}).items():
        if record.get("path") and record.get("done"):
            rows.append(f"- {sid}: `{record['path']}`" + (f" — {record['summary'][:200]}" if record.get("summary") else ""))
    return "\n".join(rows)
