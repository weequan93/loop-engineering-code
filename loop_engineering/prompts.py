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
- Your assignment is in this prompt; `loop_next` only says whether it changed. `loop_status` shows everything.
- Full pipeline (brief → design → design review → develop → final review): `loop_stage_done` records the brief
  or design document; `loop_requirements` records requirements; `loop_plan` adds or updates tasks (with checks,
  `covers`, `role`, `team`, and `final=true` to submit the plan).
- `loop_task` starts or finishes a task. `block` (needs a person) or `defer` (waits for a condition) holds
  only that task, with the reason; the other tasks go on. A task stuck on its checks: split it with `loop_plan`
  (drop it, add smaller tasks that cover its requirements) rather than retrying the same thing.
- `loop_check` asks the controller to run checks; only controller-run checks count as evidence.
- `loop_note` records progress, decisions and findings. Before you stop, write one
  `kind="handoff"` note: what changed, what is next, and any traps. The next turn starts fresh
  and depends on it.
- `loop_ask` asks the human. Use `kind="approval"` before any action under "needs approval", and ask
  when the documents leave a product decision open. Everything else is yours to decide: choose the best
  option, record it with `loop_note(kind="decision")` and continue. To get the human's view without
  stopping, ask with `blocking=false`.
- `loop_block` only when no progress is possible without a person. Include the exact
  reproduction and what you already tried.
- `loop_finish` is for when every task is finished. The controller then runs acceptance.

Rules: the controller decides completion. Never edit `.loop/` files directly. Never weaken,
delete or bypass checks. Stay within the constraints. One meaningful step per turn is fine.
Stop when your assignment is done or you are waiting on the controller or a human.

Keep your context small; everything you read is re-sent on every later step of this turn.
- Read state with `loop_status`, `.loop/STATUS.md`, the goal's `GOAL.md` or `REQUIREMENTS.md`, never `.loop/`
  JSON files.
- Locate, then read: find the place with `rg -n` (or `grep -n`), then read only those lines (`sed -n 'A,Bp'`).
  Never print a whole large file, log or JSON; pipe long command output through `tail -n 40` or `head`.
- Start from the task's working set (`Read first`) and widen only when it is not enough.
- Verify what this turn touched rather than re-auditing everything; the controller runs the full checks.
- Write no per-turn report or summary documents; the handoff note is the report. Write a document only when
  the assignment names it.
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
    from .engine import (FINAL_REVIEW, GATE_ROUNDS, GATE_STAGE_IDS, find_gate, pipeline, review_facets,
                         uncovered)
    full = pipeline(goal)
    documents = _stage_documents(goal, state)
    revise = act.get("revise_after")
    if revise:
        record = state.get("stages", {}).get(revise) or {}
        lines.append(f"**{revise} sent this stage back.** Address these findings:\n{tail(record.get('findings', ''), 6000)}")
    if documents and kind not in {"work", "repair"}:
        lines.append("Documents produced by earlier stages (read them):\n" + documents)
    if kind == "brief":
        lines.append(
            f"Stage 1 of 3: brief. Write `{act['output']}` (create the folder): the problem, who the users are, their "
            "main journeys, what success looks like, and the scope, with an explicit out-of-scope list. Then record "
            "this goal's requirements with `loop_requirements`: `id` (reuse the document's own ID, otherwise R-001…), "
            "`text` (what must be true), `source` (doc path and section), `verify` (how it will be proven). Record only "
            "what THIS goal must deliver; later milestones go to the out-of-scope list. Aim for at most about 20 "
            "requirements: if the goal needs far more, propose splitting it with loop_ask instead of recording them "
            "all. Do not invent requirements; ask when the documents are ambiguous or a business decision is open. "
            f"Finish with `loop_stage_done(stage=\"brief\", path=\"{act['output']}\", summary=...)` and "
            "`loop_requirements(final=true)`. No production code in this stage.")
        if state.get("requirements"):
            lines.append(f"Already recorded: {len(state['requirements'])} requirements "
                         f"({', '.join(r['id'] for r in state['requirements'][:40])}).")
    elif kind == "design":
        lines.append(
            f"Stage 2 of 3: design and plan. Write `{act['output']}`, short and concrete: the chosen approach and the "
            "trade-offs that decided it, contracts between parts, risks, and the resources the work needs (tools, "
            "services, accounts), checked to be really available. Prove feasibility first: for any requirement you "
            "are not sure the platform or budget allows, run a small spike now; if one is infeasible, say so and ask "
            "with loop_ask instead of planning around it. Then record the tasks with `loop_plan`: each task has "
            "`covers` (requirement IDs), `depends_on` (only real ones; independent tasks run in parallel), a `detail`, "
            "a `role` (architect, backend, frontend, mobile, data, tester, security, devops, or a project role), and "
            "real `checks` that prove its requirements. These checks are the test design: no separate test plan. "
            "Each task is one result with its own pass/fail: at most 6 requirements and 8 checks. "
            "Give each task `reads`: the few files or doc sections (`path#heading`) it needs, so its turn starts "
            "there instead of searching the repository. "
            "Size each task to finish in a few turns; at most about 20 tasks. Use `team` only when a task really "
            f"splits into slices with separate files (at most {goal['agent'].get('team_limit', 4)} seats). Finish with "
            f"`loop_stage_done(stage=\"design\", path=\"{act['output']}\", summary=...)` and `loop_plan(final=true)`. "
            "An independent reviewer then checks the brief, requirements, design and plan once before development.\n\n"
            f"Requirements:\n{requirement_index(goal, state)}\n\nGoal acceptance checks (staged):\n"
            + _checks_text(goal["acceptance"]))
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
        if task.get("reads"):
            lines.append("Read first (working set; widen only if it is not enough):\n"
                         + "\n".join(f"- `{r}`" for r in task["reads"]))
        if task.get("role"):
            lines.append(f"Suggested specialist role: {task['role']}.")
        if task.get("last_failure"):
            lines.append(f"Previous attempt {task['attempts']} failed these checks:\n```\n"
                         f"{tail(task['last_failure']['tail'], 3000)}\n```")
        if task["notes"]:
            lines.append("Task notes:\n" + "\n".join(f"- {n['text'][:500]}" for n in task["notes"][-5:]))
        batch = [t for t in state["tasks"] if t["id"] in (act.get("batch") or [])]
        if batch:
            lines.append(
                f"## Parallel tasks this turn ({len(batch)} more)\n\nThese tasks are also ready and independent of "
                f"`{task['id']}`. Work on all of them at once: start one sub-agent per task with your host's sub-agent "
                "feature (Codex: spawn_agent; Claude Code: the Task tool) and keep the main task yourself. Give each "
                "sub-agent its task, its checks, its role guide and exact file ownership, so that no two agents edit "
                "the same file. Wait for all, integrate, then call `loop_task(action=\"done\")` for each finished "
                "task: one check job runs at a time, so later claims are queued and the controller verifies them in "
                "order (do not wait for them). If the goal's constraints require serial work (for example "
                "performance measurements with nothing else running), or your host cannot start sub-agents, do only "
                f"`{task['id']}`.\n\n" + "\n\n".join(
                    f"### `{t['id']}`: {t['title']}\n{(t.get('detail') or '')[:1500]}\nRole: {t.get('role') or '-'}"
                    f"\nChecks:\n{_checks_text(t['checks'])}" for t in batch))
    elif kind == "review" and act["review"] in GATE_STAGE_IDS:
        record = state.get("stages", {}).get(act["review"]) or {}
        rounds = record.get("rounds", 0)
        tasks = "\n".join(f"- `{t['id']}` [{t.get('role') or '-'}] {t['title']} ← {t['depends_on']} covers "
                           f"{t.get('covers', [])} checks {[c['id'] for c in t['checks']]}"
                           for t in state["tasks"] if t["status"] != "dropped")
        lines += ["You are the independent design reviewer. Before any code is written, read the brief and the design "
                  "yourself and judge once: are the requirements faithful to the documents, in scope and verifiable? "
                  "Is the design feasible on this platform and within its budgets? Do the tasks cover every "
                  "requirement with checks that would really prove it (not `echo ok`), in a sensible order and size? "
                  "Are the needed resources really available? Fail only for problems that would make the goal wrong "
                  "or undeliverable; list smaller points as advice in the findings of a pass.",
                  f"This is round {rounds + 1} of {GATE_ROUNDS}; after {GATE_ROUNDS} failed rounds a person decides.",
                  f"Requirements:\n{requirement_index(goal, state)}", f"Tasks:\n{tasks}",
                  "Do NOT modify files. Finish with loop_review(review_id='design_review', verdict='pass'|'fail', "
                  "findings=..., back_to='brief'|'design') with concrete gaps."]
    elif kind == "review" and act["review"] == FINAL_REVIEW["id"]:
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
        rounds = (state["reviews"].get(FINAL_REVIEW["id"]) or {}).get("rounds", 0)
        base = state.get("base_ref")
        lines += ["You are the final reviewer. You did not write this work. Judge the finished goal once, from every "
                  "angle below; with your host's sub-agents you may review the angles in parallel, read-only. Open the "
                  "code and tests to confirm the evidence is meaningful; run read-only commands if needed.",
                  "Angles:\n" + "\n".join(f"- **{name}**: {what}" for name, what in review_facets(goal)),
                  (f"The work since approval is `git diff {base}` plus uncommitted changes." if base else
                   "Inspect the current working tree and recent history."),
                  "Traceability (requirement → tasks → task checks):\n" + ("\n".join(matrix) or "- (no requirements)"),
                  "Staged acceptance results:\n" + staged,
                  f"This is round {rounds + 1} of {GATE_ROUNDS}; after {GATE_ROUNDS} failed rounds a person decides. "
                  "Fail only for real defects or unproven requirements; list minor points as advice in a pass.",
                  f"Do NOT modify files. Finish with loop_review(review_id='{FINAL_REVIEW['id']}', verdict='pass'|'fail', "
                  "findings=...) with findings grouped by angle: file:line, what is wrong, and why it matters."]
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


GOAL_BLOCK_CHARS = 2500  # objective, and constraints together, beyond this go by reference to GOAL.md


def goal_path(goal: dict) -> str:
    return f".loop/goals/{goal['id']}/GOAL.md"


def first_sentence(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    for stop in (". ", "。", "; "):
        if stop in text:
            text = text[:text.index(stop) + len(stop)].strip()
            break
    return short(text, limit)


def context_block(goal: dict, state: dict) -> str:
    """The goal as every fresh turn sees it: short, with the full text one read away in GOAL.md."""
    ref = goal_path(goal)
    objective = goal["objective"]
    if len(objective) > GOAL_BLOCK_CHARS:
        objective = objective[:GOAL_BLOCK_CHARS].rstrip() + f"… (full objective: `{ref}`)"
    parts = [f"# Goal: {goal['title']} ({goal['kind']})", "", objective]
    if goal["context"]:
        parts += ["", "Context to read (paths or references):", *[f"- {c}" for c in goal["context"]]]
    constraints = goal["constraints"]
    if constraints and sum(len(c) for c in constraints) <= GOAL_BLOCK_CHARS:
        parts += ["", "Constraints:", *[f"- {c}" for c in constraints]]
    elif constraints:
        parts += ["", f"Constraints (first sentence of each; they bind in full: read `{ref}` before work they "
                      "touch):", *[f"{i}. {first_sentence(c)}" for i, c in enumerate(constraints, 1)]]
    parts += ["", "Needs approval first (`loop_ask kind=approval`):",
              *[f"- {a}" for a in goal["policy"]["approval_required"]],
              "Anything not listed here or reserved by the constraints is pre-approved: decide, record it, continue."]
    answered = [q for q in state["questions"] if q["answer"] is not None]
    if answered:
        more = f" (all {len(answered)}: `{ref}`)" if len(answered) > 3 else ""
        parts += ["", f"Latest human answers{more}:",
                  *[f"- {q['text'][:200]} → **{q['answer'][:300]}**" for q in answered[-3:]]]
    if state["tasks"]:
        counts = {}
        for t in state["tasks"]:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        active = [t["id"] for t in state["tasks"] if t["status"] in {"active", "blocked"}][:12]
        parts += ["", f"Plan: {counts.get('done', 0)}/{len(state['tasks'])} tasks done ("
                  + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ")"
                  + (f"; active: {', '.join(active)}" if active else "") + ". `loop_status` lists every task."]
    handoff = next((n for n in reversed(state["notes"]) if n["kind"] == "handoff"), None)
    decisions = [n for n in state["notes"] if n["kind"] in {"decision", "risk"}][-2:]
    if handoff or decisions:
        parts += ["", "Latest notes:"]
        parts += [f"- {n['at']} {n['kind']}: {short(n['text'], 800)}" for n in decisions]
        if handoff:
            parts.append(f"- {handoff['at']} handoff (newest): {short(handoff['text'], 1500)}")
    return "\n".join(parts)


def goal_markdown(goal: dict, state: dict) -> str:
    """Generated reference with the goal's full text; the turn prompt carries a short version (context_block)."""
    lines = [f"# Goal · {goal['title']} ({goal['kind']})", "",
             "Generated by the controller from the approved goal; do not edit.", "", "## Objective", "",
             goal["objective"]]
    if goal["context"]:
        lines += ["", "## Context", "", *[f"- {c}" for c in goal["context"]]]
    if goal["constraints"]:
        lines += ["", "## Constraints", "", *[f"{i}. {c}" for i, c in enumerate(goal["constraints"], 1)]]
    lines += ["", "## Needs approval first", "", *[f"- {a}" for a in goal["policy"]["approval_required"]]]
    answered = [q for q in state["questions"] if q["answer"] is not None]
    if answered:
        lines += ["", "## Human answers", "", *[f"- `{q['id']}` {q['text']} → **{q['answer']}**" for q in answered]]
    decisions = [n for n in state["notes"] if n["kind"] == "decision"][-30:]
    if decisions:
        lines += ["", "## Decisions (latest 30)", "", *[f"- {n['at']}: {n['text']}" for n in decisions]]
    return "\n".join(lines) + "\n"


REVIEW_PROTOCOL = """\
## Reviewer protocol

You are a read-only reviewer in a controller-run loop. Read whatever you need: open the documents,
plans, code and check scripts, and run read-only shell commands and tests. Of the Loop tools, use
`loop_status`, `loop_next`, `loop_note` and `loop_review`. Do not edit files and do not start other
agents. Record exactly one verdict with `loop_review`, then stop. Read loop state with `loop_status`,
`.loop/STATUS.md` or the goal's `REQUIREMENTS.md`, never `.loop/` JSON files.
"""


def turn_sections(goal: dict, state: dict, act: dict, iteration: int,
                  project_root: Path | None = None) -> list[tuple[str, str]]:
    """The prompt's parts by name, so the runner can bill each one (see the turn's context.json)."""
    if act["kind"] == "review":
        return [("intro", f"You are turn {iteration} of an autonomous Loop Engineering run: an independent review."),
                ("goal", context_block(goal, state)), ("assignment", assignment(goal, state, act, project_root)),
                ("protocol", REVIEW_PROTOCOL)]
    return [("intro", f"You are turn {iteration} of an autonomous Loop Engineering run in this repository."),
            ("goal", context_block(goal, state)), ("assignment", assignment(goal, state, act, project_root)),
            ("playbook", playbook(goal["kind"])), ("protocol", PROTOCOL)]


def turn_prompt(goal: dict, state: dict, act: dict, iteration: int, project_root: Path | None = None) -> str:
    return "\n\n".join(text for _, text in turn_sections(goal, state, act, iteration, project_root))


def requirement_index(goal: dict, state: dict) -> str:
    """IDs and short text only; the full text, sources and verify plans are in the goal's REQUIREMENTS.md."""
    rows = "\n".join(f"- `{r['id']}` {short(r['text'], 110)}" for r in state.get("requirements", []))
    return (f"(full text, sources and verify plans: `.loop/goals/{goal['id']}/REQUIREMENTS.md`; read the entries "
            f"you need)\n{rows}")


def short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def requirements_markdown(goal: dict, state: dict) -> str:
    """Generated reference for agents and humans; written next to STATUS.md."""
    lines = [f"# Requirements · {goal['title']}", "",
             f"{len(state.get('requirements', []))} requirements" + (" · final" if state.get("requirements_final") else "")
             + ". Generated by the controller; do not edit.", ""]
    for r in state.get("requirements", []):
        covering = [t["id"] for t in state["tasks"] if r["id"] in t.get("covers", []) and t["status"] != "dropped"]
        lines += [f"## {r['id']}", "", r["text"], "", f"- Source: {r.get('source') or '-'}",
                  f"- Verify: {r.get('verify') or '-'}", f"- Covered by: {', '.join(covering) or 'no task yet'}", ""]
    return "\n".join(lines)


def continuation_prompt(goal: dict, state: dict, act: dict, iteration: int, project_root: Path | None = None) -> str:
    """A resumed session already holds the goal, playbook and protocol: send only the current step and what changed."""
    since = state["turns"][-1]["started_at"] if state["turns"] else ""
    lines = [f"You are turn {iteration} of the same autonomous Loop Engineering run, continuing in this session. "
             "Your earlier context, playbook and protocol still apply; below is the current step and what changed."]
    answers = [q for q in state["questions"] if q["answer"] is not None and (q.get("answered_at") or "") > since]
    if answers:
        lines.append("Human answers since your last turn:\n" + "\n".join(
            f"- {q['text'][:200]} → **{q['answer'][:400]}**" for q in answers))
    notes = [n for n in state["notes"] if n["kind"] in {"decision", "risk"} and n["at"] > since
             and not str(n.get("by", "")).startswith("agent")][-3:]
    if notes:
        lines.append("Decisions recorded since your last turn:\n" + "\n".join(f"- {n['text'][:1200]}" for n in notes))
    lines.append(_assignment(goal, state, act))
    lines.append("Before you stop, record a `loop_note(kind=\"handoff\")` as usual.")
    return "\n\n".join(lines)


def _stage_documents(goal: dict, state: dict) -> str:
    rows = []
    for sid, record in (state.get("stages") or {}).items():
        if record.get("path") and record.get("done"):
            rows.append(f"- {sid}: `{record['path']}`" + (f" — {record['summary'][:200]}" if record.get("summary") else ""))
    return "\n".join(rows)
