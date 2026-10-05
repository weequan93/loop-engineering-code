"""Bound specialist drafts inside one native task; the host owns actual agents.

Copies permit parallel drafting, not parallel authoritative task admission.
Collection is journaled and serial; original controller checks still follow.
"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from .contracts import ContractError, load
from .native_workbench import diff, directory
from .scenarios import inputs, role_assignment
from .team_automation import encode
from .team_memory import context_bytes, fit_task_request
from .workspace import atomic_write, check_write_scope


OCCUPIED = {"PREPARED", "RUNNING", "DRAFT_READY", "BLOCKED"}


def records(data):
    return data.get("native_host", {}).get("specialists", {})


def text(value, name, limit=8192):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ContractError("Invalid worker " + name)
    return value


def matches(data, worker):
    request = data["records"].get(worker["task_id"], {}).get("request")
    parent = data["native_host"].get("workbenches", {}).get(worker["workbench_id"])
    return bool(request and request["id"] == worker["request_id"] and parent and
                parent["status"] == "DRAFT" and parent["request_id"] == worker["request_id"])


def active(service, data, worker):
    service.team._assert_inputs(data)
    if service.team.teams.signal(data["team_id"]) or data["status"] != "ACTIVE":
        raise ContractError("Stopped team cannot dispatch or collect workers")
    if not matches(data, worker):
        raise ContractError("Worker has no matching current request; preserve its files")
    service.team._assert_request_candidate(data, worker["task_id"])


def paths_overlap(left, right):
    return any(a.casefold() == b.casefold() or a.casefold().startswith(b.casefold() + "/") or
               b.casefold().startswith(a.casefold() + "/") for a in left for b in right)


def assign(service, team_id, task_id, workbench_id, assignment_id, role, title, goal,
           write_paths, depends_on):
    for value, name, limit in ((assignment_id, "assignment_id", 256), (role, "role", 256),
                               (title, "title", 256), (goal, "goal", 8192)):
        text(value, name, limit)
    if not isinstance(write_paths, list) or len(write_paths) > 128 or not isinstance(depends_on, list) or len(depends_on) > 128:
        raise ContractError("Worker paths and dependencies must be bounded lists")
    for value in write_paths:
        text(value, "write path", 256)
        if any(c in value for c in "*?[]"):
            raise ContractError("Worker write paths must be explicit files, without globs")
    for value in depends_on:
        text(value, "dependency", 256)
    if len(set(depends_on)) != len(depends_on):
        raise ContractError("Duplicate worker dependency")
    packet = service.task_request(team_id, task_id)
    request_id = packet["team_assignment"]["request_id"]
    configuration = {"task_id": task_id, "workbench_id": workbench_id, "assignment_id": assignment_id,
                     "role": role, "title": title, "goal": goal, "write_paths": write_paths, "depends_on": depends_on}
    with service.team.teams.writer(team_id) as data:
        parent = data["native_host"].get("workbenches", {}).get(workbench_id)
        probe = {**configuration, "request_id": request_id}
        active(service, data, probe)
        from .native_verification import assert_no_running
        assert_no_running(data, task_id)
        if parent["task_id"] != task_id:
            raise ContractError("Worker needs the matching task workbench")
        workers = data["native_host"].setdefault("specialists", {})
        previous = next((w for w in workers.values() if w["workbench_id"] == workbench_id and
                         w["assignment_id"] == assignment_id), None)
        if previous:
            if any(previous[key] != value for key, value in configuration.items()):
                raise ContractError("Worker assignment is frozen; use a new assignment ID for reviewed changes")
            return deepcopy(previous)
        policy = load(service.project / ".loop/team-policy.json", "team-policy")
        if data["native_host"].setdefault("worker_policy", policy) != policy:
            raise ContractError("Native worker policy is frozen")
        if sum(w["status"] in OCCUPIED for w in workers.values()) >= policy["max_parallel"]:
            raise ContractError("Worker parallel slots are full; collect results or stop actual old writers")
        if len(workers) >= 128:
            raise ContractError("Native worker history limit reached; preserve drafts and review a new batch")
        siblings = {w["assignment_id"]: w for w in workers.values() if
                    w["workbench_id"] == workbench_id and w["request_id"] == request_id}
        if any(key not in siblings or siblings[key]["status"] != "COLLECTED" for key in depends_on):
            raise ContractError("Worker dependencies must already be collected into this workbench")
        manifest, documents = inputs(service.project)
        selected = {d["role"]: d for d in json.loads(documents[".loop/team.json"])["decisions"]}
        if selected.get(role, {}).get("state") not in {"active", "covered"}:
            raise ContractError("Worker role must be selected as active or covered")
        professional = role_assignment(manifest, documents, role, include_covered=False)
        if professional["role"]["independent"] and write_paths:
            raise ContractError("Independent professionals may only provide read-only advice here")
        root = directory(parent)
        task = service.team._child(data, task_id)["task"]
        for index, value in enumerate(write_paths):
            check_write_scope(root, value, task)
            if paths_overlap([value], write_paths[:index]):
                raise ContractError("Worker write paths overlap or repeat")
        for sibling in siblings.values():
            if sibling["status"] == "COLLECTED" or not paths_overlap(write_paths, sibling["write_paths"]):
                continue
            if sibling["status"] == "STOPPED" and sibling.get("pending_worker_import") is None:
                _, changes = diff(service, data, sibling)
                if not changes:
                    continue
            raise ContractError("Worker write paths overlap an uncollected assignment")
        container = Path(service.workbench_parent or tempfile.gettempdir()).resolve()
        if container.is_relative_to(service.project):
            raise ContractError("Native worker copies must be outside the shared project")
        folder = Path(tempfile.mkdtemp(prefix="loop-native-worker-", dir=container)).resolve()
        now = int(service.clock())
        worker = {**deepcopy(configuration), "id": "worker-" + uuid4().hex, "team_id": team_id,
                  "request_id": request_id, "directory": str(folder / "project"),
                  "context_path": str(folder / "context.json"), "status": "PREPARED", "agent_id": None,
                  "started_epoch": now, "deadline_epoch": now + policy["timeout_seconds"],
                  "timeout_seconds": policy["timeout_seconds"], "detail": "待宿主派发并绑定实际 agent。"}
        context = deepcopy(packet)
        context.pop("response_schema", None)
        context.update(mode="supervised_specialist_draft", worker=deepcopy(worker),
                       professional={"role": professional["role"], "owner": professional["owner"],
                                     "instructions": [{"path": p, "content": documents[p]}
                                                      for p in professional["instruction_paths"]]})
        context["dependency_results"] = [{"assignment_id": key, "role": siblings[key]["role"],
            "summary": siblings[key]["summary"][:2048], "summary_truncated": len(siblings[key]["summary"]) > 2048,
            "files": siblings[key]["collected_files"], "candidate": siblings[key]["draft_candidate"]}
            for key in depends_on]
        context["source_guidance"] = (
            "The formal task packet and its optional excerpts describe the original accepted request. "
            "This worker's actual copy also includes already collected drafts; read its current files before editing. "
            "Dependency summaries are host reports, not signed acceptance evidence.")
        context["team_assignment"]["authority"] = (
            "Read and edit only this disposable specialist copy and exactly worker.write_paths. Empty paths mean read-only. "
            "Do not edit the shared project or coordinating workbench, change controls, spawn coding agents, or claim "
            "formal completion. Produce concrete files early, obey worker.deadline_epoch, and return a short result, "
            "files and actual blocker. Only the coordinator serially collects this draft; original checks remain required. "
            "The copy shares host OS authority, is not containment, and advisory review is not signed independent evidence.")
        limit = service.team.profile(data)["context_max_bytes"]
        fit_task_request(context, limit)
        with service.team.store.workspace_writer(root):
            base = service.team.snapshots.capture(root, service.team.profile(data))
            service.team.snapshots.materialize(base, Path(worker["directory"]))
        worker["base"] = service.team.snapshots.put(encode(base))
        atomic_write(Path(worker["context_path"]), context_bytes(context))
        workers[worker["id"]] = worker
        service.team.teams.save(data, "host.worker_assigned")
        service.team._apply_signal(data)
    active(service, service._active(team_id), worker)
    return deepcopy(worker)


def update(service, team_id, worker_id, agent_id, status, detail):
    if status not in {"RUNNING", "DRAFT_READY", "BLOCKED", "STOPPED"}:
        raise ContractError("Invalid worker status")
    text(detail, "detail", 2048)
    if agent_id is not None:
        text(agent_id, "actual agent ID", 256)
    service._data(team_id, native=True)
    with service.team.teams.writer(team_id) as data:
        worker = records(data).get(worker_id)
        if not worker:
            raise ContractError("Unknown native worker")
        if status != "STOPPED":
            active(service, data, worker)
        if worker["status"] == "COLLECTED" or worker.get("pending_worker_import") is not None and status != "STOPPED":
            raise ContractError("Collected or importing worker cannot be restarted")
        if worker["status"] == "STOPPED" and status != "STOPPED":
            raise ContractError("Stopped worker cannot be restarted; use a new bounded assignment")
        if worker["agent_id"] is not None and worker["agent_id"] != agent_id:
            raise ContractError("Worker actual agent identity is frozen")
        if agent_id is None and not (worker["status"] == "PREPARED" and status == "STOPPED" or
                                     worker["status"] == "STOPPED" and status == "STOPPED"):
            raise ContractError("Worker update needs an actual native agent ID")
        if agent_id is not None and any(w["id"] != worker_id and w["agent_id"] == agent_id and
                                       w["status"] in OCCUPIED for w in records(data).values()):
            raise ContractError("Actual agent already owns another occupied assignment")
        worker.update(agent_id=agent_id, status=status, detail=detail, updated_epoch=int(service.clock()))
        service.team.teams.save(data, "host.worker_updated")
        service.team._apply_signal(data)
    return deepcopy(worker)


def collect(service, team_id, worker_id, summary):
    text(summary, "collection summary")
    service._active(team_id)
    with service.team.teams.writer(team_id) as data:
        worker = records(data).get(worker_id)
        if not worker:
            raise ContractError("Unknown native worker")
        active(service, data, worker)
        if worker["status"] == "COLLECTED":
            return deepcopy(worker)
        if worker["status"] not in {"DRAFT_READY", "STOPPED"} or not worker["agent_id"]:
            raise ContractError("Stop the actual writer or receive its finished draft before collecting")
        parent = data["native_host"]["workbenches"][worker["workbench_id"]]
        root = directory(parent)
        task = service.team._child(data, worker["task_id"])["task"]
        with service.team.store.workspace_writer(root):
            if worker.get("pending_worker_import") is None:
                candidate, changes = diff(service, data, worker)
                if any(c["path"] not in worker["write_paths"] for c in changes):
                    raise ContractError("Worker changed files outside its explicit write paths (read-only if empty)")
                prepared = service.team.snapshots.prepare_changes(root, task, changes)
                if service.team.snapshots.capture(directory(worker), service.team.profile(data))["digest"] != candidate["digest"]:
                    raise ContractError("Worker draft changed during collection; stop its writer")
                worker["pending_worker_import"] = {"changes": prepared, "candidate": candidate["digest"], "summary": summary}
                service.team.teams.save(data, "host.worker_import_prepared")
            # Preserve the frozen journal on crashes or stop signals. Retrying
            # never imports a later edit to the worker's disposable copy.
            active(service, data, worker)
            pending = worker["pending_worker_import"]
            service.team.snapshots.apply_prepared(root, task, pending["changes"])
            worker.update(status="COLLECTED", summary=pending["summary"], draft_candidate=pending["candidate"],
                          collected_files=[c["path"] for c in pending["changes"]], collected_epoch=int(service.clock()))
            worker.pop("pending_worker_import")
            service.team.teams.save(data, "host.worker_collected")
            service.team._apply_signal(data)
    return deepcopy(worker)


def board(service, team_id):
    data = service._data(team_id, native=True)
    policy = data["native_host"].get("worker_policy") or load(service.project / ".loop/team-policy.json", "team-policy")
    rows = []
    for worker in records(data).values():
        row = deepcopy(worker)
        row.update(current=matches(data, worker), expired=bool(worker["status"] in OCCUPIED and
                   service.clock() >= worker["deadline_epoch"]), problem=None)
        if worker["status"] == "COLLECTED":
            files = worker["collected_files"]
            row.update(changed_files=files, changed_file_count=len(files))
        elif row["current"]:
            try:
                _, changes = diff(service, data, worker)
                row.update(changed_files=[c["path"] for c in changes], changed_file_count=len(changes))
                if any(c["path"] not in worker["write_paths"] for c in changes):
                    row["problem"] = "专业草稿修改了未分配的文件；只读咨询不能写入文件。"
            except (OSError, ValueError) as exc:
                row.update(changed_files=[], changed_file_count=None, problem=str(exc))
        else:
            row.update(changed_files=[], changed_file_count=None, problem="历史副本保留；停止实际旧工作者后再派发。")
        row.pop("pending_worker_import", None)
        row["import_pending"] = worker.get("pending_worker_import") is not None
        rows.append(row)
    occupied = sum(w["status"] in OCCUPIED for w in rows)
    return {"team_id": team_id, "workers": rows, "max_parallel": policy["max_parallel"],
            "occupied_slots": occupied, "available_slots": max(0, policy["max_parallel"] - occupied),
            "enforced_limits": {"max_parallel": policy["max_parallel"], "worker_timeout_seconds": policy["timeout_seconds"],
                                "max_assignment_records": 128},
            "assurance": "Host-declared actual identities and work reports; file counts are observed drafts. "
                         "Native host capacity and total native dispatch/rework/wait usage are not enforced here. "
                         "Collection is not authoritative acceptance or independent evidence."}


def assert_submittable(service, data, task_id):
    for worker in records(data).values():
        if worker["task_id"] != task_id:
            continue
        if worker["status"] in OCCUPIED or worker.get("pending_worker_import") is not None:
            raise ContractError("Task has uncollected or occupied specialist workers; collect or stop actual writers first")
        if matches(data, worker) and worker["status"] == "STOPPED":
            _, changes = diff(service, data, worker)
            if changes:
                raise ContractError("Stopped worker still has uncollected draft files")
