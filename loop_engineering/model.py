"""Goal and state schemas, validated by hand so the core needs only the stdlib.

Ownership split (the main v1 lesson):
- ``goal.json`` is the human contract: objective, acceptance checks, policy.
  Agents may draft it during intake, but only an explicit approval freezes it;
  any later edit requires re-approval.
- ``state.json`` is controller-owned runtime state, including the agent's plan.
  Agents change it only through controller operations, never by editing files.
"""

from __future__ import annotations

from copy import deepcopy

from .util import LoopError, digest, new_id, now, slug, text

KINDS = ("develop", "operate", "investigate")

GOAL_STATUSES = ("draft", "ready", "running", "waiting", "blocked", "paused", "idle",
                 "done", "failed", "limit", "stopped")
TERMINAL = {"done", "failed", "stopped"}
TASK_STATUSES = ("pending", "active", "done", "blocked", "dropped")
BLOCK_CATEGORIES = ("decision", "permission", "external", "environment", "scope", "other")

TEST_STAGES = ("unit", "integration", "e2e", "performance", "security", "regression", "acceptance")
PIPELINES = (None, "full")
SKIPPABLE_STAGES = ("intake", "requirements_review", "solution", "test_design", "resources", "plan_review",
                    "code-review", "security-review", "performance-review", "final-acceptance", "product-acceptance")

DEFAULT_APPROVALS = [
    "deploy or release to a shared or production environment",
    "delete or overwrite data that is not reproducible from the repository",
    "spend money or start paid resources",
    "send messages, emails or posts to people",
    "change credentials, secrets, access control or security settings",
    "force-push, rewrite published git history or delete branches",
]

DEFAULT_POLICY = {
    "max_iterations": 40,          # agent turns for this goal
    "max_hours": 12.0,             # wall clock from first run
    "turn_timeout_minutes": 60,
    "stagnation_limit": 3,         # consecutive turns without observable progress
    "max_task_attempts": 4,        # rejected "task done" claims before the task blocks
    "max_repair_rounds": 5,        # failed final acceptance before the goal blocks
    "max_cost_usd": None,          # enforced only where the adapter reports cost
    "approval_required": DEFAULT_APPROVALS,
    "allow_network": True,
    "fresh_session_each_turn": True,
    "pause_on_interrupt": False,
    "check_timeout_seconds": 900,
}

DEFAULT_AGENT = {"adapter": "codex", "model": None, "permission_mode": None, "extra_args": []}


def check(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise LoopError(f"{where}: a check must be an object")
    allowed = {"id", "run", "argv", "timeout", "cwd", "env", "description", "expect_exit", "stage"}
    unknown = set(value) - allowed
    if unknown:
        raise LoopError(f"{where}: unknown check fields {sorted(unknown)}")
    result = {"id": slug(value.get("id", ""), f"{where} id")}
    if ("run" in value) == ("argv" in value):
        raise LoopError(f"{where}: give exactly one of 'run' (shell text) or 'argv' (list)")
    if "run" in value:
        result["run"] = text(value["run"], f"{where} run", 4000)
    else:
        argv = value["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise LoopError(f"{where}: argv must be a non-empty list of strings")
        result["argv"] = list(argv)
    timeout = value.get("timeout", None)
    if timeout is not None and (type(timeout) is not int or not 1 <= timeout <= 86400):
        raise LoopError(f"{where}: timeout must be 1..86400 seconds")
    if timeout is not None:
        result["timeout"] = timeout
    cwd = value.get("cwd", ".")
    if not isinstance(cwd, str) or cwd.startswith("/") or ".." in cwd.split("/"):
        raise LoopError(f"{where}: cwd must be a relative path inside the project")
    result["cwd"] = cwd
    env = value.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise LoopError(f"{where}: env must map strings to strings")
    if env:
        result["env"] = env
    expect = value.get("expect_exit", 0)
    if type(expect) is not int:
        raise LoopError(f"{where}: expect_exit must be an integer")
    result["expect_exit"] = expect
    if value.get("description"):
        result["description"] = text(value["description"], f"{where} description", 2000)
    if value.get("stage") is not None:
        if value["stage"] not in TEST_STAGES:
            raise LoopError(f"{where}: stage must be one of {TEST_STAGES}")
        result["stage"] = value["stage"]
    return result


def review(value, where: str) -> dict:
    """An acceptance gate that a command cannot decide.

    by=agent: a separate read-only reviewer turn (fresh session, optionally another
    adapter/model) records a verdict. by=human: a person follows the procedure and
    approves. Both bind to the workspace fingerprint they reviewed.
    """
    if not isinstance(value, dict):
        raise LoopError(f"{where}: a review must be an object")
    unknown = set(value) - {"id", "by", "instructions", "adapter", "model", "timeout_minutes", "role"}
    if unknown:
        raise LoopError(f"{where}: unknown review fields {sorted(unknown)}")
    by = value.get("by", "agent")
    if by not in {"agent", "human"}:
        raise LoopError(f"{where}: by must be agent or human")
    result = {"id": slug(value.get("id", ""), f"{where} id"), "by": by,
              "instructions": text(value.get("instructions"), f"{where} instructions", 8000)}
    for key in ("adapter", "model", "role"):
        if value.get(key):
            result[key] = text(value[key], f"{where} {key}", 200)
    if value.get("timeout_minutes") is not None:
        minutes = value["timeout_minutes"]
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise LoopError(f"{where}: timeout_minutes must be 1..1440")
        result["timeout_minutes"] = minutes
    return result


def checks(values, where: str) -> list[dict]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise LoopError(f"{where} must be a list of checks")
    result = [check(v, f"{where}[{i}]") for i, v in enumerate(values)]
    ids = [c["id"] for c in result]
    if len(ids) != len(set(ids)):
        raise LoopError(f"{where}: check ids must be unique")
    return result


def policy(values) -> dict:
    values = values or {}
    if not isinstance(values, dict):
        raise LoopError("policy must be an object")
    unknown = set(values) - set(DEFAULT_POLICY)
    if unknown:
        raise LoopError(f"Unknown policy fields {sorted(unknown)}")
    result = deepcopy(DEFAULT_POLICY)
    result.update(values)
    for key in ("max_iterations", "turn_timeout_minutes", "stagnation_limit", "max_task_attempts",
                "max_repair_rounds", "check_timeout_seconds"):
        if type(result[key]) is not int or result[key] < 1:
            raise LoopError(f"policy.{key} must be a positive integer")
    if not isinstance(result["max_hours"], (int, float)) or result["max_hours"] <= 0:
        raise LoopError("policy.max_hours must be positive")
    if result["max_cost_usd"] is not None and (not isinstance(result["max_cost_usd"], (int, float))
                                               or result["max_cost_usd"] <= 0):
        raise LoopError("policy.max_cost_usd must be positive or null")
    if not isinstance(result["approval_required"], list) or not all(
            isinstance(x, str) for x in result["approval_required"]):
        raise LoopError("policy.approval_required must be a list of strings")
    for key in ("allow_network", "fresh_session_each_turn", "pause_on_interrupt"):
        if type(result[key]) is not bool:
            raise LoopError(f"policy.{key} must be true or false")
    return result


EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
STAGE_PROFILES = ("intake", "requirements", "requirements_review", "solution", "test_design", "plan", "resources",
                  "plan_review", "develop", "repair", "review", "final_acceptance", "product_acceptance",
                  "planner", "remediate", "report")


def profile(value, where: str) -> dict:
    if not isinstance(value, dict) or set(value) - {"adapter", "model", "effort", "skill"}:
        raise LoopError(f"{where}: a profile is {{adapter?, model?, effort?, skill?}}")
    result = {}
    for key in ("adapter", "model", "skill"):
        if value.get(key):
            result[key] = text(value[key], f"{where}.{key}", 200)
    if value.get("effort") is not None:
        if value["effort"] not in EFFORTS:
            raise LoopError(f"{where}.effort must be one of {EFFORTS}")
        result["effort"] = value["effort"]
    return result


def agent(values) -> dict:
    """Default agent plus optional per-stage / per-role profiles (adapter, model, effort).

    Example: {"adapter": "codex", "model": "gpt-5.1-codex-mini",
              "stages": {"requirements": {"effort": "high", "model": "gpt-5.1"}, "review": {"adapter": "claude"}},
              "roles": {"architect": {"model": "gpt-5.1", "effort": "xhigh"}}}
    """
    values = values or {}
    if not isinstance(values, dict):
        raise LoopError("agent must be an object")
    unknown = set(values) - set(DEFAULT_AGENT) - {"effort", "stages", "roles", "team_limit"}
    if unknown:
        raise LoopError(f"Unknown agent fields {sorted(unknown)}")
    result = {**DEFAULT_AGENT, **values}
    # Optional keys are only present when set, so older goals keep their contract digest.
    if values.get("effort") is not None:
        if values["effort"] not in EFFORTS:
            raise LoopError(f"agent.effort must be one of {EFFORTS}")
    else:
        result.pop("effort", None)
    if "team_limit" in values:
        if type(values["team_limit"]) is not int or not 1 <= values["team_limit"] <= 12:
            raise LoopError("agent.team_limit must be 1..12 sub-agents per task")
    if "stages" in values:
        stages = values["stages"]
        if not isinstance(stages, dict) or set(stages) - set(STAGE_PROFILES):
            raise LoopError(f"agent.stages keys must be among {STAGE_PROFILES}")
        result["stages"] = {k: profile(v, f"agent.stages.{k}") for k, v in stages.items()}
    if "roles" in values:
        roles = values["roles"]
        if not isinstance(roles, dict) or not all(isinstance(k, str) and k for k in roles):
            raise LoopError("agent.roles maps task roles (architect, backend, tester, ...) to profiles")
        result["roles"] = {k: profile(v, f"agent.roles.{k}") for k, v in roles.items()}
    if not isinstance(result["adapter"], str) or not result["adapter"]:
        raise LoopError("agent.adapter must name an adapter (claude, codex, or a configured command)")
    if not isinstance(result["extra_args"], list) or not all(isinstance(a, str) for a in result["extra_args"]):
        raise LoopError("agent.extra_args must be a list of strings")
    return result


def goal(values: dict) -> dict:
    if not isinstance(values, dict):
        raise LoopError("A goal must be an object")
    allowed = {"schema", "id", "kind", "title", "objective", "context", "constraints", "acceptance",
               "reviews", "policy", "schedule", "agent", "created_at", "pipeline"}
    unknown = set(values) - allowed
    if unknown:
        raise LoopError(f"Unknown goal fields {sorted(unknown)}")
    kind = values.get("kind", "develop")
    if kind not in KINDS:
        raise LoopError(f"goal kind must be one of {KINDS}")
    result = {
        "schema": "loop.goal/2",
        "id": slug(values.get("id") or new_id("goal"), "goal id"),
        "kind": kind,
        "title": text(values.get("title"), "title", 200),
        "objective": text(values.get("objective"), "objective", 20000),
        "context": _strings(values.get("context", []), "context"),
        "constraints": _strings(values.get("constraints", []), "constraints"),
        "acceptance": checks(values.get("acceptance", []), "acceptance"),
        "reviews": _reviews(values.get("reviews", [])),
        "policy": policy(values.get("policy")),
        "schedule": None,
        "agent": agent(values.get("agent")),
        "created_at": values.get("created_at") or now(),
        "pipeline": values.get("pipeline"),
    }
    if isinstance(result["pipeline"], dict):
        skip = result["pipeline"].get("skip", [])
        if set(result["pipeline"]) - {"skip"} or not isinstance(skip, list) or set(skip) - set(SKIPPABLE_STAGES):
            raise LoopError(f'pipeline is "full" or {{"skip": [...]}} with stages from {SKIPPABLE_STAGES}')
        result["pipeline"] = {"skip": sorted(set(skip))}
    elif result["pipeline"] not in PIPELINES:
        raise LoopError('pipeline must be "full", {"skip": [...]} or null')
    if result["pipeline"] and kind != "develop":
        raise LoopError("The full pipeline applies to develop goals")
    if kind == "operate":
        schedule = values.get("schedule") or {}
        interval = schedule.get("interval_minutes") if isinstance(schedule, dict) else None
        if type(interval) is not int or not 1 <= interval <= 10080:
            raise LoopError("An operate goal needs schedule.interval_minutes (1..10080)")
        result["schedule"] = {"interval_minutes": interval}
        if (values.get("policy") or {}).get("pause_on_interrupt") is None:
            result["policy"]["pause_on_interrupt"] = True
    elif values.get("schedule"):
        raise LoopError("Only operate goals take a schedule")
    return result


def _reviews(values) -> list[dict]:
    if not isinstance(values, list):
        raise LoopError("reviews must be a list")
    result = [review(v, f"reviews[{i}]") for i, v in enumerate(values)]
    if len({r["id"] for r in result}) != len(result):
        raise LoopError("review ids must be unique")
    return result


def approval_problems(goal_data: dict) -> list[str]:
    """What must be fixed before a goal can be approved."""
    problems = []
    if goal_data["kind"] in {"develop", "operate"} and not goal_data["acceptance"]:
        problems.append("Add at least one acceptance check (the controller, not the agent, decides completion)."
                        if goal_data["kind"] == "develop" else
                        "Add at least one health check as acceptance (it runs every cycle).")
    return problems


def contract_digest(goal_data: dict) -> str:
    keys = ["id", "kind", "objective", "acceptance", "reviews", "policy", "schedule", "constraints", "agent"]
    if goal_data.get("pipeline"):  # absent for older goals, so their approvals stay valid
        keys.append("pipeline")
    return digest({k: goal_data[k] for k in keys})


def _strings(values, field: str) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
        raise LoopError(f"{field} must be a list of non-empty strings")
    return [v.strip() for v in values]


def task(values: dict, existing: dict | None = None) -> dict:
    if not isinstance(values, dict):
        raise LoopError("A task must be an object")
    allowed = {"id", "title", "detail", "depends_on", "checks", "role", "covers", "team"}
    unknown = set(values) - allowed
    if unknown:
        raise LoopError(f"Unknown task fields {sorted(unknown)} (status changes use loop_task)")
    base = deepcopy(existing) if existing else {
        "id": slug(values.get("id", ""), "task id"), "status": "pending", "attempts": 0,
        "notes": [], "created_at": now(), "summary": None}
    if "title" in values or not existing:
        base["title"] = text(values.get("title"), "task title", 200)
    if "detail" in values:
        base["detail"] = text(values.get("detail"), "task detail", 8000, required=False)
    base.setdefault("detail", "")
    if "role" in values:
        base["role"] = text(values.get("role"), "task role", 60, required=False) or None
    if "depends_on" in values or not existing:
        deps = values.get("depends_on", [])
        if not isinstance(deps, list):
            raise LoopError("depends_on must be a list of task ids")
        base["depends_on"] = [slug(d, "dependency") for d in deps]
    if "checks" in values or not existing:
        base["checks"] = checks(values.get("checks", []), f"task {base['id']} checks")
    if "team" in values or not existing:
        base["team"] = team(values.get("team", []), base["id"])
    if "covers" in values or not existing:
        covers = values.get("covers", [])
        if not isinstance(covers, list) or not all(isinstance(c, str) and c for c in covers):
            raise LoopError("covers must be a list of requirement ids")
        base["covers"] = list(dict.fromkeys(covers))
    base["updated_at"] = now()
    return base


def team(values, task_id: str) -> list[dict]:
    """Specialist seats for one task: [{"role": "backend", "count": 2, "focus": "API + persistence"}]."""
    if values in (None, []):
        return []
    if not isinstance(values, list):
        raise LoopError(f"task {task_id} team must be a list of {{role, count, focus?}}")
    seats = []
    for value in values:
        if not isinstance(value, dict) or set(value) - {"role", "count", "focus"}:
            raise LoopError(f"task {task_id} team entries are {{role, count, focus?}}")
        count = value.get("count", 1)
        if type(count) is not int or not 1 <= count <= 8:
            raise LoopError(f"task {task_id} team count must be 1..8")
        seat = {"role": text(value.get("role"), f"task {task_id} team role", 60), "count": count}
        if value.get("focus"):
            seat["focus"] = text(value["focus"], f"task {task_id} team focus", 500)
        seats.append(seat)
    return seats


def requirement(value) -> dict:
    if not isinstance(value, dict):
        raise LoopError("A requirement must be an object")
    unknown = set(value) - {"id", "text", "source", "verify", "kind", "priority"}
    if unknown:
        raise LoopError(f"Unknown requirement fields {sorted(unknown)}")
    rid = value.get("id")
    if not isinstance(rid, str) or not rid.strip() or len(rid) > 80:
        raise LoopError("requirement id must be text (reuse the document's own IDs when it has them)")
    result = {"id": rid.strip(), "text": text(value.get("text"), f"requirement {rid} text", 4000),
              "source": text(value.get("source"), f"requirement {rid} source (doc path and section/line)", 500),
              "verify": text(value.get("verify"), f"requirement {rid} verify (how it will be proven)", 2000)}
    for key in ("kind", "priority"):
        if value.get(key):
            result[key] = text(value[key], f"requirement {rid} {key}", 60)
    return result


def new_state(goal_data: dict) -> dict:
    return {
        "schema": "loop.state/2",
        "goal_id": goal_data["id"],
        "status": "draft",
        "status_reason": "Drafted. Review the goal and approve it to start.",
        "approved_digest": None,
        "approved_by": None,
        "approved_at": None,
        "tasks": [],
        "questions": [],
        "checks": {},
        "reviews": {},
        "review_turn": None,
        "jobs": {},
        "iteration": 0,
        "repair_rounds": 0,
        "stagnant_turns": 0,
        "turns": [],
        "usage": {"turns": 0, "cost_usd": 0.0, "cost_complete": True, "input_tokens": 0,
                  "output_tokens": 0, "agent_seconds": 0.0},
        "started_at": None,
        "notes": [],
        "findings": [],
        "incidents": [],
        "blocker": None,
        "report": None,
        "control": None,
        "fingerprint": None,
        "next_cycle_at": None,
        "integrity": "ok",
        "created_at": now(),
        "updated_at": now(),
    }
