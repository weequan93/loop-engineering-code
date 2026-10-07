"""The goal state machine: who acts next, and what each operation may change.

``next_action`` is deterministic over (goal, state, workspace fingerprint). It
returns one of three actors:

- ``agent``      – a coding agent turn is useful (plan, work, repair, remediate, report)
- ``controller`` – the controller acts itself (verify acceptance, health check)
- ``human``      – only a person can unblock (approve, answer, review a blocker)
- ``none``       – nothing to do (paused, idle until the next cycle, terminal)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import checks as checks_mod
from . import model
from .util import LoopError, new_id, now, parse_time, slug, tail, text

NOTE_KINDS = ("progress", "handoff", "decision", "risk", "hypothesis", "evidence", "root_cause")
FINDING_KINDS = {"hypothesis", "evidence", "root_cause"}


def action(kind: str, actor: str, summary: str, **extra) -> dict:
    return {"kind": kind, "actor": actor, "summary": summary, **extra}


def open_questions(state: dict, blocking_only: bool = True) -> list[dict]:
    return [q for q in state["questions"] if q.get("answer") is None and (q["blocking"] or not blocking_only)]


def task_by_id(state: dict, task_id: str) -> dict:
    for task in state["tasks"]:
        if task["id"] == task_id:
            return task
    raise LoopError(f"Unknown task {task_id}. Known: {[t['id'] for t in state['tasks']]}")


def ready_tasks(state: dict) -> list[dict]:
    finished = {t["id"] for t in state["tasks"] if t["status"] in {"done", "dropped"}}
    return [t for t in state["tasks"] if t["status"] == "pending" and set(t["depends_on"]) <= finished]


def open_incident(state: dict) -> dict | None:
    return next((i for i in reversed(state["incidents"]) if i["status"] == "open"), None)


# --------------------------------------------------------------------- the full pipeline
# intake → requirements → requirements review → solution → test design → plan (+ staffing) → resources
# → plan review → develop → staged tests → code / security / performance reviews → final acceptance
# → product acceptance → human reviews. Each stage has a role (roles.WORKFLOWS) and a controller gate.

PRE_STAGES = [
    {"id": "intake", "type": "artifact", "file": "product-brief.md",
     "summary": "Write the product brief: problem, users, journeys, goals, success metrics, scope and non-scope."},
    {"id": "requirements", "type": "requirements",
     "summary": "Read the documents and record every requirement with its source and how it will be verified."},
    {"id": "requirements_review", "type": "gate", "subject": ["intake", "requirements"], "back_to": "requirements",
     "summary": "Independent product review of the brief and the recorded requirements."},
    {"id": "solution", "type": "artifact", "file": "solution.md",
     "summary": "Design the solution: options and trade-offs, chosen architecture, contracts, risks."},
    {"id": "test_design", "type": "artifact", "file": "test-plan.md",
     "summary": "Design the test cases that will prove every requirement, before development."},
    {"id": "plan", "type": "plan",
     "summary": "Plan tasks that cover every requirement, with roles, teams and real checks, then finalize."},
    {"id": "resources", "type": "artifact", "file": "readiness.md",
     "summary": "Probe and confirm every tool, service, environment and account the plan needs."},
    {"id": "plan_review", "type": "gate", "subject": ["solution", "test_design", "plan", "resources"],
     "back_to": "plan", "summary": "Independent review of the solution, test plan, task plan and readiness."},
]
STAGE_BY_ID = {s["id"]: s for s in PRE_STAGES}
GATE_STAGE_IDS = {s["id"] for s in PRE_STAGES if s["type"] == "gate"}
FINAL_GATE = {"id": "final-acceptance", "by": "agent", "role": "acceptance-lead", "timeout_minutes": 30,
              "instructions": "Final acceptance: verify, requirement by requirement, that real evidence (task "
                              "checks, staged test results, reviews) proves each one on the current candidate."}
PRODUCT_GATE = {"id": "product-acceptance", "by": "agent", "role": "product", "timeout_minutes": 30,
                "instructions": "Product acceptance: use the product as a user would and judge it against the "
                                "product brief, the journeys and the success metrics."}
SPECIALIST_GATES = [
    {"id": "code-review", "by": "agent", "role": "reviewer", "timeout_minutes": 30,
     "instructions": "Code review of the whole change against the requirements, solution and test plan."},
    {"id": "security-review", "by": "agent", "role": "security", "timeout_minutes": 30,
     "instructions": "Security review: threat-model the change; authorization, input handling, secrets, data "
                     "exposure and dependencies."},
    {"id": "performance-review", "by": "agent", "role": "performance", "timeout_minutes": 30,
     "instructions": "Performance review against the performance requirements and budgets, with reproducible "
                     "measurements."},
]
PERFORMANCE_WORDS = ("performance", "latency", "throughput", "p95", "p99", "load", "性能", "延迟", "吞吐")


def pipeline(goal: dict) -> bool:
    value = goal.get("pipeline")
    return goal["kind"] == "develop" and (value == "full" or isinstance(value, dict))


def skipped(goal: dict) -> set:
    value = goal.get("pipeline")
    return set(value.get("skip", [])) if isinstance(value, dict) else set()


def pre_stages(goal: dict) -> list[dict]:
    return [s for s in PRE_STAGES if s["id"] not in skipped(goal)] if pipeline(goal) else []


def artifact_path(goal: dict, stage_def: dict) -> str:
    return f"docs/loop/{goal['id']}/{stage_def['file']}"


def _needs_performance(goal: dict) -> bool:
    text_ = (goal["objective"] + " " + " ".join(goal["constraints"])).lower()
    return (any(c.get("stage") == "performance" for c in goal["acceptance"]) or
            any(word in text_ for word in PERFORMANCE_WORDS))


def review_gates(goal: dict) -> list[dict]:
    """Post-development gates: the goal's agent reviews, built-in specialist reviews (code, security,
    performance when relevant), final acceptance, product acceptance, then human reviews."""
    gates_ = list(goal["reviews"])
    if not pipeline(goal):
        return gates_
    skip = skipped(goal)
    agent_gates = [g for g in gates_ if g["by"] == "agent"]
    human_gates = [g for g in gates_ if g["by"] != "agent"]
    ids = {g["id"] for g in gates_}

    def covered(role: str, word: str) -> bool:
        return any(g.get("role") == role or word in g["id"] for g in agent_gates)
    specialists = []
    for gate in SPECIALIST_GATES:
        role, word = gate["role"], gate["id"].split("-")[0]
        if gate["id"] in skip or gate["id"] in ids or covered(role, word):
            continue
        if gate["id"] == "performance-review" and not _needs_performance(goal):
            continue
        specialists.append(gate)
    closing = [g for g in (FINAL_GATE, PRODUCT_GATE) if g["id"] not in skip and g["id"] not in ids]
    return agent_gates + specialists + closing + human_gates


def find_gate(goal: dict, review_id: str | None) -> dict | None:
    if not review_id:
        return None
    if review_id in GATE_STAGE_IDS and any(s["id"] == review_id for s in pre_stages(goal)):
        return {"id": review_id, "by": "agent", "timeout_minutes": 20, "instructions": STAGE_BY_ID[review_id]["summary"]}
    return next((g for g in review_gates(goal) if g["id"] == review_id), None)


def plan_digest(state: dict) -> str:
    from .util import digest
    return digest({"tasks": [(t["id"], t["title"], t.get("detail", ""), t["depends_on"], t.get("covers", []),
                              [c["id"] for c in t.get("checks", [])], t.get("role"), t.get("team", []),
                              t["status"] == "dropped") for t in state["tasks"]]})


def requirements_digest(state: dict) -> str:
    from .util import digest
    return digest([(r["id"], r["text"], r["verify"]) for r in state.get("requirements", [])])


def subject_digest(goal: dict, state: dict, gate_def: dict) -> str:
    from .util import digest
    parts = {}
    for sid in gate_def["subject"]:
        if sid in skipped(goal):
            continue
        kind = STAGE_BY_ID[sid]["type"]
        if kind == "artifact":
            parts[sid] = (state.get("stages", {}).get(sid) or {}).get("digest")
        elif kind == "requirements":
            parts[sid] = requirements_digest(state)
        elif kind == "plan":
            parts[sid] = plan_digest(state)
    return digest(parts)


def stage_complete(goal: dict, state: dict, stage_def: dict) -> bool:
    kind, record = stage_def["type"], state.get("stages", {}).get(stage_def["id"]) or {}
    if kind == "artifact":
        return bool(record.get("done"))
    if kind == "requirements":
        return bool(state.get("requirements_final"))
    if kind == "plan":
        return bool(state.get("plan_final"))
    return record.get("verdict") == "pass" and record.get("digest") == subject_digest(goal, state, stage_def)


def reset_stages(goal: dict, state: dict, first: str, gate_id: str) -> list[str]:
    ids = [s["id"] for s in pre_stages(goal)]
    if first not in ids:
        return []
    end = ids.index(gate_id) if gate_id in ids else len(ids)
    reset = ids[ids.index(first):end]
    for sid in reset:
        kind = STAGE_BY_ID[sid]["type"]
        if kind == "artifact":
            state.setdefault("stages", {}).setdefault(sid, {})["done"] = False
        elif kind == "requirements":
            state["requirements_final"] = False
        elif kind == "plan":
            state["plan_final"] = False
    return reset


def failed_gate_for(goal: dict, state: dict, stage_id: str) -> str | None:
    """The gate whose failure sent this stage back (its findings belong in the assignment)."""
    for gate_id in GATE_STAGE_IDS:
        record = state.get("stages", {}).get(gate_id) or {}
        if record.get("verdict") == "fail" and stage_id in record.get("reset", []):
            return gate_id
    return None


def uncovered(state: dict) -> list[str]:
    covered = {c for t in state["tasks"] if t["status"] != "dropped" for c in t.get("covers", [])}
    return [r["id"] for r in state.get("requirements", []) if r["id"] not in covered]


def stage(goal: dict, state: dict, act: dict) -> str:
    """The pipeline stage id an action belongs to (status displays, profiles)."""
    if state["status"] == "done":
        return "done"
    kind, review = act["kind"], act.get("review")
    if kind in STAGE_BY_ID:
        return kind
    if kind == "review":
        if review in GATE_STAGE_IDS:
            return review
        if review in {FINAL_GATE["id"], PRODUCT_GATE["id"]}:
            return review
        return "reviews"
    return {"work": "develop", "verify": "test", "repair": "reviews" if review else "test"}.get(kind, kind)


def profile_for(goal: dict, state: dict, act: dict, project_root=None) -> dict:
    """Which role, adapter, model and effort run this turn.

    Role: the task's role (develop/repair) > the review's role > the workflow's stage role.
    Profile: review gate's own adapter/model > agent.roles[role] > agent.stages[stage] > goal default.
    """
    agent = goal["agent"]
    chosen = {"adapter": agent["adapter"], "model": agent.get("model"), "effort": agent.get("effort"),
              "stage": None, "role": None}
    kind, review = act["kind"], act.get("review")
    stage_key = {"requirements": "requirements", "plan": "plan", "work": "develop", "remediate": "remediate",
                 "report": "report", "planner": "planner", "repair": "repair", "intake": "intake",
                 "solution": "solution", "test_design": "test_design", "resources": "resources"}.get(kind)
    if kind == "review":
        stage_key = (review if review in GATE_STAGE_IDS else
                     "final_acceptance" if review == FINAL_GATE["id"] else
                     "product_acceptance" if review == PRODUCT_GATE["id"] else "review")
    chosen["stage"] = stage_key
    layers = [(agent.get("stages") or {}).get(stage_key) or {}]
    role = None
    if kind in {"work", "repair"} and act.get("task"):
        role = next((t for t in state["tasks"] if t["id"] == act["task"]), {}).get("role")
    gate = (find_gate(goal, review) or {}) if kind == "review" and review else {}
    role = role or gate.get("role")
    if kind == "review" and not role and review and "security" in review:
        role = "security"  # a review named after security gets the security specialist by default
    if not role:
        from .roles import stage_role
        role = stage_role(project_root, goal, stage_key)
    if role:
        chosen["role"] = role
        layers.append((agent.get("roles") or {}).get(role) or {})
    if gate:
        layers.append({k: gate[k] for k in ("adapter", "model") if gate.get(k)})
    chosen["skill"] = None
    for layer in layers:
        chosen.update({k: v for k, v in layer.items() if v})
    return chosen


def next_action(goal: dict, state: dict, fingerprint: str | None = None) -> dict:
    if state.get("integrity", "ok") != "ok":
        return action("repair_state", "human", state["integrity"] + ". Inspect, then run `loop repair`.")
    if state["approved_digest"] is None:
        problems = model.approval_problems(goal)
        return action("approve_goal", "human", "Goal is a draft. " + (" ".join(problems) if problems else
                      "Review goal.json and approve it (`loop approve` or loop_goal_approve after the user confirms)."),
                      problems=problems)
    if state["approved_digest"] != model.contract_digest(goal):
        return action("approve_goal", "human", "goal.json changed after approval; review and approve the new contract.")
    if state["status"] in model.TERMINAL:
        return action("finished", "none", state["status_reason"])
    if state.get("control") == "pause" or state["status"] == "paused":
        return action("paused", "none", "Paused by an operator. Resume with `loop resume`.")
    questions = open_questions(state)
    if questions:
        return action("answer", "human", f"{len(questions)} blocking question(s) need an answer.",
                      questions=[q["id"] for q in questions])
    if state["blocker"]:
        b = state["blocker"]
        return action("unblock", "human", f"Blocked ({b['category']}): {b['reason']}")
    if state["status"] == "limit":
        return action("limit", "human", state["status_reason"])
    running = [j for j in state.get("jobs", {}).values() if j["status"] == "running"]
    if running:
        # A long controller check is in flight: wait for it instead of spending agent
        # turns polling it (v2 lesson: polling turns looked like stagnation).
        job = sorted(running, key=lambda j: j["started_at"])[0]
        return action("wait_job", "controller", f"Waiting for check job {job['id']} "
                      f"({', '.join(job['checks'])}), started {job['started_at']}.", job=job["id"])

    if goal["kind"] == "operate":
        incident = open_incident(state)
        if incident:
            if incident.get("needs_recheck"):
                return action("health_check", "controller", f"Re-check health after remediation of {incident['id']}.")
            return action("remediate", "agent", f"Health check failed ({', '.join(incident['failing'])}). "
                          "Diagnose and remediate within policy.", incident=incident["id"])
    if pipeline(goal):
        for stage_def in pre_stages(goal):
            if stage_complete(goal, state, stage_def):
                continue
            sid, failed = stage_def["id"], failed_gate_for(goal, state, stage_def["id"])
            extra = {"review": failed} if failed else {}
            summary = (f"{stage_def['summary']} (revise after {failed} findings)" if failed else stage_def["summary"])
            if stage_def["type"] == "artifact":
                return action(sid, "agent", summary, output=artifact_path(goal, stage_def), revise_after=failed)
            if stage_def["type"] in {"requirements", "plan"}:
                return action(sid, "agent", summary, revise_after=failed, **extra)
            return action("review", "agent", stage_def["summary"], review=sid)
    active = [t for t in state["tasks"] if t["status"] == "active"]
    if active:
        return action("work", "agent", f"Continue task {active[0]['id']}: {active[0]['title']}", task=active[0]["id"])
    ready = ready_tasks(state)
    if ready:
        return action("work", "agent", f"Start task {ready[0]['id']}: {ready[0]['title']}", task=ready[0]["id"])
    pending = [t for t in state["tasks"] if t["status"] in {"pending", "blocked"}]
    if pending:
        return action("unblock", "human", "Remaining tasks wait on blocked or missing dependencies: " +
                      ", ".join(f"{t['id']}<-{t['depends_on']}" for t in pending))

    if goal["kind"] == "operate":
        due = parse_time(state.get("next_cycle_at"))
        if due is None or due <= datetime.now(timezone.utc).timestamp():
            return action("health_check", "controller", "Run the scheduled health checks.")
        return action("idle", "none", f"Healthy. Next health check at {state['next_cycle_at']}.",
                      until=state["next_cycle_at"])

    if not state["tasks"]:
        if goal["kind"] == "investigate":
            return action("plan", "agent", "Read the context, state hypotheses and plan the investigation as tasks.")
        return action("plan", "agent", "Read the context and break the goal into verifiable tasks.")
    if goal["kind"] == "investigate" and not state.get("report"):
        return action("report", "agent", "All investigation tasks are finished: write the root-cause report "
                      "and call loop_finish.")
    verification = state.get("verification")
    if goal["acceptance"]:
        if not verification or (fingerprint and verification["fingerprint"] != fingerprint):
            return action("verify", "controller", "All tasks are finished; the controller runs the acceptance checks.")
        if not verification["passed"]:
            return action("repair", "agent", "Final acceptance failed: " + ", ".join(verification["failing"]) +
                          ". Fix the causes; the controller re-verifies after your turn.")
    reviewed_at = (verification or {}).get("fingerprint") or fingerprint
    for gate in review_gates(goal):
        result = state["reviews"].get(gate["id"])
        current = result and (not fingerprint or result["fingerprint"] == fingerprint)
        if current and result["verdict"] == "pass":
            continue
        if current and result["verdict"] == "fail":
            return action("repair", "agent", f"Review {gate['id']} failed. Address its findings; the controller "
                          "re-verifies and re-reviews after your turn.", review=gate["id"])
        if gate["by"] == "agent":
            return action("review", "agent", f"Independent review {gate['id']} of {reviewed_at}.", review=gate["id"])
        return action("verify", "controller", f"Human review {gate['id']} needs a fresh request.")
    return action("finished", "none", state["status_reason"])


# --------------------------------------------------------------------- operations
# Each operation takes (goal, state) inside GoalStore.mutate and edits state in place.


def gates(goal: dict, state: dict, fingerprint: str | None, passed_reason: str) -> None:
    """After acceptance passes: finish, or request the reviews that are still missing."""
    pending = [g for g in review_gates(goal) if not (
        (r := state["reviews"].get(g["id"])) and r["verdict"] == "pass" and r["fingerprint"] == fingerprint)]
    if not pending:
        count = len(review_gates(goal))
        state["status"], state["status_reason"] = "done", passed_reason + (
            f" {count} review(s) passed." if count else "")
        return
    agent_pending = any(g["by"] == "agent" for g in pending)
    for gate in pending:
        if gate["by"] != "human" or agent_pending:  # people review only after agent reviews pass
            continue
        if any(q.get("review") == gate["id"] and q["answer"] is None for q in state["questions"]):
            continue
        state["questions"].append({
            "id": new_id("q"), "kind": "approval", "review": gate["id"], "fingerprint": fingerprint,
            "text": f"Human review '{gate['id']}': {gate['instructions']}\nApprove if it passes; otherwise "
                    "answer with what failed.", "options": ["approve", "<what failed>"], "blocking": True,
            "asked_at": now(), "by": "controller", "action": None, "answer": None, "answered_at": None,
            "answered_by": None})
    names = ", ".join(g["id"] for g in pending)
    state["status"] = "running" if agent_pending else "waiting"
    state["status_reason"] = f"{passed_reason} Waiting for review(s): {names}."


def record_review(goal: dict, state: dict, review_id: str, verdict: str, findings: str,
                  fingerprint: str | None, by: str, back_to: str | None = None) -> dict:
    gate = find_gate(goal, review_id)
    if not gate:
        raise LoopError(f"Unknown review {review_id}")
    if verdict not in {"pass", "fail"}:
        raise LoopError("verdict must be pass or fail")
    findings = text(findings, "findings", 20000, required=verdict == "fail")
    if review_id in GATE_STAGE_IDS and pipeline(goal):
        stage_def = STAGE_BY_ID[review_id]
        ids = [x["id"] for x in pre_stages(goal)]
        target = back_to if back_to in ids and ids.index(back_to) < ids.index(review_id) else stage_def["back_to"]
        previous = state.setdefault("stages", {}).get(review_id) or {}
        record = {"verdict": verdict, "findings": findings, "digest": subject_digest(goal, state, stage_def),
                  "at": now(), "by": by, "rounds": previous.get("rounds", 0), "reset": []}
        state["stages"][review_id] = record
        if verdict == "fail":
            record["rounds"] += 1
            record["back_to"] = target
            record["reset"] = reset_stages(goal, state, target, review_id)
            if record["rounds"] > goal["policy"]["max_repair_rounds"]:
                state["blocker"] = {"category": "scope", "reason": f"{review_id} failed {record['rounds']} times; "
                                    "the goal or the documents may need a human decision.", "evidence": findings[:6000],
                                    "attempted": "Revisions", "at": now(), "by": "controller"}
                state["status"], state["status_reason"] = "blocked", state["blocker"]["reason"]
            else:
                state["status_reason"] = f"{review_id} failed; back to {target}."
        else:
            state["status_reason"] = f"{review_id} passed."
        return {"_event": {"review": review_id, "verdict": verdict, "by": by, "back_to": record.get("back_to")}}
    state["reviews"][review_id] = {"verdict": verdict, "findings": findings, "fingerprint": fingerprint,
                                   "at": now(), "by": by}
    if verdict == "fail":
        state["repair_rounds"] = state.get("repair_rounds", 0) + 1
        if state["repair_rounds"] > goal["policy"]["max_repair_rounds"]:
            state["blocker"] = {"category": "other", "reason": f"Review {review_id} still fails after "
                                f"{state['repair_rounds'] - 1} repair rounds", "evidence": findings[:6000],
                                "attempted": "Repair turns", "at": now(), "by": "controller"}
            state["status"], state["status_reason"] = "blocked", state["blocker"]["reason"]
        else:
            state["status"], state["status_reason"] = "running", f"Review {review_id} failed; repair needed."
    else:
        verification = state.get("verification") or {}
        if not goal["acceptance"] or (verification.get("passed") and verification.get("fingerprint") == fingerprint):
            gates(goal, state, fingerprint, f"Review {review_id} passed.")
    return {"_event": {"review": review_id, "verdict": verdict, "by": by}}


def approve(goal: dict, state: dict, by: str) -> dict:
    problems = model.approval_problems(goal)
    if problems:
        raise LoopError("Cannot approve yet: " + " ".join(problems))
    state["approved_digest"] = model.contract_digest(goal)
    state["approved_by"], state["approved_at"] = by, now()
    if state["status"] in {"draft", "ready"} or state["status"] not in model.TERMINAL:
        state["status"], state["status_reason"] = "ready", f"Approved by {by}. Start with `loop start`."
    return {"_event": {"by": by, "digest": state["approved_digest"]}}


def complete_artifact(goal: dict, state: dict, stage_id: str, path: str, file_hash: str, summary: str) -> dict:
    stage_def = STAGE_BY_ID.get(stage_id)
    if not pipeline(goal) or not stage_def or stage_def["type"] != "artifact" or stage_id in skipped(goal):
        raise LoopError(f"{stage_id} is not a document stage of this goal's pipeline")
    state.setdefault("stages", {})[stage_id] = {"done": True, "path": path, "digest": file_hash, "at": now(),
                                                "summary": text(summary, "summary", 4000, required=False)}
    _resume_status(state, f"{stage_id} written: {path}.")
    return {"_event": {"stage": stage_id, "path": path}}


def set_requirements(goal: dict, state: dict, items: list[dict] | None, drop: list[str] | None,
                     final: bool) -> dict:
    items, drop = items or [], drop or []
    if not isinstance(items, list) or not isinstance(drop, list):
        raise LoopError("requirements and drop must be lists")
    current = {r["id"]: r for r in state.setdefault("requirements", [])}
    for value in items:
        requirement = model.requirement(value)
        current[requirement["id"]] = requirement
    for rid in drop:
        current.pop(rid, None)
    if len(current) > 500:
        raise LoopError("Requirements are limited to 500 per goal; split the goal")
    changed = bool(items or drop)
    state["requirements"] = list(current.values())
    if changed and state.get("plan_final"):
        state["plan_final"] = False  # requirements changed: plan and plan review must be redone
        state["status_reason"] = "Requirements changed; the plan needs to be finalized and reviewed again."
    if final:
        if not state["requirements"]:
            raise LoopError("Record at least one requirement before finalizing")
        state["requirements_final"] = True
        state["status_reason"] = f"{len(state['requirements'])} requirements recorded; planning next."
    elif changed:
        state["requirements_final"] = False
    return {"_event": {"requirements": len(state["requirements"]), "final": final}}


def plan(goal: dict, state: dict, upsert: list[dict] | None, drop: list[dict] | None, final: bool = False) -> dict:
    upsert, drop = upsert or [], drop or []
    if not upsert and not drop and not final:
        raise LoopError("Give tasks to add/update, or tasks to drop")
    by_id = {t["id"]: t for t in state["tasks"]}
    changed = []
    for values in upsert:
        task_id = slug(values.get("id", ""), "task id") if isinstance(values, dict) else None
        existing = by_id.get(task_id)
        if existing and existing["status"] in {"done", "dropped"} and set(values) - {"id", "detail"}:
            raise LoopError(f"Task {task_id} is {existing['status']}; add a new task instead of rewriting history")
        updated = model.task(values, existing)
        if existing:
            state["tasks"][state["tasks"].index(existing)] = updated
        else:
            state["tasks"].append(updated)
        by_id[updated["id"]] = updated
        changed.append(updated["id"])
    for item in drop:
        if not isinstance(item, dict):
            raise LoopError("drop entries are {id, reason}")
        target = task_by_id(state, item.get("id", ""))
        if target["status"] == "done":
            raise LoopError(f"Task {target['id']} is already done")
        target.update(status="dropped", summary=text(item.get("reason"), "drop reason", 2000), updated_at=now())
        changed.append(target["id"])
    ids = {t["id"] for t in state["tasks"]}
    for task in state["tasks"]:
        missing = set(task["depends_on"]) - ids
        if missing:
            raise LoopError(f"Task {task['id']} depends on unknown tasks {sorted(missing)}")
    _assert_acyclic(state["tasks"])
    limit = goal["agent"].get("team_limit", 4)
    for task in state["tasks"]:
        seats = sum(s["count"] for s in task.get("team", []))
        if seats > limit:
            raise LoopError(f"Task {task['id']} asks for {seats} specialists; the limit is {limit} "
                            "(agent.team_limit). Split the task or reduce the team.")
    if len(state["tasks"]) > 200:
        raise LoopError("Plans are limited to 200 tasks; group work into larger tasks")
    if pipeline(goal):
        known = {r["id"] for r in state.get("requirements", [])}
        for task in state["tasks"]:
            unknown = set(task.get("covers", [])) - known
            if unknown:
                raise LoopError(f"Task {task['id']} covers unknown requirements {sorted(unknown)}")
        if changed:
            state["plan_final"] = False
        if final:
            missing = uncovered(state)
            if missing:
                raise LoopError(f"Plan cannot be final: requirements not covered by any task: {missing}")
            unchecked = [t["id"] for t in state["tasks"] if t["status"] not in {"done", "dropped"} and not t["checks"]]
            if unchecked:
                raise LoopError(f"Plan cannot be final: tasks without checks: {unchecked}")
            state["plan_final"] = True
    _resume_status(state, "Plan finalized; waiting for plan review." if final else "Plan updated.")
    return {"_event": {"tasks": changed, "final": final}}


def _assert_acyclic(tasks: list[dict]) -> None:
    graph = {t["id"]: t["depends_on"] for t in tasks}
    seen, stack = set(), set()

    def visit(node):
        if node in stack:
            raise LoopError(f"Task dependencies form a cycle through {node}")
        if node in seen:
            return
        stack.add(node)
        for dep in graph.get(node, []):
            visit(dep)
        stack.discard(node)
        seen.add(node)
    for node in graph:
        visit(node)


def start_task(goal: dict, state: dict, task_id: str) -> dict:
    task = task_by_id(state, task_id)
    if task["status"] == "active":
        return {"_event": {"task": task_id, "already": True}}
    if task not in ready_tasks(state):
        raise LoopError(f"Task {task_id} is {task['status']} or waits on dependencies {task['depends_on']}")
    task.update(status="active", started_at=task.get("started_at") or now(), updated_at=now())
    _resume_status(state, f"Working on {task_id}.")
    return {"_event": {"task": task_id}}


def claim_done(goal: dict, state: dict, task_id: str, summary: str) -> dict:
    """Agent says a task is done. Tasks without checks finish now; others need a check job."""
    task = task_by_id(state, task_id)
    if task["status"] not in {"active", "pending"}:
        raise LoopError(f"Task {task_id} is {task['status']}")
    if task["status"] == "pending" and task not in ready_tasks(state):
        raise LoopError(f"Task {task_id} waits on dependencies {task['depends_on']}")
    task["status"] = "active"
    task["claim"] = {"summary": text(summary, "summary", 8000), "at": now()}
    if not task.get("checks"):
        _finish_task(task, "Done (no task checks; final acceptance still applies).")
        return {"_event": {"task": task_id, "verified": False}, "needs_checks": False}
    return {"_event": {"task": task_id}, "needs_checks": True,
            "checks": [f"{task_id}.{c['id']}" for c in task["checks"]]}


def _finish_task(task: dict, summary: str) -> None:
    claim = task.get("claim", {}).get("summary")
    task.update(status="done", summary=claim or summary, finished_at=now(), updated_at=now())


def apply_task_checks(goal: dict, state: dict, task_id: str, results: dict) -> dict:
    task = task_by_id(state, task_id)
    failing = [k for k, v in results.items() if v["status"] != "pass"]
    if not failing:
        _finish_task(task, "Task checks passed.")
        _resume_status(state, f"Task {task_id} verified.")
        return {"_event": {"task": task_id, "passed": True}}
    task["attempts"] = task.get("attempts", 0) + 1
    task["last_failure"] = {"checks": failing, "at": now(),
                            "tail": tail("\n\n".join(f"## {k}\n{results[k]['tail']}" for k in failing), 6000)}
    task["updated_at"] = now()
    if task["attempts"] >= goal["policy"]["max_task_attempts"]:
        task["status"] = "blocked"
        state["blocker"] = {"category": "other", "reason": f"Task {task_id} failed its checks "
                            f"{task['attempts']} times ({', '.join(failing)}).", "evidence": task["last_failure"]["tail"],
                            "attempted": "Repeated repair turns", "at": now(), "by": "controller"}
        state["status"], state["status_reason"] = "blocked", state["blocker"]["reason"]
    else:
        state["status_reason"] = f"Task {task_id} checks failed: {', '.join(failing)}."
    return {"_event": {"task": task_id, "passed": False, "failing": failing}}


def apply_verification(goal: dict, state: dict, results: dict, fingerprint: str) -> dict:
    failing = sorted(k for k, v in results.items() if v["status"] != "pass")
    state["verification"] = {"at": now(), "fingerprint": fingerprint, "passed": not failing, "failing": failing,
                             "results": {k: v["status"] for k, v in results.items()}}
    if not failing:
        gates(goal, state, fingerprint, f"All {len(results)} acceptance checks passed on {fingerprint}.")
        return {"_event": {"passed": True}}
    state["repair_rounds"] = state.get("repair_rounds", 0) + 1
    if state["repair_rounds"] > goal["policy"]["max_repair_rounds"]:
        state["blocker"] = {"category": "other", "reason": f"Acceptance still fails after {state['repair_rounds'] - 1} "
                            f"repair rounds: {', '.join(failing)}", "evidence": _failure_tail(results, failing),
                            "attempted": "Repair turns", "at": now(), "by": "controller"}
        state["status"], state["status_reason"] = "blocked", state["blocker"]["reason"]
    else:
        state["status"] = "running"
        state["status_reason"] = f"Acceptance failed ({', '.join(failing)}); repair round {state['repair_rounds']}."
    return {"_event": {"passed": False, "failing": failing}}


def _failure_tail(results: dict, failing: list[str]) -> str:
    return tail("\n\n".join(f"## {k} ({results[k]['status']}, exit {results[k]['exit_code']})\n{results[k]['tail']}"
                            for k in failing), 6000)


def apply_health(goal: dict, state: dict, results: dict) -> dict:
    failing = sorted(k for k, v in results.items() if v["status"] != "pass")
    interval = goal["schedule"]["interval_minutes"]
    cycle = {"at": now(), "passed": not failing, "failing": failing}
    state.setdefault("cycles", []).append(cycle)
    state["cycles"] = state["cycles"][-50:]
    incident = open_incident(state)
    if not failing:
        if incident:
            incident.update(status="resolved", resolved_at=now(), needs_recheck=False)
        state["next_cycle_at"] = (datetime.now(timezone.utc) + timedelta(minutes=interval)).isoformat(timespec="seconds")
        state["status"], state["status_reason"] = "idle", f"Healthy. Next check {state['next_cycle_at']}."
        return {"_event": {"healthy": True, "resolved": incident["id"] if incident else None}}
    if incident:
        incident.update(failing=failing, needs_recheck=False, evidence=_failure_tail(results, failing),
                        rounds=incident.get("rounds", 0) + 1)
        if incident["rounds"] > goal["policy"]["max_repair_rounds"]:
            state["blocker"] = {"category": "other", "reason": f"Incident {incident['id']} still failing after "
                                f"{incident['rounds'] - 1} remediation rounds", "evidence": incident["evidence"],
                                "attempted": "Remediation turns", "at": now(), "by": "controller"}
            state["status"], state["status_reason"] = "blocked", state["blocker"]["reason"]
            return {"_event": {"healthy": False, "incident": incident["id"], "escalated": True}}
    else:
        incident = {"id": new_id("inc"), "opened_at": now(), "status": "open", "failing": failing,
                    "evidence": _failure_tail(results, failing), "rounds": 0, "turns_used": 0, "needs_recheck": False}
        state["incidents"].append(incident)
        state["incidents"] = state["incidents"][-50:]
    state["status"], state["status_reason"] = "running", f"Incident {incident['id']}: {', '.join(failing)} failing."
    return {"_event": {"healthy": False, "incident": incident["id"]}}


def note(goal: dict, state: dict, kind: str, body: str, task_id: str | None = None,
         finding_id: str | None = None, finding_status: str | None = None, by: str = "agent") -> dict:
    if kind not in NOTE_KINDS:
        raise LoopError(f"note kind must be one of {NOTE_KINDS}")
    body = text(body, "note", 8000)
    entry = {"at": now(), "kind": kind, "text": body, "by": by}
    if task_id:
        task = task_by_id(state, task_id)
        task["notes"] = (task["notes"] + [entry])[-20:]
        entry["task"] = task_id
    if kind in FINDING_KINDS:
        if finding_id:
            finding = next((f for f in state["findings"] if f["id"] == finding_id), None)
            if not finding:
                raise LoopError(f"Unknown finding {finding_id}")
            finding["updates"] = (finding.get("updates", []) + [entry])[-20:]
            if finding_status:
                finding["status"] = _finding_status(finding_status)
        else:
            finding = {"id": new_id("f"), "kind": kind, "text": body, "at": now(),
                       "status": _finding_status(finding_status or ("open" if kind == "hypothesis" else "recorded"))}
            state["findings"].append(finding)
            state["findings"] = state["findings"][-200:]
            entry["finding"] = finding["id"]
    state["notes"] = (state["notes"] + [entry])[-60:]
    return {"_event": {"kind": kind, "task": task_id, "finding": entry.get("finding") or finding_id},
            "finding": entry.get("finding") or finding_id}


def _finding_status(value: str) -> str:
    allowed = {"open", "confirmed", "rejected", "recorded"}
    if value not in allowed:
        raise LoopError(f"finding status must be one of {sorted(allowed)}")
    return value


def ask(goal: dict, state: dict, question: str, options: list[str] | None, blocking: bool,
        kind: str = "question", action_text: str | None = None, by: str = "agent") -> dict:
    if kind not in {"question", "approval"}:
        raise LoopError("question kind must be question or approval")
    if options is not None and (not isinstance(options, list) or not all(isinstance(o, str) for o in options)):
        raise LoopError("options must be a list of strings")
    if len(open_questions(state, blocking_only=False)) >= 20:
        raise LoopError("20 questions are already open; wait for answers")
    entry = {"id": new_id("q"), "kind": kind, "text": text(question, "question", 4000),
             "options": options or (["approve", "deny"] if kind == "approval" else []),
             "blocking": bool(blocking) or kind == "approval", "asked_at": now(), "by": by,
             "action": text(action_text, "action", 4000, required=False) or None,
             "answer": None, "answered_at": None, "answered_by": None}
    state["questions"].append(entry)
    if entry["blocking"]:
        state["status"], state["status_reason"] = "waiting", f"Waiting for an answer to {entry['id']}: {entry['text'][:120]}"
    return {"_event": {"question": entry["id"], "kind": kind, "blocking": entry["blocking"]}, "question": entry}


def answer(goal: dict, state: dict, question_id: str, reply: str, by: str) -> dict:
    question = next((q for q in state["questions"] if q["id"] == question_id), None)
    if not question:
        raise LoopError(f"Unknown question {question_id}")
    if question["answer"] is not None:
        raise LoopError(f"Question {question_id} was already answered: {question['answer']}")
    question.update(answer=text(reply, "answer", 8000), answered_at=now(), answered_by=by)
    if question["kind"] == "approval":
        question["approved"] = reply.strip().lower() in {"approve", "approved", "yes", "y", "ok", "pass",
                                                         "同意", "批准", "是", "通过"}
    if question.get("review"):
        record_review(goal, state, question["review"], "pass" if question["approved"] else "fail",
                      "" if question["approved"] else reply, question.get("fingerprint"), by)
        return {"_event": {"question": question_id, "by": by, "review": question["review"]}}
    if not open_questions(state):
        _resume_status(state, f"Answered {question_id}.")
    return {"_event": {"question": question_id, "by": by}}


def block(goal: dict, state: dict, category: str, reason: str, evidence: str, attempted: str,
          by: str = "agent") -> dict:
    if category not in model.BLOCK_CATEGORIES:
        raise LoopError(f"category must be one of {model.BLOCK_CATEGORIES}")
    reason = text(reason, "reason", 4000)
    if category in {"permission", "environment", "external"}:
        # v1 lesson: an unverified "permission" blocker stopped two projects for a
        # day. Demand the exact reproduction and what was tried first.
        evidence = text(evidence, "evidence (exact command, output and error)", 12000)
        attempted = text(attempted, "attempted (workarounds and diagnosis already tried)", 8000)
    else:
        evidence = text(evidence, "evidence", 12000, required=False)
        attempted = text(attempted, "attempted", 8000, required=False)
    state["blocker"] = {"category": category, "reason": reason, "evidence": evidence, "attempted": attempted,
                        "at": now(), "by": by}
    state["status"], state["status_reason"] = "blocked", f"Blocked ({category}): {reason}"
    return {"_event": {"category": category}}


def unblock(goal: dict, state: dict, note_text: str, by: str) -> dict:
    if not state["blocker"] and state["status"] not in {"blocked", "limit", "stopped", "failed"}:
        raise LoopError("Goal is not blocked")
    previous = state["blocker"]
    state["blocker"] = None
    for task in state["tasks"]:
        if task["status"] == "blocked":
            task.update(status="active", attempts=0)
    if state["status"] == "limit":
        state["iteration_base"] = state["iteration"]
        state["started_at"] = now()
    state["repair_rounds"] = 0
    state["stagnant_turns"] = 0
    state["notes"].append({"at": now(), "kind": "decision", "by": by,
                           "text": "Unblocked: " + text(note_text, "note", 4000)})
    state["status"], state["status_reason"] = "ready", f"Unblocked by {by}: {note_text[:160]}"
    return {"_event": {"previous": previous["category"] if previous else None, "by": by}}


def finish(goal: dict, state: dict, report: str, fingerprint: str | None = None) -> dict:
    unfinished = [t["id"] for t in state["tasks"] if t["status"] in {"pending", "active", "blocked"}]
    if unfinished:
        raise LoopError(f"Finish or drop these tasks first: {unfinished}")
    if goal["kind"] == "operate":
        raise LoopError("Operate goals do not finish; stop them with `loop stop`")
    state["report"] = {"text": text(report, "report", 40000), "at": now()}
    if not goal["acceptance"]:
        gates(goal, state, fingerprint, "Report recorded." + ("" if goal["reviews"] else
              " No acceptance checks or reviews were defined, so a human should read the report."))
        return {"_event": {"verify": False}, "verify": False}
    state["status_reason"] = "Report recorded; the controller is verifying acceptance."
    return {"_event": {"verify": True}, "verify": True}


def control(goal: dict, state: dict, command: str, by: str, runner_live: bool = False) -> dict:
    if command == "pause":
        if state["status"] in model.TERMINAL:
            raise LoopError(f"Goal is {state['status']}")
        state["control"] = "pause"
        state["status_reason"] = f"Pause requested by {by}; the runner stops after the current step."
        if not runner_live:
            state["status"] = "paused"
    elif command == "resume":
        state["control"] = None
        if state["status"] in {"paused", "stopped", "failed", "limit"} and not state["blocker"]:
            if state["status"] == "limit":
                state["iteration_base"] = state["iteration"]
                state["started_at"] = now()
            state["status"], state["status_reason"] = "ready", f"Resumed by {by}."
    elif command == "stop":
        state["control"] = "stop"
        state["status"], state["status_reason"] = "stopped", f"Stopped by {by}."
    else:
        raise LoopError("control must be pause, resume or stop")
    return {"_event": {"command": command, "by": by}}


def _resume_status(state: dict, reason: str) -> None:
    if open_questions(state) or state["blocker"]:
        return
    if state["status"] == "waiting":
        state["status"] = "ready"
    if state["status"] in {"ready", "running", "idle"}:
        state["status_reason"] = reason


def budget_problem(goal: dict, state: dict) -> str | None:
    p, used = goal["policy"], state["iteration"] - state.get("iteration_base", 0)
    if goal["kind"] == "operate":
        incident = open_incident(state)
        if incident and incident.get("turns_used", 0) >= p["max_iterations"]:
            return f"Incident {incident['id']} used its {p['max_iterations']} agent turns"
        return None
    if used >= p["max_iterations"]:
        return f"Used all {p['max_iterations']} agent turns"
    started = parse_time(state.get("started_at"))
    if started and (datetime.now(timezone.utc).timestamp() - started) / 3600 >= p["max_hours"]:
        return f"Reached the {p['max_hours']}h wall-clock limit"
    if p["max_cost_usd"] is not None and state["usage"]["cost_usd"] >= p["max_cost_usd"]:
        return f"Reported cost ${state['usage']['cost_usd']:.2f} reached the ${p['max_cost_usd']:.2f} limit"
    return None


def check_scope_for_task(state: dict, task_id: str) -> list[str]:
    task = task_by_id(state, task_id)
    return [f"{task_id}.{c['id']}" for c in task.get("checks", [])]


def verify_now(store, actor: str = "controller", cancelled=lambda: False) -> dict:
    """Run acceptance synchronously and apply the result (runner, CLI, tests)."""
    goal = store.goal()
    qualified = checks_mod.select(goal, store.state(), "acceptance", None)
    results = checks_mod.execute(store, qualified, actor=actor, cancelled=cancelled)
    stamp = next(iter(results.values()))["fingerprint"] if results else checks_mod.fingerprint(store.project.root)
    if goal["kind"] == "operate":
        return store.mutate("goal.health", lambda g, s: apply_health(g, s, results))
    return store.mutate("goal.verified", lambda g, s: apply_verification(g, s, results, stamp))
