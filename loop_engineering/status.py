"""Human- and agent-readable status: STATUS.md, a one-line summary and JSON."""

from __future__ import annotations

from . import model
from .util import age_seconds, atomic_write

ICONS = {"pending": "○", "active": "◐", "done": "●", "blocked": "✖", "dropped": "–",
         "pass": "✓", "fail": "✗", "error": "!", "timeout": "⏱", "cancelled": "–", "running": "…"}


def runner_view(store) -> dict:
    from . import procs
    runner = store.runner()
    if not runner:
        return {"state": "not running"}
    beat = age_seconds(runner.get("heartbeat_at"))
    live = procs.alive(runner.get("pid"), runner.get("identity"))
    if not live:
        state = "exited" if runner.get("exited_at") else "lost (process gone without exit record)"
    elif beat is not None and beat > 180:
        state = f"stale heartbeat ({int(beat)}s)"
    else:
        state = "running"
    return {**runner, "state": state, "heartbeat_age_s": None if beat is None else int(beat)}


def pipeline_view(goal: dict, state: dict, act: dict) -> list[dict] | None:
    from . import engine
    if not engine.pipeline(goal):
        return None
    current = engine.stage(goal, state, act)
    gates = engine.review_gates(goal)
    names = ([s["id"] for s in engine.pre_stages(goal)] + ["develop", "test", "reviews"] +
             [g["id"] for g in gates if g["id"] in {engine.FINAL_GATE["id"], engine.PRODUCT_GATE["id"]}] + ["done"])
    stages_ = state.get("stages", {})

    def detail(name: str) -> str:
        if name in engine.STAGE_BY_ID:
            kind = engine.STAGE_BY_ID[name]["type"]
            record = stages_.get(name) or {}
            if kind == "artifact":
                return record.get("path", "") if record.get("done") else "pending"
            if kind == "gate":
                return record.get("verdict", "pending") + (f" (round {record['rounds']})" if record.get("rounds") else "")
            if kind == "requirements":
                return f"{len(state.get('requirements', []))} recorded" + (" · final" if state.get("requirements_final") else "")
            return (f"{len(state['tasks'])} tasks · {len(engine.uncovered(state))} uncovered"
                    + (" · final" if state.get("plan_final") else ""))
        if name == "develop":
            return f"{sum(t['status'] == 'done' for t in state['tasks'])}/{len(state['tasks'])} tasks"
        if name == "test":
            return ", ".join(f"{c.get('stage', 'acceptance')}:{state['checks'].get('acceptance.' + c['id'], {}).get('status', '-')}"
                             for c in goal["acceptance"])
        if name == "reviews":
            return ", ".join(f"{g['id']}:{(state['reviews'].get(g['id']) or {}).get('verdict', '-')}" for g in gates
                             if g["id"] not in {engine.FINAL_GATE["id"], engine.PRODUCT_GATE["id"]}) or "none"
        if name in {engine.FINAL_GATE["id"], engine.PRODUCT_GATE["id"]}:
            return (state["reviews"].get(name) or {}).get("verdict", "pending")
        return ""
    reached = names.index(current) if current in names else -1
    return [{"stage": name, "state": "done" if current == "done" or i < reached else
             "current" if i == reached else "pending", "detail": detail(name)} for i, name in enumerate(names)]


def summary(store, goal=None, state=None) -> dict:
    from .engine import next_action
    goal = goal or store.goal()
    state = state or store.state()
    tasks = state["tasks"]
    counts = {s: sum(1 for t in tasks if t["status"] == s) for s in model.TASK_STATUSES}
    acceptance = {f"acceptance.{c['id']}": state["checks"].get(f"acceptance.{c['id']}", {}).get("status", "not run")
                  for c in goal["acceptance"]}
    p = goal["policy"]
    act = next_action(goal, state, (state.get("verification") or {}).get("fingerprint"))
    return {
        "pipeline": pipeline_view(goal, state, act),
        "goal": goal["id"], "title": goal["title"], "kind": goal["kind"],
        "status": state["status"], "reason": state["status_reason"],
        "next": act,
        "tasks": {"total": len(tasks), **counts},
        "acceptance": acceptance,
        "open_questions": [{"id": q["id"], "kind": q["kind"], "text": q["text"], "options": q["options"],
                            "blocking": q["blocking"]} for q in state["questions"] if q["answer"] is None],
        "blocker": state["blocker"],
        "iteration": state["iteration"], "max_iterations": p["max_iterations"],
        "usage": state["usage"], "runner": runner_view(store),
        "incident": next((i["id"] for i in state["incidents"] if i["status"] == "open"), None),
        "next_cycle_at": state.get("next_cycle_at"), "updated_at": state["updated_at"],
    }


def line(store) -> str:
    try:
        s = summary(store)
    except Exception as exc:  # the status line must never fail
        return f"loop: {type(exc).__name__}"
    t = s["tasks"]
    checks = list(s["acceptance"].values())
    passed = sum(1 for c in checks if c == "pass")
    cost = f" · ${s['usage']['cost_usd']:.2f}" if s["usage"].get("cost_usd") else ""
    runner = "" if s["runner"]["state"] == "running" else (" · runner off" if s["runner"]["state"] in
                                                            {"not running", "exited"} else " · runner ?")
    flag = " · ❓" + str(len(s["open_questions"])) if s["open_questions"] else ""
    return (f"loop[{s['kind']}] {s['title'][:40]} · {s['status']} · tasks {t['done']}/{t['total']}"
            f" · checks {passed}/{len(checks)} · turn {s['iteration']}/{s['max_iterations']}{cost}{flag}{runner}")


def markdown(store) -> str:
    goal, state = store.goal(), store.state()
    s = summary(store, goal, state)
    out = [f"# Loop · {goal['title']}", "",
           f"**{s['status'].upper()}** — {s['reason']}", "",
           f"- Goal `{goal['id']}` · kind **{goal['kind']}** · updated {state['updated_at']}",
           f"- Next: **{s['next']['kind']}** ({s['next']['actor']}) — {s['next']['summary']}",
           f"- Runner: {s['runner']['state']}" + (f" · pid {s['runner'].get('pid')} · adapter "
                                                   f"{s['runner'].get('adapter')}" if s['runner'].get('pid') else ""),
           f"- Turns {state['iteration']}/{goal['policy']['max_iterations']} · agent time "
           f"{state['usage']['agent_seconds'] / 60:.1f} min · reported cost ${state['usage']['cost_usd']:.2f}"
           + ("" if state["usage"].get("cost_complete", True) else " (partial: some turns reported no cost)"),
           ""]
    if s["pipeline"]:
        marks = {"done": "●", "current": "◐", "pending": "○"}
        out += ["## Pipeline", "", " → ".join(f"{marks[p['state']]} {p['stage']}" for p in s["pipeline"]), ""]
        out += [f"- {p['stage']}: {p['detail']}" for p in s["pipeline"] if p["detail"]]
        out.append("")
    if state["blocker"]:
        b = state["blocker"]
        out += [f"## Blocked: {b['category']}", "", b["reason"], ""]
        if b.get("attempted"):
            out += ["Tried: " + b["attempted"], ""]
        out += ["Resolve, then run `loop unblock --note \"what changed\"`.", ""]
    questions = [q for q in state["questions"] if q["answer"] is None]
    if questions:
        out += ["## Questions for you", ""]
        for q in questions:
            opts = f" Options: {', '.join(q['options'])}." if q["options"] else ""
            out += [f"- `{q['id']}` {'(blocking) ' if q['blocking'] else ''}{q['text']}{opts}",
                    f"  Answer: `loop answer {q['id']} \"...\"`"]
        out.append("")
    if goal["objective"]:
        objective = goal["objective"].strip().splitlines()
        out += ["## Objective", "", *objective[:12], *(["…"] if len(objective) > 12 else []), ""]
    if state["tasks"]:
        t = s["tasks"]
        out += [f"## Tasks ({t['done']}/{t['total']} done)", ""]
        for task in state["tasks"]:
            extra = ""
            if task.get("last_failure") and task["status"] != "done":
                extra = f" — last checks failed: {', '.join(task['last_failure']['checks'])}"
            deps = f" ← {', '.join(task['depends_on'])}" if task["depends_on"] else ""
            who = ([f"{s['count']}×{s['role']}" for s in task.get("team", [])] or
                   ([task["role"]] if task.get("role") else []))
            deps += f" · {' + '.join(who)}" if who else ""
            out.append(f"- {ICONS.get(task['status'], '?')} `{task['id']}` {task['title']}{deps}{extra}")
        out.append("")
    jobs = [j for j in state.get("jobs", {}).values() if j["status"] == "running"]
    if jobs:
        out += ["## Checks running now", ""]
        out += [f"- `{j['id']}` {', '.join(j['checks'])} · started {j['started_at']} · pid {j.get('pid')}" for j in jobs]
        out.append("")
    if goal["acceptance"]:
        out += ["## Acceptance checks", ""]
        for c in goal["acceptance"]:
            result = state["checks"].get(f"acceptance.{c['id']}")
            label = result["status"] if result else "not run"
            when = f" · {result['finished_at']}" if result else ""
            out.append(f"- {ICONS.get(label, '○')} `{c['id']}` {label}{when} — "
                       f"`{c.get('run') or ' '.join(c['argv'])}`")
        out.append("")
    from .engine import review_gates
    if review_gates(goal):
        out += ["## Reviews", ""]
        for gate in review_gates(goal):
            result = state["reviews"].get(gate["id"])
            label = result["verdict"] if result else "pending"
            out.append(f"- {ICONS.get({'pass': 'pass', 'fail': 'fail'}.get(label, ''), '○')} `{gate['id']}` "
                       f"({gate['by']}) {label}" + (f" · {result['at']}" if result else "") +
                       (f" — {result['findings'][:200]}" if result and result.get("findings") else ""))
        out.append("")
    if goal["kind"] == "operate":
        incidents = [i for i in state["incidents"] if i["status"] == "open"]
        cycles = state.get("cycles", [])[-5:]
        out += ["## Operations", "", f"- Schedule: every {goal['schedule']['interval_minutes']} min · next "
                f"{state.get('next_cycle_at') or 'now'}"]
        out += [f"- Open incident `{i['id']}` since {i['opened_at']}: {', '.join(i['failing'])}" for i in incidents]
        out += [f"- Cycle {c['at']}: {'healthy' if c['passed'] else 'failing ' + ', '.join(c['failing'])}"
                for c in reversed(cycles)]
        out.append("")
    findings = [f for f in state["findings"] if f["status"] != "rejected"][-10:]
    if findings:
        out += ["## Findings", ""]
        out += [f"- `{f['id']}` [{f['kind']}/{f['status']}] {f['text'][:300]}" for f in findings]
        out.append("")
    if state["turns"]:
        out += ["## Recent turns", ""]
        for turn in state["turns"][-5:]:
            model = f" · {turn.get('adapter')}{'/' + turn['model'] if turn.get('model') else ''}" \
                    f"{' @' + turn['effort'] if turn.get('effort') else ''}" if turn.get("adapter") else ""
            out.append(f"- #{turn['n']} {turn['action']}{model} · {turn['outcome']} · {turn.get('seconds', 0):.0f}s"
                       f"{' · progress' if turn.get('progress') else ' · no progress'} — "
                       f"{(turn.get('summary') or '').strip()[:200]}")
        out.append("")
    notes = state["notes"][-6:]
    if notes:
        out += ["## Recent notes", ""]
        out += [f"- {n['at']} [{n['kind']}] {n['text'][:300]}" for n in notes]
        out.append("")
    if state.get("report"):
        out += ["## Report", "", state["report"]["text"][:4000], ""]
    from . import queue as queue_mod
    queued = queue_mod.view(store.project)
    if queued:
        out += ["## Goal queue", ""]
        for row in queued:
            marker = "▶" if row["id"] == goal["id"] else ("✓" if row["status"] == "done" else "○")
            approval = "" if row["status"] == "done" else (" · approved" if row["approved"] else " · needs approval")
            out.append(f"- {marker} {row['position']}. `{row['id']}` {row['title']} — {row['status']}{approval}")
        out += ["", "The runner continues with the next approved goal after this one is done.", ""]
    out += ["---", "Controller-owned file; regenerated on every change. Commands: `loop status`, `loop next`, "
            "`loop dashboard`. Agents: use the loop MCP tools."]
    return "\n".join(out) + "\n"


def render_files(store) -> None:
    text = markdown(store)
    atomic_write(store.status_path, text)
    if store.project.current_id() == store.id:
        atomic_write(store.project.loop / "STATUS.md", text)
