"""Read-only check routing for a supervised native host, never evaluation evidence."""

import os
from pathlib import Path
import shutil
from copy import deepcopy
from urllib.parse import unquote, urlparse

from reference.core import canonical_digest
from .contracts import ContractError, load, validate
from .execution_tools import configured_tools
from .native_contracts import load_engine
from .workspace import matches, safe_path

EVALUATOR_ROLES = {"review": "reviewer", "human": "human",
                   "interaction": "interaction", "artifact": "artifact"}
EXECUTOR_TYPES = {"browser": "interaction", "http_load": "artifact"}
ASSURANCE = ("Configuration and current authority availability only, not executed checks, "
             "a connected external evaluator, or passing evidence. Advisory reports cannot satisfy formal checks.")


def _runtime_problems(plan):
    if plan["kind"] != "browser":
        return []
    problems = []
    for name in ("node", "browser_executable"):
        path = Path(plan[name])
        if not path.is_file() or not os.access(path, os.X_OK):
            problems.append(name + " is unavailable")
    module = Path(plan["playwright_module"])
    if not module.is_dir() or not (module / "package.json").is_file():
        problems.append("playwright_module is unavailable")
    return problems


def check_route(check, task, config, tools, authorities, *, workspace=None, review=None):
    """Match ID, procedure kind, check type, authority and supervised execution."""
    result = {"check_id": check["id"], "type": check["type"],
              "independent": bool(check.get("independent")),
              "executor_plan_registered": False, "executor_registered": False,
              "authority_registered": False, "evaluator_role": EVALUATOR_ROLES.get(check["type"]),
              "route": "missing_authority", "automatic_available": False,
              "problems": [], "runtime_validated": False}
    allowed = "run_checks" in task["authorization"]["allowed_actions"]
    if check["type"] == "command":
        command = check["argv"][0]
        path = Path(command)
        if workspace is not None and not path.is_absolute() and "/" in command:
            path = Path(workspace) / check.get("cwd", ".") / path
            executable = path.is_file() and os.access(path, os.X_OK)
        else:
            executable = shutil.which(command) is not None
        result.update(route="controller_command", automatic_available=allowed and executable,
                      summary="由原控制器执行命令检查；配置不代表已经通过。")
        if not allowed:
            result["problems"].append("Task does not authorize check execution")
        if not executable:
            result["problems"].append("Check executable is unavailable: " + command)
        return result
    expected = result["evaluator_role"]
    for key in (config or {}).get("evaluator_keys", []):
        if key["role"] != expected or check["id"] not in key["check_ids"]:
            continue
        try:
            if authorities.key(key["key_id"])["role"] == expected:
                result["authority_registered"] = True
                break
        except (OSError, ValueError):
            continue  # Never return private key contents or treat a revoked key as available.
    if not result["authority_registered"]:
        result["problems"].append("No enabled registered evaluator authority matches this check/type")
    if review and (review["registered"] or review["problems"]):
        result.update(executor_kind="registered_review", executor_plan_registered=review["registered"])
        result["problems"].extend(review["problems"])
        return _select_route(result)
    plan = next((item for item in (tools or {}).get("executors", [])
                 if item["check_id"] == check["id"]), None)
    if plan:
        result["executor_kind"] = plan["kind"]
        result["executor_plan_registered"] = EXECUTOR_TYPES.get(plan["kind"]) == check["type"]
        if not result["executor_plan_registered"]:
            result["problems"].append("Registered executor kind does not match this check type")
        else:
            result["problems"].extend(_runtime_problems(plan))
            if not allowed:
                result["problems"].append("Task does not authorize external check execution")
            if task["autonomy"] == "unattended" or (config or {}).get("runtime", {}).get("backend") != "local":
                result["problems"].append("Registered executors require the supervised local runtime")
    return _select_route(result)


def _select_route(result):
    result["executor_registered"] = bool(result["executor_plan_registered"] and
                                         result["authority_registered"] and not result["problems"])
    result["automatic_available"] = result["executor_registered"]
    if result["executor_registered"]:
        route = "registered_review" if result.get("executor_kind") == "registered_review" else "registered_executor"
        result.update(route=route, summary="可调用匹配的已注册执行器；仍需实际执行并验证结果。")
    elif result["authority_registered"]:
        result.update(route="signed_import", summary="已登记外部签名导入权限，需实际评估者提交当前候选的合法结果；未证明评估者已连接。")
    else:
        result["summary"] = "缺少匹配且有效的评估权威；可继续已授权的开发，正式验收仍未具备条件。"
    return result


def _review_route(service, child, check_id, policy, registration):
    from .native_review import executor_for
    result = {"registered": False, "problems": []}
    if not registration["registered"]:
        return result
    if registration["problem"]:
        result["problems"].append("Registered review configuration unavailable: " + registration["problem"])
        return result
    try:
        descriptor = executor_for(service, child, check_id)
    except (OSError, ValueError) as exc:
        result["problems"].append(str(exc)[:256])
        return result
    if descriptor is None:
        return result
    result["registered"] = True
    task, config = child["task"], child["native"]["config"]
    if "run_checks" not in task["authorization"]["allowed_actions"]:
        result["problems"].append("Task does not authorize independent review execution")
    if task["autonomy"] == "unattended" or config["runtime"]["backend"] != "local":
        result["problems"].append("Registered review requires the supervised local runtime")
    if any(limits.get(name) is not None for limits in (task["limits"], policy or {})
           for name in ("max_tokens", "max_cost_microunits")):
        result["problems"].append("Registered review CLI cannot enforce hard token/cost caps")
    return result


def repair_brief(service, data, task_id):
    repair_id = data["tasks"][task_id].get("repair_task") or task_id
    assignment = data["tasks"][repair_id]
    task = load(safe_path(Path(data["workspace"]), assignment["task_file"]), "task")
    if canonical_digest(task) != assignment["contract_digest"]:
        raise ContractError("Repair scope differs from the frozen task contract")
    return {"repair_task": repair_id, "repair_owner": assignment["owner"],
            "repair_write_allow": task["scope"]["write_allow"], "repair_write_deny": task["scope"]["write_deny"],
            "repair_edit_authorized": ("edit_workspace" in task["authorization"]["allowed_actions"] and any(
                pattern not in task["scope"]["write_deny"] and not matches(pattern, task["scope"]["write_deny"])
                for pattern in task["scope"]["write_allow"])),
            "repair_requires_reviewed_plan": data["records"][repair_id]["status"] == "COMPLETE"}


def cached_result_imports(controller, child, snapshot, environment):
    """Expose current signed RESULT recovery without accepting it as passing evidence."""
    result = {}
    checks = {check["id"]: check for check in child["task"]["checks"]}
    for attempt in child["native"].get("execution_attempts", {}).values():
        if attempt["status"] != "RESULT":
            continue
        envelope = attempt.get("envelope")
        if envelope is None:
            raise ContractError("Cached evaluator RESULT has no signed envelope")
        record = controller.store.authorities.verify(envelope, "evidence")
        validate("evidence", record)
        check = checks.get(record["check_id"])
        if check is None or check["type"] == "command":
            raise ContractError("Cached evaluator RESULT does not match a formal external check")
        bindings = {"task_id": child["task"]["task_id"], "contract_digest": child["state"]["contract_digest"],
                    "snapshot_digest": snapshot, "environment_digest": environment,
                    "check_id": check["id"], "check_digest": canonical_digest(check)}
        if any(record[field] != expected for field, expected in bindings.items()):
            continue  # Historical RESULT cannot be imported for the current candidate.
        expected_role = EVALUATOR_ROLES[check["type"]]
        if not any(key["key_id"] == envelope["key_id"] and key["role"] == expected_role and
                   check["id"] in key["check_ids"] for key in child["native"]["config"]["evaluator_keys"]) or envelope["role"] != expected_role:
            raise ContractError("Cached evaluator RESULT has no matching registered authority")
        request_id = record["extensions"].get("request_id")
        saved = child["native"]["evaluation_requests"].get(request_id)
        if saved is None:
            raise ContractError("Cached evaluator RESULT has no original frozen request")
        if saved["consumed"]:
            continue
        request = saved["request"]
        if any(request[field] != expected for field, expected in bindings.items()):
            raise ContractError("Cached evaluator RESULT differs from its frozen request")
        if (record["executor_run_id"] != request["context_id"] or
                record["extensions"].get("context_id") != request["context_id"] or
                request["context_id"] == child["native"]["implementer_context_id"]):
            raise ContractError("Cached evaluator RESULT did not use the requested separate context")
        controller._check_artifacts([record])
        for ref in record["artifacts"]:
            path = Path(unquote(urlparse(ref["uri"]).path))
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 10485760:
                raise ContractError("Cached evaluator RESULT artifact is unsupported")
        result[check["id"]] = {"import_pending": True, "cached_result": record["result"],
            "cached_summary": record["summary"], "cached_envelope": deepcopy(envelope)}
    return result


def verification_preflight(service, data):
    root = Path(data["workspace"])
    result = {"checks": [], "problems": [], "registered_execution_tools": [],
              "readiness_issues": [],
              "assurance": ASSURANCE, "blocks_development": False,
              "counts": {route: 0 for route in ("controller_command", "registered_executor", "registered_review", "signed_import", "missing_authority")},
              "formal_checks_pending": not bool(data["tasks"])}
    try:
        service.team._assert_inputs(data)
        profile = service.team.profile(data)
    except (OSError, ValueError) as exc:
        result["problems"].append({"task_id": None, "problem": str(exc)[:256]})
        return result
    try:
        tools = configured_tools(root, profile)
        result["registered_execution_tools"] = [{"check_id": item["check_id"],
            "kind": item["kind"], "type": EXECUTOR_TYPES[item["kind"]]}
            for item in (tools or {}).get("executors", [])]
    except (OSError, ValueError) as exc:
        tools = None
        result["problems"].append({"task_id": None, "problem": str(exc)[:256]})
    from .native_review import status
    registration = status(service.team.store, root)
    result["review_runner"] = {name: registration[name] for name in ("registered", "available", "problem")}
    policy_path = safe_path(root, ".loop/team-policy.json")
    policy = load(policy_path, "team-policy") if policy_path.exists() else None
    for task_id, assignment in data["tasks"].items():
        try:
            record = data["records"][task_id]
            integrated = bool(record.get("integration_run_id") and service.team._exists(record["integration_run_id"]))
            if record["child_run_id"] and service.team._exists(record["child_run_id"]):
                child = service.team._child(data, task_id, verification=integrated)
                task, config = child["task"], child.get("native", {}).get("config")
                child_tools = configured_tools(Path(child["workspace"]), child["profile"])
            else:
                task = load(safe_path(root, assignment["task_file"]), "task")
                if canonical_digest(task) != assignment["contract_digest"]:
                    raise ContractError("Frozen task contract changed; route is not current")
                config = load_engine(safe_path(root, assignment["engine_file"])) if assignment["engine_file"] else None
                child_tools = tools
                child = {"workspace": str(root), "profile": profile, "task": task, "native": {"config": config}}
            from .native_readiness import task_diagnostics, effective_budget, budget_issues
            result["readiness_issues"].extend(task_diagnostics(task, root,
                load(root / ".loop/questions.json", "questions")))
            result["readiness_issues"].extend(budget_issues(task, effective_budget(service, task, config, child_tools)))
            for check in task["checks"]:
                review = (_review_route(service, child, check["id"], policy, registration)
                          if check["type"] == "review" and config is not None and "native_host" in data else None)
                result["checks"].append({"task_id": task_id, **check_route(check, task, config, child_tools,
                    service.team.store.authorities, workspace=child["workspace"], review=review)})
        except (OSError, ValueError) as exc:
            result["problems"].append({"task_id": task_id, "problem": str(exc)[:256]})
    result["counts"] = {route: sum(item["route"] == route for item in result["checks"])
                        for route in ("controller_command", "registered_executor", "registered_review", "signed_import", "missing_authority")}
    return result


def compact_preflight(result):
    unresolved = [item for item in result["checks"] if item["route"] in {"signed_import", "missing_authority"}]
    return {"counts": result["counts"], "formal_checks_pending": result["formal_checks_pending"],
            "readiness_issues": result.get("readiness_issues", [])[:12],
            "registered_execution_tools": result["registered_execution_tools"][:16],
            "review_runner": result.get("review_runner", {"registered": False, "available": False, "problem": None}),
            "unresolved": [{"task_id": item["task_id"], "check_id": item["check_id"],
                "type": item["type"], "route": item["route"]} for item in unresolved[:12]],
            "omitted_unresolved": max(0, len(unresolved) - 12), "assurance": ASSURANCE}
