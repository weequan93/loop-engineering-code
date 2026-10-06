"""Supervised native drafts in a disposable copy; only the controller integrates."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from .contracts import ContractError
from .file_changes import change_from_bytes, proposal_version
from .team_automation import encode
from .team_memory import byte_size, context_bytes, fit_task_request
from .workspace import atomic_write, check_write_scope


def directory(record):
    path = Path(record["directory"])
    if path.is_symlink() or path.resolve() != path or not path.is_dir():
        raise ContractError("Native workbench is missing or redirected; preserve the draft and request fresh context")
    if any(p.name.casefold() in {".git", ".loop"} for p in path.iterdir()):
        raise ContractError("Native draft cannot add project control or Git files")
    return path


def prepare(service, team_id, task_id):
    parent_directory = Path(service.workbench_parent or tempfile.gettempdir()).resolve()
    if parent_directory.is_relative_to(service.project):
        raise ContractError("Native workbench must be outside the shared project")
    packet = service.task_request(team_id, task_id)
    request_id = packet["team_assignment"]["request_id"]
    with service.team.teams.writer(team_id) as current:
        service.team._assert_inputs(current)
        if service.team.teams.signal(team_id) or current["status"] != "ACTIVE":
            raise ContractError("Stopped team cannot prepare a native workbench")
        service.team._assert_request_candidate(current, task_id)
        records = current["native_host"].setdefault("workbenches", {})
        record = next((r for r in records.values() if r["task_id"] == task_id and r["request_id"] == request_id), None)
        if record is None:
            if len(records) >= 64:
                raise ContractError("Native workbench history limit reached; preserve drafts and review a new batch")
            base = service.team.snapshots.capture(service.project, service.team.profile(current))
            if base["digest"] != current["records"][task_id]["request"]["snapshot"]:
                raise ContractError("Sources changed before draft preparation")
            parent = Path(tempfile.mkdtemp(prefix="loop-native-workbench-", dir=parent_directory)).resolve()
            path = parent / "project"
            service.team.snapshots.materialize(base, path)
            record = {"id": "workbench-" + uuid4().hex, "task_id": task_id, "request_id": request_id,
                "directory": str(path), "base": service.team.snapshots.put(encode(base)), "status": "DRAFT"}
            records[record["id"]] = record
            service.team.teams.save(current, "host.workbench_prepared")
            service.team._apply_signal(current)
        directory(record)
        worker = current["native_host"]["workers"][task_id]
    active = service._active(team_id)
    service.team._assert_request_candidate(active, task_id)
    if active["records"][task_id]["request"]["id"] != request_id:
        raise ContractError("Task request was replaced before workbench dispatch")
    result = deepcopy(packet)
    result["mode"] = "supervised_workbench_draft"
    result["team_assignment"]["authority"] = (
        "Edit only the supplied disposable workbench, using native file/read tools. Do not edit the shared project, "
        "change contracts or start other coding agents. Produce a concrete draft within the waiting deadline. "
        "Return a short file/result summary, not file contents or a giant AgentStep JSON. The coordinator imports "
        "the draft with loop_workbench_submit; only Loop applies the scoped diff and runs authoritative checks. "
        "This copy shares host OS authority; it is not runtime containment. Exploratory local results do not attest acceptance.")
    result["workbench"] = {**record, "context_path": str(Path(record["directory"]).parent / "context.json"),
        "timeout_seconds": worker["timeout_seconds"], "deadline_epoch": worker["deadline_epoch"]}
    result.pop("response_schema", None)
    fit_task_request(result, service.team.profile(active)["context_max_bytes"])
    atomic_write(Path(result["workbench"]["context_path"]), context_bytes(result))
    return result


def diff(service, data, record):
    root = directory(record)
    base = json.loads(service.team.snapshots.read(record["base"]).decode())
    candidate = service.team.snapshots.capture(root, service.team.profile(data))
    old = {f["path"]: f for f in base["manifest"]["files"]}
    new = {f["path"]: f for f in candidate["manifest"]["files"]}
    changes = []
    task = service.team._child(data, record["task_id"])["task"]
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        if any(f and f["kind"] != "file" for f in (before, after)):
            raise ContractError("Native workbench cannot import symlink changes")
        if after and after["mode"] != (before["mode"] if before else 0o644):
            raise ContractError("Native workbench cannot import permission changes")
        check_write_scope(root, name, task)
        from .native_parallel import enabled
        if enabled(data):
            owned = data["tasks"][record["task_id"]].get("draft_paths")
            if owned is not None and name not in owned:
                raise ContractError("Draft exceeds its frozen parallel file ownership")
        changes.append(change_from_bytes(name, before["sha256"] if before else None,
            service.team.snapshots.read(after["sha256"]) if after else None, proposal_version(task)))
    return candidate, changes


def status(service, team_id, task_id):
    data = service._data(team_id, native=True)
    if task_id not in data["tasks"]:
        raise ContractError("Unknown native workbench task")
    records = [r for r in data["native_host"].get("workbenches", {}).values() if r["task_id"] == task_id]
    rows = []
    for record in records[-12:]:
        row = {**record, "current": bool(data["records"][task_id].get("request") and
            data["records"][task_id]["request"]["id"] == record["request_id"])}
        if record["status"] == "SUBMITTED":
            row.update(changed_files=record["submitted_files"], changed_file_count=len(record["submitted_files"]), problem=None)
            rows.append(row)
            continue
        if not row["current"]:
            row.update(changed_files=[], changed_file_count=None, problem="历史副本保留；核对其路径中的草稿后再使用，不自动重新扫描。")
            rows.append(row)
            continue
        try:
            snapshot, changes = diff(service, data, record)
            row.update(draft_candidate=snapshot["digest"], changed_files=[c["path"] for c in changes][:100],
                changed_file_count=len(changes), problem=None)
            from .native_parallel import changed_paths, enabled
            _, paths = changed_paths(service, data, record)
            row.update(project_changed_paths=paths, refresh_required=bool(paths) and enabled(data))
            if paths and not enabled(data):
                from .native_instruction_recovery import inspect
                try:
                    upgrade = inspect(service, data, task_id, record["id"])
                    row["instruction_recovery"] = {k: upgrade[k] for k in ("changed_paths", "instruction_changes", "worker_candidates")}
                except (OSError, ValueError) as exc:
                    row["instruction_recovery_problem"] = str(exc)
        except (OSError, ValueError) as exc:
            row.update(changed_files=[], changed_file_count=None, problem=str(exc))
        rows.append(row)
    return {"team_id": team_id, "task_id": task_id, "workbenches": rows,
        "host_wait": service.host_wait(data, task_id),
        "assurance": "Draft files only; no authoritative check, independent review or completion is claimed"}


def prepare_submission(service, team_id, task_id, workbench_id, summary):
    """Freeze the stable draft before handing execution to a background worker."""
    from .native_workers import assert_submittable
    data = service._active(team_id)
    record = data["native_host"].get("workbenches", {}).get(workbench_id)
    request = data["records"].get(task_id, {}).get("request")
    if not record or record["task_id"] != task_id or not request or request["id"] != record["request_id"]:
        raise ContractError("Draft has no matching current request; inspect preserved files and prepare fresh context")
    service.team._assert_request_candidate(data, task_id)
    assert_submittable(service, data, task_id)
    from .native_verification import assert_no_running
    assert_no_running(data, task_id)
    candidate, changes = diff(service, data, record)
    child = service.team._child(data, task_id)
    # All paths/preconditions are checked together before any shared-project edit.
    service.team.snapshots.prepare_changes(service.project, child["task"], changes)
    step = {"schema_version": proposal_version(child["task"]), "step_id": "draft-" + uuid4().hex,
        "task_id": task_id, "contract_digest": child["state"]["contract_digest"],
        "base_snapshot_digest": request["snapshot"], "intent": "act" if changes else "request_verification",
        "criterion_ids": [c["id"] for c in child["task"]["criteria"]], "summary": summary,
        "expected_observation": "Run the original contract checks on the collected draft",
        "changes": changes, "evidence_refs": [], "blocker": None, "next_action": None}
    if not isinstance(summary, str) or not summary.strip() or len(summary.encode()) > 8192 or byte_size(step) > 1048576:
        raise ContractError("Collected native draft exceeds its summary/proposal byte limit")
    # Capture again before submission; model text and file mtimes cannot certify a stable draft.
    if service.team.snapshots.capture(directory(record), service.team.profile(data))["digest"] != candidate["digest"]:
        raise ContractError("Draft changed during collection; stop its writer and collect a stable version")
    return {"team_id": team_id, "task_id": task_id, "request_id": request["id"], "step": step,
            "workbench_id": workbench_id, "draft_candidate": candidate["digest"],
            "submitted_files": [c["path"] for c in changes]}


def finish_submission(service, submission):
    team_id, workbench_id = submission["team_id"], submission["workbench_id"]
    with service.team.teams.writer(team_id) as current:
        current["native_host"]["workbenches"][workbench_id].update(status="SUBMITTED",
            draft_candidate=submission["draft_candidate"], submitted_files=submission["submitted_files"])
        service.team.teams.save(current, "host.workbench_submitted")
        service.team._apply_signal(current)


def submit(service, team_id, task_id, workbench_id, summary):
    submission = prepare_submission(service, team_id, task_id, workbench_id, summary)
    service.task_submit(**{key: submission[key] for key in ("team_id", "task_id", "request_id", "step")})
    finish_submission(service, submission)
    return service.progress(team_id)
