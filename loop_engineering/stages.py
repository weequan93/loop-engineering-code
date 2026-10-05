"""Sequential dependency scheduler; stage evidence belongs to one exact candidate."""

from copy import deepcopy

from .contracts import ContractError
from .native_contracts import validate_native
from .workspace import matches


def validate_stages(task: dict, stages: list[dict]) -> None:
    for stage in stages:
        validate_native("stage", stage)
    ids = [stage["id"] for stage in stages]
    if len(ids) != len(set(ids)):
        raise ContractError("Duplicate stage ID")
    if task["workflow"] == "staged" and not stages:
        raise ContractError("A staged task requires a stage dependency graph")
    if task["workflow"] != "staged" and stages:
        raise ContractError("Stages require workflow=staged")
    owners = [criterion for stage in stages for criterion in stage["criterion_ids"]]
    criteria = {item["id"] for item in task["criteria"]}
    if stages and (len(owners) != len(set(owners)) or set(owners) != criteria):
        raise ContractError("Every criterion must have exactly one stage owner")
    pending = set(ids)
    while pending:
        ready = {stage["id"] for stage in stages if stage["id"] in pending
                 and not (set(stage["depends_on"]) & pending)
                 and set(stage["depends_on"]).issubset(ids)}
        if not ready:
            raise ContractError("Stage graph has a cycle or unknown dependency")
        pending -= ready


def statuses(stages: list[dict], passed_criteria: set[str], snapshot: str) -> list[dict]:
    pending, results = list(stages), {}
    while pending:
        stage = next(s for s in pending if set(s["depends_on"]).issubset(results))
        passed = set(stage["criterion_ids"]).issubset(passed_criteria)
        dependencies_passed = all(results[ref]["status"] == "passed" for ref in stage["depends_on"])
        results[stage["id"]] = {"id": stage["id"], "status": "passed" if passed and dependencies_passed
                               else "ready" if dependencies_passed else "waiting",
                               "snapshot_digest": snapshot}
        pending.remove(stage)
    return [results[stage["id"]] for stage in stages]


def active_stage(stages: list[dict], state: list[dict], snapshot: str) -> dict | None:
    status = {item["id"]: item["status"] for item in state if item["snapshot_digest"] == snapshot}
    for stage in stages:
        if status.get(stage["id"]) != "passed" and all(status.get(dep) == "passed" for dep in stage["depends_on"]):
            return deepcopy(stage)
    return None


def admit_stage(stage: dict | None, step: dict) -> None:
    if stage is None or step["intent"] != "act":
        return
    if not set(step["criterion_ids"]).issubset(stage["criterion_ids"]):
        raise ContractError("Step targets criteria outside the active stage")
    for change in step["changes"]:
        if matches(change["path"], stage["write_deny"]) or not matches(change["path"], stage["write_allow"]):
            raise ContractError("Step writes outside the active stage scope")
