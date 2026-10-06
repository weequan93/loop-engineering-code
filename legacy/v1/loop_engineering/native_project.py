"""Bounded, host-authorized succession of accepted native batches.

This is coordination, not a model daemon or proof that a product is complete.
The queue is an authenticated host declaration; every successor still enters
requirements/planning and the original contract/evidence admission gates.
"""

from copy import deepcopy

from reference.core import canonical_digest
from .contracts import ContractError, load
from .native_workers import text
from .spec_setup import intake
from .store import utc_now
from .workspace import byte_digest, safe_path

MAX_CONTROLLER_SECONDS = 2592000
MAX_BUDGET_AMENDMENTS = 64


def budget_seconds(value):
    if type(value) is not int or not 1 <= value <= MAX_CONTROLLER_SECONDS:
        raise ContractError("Project controller budget must be an integer from 1 to 2592000 seconds")
    return value


def effective_budget(root):
    """Replay separately authenticated increases without editing the declaration."""
    original = budget_seconds(root["native_host"]["project_queue"]["controller_budget_seconds"])
    current = original
    amendments = root["native_host"].get("project_budget_amendments", [])
    if not isinstance(amendments, list) or len(amendments) > MAX_BUDGET_AMENDMENTS:
        raise ContractError("Invalid project budget amendment history")
    seen = set()
    for item in amendments:
        if (not isinstance(item, dict) or set(item) != {"amendment_id", "authorization", "reason",
                "expected_controller_budget_seconds", "controller_budget_seconds", "recorded_controller_ms",
                "latest_team_id", "root_revision_before", "recorded_at"}):
            raise ContractError("Invalid project budget amendment record")
        text(item["amendment_id"], "budget amendment ID", 64)
        text(item["authorization"], "actual budget authorization", 8192)
        text(item["reason"], "budget amendment reason", 2048)
        if (item["amendment_id"] in seen or budget_seconds(item["expected_controller_budget_seconds"]) != current
                or budget_seconds(item["controller_budget_seconds"]) <= current):
            raise ContractError("Project budget amendment chain is inconsistent")
        if (type(item["recorded_controller_ms"]) is not int or item["recorded_controller_ms"] < 0
                or type(item["root_revision_before"]) is not int or item["root_revision_before"] < 1):
            raise ContractError("Invalid project budget amendment accounting")
        seen.add(item["amendment_id"])
        current = item["controller_budget_seconds"]
    return original, current, amendments


def root_data(service, data):
    identity = data.get("native_host", {}).get("project_root")
    return service._data(identity, native=True) if identity else data


def members(service, root):
    result = [root]
    for row in service.team.teams.runs():
        if row["workspace"] != str(service.project) or row["team_id"] == root["team_id"]:
            continue
        item = service._data(row["team_id"])
        if item.get("native_host", {}).get("project_root") == root["team_id"]:
            result.append(item)
    return result


def recorded_milliseconds(service, batches):
    identities = {record.get(name) for batch in batches for record in batch["records"].values()
                  for name in ("child_run_id", "integration_run_id")} - {None}
    elapsed = 0
    for identity in identities:
        if not service.team._exists(identity):
            with service.team.store.connect() as connection:
                admitted = connection.execute("SELECT 1 FROM replay WHERE run_id=? LIMIT 1", (identity,)).fetchone()
            reserved = any(record["status"] in {"PENDING", "RUNNING"} and identity in
                (record.get("child_run_id"), record.get("integration_run_id")) for batch in batches
                for record in batch["records"].values())
            if not admitted and reserved:
                continue  # Only an allocated identity; no authenticated child ever admitted.
            raise ContractError("Retained Run does not exist; do not treat missing accounting as zero")
        elapsed += service.team.store.get(identity)["elapsed_ms"]
    return elapsed


def recorded_seconds(service, batches):
    elapsed = recorded_milliseconds(service, batches)
    # Saved request packets use the integer-only protocol. Round total retained
    # milliseconds upward, so a fractional second cannot expand the allowance.
    return (elapsed + 999) // 1000


def inspect(service, data):
    root = root_data(service, data)
    queue = root.get("native_host", {}).get("project_queue")
    if queue is None:
        return None
    batches = members(service, root)
    successors = {item["native_host"]["project_index"]: item for item in batches if item != root}
    if len(successors) != len(batches) - 1 or set(successors) != set(range(len(successors))):
        raise ContractError("Project succession is inconsistent; inspect authenticated history")
    latest = successors[max(successors)] if successors else root
    original_budget, current_budget, amendments = effective_budget(root)
    elapsed_ms = recorded_milliseconds(service, batches)
    seconds = (elapsed_ms + 999) // 1000
    return {"root_team_id": root["team_id"], "objective": queue["objective"],
            "authorization": queue["authorization"], "milestones": queue["milestones"],
            "original_controller_budget_seconds": original_budget,
            "controller_budget_seconds": current_budget,
            "budget_amendments": deepcopy(amendments),
            "recorded_controller_seconds": seconds,
            "recorded_controller_ms": elapsed_ms,
            "remaining_controller_seconds": max(0, current_budget - seconds),
            "successor_batches": len(successors), "max_successor_batches": len(queue["milestones"]),
            "latest_team_id": latest["team_id"], "latest_status": latest["status"],
            "stop_signal": root["native_host"].get("project_signal") or service.team.teams.signal(root["team_id"]),
            "queue_exhausted": len(successors) == len(queue["milestones"]),
            "host_usage": "unknown; native conversations/delegation are outside controller accounting",
            "accounting_scope": "Registered root and its successors; earlier project batches retain their separate original ledgers. Seconds rounded upward from retained milliseconds.",
            "assurance": "Host-declared goal and authorized sequence; batch evidence covers only accepted contracts. Queue exhaustion alone does not certify the whole product."}


def register(service, team_id, objective, authorization, milestones, controller_budget_seconds):
    if service.handshake()["restart_required"]:
        raise ContractError("Reconnect changed framework code before project supervision")
    text(objective, "project objective", 8192)
    text(authorization, "actual user authorization", 8192)
    budget_seconds(controller_budget_seconds)
    if not isinstance(milestones, list) or not 1 <= len(milestones) <= 16:
        raise ContractError("Declare between one and sixteen authorized successor milestones")
    seen = set()
    for item in milestones:
        if not isinstance(item, dict) or set(item) != {"id", "brief"}:
            raise ContractError("Each successor milestone needs only id and brief")
        text(item["id"], "milestone ID", 64)
        if not isinstance(item["brief"], str):
            raise ContractError("Milestone brief must be text")
        intake(item["brief"])
        if item["id"] in seen:
            raise ContractError("Project milestone IDs must be unique")
        seen.add(item["id"])
    declaration = deepcopy(dict(objective=objective, authorization=authorization,
        milestones=milestones, controller_budget_seconds=controller_budget_seconds))
    data = service._data(team_id, native=True)
    if data["native_host"].get("project_root"):
        raise ContractError("A successor cannot create a new queue or reset the original project budget")
    with service.team.teams.writer(team_id) as current:
        previous = current["native_host"].get("project_queue")
        if previous:
            if previous != declaration:
                raise ContractError("Project queue is frozen; scope changes need reviewed authorization and cap increases use loop_project_budget_amend")
        else:
            if current["status"] in {"PAUSED", "CANCELLED", "BLOCKED"} or service.team.teams.signal(team_id):
                raise ContractError("Stopped teams cannot opt into project continuation")
            service.operations.assert_idle(current)
            if current["status"] == "COMPLETE" and not service.team.status(team_id)["final_candidate_current"]:
                raise ContractError("Stale completion cannot authorize a successor queue")
            if recorded_seconds(service, [current]) >= controller_budget_seconds:
                raise ContractError("Project budget already exhausted by retained controller usage")
            if current.get("stage") == "EXECUTION" and current["status"] != "COMPLETE":
                service.team._assert_inputs(current)
                upper = 2 * sum(load(safe_path(service.project, task["task_file"]))["limits"]["max_wall_seconds"]
                                for task in current["tasks"].values())
                if upper > controller_budget_seconds:
                    raise ContractError("Project budget must retain the existing frozen child/integration limits")
            current["native_host"]["project_queue"] = declaration
            service.team.teams.save(current, "host.project_queue_registered")
    return service.progress(team_id)


def amend_budget(service, team_id, amendment_id, expected_controller_budget_seconds,
                 controller_budget_seconds, authorization, reason):
    """CAS a user-authorized absolute cap; retries cannot append the delta twice."""
    if service.handshake()["restart_required"]:
        raise ContractError("Reconnect changed framework code before project budget amendment")
    text(amendment_id, "budget amendment ID", 64)
    text(authorization, "actual budget authorization", 8192)
    text(reason, "budget amendment reason", 2048)
    budget_seconds(expected_controller_budget_seconds)
    budget_seconds(controller_budget_seconds)
    request = dict(amendment_id=amendment_id, authorization=authorization, reason=reason,
        expected_controller_budget_seconds=expected_controller_budget_seconds,
        controller_budget_seconds=controller_budget_seconds)
    root = root_data(service, service._data(team_id, native=True))
    with service.team.store._lock(service.team.store.run_dir(root["team_id"]) / "project.lock",
                                  "Another operation owns project succession"):
        with service.team.teams.writer(root["team_id"]) as current:
            project = inspect(service, current)
            if project is None:
                raise ContractError("Project budget amendment needs a registered queue")
            previous = next((row for row in project["budget_amendments"]
                             if row["amendment_id"] == amendment_id), None)
            if previous is not None:
                if any(previous[name] != value for name, value in request.items()):
                    raise ContractError("Budget amendment ID is already bound to a different request")
            else:
                assert_running(service, current)
                latest = service._data(project["latest_team_id"], native=True)
                if latest["status"] in {"PAUSED", "CANCELLED"} or service.team.teams.signal(latest["team_id"]):
                    raise ContractError("Stopped project batches cannot receive a new budget amendment")
                if expected_controller_budget_seconds != project["controller_budget_seconds"]:
                    raise ContractError("Stale project budget expectation; inspect the retained amendment history")
                if controller_budget_seconds <= expected_controller_budget_seconds:
                    raise ContractError("Project budget amendments must strictly increase the absolute cap")
                if len(project["budget_amendments"]) >= MAX_BUDGET_AMENDMENTS:
                    raise ContractError("Project budget amendment history limit reached")
                amendments = current["native_host"].setdefault("project_budget_amendments", [])
                amendments.append({**request, "recorded_controller_ms": project["recorded_controller_ms"],
                    "latest_team_id": project["latest_team_id"], "root_revision_before": current["revision"],
                    "recorded_at": utc_now()})
                service.team.teams.save(current, "host.project_budget_amended")
        # Also recover a committed amendment whose response was lost. This never
        # resumes the latest batch, edits its checks or changes per-child limits.
        return service.progress(project["latest_team_id"])


def assert_running(service, data):
    root = root_data(service, data)
    if root["native_host"].get("project_queue") and (
            root["native_host"].get("project_signal") or service.team.teams.signal(root["team_id"])):
        raise ContractError("Project continuation is paused/cancelled; preserve its original queue")


def control(service, team_id, action):
    if service.handshake()["restart_required"]:
        raise ContractError("Reconnect changed framework code before project control")
    if action not in {"pause", "resume", "cancel"}:
        raise ContractError("Unknown project control action")
    root = root_data(service, service._data(team_id, native=True))
    with service.team.store._lock(service.team.store.run_dir(root["team_id"]) / "project.lock",
                                  "Another operation owns project succession"):
        project = inspect(service, root)
        if project is None:
            raise ContractError("Project control needs a registered queue")
        if project["stop_signal"] == "CANCELLED":
            raise ContractError("Cancelled project succession is terminal")
        latest = service._data(project["latest_team_id"], native=True)
        if action == "resume" and latest["status"] == "PAUSED":
            service.control(latest["team_id"], "resume")
        with service.team.teams.writer(root["team_id"]) as current:
            current["native_host"]["project_signal"] = {"pause": "PAUSED", "cancel": "CANCELLED", "resume": None}[action]
            service.team.teams.save(current, "host.project_" + action)
        if action != "resume" and latest["status"] not in {"COMPLETE", "CANCELLED", "BLOCKED"}:
            service.control(latest["team_id"], action)
        return service.progress(latest["team_id"])


def assert_plan_budget(service, data, values):
    project = inspect(service, data)
    if project is None:
        return
    contracts = [value for path, value in values.items() if path.startswith(".loop/tasks/") and "checks" in value]
    # Native integration may create a second child. Reserve both complete child
    # limits, including repair/reruns, rather than replenishing the project cap.
    upper = 2 * sum(task["limits"]["max_wall_seconds"] for task in contracts)
    if upper > project["remaining_controller_seconds"]:
        raise ContractError("Proposed child/integration limits exceed the remaining cumulative project controller budget")


def preflight_budget(service, team_id, contracts, result):
    project = inspect(service, service._data(team_id, native=True))
    if project is None:
        return result
    upper = 2 * sum(entry["task"]["limits"]["max_wall_seconds"] for entry in contracts)
    fits = upper <= project["remaining_controller_seconds"]
    result["project_budget"] = {"root_team_id": project["root_team_id"],
        "controller_budget_seconds": project["controller_budget_seconds"],
        "original_controller_budget_seconds": project["original_controller_budget_seconds"],
        "recorded_controller_seconds": project["recorded_controller_seconds"],
        "remaining_controller_seconds": project["remaining_controller_seconds"],
        "proposed_child_integration_limit_seconds": upper, "fits": fits,
        "shortfall_controller_seconds": max(0, project["recorded_controller_seconds"] + upper
                                            - project["controller_budget_seconds"]),
        "minimum_controller_budget_seconds": project["recorded_controller_seconds"] + upper,
        "amendment_tool": "loop_project_budget_amend",
        "accounting_scope": project["accounting_scope"], "host_usage": project["host_usage"]}
    if not fits:
        result["issues"].append({"task_id": None, "code": "project_budget_insufficient", "kind": "user_input",
            "detail": "Proposed child/integration limits exceed the effective cumulative allowance. Prepare a concrete bounded plan and obtain actual authorization for its larger absolute cap, then use loop_project_budget_amend with the current expected cap. Original usage, contracts and evidence remain; amendment grants no new effects or per-child limits."})
        result["questions_to_resolve"] = [item for item in result["issues"] if item["kind"] == "user_input"]
        result["ready_for_automatic_acceptance"] = False
    return result


def completion_action(state, data):
    project = state.get("project_supervision")
    if project is None:
        return None
    base = {"tool": None, "arguments": {}, "continue_work": False, "completion_scope": "batch"}
    if project.get("stop_signal"):
        return {**base, "kind": "project_stopped", "summary": "项目续批已暂停或取消；保留已完成批次和累计用量。"}
    if project["latest_team_id"] != state["team_id"]:
        return {**base, "kind": "follow_project_batch", "summary": "本批已完成，继续已建立的后续批次。",
                "tool": "loop_next", "arguments": {"team_id": project["latest_team_id"]}, "continue_work": True}
    if project["queue_exhausted"]:
        return {**base, "kind": "project_queue_complete", "summary": "已声明的后续批次全部验收完成；只覆盖这些合同，不代表未列入的产品范围已完成。"}
    if not project["remaining_controller_seconds"]:
        return {**base, "kind": "project_budget_exhausted", "summary": "项目累计控制器额度已耗尽；不得新建批次重置。"}
    milestone = project["milestones"][project["successor_batches"]]
    return {**base, "kind": "advance_project", "summary": "本批验收完成，按已记录授权准备下一批需求和计划。",
            "tool": "loop_project_advance", "arguments": {"team_id": state["team_id"]},
            "continue_work": True, "next_milestone": milestone}


def advance(service, team_id):
    if service.handshake()["restart_required"]:
        raise ContractError("Reconnect changed framework code before advancing the project")
    data = service._data(team_id, native=True)
    root = root_data(service, data)
    with service.team.store._lock(service.team.store.run_dir(root["team_id"]) / "project.lock",
                                  "Another operation owns project succession"):
        data = service._data(team_id, native=True)
        project = inspect(service, data)
        if project is None:
            raise ContractError("Project continuation needs actual authorization and a bounded registered queue")
        assert_running(service, data)
        # Recover a create whose reply was lost. No control files were written by
        # creation; project links are committed inside that team's first event.
        if project["latest_team_id"] != team_id:
            return service.progress(project["latest_team_id"])
        state = service.progress(team_id)
        if state["status"] != "COMPLETE" or not state["final_candidate_current"] or state["evidence_problems"]:
            raise ContractError("Only a current, fully accepted predecessor can advance the project")
        if project["queue_exhausted"] or not project["remaining_controller_seconds"]:
            raise ContractError("Project queue or cumulative controller budget is exhausted")
        service.operations.assert_idle(data)
        related = [row for row in service.team.teams.runs() if row["workspace"] == str(service.project)]
        service._check_previous_effects(related)
        service._check_spend_policy()
        index = project["successor_batches"]
        milestone = project["milestones"][index]
        plan = {**deepcopy(data["plan"]), "tasks": [], "final_task": None, "scope": milestone["brief"]}
        child = {"workspace": str(service.project), "plan": plan, "tasks": {}, "records": {}, "dependencies": {},
            "bound_files": {name: byte_digest((service.project / name).read_bytes())
                            for name in (".loop/project.json", ".loop/team-policy.json")},
            "scenario_digest": data["scenario_digest"], "status": "ACTIVE", "reason": None, "stage": "SPEC",
            "specification": intake(milestone["brief"]),
            "native_host": {"repair_rounds": 0, "max_repairs": 3, "request": None,
                "usage": "unknown; host conversation and delegation are outside the controller ledger",
                "project_root": root["team_id"], "project_index": index,
                "predecessor": {"team_id": team_id, "revision": data["revision"],
                                "checkpoint_digest": canonical_digest(data)}}}
        # Bind current controls, not a historic scenario changed by prior batches.
        from .scenarios import task_inputs_digest
        child["scenario_digest"] = task_inputs_digest(service.project)
        child["initial_snapshot"] = service.team.snapshots.capture(service.project, service.team.profile(data))
        service.team.teams.create(child)
        return service.progress(child["team_id"])
