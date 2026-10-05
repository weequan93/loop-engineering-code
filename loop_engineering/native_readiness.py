"""Early planning diagnostics. Declarations never count as executed evidence."""

from .contracts import ContractError, load, relative_path, validate
from .workspace import safe_path


PLANNING_GUIDANCE = {
    "tool": "loop_plan_preflight",
    "decisions": ["Accepted milestone and stop boundary", "Acceptance environment, permissions and real devices",
                  "Local delivery versus signing/deployment", "External services, data policy and authorized spending"],
    "procedure": "Read docs and prior actual answers first. Batch only unresolved material decisions during SPEC/INTAKE; "
                 "do not ask again for authorized work or routine implementation choices. Preview every proposed task's "
                 "checks before freezing. Plan missing measurement producers, repair ownership, check time and rerun reserve. "
                 "Use extensions.verification_plan for declared measurement coverage and actual decision IDs. "
                 "Continue independent authorized work when an external acceptance capability is pending.",
}


def validate_plan(task):
    plan = task.get("extensions", {}).get("verification_plan")
    if plan is None:
        return
    validate("verification-plan", plan)
    known = {check["id"] for check in task["checks"]}
    external = {check["id"] for check in task["checks"] if check["type"] != "command"}
    if not set(plan.get("external_timeouts", {})).issubset(external):
        raise ContractError("External timeout declarations need existing non-command check IDs")
    seen = set()
    for row in plan["measurements"]:
        if row["check_id"] not in known or row["check_id"] in seen:
            raise ContractError("Verification plan needs unique existing check IDs")
        seen.add(row["check_id"])
        missing = set(row["required"]) - set(row["produced"])
        if missing:
            raise ContractError("Verification producer does not cover required measurements: " + ", ".join(sorted(missing)))
        for path in row["producer_paths"]:
            relative_path(path)
    seconds = sum(c.get("timeout_seconds", 0) for c in task["checks"])
    if seconds + plan["repair_reserve_seconds"] > task["limits"]["max_wall_seconds"]:
        raise ContractError("Verification timeouts plus repair reserve exceed the task wall limit")


def validate_decisions(task, questions):
    plan = task.get("extensions", {}).get("verification_plan", {})
    by_id = {q["id"]: q for q in questions["questions"]}
    for identity in plan.get("decision_ids", []):
        if identity not in by_id or not by_id[identity]["answers"]:
            raise ContractError("Verification plan needs an actual recorded answer: " + identity)


def effective_budget(service, task, config, tools):
    """A conservative sequential verification allowance, not a duration forecast.

    Each external import triggers another controller verification of commands.
    Missing external durations are unknown, never zero. The repair reserve is
    additional to this first pass and does not silently extend a frozen limit.
    """
    from .native_review import registration
    plan = task.get("extensions", {}).get("verification_plan", {})
    declared = plan.get("external_timeouts", {})
    commands = sum(c["timeout_seconds"] for c in task["checks"] if c["type"] == "command")
    external = [c for c in task["checks"] if c["type"] != "command"]
    registration_problem = None
    try:
        review = registration(service.team.store, service.project)
    except (OSError, ValueError) as exc:
        review, registration_problem = None, str(exc)[:256]
    durations, unknown = {}, []
    for check in external:
        executor = next((p for p in (tools or {}).get("executors", []) if p["check_id"] == check["id"]), None)
        seconds = review["timeout_seconds"] if check["type"] == "review" and review else (
            executor.get("timeout_seconds") if executor else None)
        if seconds is None:
            seconds = declared.get(check["id"])
        if seconds is None:
            unknown.append(check["id"])
        else:
            durations[check["id"]] = max(seconds, declared.get(check["id"], 0))
    rounds = 1 + len(external)
    seconds = commands * rounds + sum(durations.values())
    reserve = plan.get("repair_reserve_seconds", 0)
    return {"command_seconds_per_pass": commands, "command_passes": rounds,
            "external_seconds": durations, "unknown_check_ids": unknown,
            "verification_seconds": seconds, "repair_reserve_seconds": reserve,
            "required_seconds": seconds + reserve, "limit_seconds": task["limits"]["max_wall_seconds"],
            "fits": seconds + reserve <= task["limits"]["max_wall_seconds"],
            "complete": not unknown and registration_problem is None,
            "registration_problem": registration_problem,
            "scope": "One candidate, sequential external imports and repeated command checks; repairs use the separate reserve. Host work and setup need additional headroom."}


def budget_issues(task, budget):
    result = []
    def issue(code, detail):
        result.append(dict(task_id=task["task_id"], kind="engineering", code=code, detail=detail))
    if not budget["fits"]:
        issue("effective_budget_exceeded", "Effective check time plus repair reserve exceeds the task wall limit: " +
              str(budget["required_seconds"]) + " > " + str(budget["limit_seconds"]))
    if not budget["complete"]:
        issue("effective_budget_unknown", "Resolve external check durations before promising automatic acceptance: " +
              ", ".join(budget["unknown_check_ids"]) + (budget["registration_problem"] or ""))
    if task.get("extensions", {}).get("verification_plan") and budget["repair_reserve_seconds"] < budget["verification_seconds"]:
        issue("effective_rerun_reserve", "Repair reserve is smaller than one complete verification allowance including external reviews and command reruns.")
    return result


def task_diagnostics(task, root, questions):
    rows = []
    def issue(code, kind, detail, **extra):
        rows.append(dict(task_id=task["task_id"], code=code, kind=kind, detail=detail, **extra))
    scenarios = task.get("extensions", {}).get("verification_scenarios", [])
    for surface in sorted({s["surface"] for s in scenarios}):
        issue("host_verification_required", "external_dependency",
              "Confirm an actual host interaction route and early launch/login/backend smoke for " + surface +
              "; scenario declarations and the observation bridge are not executable capability proof.", surface=surface)
    plan = task.get("extensions", {}).get("verification_plan")
    if plan is None:
        issue("coverage_undeclared", "engineering", "Measurement coverage and repair reserve are not declared; inspect the actual producers before promising acceptance.")
    else:
        answers = {q["id"]: q["answers"] for q in questions["questions"]}
        for identity in plan["decision_ids"]:
            if not answers.get(identity):
                issue("decision_pending", "user_input", "Record the user's actual decision during intake.", question_id=identity)
        for row in plan["measurements"]:
            for path in row["producer_paths"]:
                if not safe_path(root, path).is_file():
                    issue("producer_missing", "engineering", "Implement the declared measurement producer before this check: " + path,
                          check_id=row["check_id"])
        seconds = sum(c.get("timeout_seconds", 0) for c in task["checks"])
        if seconds and plan["repair_reserve_seconds"] < seconds:
            issue("rerun_reserve", "engineering", "Declared repair reserve is smaller than one full command-check timeout allowance.")
    return rows


def preview(service, team_id, contracts):
    """Inspect proposed contracts without writing controls, creating keys or running checks."""
    from .execution_tools import configured_tools
    from .native_contracts import validate_native
    from .native_preflight import check_route, _review_route, ASSURANCE
    from .native_review import status
    data = service._data(team_id, native=True)
    service.team._assert_inputs(data)
    if not isinstance(contracts, list) or not 1 <= len(contracts) <= 128:
        raise ContractError("Preflight needs between 1 and 128 proposed task contracts")
    profile = service.team.profile(data)
    root = service.project
    questions = load(root / ".loop/questions.json", "questions")
    tools = configured_tools(root, profile)
    registration = status(service.team.store, root)
    policy = load(root / ".loop/team-policy.json", "team-policy")
    checks, issues, seen, budgets = [], [], set(), []
    for entry in contracts:
        task, config = entry["task"], entry.get("engine")
        validate("task", task)
        if task["task_id"] in seen:
            raise ContractError("Preflight task IDs must be unique")
        seen.add(task["task_id"])
        if config is not None:
            validate_native("engine", config)
        issues.extend(task_diagnostics(task, root, questions))
        budget = effective_budget(service, task, config, tools)
        budgets.append({"task_id": task["task_id"], **budget})
        issues.extend(budget_issues(task, budget))
        child = {"workspace": str(root), "profile": profile, "task": task, "native": {"config": config}}
        for check in task["checks"]:
            review = (_review_route(service, child, check["id"], policy, registration)
                      if check["type"] == "review" and config is not None else None)
            route = check_route(check, task, config, tools, service.team.store.authorities,
                                workspace=root, review=review)
            checks.append({"task_id": task["task_id"], **route})
            if not route["automatic_available"]:
                issues.append(dict(task_id=task["task_id"], check_id=check["id"], code="execution_unavailable",
                    kind="engineering" if check["type"] == "command" else "external_dependency",
                    detail="; ".join(route["problems"]) or route["summary"]))
    return {"team_id": team_id, "checks": checks, "issues": issues, "effective_budgets": budgets,
            "questions_to_resolve": [v for v in issues if v["kind"] == "user_input"],
            "ready_for_automatic_acceptance": not issues, "assurance": ASSURANCE,
            "scope": "Supplied contracts only; required role reviews added at plan submission must also have an executor"}
