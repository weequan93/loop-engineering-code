"""Journal a narrow stage-configuration repair without restarting accepted work.

Only empty graphs of staged native tasks that never created a child can receive
the documented single-stage default. Contracts, authorities, budgets, task IDs,
team dependencies and existing evidence remain frozen.
"""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from reference.core import canonical_digest, strict_json_loads
from .contracts import ContractError, load
from .native_contracts import load_engine
from .stages import validate_stages
from .team_automation import encode
from .workspace import atomic_write, byte_digest, safe_path


def inspect(service, data):
    pending = data.get("native_host", {}).get("stage_repair")
    if pending:
        return {"problems": [], "repairable": True, "pending": True,
                "task_ids": [item["task_id"] for item in pending["writes"]]}
    problems, task_ids = [], []
    root = Path(data["workspace"])
    for task_id, assignment in data["tasks"].items():
        if not assignment["engine_file"]:
            continue
        task, config = None, None
        try:
            task = load(safe_path(root, assignment["task_file"]), "task")
            config = load_engine(safe_path(root, assignment["engine_file"]))
            validate_stages(task, config["stages"])
        except (OSError, ValueError) as exc:
            problems.append({"task_id": task_id, "problem": str(exc)[:512]})
            # A meaningful declared graph is never replaced by a default.
            if (isinstance(task, dict) and isinstance(config, dict) and task["task_id"] == task_id
                    and task["workflow"] == "staged" and config["stages"] == []):
                task_ids.append(task_id)
    repairable = bool(problems and len(task_ids) == len(problems) and "native_host" in data
                      and "automation" not in data and data.get("stage") == "EXECUTION"
                      and data["status"] == "ACTIVE" and not data.get("pending_control"))
    for task_id, record in data["records"].items():
        if task_id in task_ids:
            engine = data["tasks"][task_id]["engine_file"]
            repairable = repairable and sum(item["engine_file"] == engine for item in data["tasks"].values()) == 1
            repairable = repairable and record["status"] in {"PENDING", "RUNNING"} and not record.get("request")
            repairable = repairable and not any(service.team._exists(run_id) for run_id in
                (record.get("child_run_id"), record.get("integration_run_id")) if run_id)
        elif record["status"] not in {"PENDING", "COMPLETE"}:
            repairable = False
    return {"problems": problems, "repairable": bool(repairable), "pending": False, "task_ids": task_ids}


def _apply(service, data, *, explicit_resume=False):
    journal = data["native_host"]["stage_repair"]
    root = Path(data["workspace"])
    signal = service.team.teams.signal(data["team_id"])
    allowed = data["status"] in {"ACTIVE", "PAUSED"} and signal in {None, "PAUSED"} if explicit_resume else (
        data["status"] == "ACTIVE" and signal is None)
    if not allowed:
        raise ContractError("Stopped team cannot repair a frozen stage configuration")
    if data["scenario_digest"] != journal["scenario_digest"]:
        raise ContractError("Stage repair scenario identity changed")
    targets = {item["path"]: item for item in journal["writes"]}
    for name, digest in data["bound_files"].items():
        current = byte_digest(safe_path(root, name).read_bytes())
        if current not in ({targets[name]["old"], targets[name]["new"]} if name in targets else {digest}):
            raise ContractError("Stage repair conflicts with a frozen input: " + name)
    from .scenarios import task_inputs_digest
    if task_inputs_digest(root) != journal["scenario_digest"]:
        raise ContractError("Stage repair scenario inputs changed")
    candidate = service.team.snapshots.capture(root, service.team.profile(data))
    if candidate["digest"] != journal["candidate"]:
        raise ContractError("Project sources changed during stage repair")
    for item in journal["writes"]:
        record = data["records"][item["task_id"]]
        if record != item["record"] or any(service.team._exists(run_id) for run_id in
                (record.get("child_run_id"), record.get("integration_run_id")) if run_id):
            raise ContractError("Stage repair task already started or changed")
        content = service.team.snapshots.read(item["new"])
        config = strict_json_loads(content.decode("utf-8"))
        old = strict_json_loads(service.team.snapshots.read(item["old"]).decode("utf-8"))
        if {k: v for k, v in config.items() if k != "stages"} != {k: v for k, v in old.items() if k != "stages"}:
            raise ContractError("Stage repair cannot change runtime, evaluators or limits")
        task = load(safe_path(root, data["tasks"][item["task_id"]]["task_file"]), "task")
        validate_stages(task, config["stages"])
    # Validate every target before any write; retries accept only old/new bytes.
    for item in journal["writes"]:
        signal = service.team.teams.signal(data["team_id"])
        if signal == "CANCELLED" or signal and not explicit_resume:
            raise ContractError("Team stopped during stage repair; preserve the original journal")
        atomic_write(safe_path(root, item["path"]), service.team.snapshots.read(item["new"]))
        data["bound_files"][item["path"]] = item["new"]
    if service.team.snapshots.capture(root, service.team.profile(data))["digest"] != journal["candidate"]:
        raise ContractError("Project sources changed during stage repair")
    signal = service.team.teams.signal(data["team_id"])
    if signal == "CANCELLED" or signal and not explicit_resume:
        raise ContractError("Team stopped during stage repair; preserve the original journal")
    service.team._assert_inputs(data)
    data["native_host"].setdefault("stage_repair_history", []).append(deepcopy(journal))
    del data["native_host"]["stage_repair"]
    service.team.teams.save(data, "host.stage_configuration_repaired")


def resume(service, team_id):
    """Replay an existing repair only on the host's explicit resume action.

    Pause remains set until all journal invariants pass and the original normal
    team resume succeeds. This does not create a repair or override cancellation.
    """
    with service.team.teams.writer(team_id) as data, service.team.store.workspace_writer(service.project):
        if data.get("native_host", {}).get("stage_repair"):
            _apply(service, data, explicit_resume=True)


def repair(service, team_id, *, dry_run=False):
    data = service._data(team_id, native=True)
    if service.team.teams.signal(team_id) or data["status"] != "ACTIVE":
        raise ContractError("Stopped team cannot repair a frozen stage configuration")
    report = inspect(service, data)
    if dry_run:
        return {"team_id": team_id, **report, "model_dispatched": False, "applied": False}
    if not report["problems"] and not report["pending"]:
        service.team._assert_inputs(data)
        return report
    if not report["repairable"]:
        raise ContractError("Stage repair only supports empty graphs of unstarted staged native tasks")
    with service.team.teams.writer(team_id) as current, service.team.store.workspace_writer(service.project):
        if not current["native_host"].get("stage_repair"):
            service.team._assert_inputs(current)
            fresh = inspect(service, current)
            if not fresh["repairable"]:
                raise ContractError("Stage repair eligibility changed")
            writes = []
            for task_id in fresh["task_ids"]:
                assignment = current["tasks"][task_id]
                path = safe_path(service.project, assignment["engine_file"])
                config = load_engine(path)
                task = load(safe_path(service.project, assignment["task_file"]), "task")
                if canonical_digest(task) != assignment["contract_digest"]:
                    raise ContractError("Stage repair task differs from its frozen contract")
                config["stages"] = [{"id": "delivery", "depends_on": [],
                    "criterion_ids": [item["id"] for item in task["criteria"]],
                    **deepcopy(task["scope"])}]
                validate_stages(task, config["stages"])
                writes.append({"task_id": task_id, "path": assignment["engine_file"],
                    "old": service.team.snapshots.put(path.read_bytes()),
                    "new": service.team.snapshots.put(encode(config)),
                    "record": deepcopy(current["records"][task_id])})
            current["native_host"]["stage_repair"] = {"id": "stage-repair-" + uuid4().hex,
                "scenario_digest": current["scenario_digest"],
                "candidate": service.team.snapshots.capture(service.project, service.team.profile(current))["digest"],
                "writes": writes}
            service.team.teams.save(current, "host.stage_configuration_repair_prepared")
        _apply(service, current)
    return inspect(service, service._data(team_id, native=True))
