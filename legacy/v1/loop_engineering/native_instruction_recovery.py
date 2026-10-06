"""Recover only exact published skill upgrades, retaining original drafts and limits.

This is administrative context reconciliation, not a new task, edit permission,
review attempt or parallel-policy opt-in. Actual writers remain host-supervised.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from .contracts import ContractError
from .host_install import PREVIOUS_SKILLS, SKILL
from .native_parallel import changed_paths
from .native_workbench import diff, directory
from .team_automation import encode
from .workspace import safe_path

PATHS = {".agents/skills/loop-engineering/SKILL.md", ".claude/skills/loop-engineering/SKILL.md"}
MAX_RECOVERIES = 4
KIND = "published_instruction"


def inspect(service, data, task_id, workbench_id):
    service.team._assert_inputs(data)
    from .native_project import assert_running
    from .native_verification import assert_no_running
    assert_running(service, data)
    service.operations.assert_idle(data)
    host = data["native_host"]
    record = data["records"].get(task_id)
    original = host.get("workbenches", {}).get(workbench_id)
    if data["status"] != "ACTIVE" or service.team.teams.signal(data["team_id"]):
        raise ContractError("Instruction reconciliation requires an active unstopped team")
    if not record or not original or original["task_id"] != task_id:
        raise ContractError("Unknown original instruction workbench")
    if host.get("stage_repair") or host.get("rework_pending"):
        raise ContractError("Recover the existing repair before instruction reconciliation")
    pending = host.get("refresh_pending")
    if pending and (pending.get("kind"), pending["task_id"], pending["original"]) != (KIND, task_id, workbench_id):
        raise ContractError("Recover the original refresh journal first")
    if original["status"] != "DRAFT" or not record.get("request"):
        raise ContractError("Instruction reconciliation needs the original unsubmitted draft")
    expected = {pending["request_id"], pending["old_request"]["id"]} if pending else {original["request_id"]}
    if record["request"]["id"] not in expected:
        raise ContractError("Original request was replaced outside instruction reconciliation")
    if any(key != task_id and row["status"] in {"RUNNING", "HANDOFF", "REJECTED"}
           for key, row in data["records"].items()):
        raise ContractError("Finish unrelated active tasks before serial instruction reconciliation")
    child = service.team._child(data, task_id)
    controller = service.team._controller(data, task_id)
    controller._assert_contract(child)
    if (record["status"] != "RUNNING" or child["state"]["status"] != "PLANNING" or
            controller.stop_signal(child) or child.get("pending_edit") or child.get("pending_process") or
            child.get("operation_started_ms") is not None or child.get("selected_evidence") or
            child["state"]["outstanding_action_ids"] or
            any(a["status"] in {"RUNNING", "RESULT"} for a in child.get("native", {}).get("execution_attempts", {}).values())):
        raise ContractError("Instruction reconciliation requires a quiescent unverified planning child")
    if controller.remaining_seconds(child) <= 0:
        raise ContractError("Original child time allowance is exhausted")
    assert_no_running(data, task_id)
    workers = [w for w in host.get("specialists", {}).values() if w["workbench_id"] == workbench_id]
    if any(w["status"] not in {"STOPPED", "COLLECTED"} or w.get("pending_worker_import") for w in workers):
        raise ContractError("Stop actual specialist writers and reconcile pending imports first")
    if any(w["status"] not in {"STOPPED", "COLLECTED"} or w.get("pending_worker_import")
           for w in host.get("specialists", {}).values() if w not in workers):
        raise ContractError("Unrelated specialist work is unresolved")
    current, changed = changed_paths(service, data, original)
    base = json.loads(service.team.snapshots.read(original["base"]))
    old_request = pending["old_request"] if pending else record["request"]
    if base["digest"] != old_request["snapshot"] or any(w["request_id"] != old_request["id"] for w in workers):
        raise ContractError("Original draft and specialist request provenance differs")
    old = {f["path"]: f for f in base["manifest"]["files"]}
    new = {f["path"]: f for f in current["manifest"]["files"]}
    published = "sha256:" + hashlib.sha256((SKILL / "SKILL.md").read_bytes()).hexdigest()
    if not changed or not set(changed).issubset(PATHS):
        raise ContractError("Only exact published skill paths may reconcile; unrelated source changes refuse")
    for name in changed:
        before, after = old.get(name), new.get(name)
        if (not before or not after or before["kind"] != "file" or after["kind"] != "file" or
                before["mode"] != after["mode"] or before["sha256"].removeprefix("sha256:") not in PREVIOUS_SKILLS or
                after["sha256"] != published):
            raise ContractError("Customized, missing, redirected or unpublished instructions refuse reconciliation")
    candidate, changes = diff(service, data, original)
    if any(c["path"] in changed for c in changes):
        raise ContractError("Coordinator draft modified the upgraded instructions")
    # Validate stopped drafts now, without importing them or discarding originals.
    drafts = {}
    for worker in workers:
        if worker["status"] == "COLLECTED":
            continue
        snapshot, edits = diff(service, data, worker)
        if any(c["path"] not in worker["write_paths"] or c["path"] in changed for c in edits):
            raise ContractError("Stopped specialist draft exceeds its original file ownership")
        drafts[worker["id"]] = snapshot["digest"]
    prepared = service.team.snapshots.prepare_changes(service.project, child["task"], changes)
    return {"team_id": data["team_id"], "task_id": task_id, "workbench_id": workbench_id,
            "candidate": current, "changed_paths": changed, "draft_candidate": candidate["digest"],
            "prepared": prepared, "worker_candidates": drafts,
            "instruction_changes": [{"path": name, "old": old[name]["sha256"], "new": new[name]["sha256"]} for name in changed]}


def reconcile(service, team_id, task_id, workbench_id, reviewed_changes=None, dry_run=False):
    if type(dry_run) is not bool:
        raise ContractError("Instruction dry_run must be a boolean")
    if service.handshake()["restart_required"]:
        raise ContractError("Reconnect the changed framework before instruction reconciliation")
    service._data(team_id, native=True)
    with service.team.teams.writer(team_id) as data:
        host = data["native_host"]
        prior = next((r for r in host.get("instruction_refresh_history", []) if r["original"] == workbench_id), None)
        if prior:
            if host["workbenches"][prior["replacement"]]["task_id"] != task_id:
                raise ContractError("Recovery belongs to another task")
            return {"team_id": team_id, "workbench": deepcopy(host["workbenches"][prior["replacement"]]),
                    "replayed": True, "provenance": deepcopy(prior)}
        report = inspect(service, data, task_id, workbench_id)
        if dry_run:
            return {k: deepcopy(report[k]) for k in ("team_id", "task_id", "workbench_id", "changed_paths",
                    "instruction_changes", "worker_candidates", "draft_candidate")} | {"eligible": True,
                "next_tool": "loop_workbench_reconcile_instructions", "arguments": {
                    "team_id": team_id, "task_id": task_id, "workbench_id": workbench_id,
                    "reviewed_changes": report["changed_paths"], "dry_run": False},
                "assurance": "Inspect exact hashes and stopped writers before reconciliation; no acceptance or new allowance"}
        if type(reviewed_changes) is not list or sorted(set(reviewed_changes)) != report["changed_paths"]:
            raise ContractError("Inspect and supply the exact changed instruction paths")
        original = host["workbenches"][workbench_id]
        pending = host.get("refresh_pending")
        if pending is None:
            if len(host.get("instruction_refresh_history", [])) >= MAX_RECOVERIES or len(host["workbenches"]) >= 64:
                raise ContractError("Bounded instruction reconciliation history exhausted")
            parent = Path(tempfile.mkdtemp(prefix="loop-native-instructions-", dir=directory(original).parent.parent)).resolve()
            pending = {"kind": KIND, "task_id": task_id, "original": workbench_id,
                "id": "workbench-" + uuid4().hex, "request_id": "request-" + uuid4().hex, "directory": str(parent / "project"),
                "candidate": report["candidate"]["digest"], "base": service.team.snapshots.put(encode(report["candidate"])),
                "prepared": report["prepared"], "draft_candidate": report["draft_candidate"],
                "worker_candidates": report["worker_candidates"], "reviewed_changes": reviewed_changes,
                "instruction_changes": report["instruction_changes"],
                "old_request": deepcopy(data["records"][task_id]["request"]),
                "host_wait": deepcopy(host["workers"][task_id])}
            host["refresh_pending"] = pending
            service.team.teams.save(data, "host.instruction_refresh_reserved")
        if (report["candidate"]["digest"] != pending["candidate"] or
                report["draft_candidate"] != pending["draft_candidate"] or
                report["worker_candidates"] != pending["worker_candidates"]):
            raise ContractError("Project or retained drafts changed during instruction reconciliation; preserve the journal")
        target = Path(pending["directory"])
        if target.is_symlink() or target.resolve() != target:
            raise ContractError("Instruction recovery copy was redirected")
        current = report["candidate"]
        manifest = {f["path"]: f for f in current["manifest"]["files"]}
        if not target.exists():
            service.team.snapshots.materialize(current, target)
        else:
            present = service.team.snapshots.capture(target, service.team.profile(data))
            desired = {c["path"]: c["new"] for c in pending["prepared"]}
            for item in present["manifest"]["files"]:
                if item != manifest.get(item["path"]) and not (item["kind"] == "file" and
                        item["mode"] == next((c["mode"] for c in pending["prepared"] if c["path"] == item["path"]), None) and
                        item["sha256"] == desired.get(item["path"])):
                    raise ContractError("Recovery copy contains unknown changes")
            # Partial materialization is recovered by the existing content-addressed bytes.
            from .workspace import atomic_write
            for name, item in manifest.items():
                if not safe_path(target, name).exists() and desired.get(name, "unchanged") is not None:
                    atomic_write(safe_path(target, name), service.team.snapshots.read(item["sha256"]), item["mode"])
        service.team.snapshots.apply_prepared(target, service.team._child(data, task_id)["task"], pending["prepared"])
        if not pending.get("context_prepared") and data["records"][task_id]["request"]["id"] != pending["request_id"]:
            controller = service.team._controller(data, task_id)
            child = controller.resume(data["records"][task_id]["child_run_id"])
            if child["state"]["status"] != "PLANNING":
                raise ContractError("Original child allowance or stop gate blocks fresh context")
            service.team._decorate(data, task_id, controller.context(child["run_id"]), request_id=pending["request_id"])
        if not pending.get("context_prepared"):
            pending["context_prepared"] = True
            service.team.teams.save(data, "host.instruction_refresh_context_prepared")
        directory({"directory": str(target)})
        expected_files = deepcopy(manifest)
        for change in pending["prepared"]:
            if change["new"] is None:
                expected_files.pop(change["path"], None)
            else:
                expected_files[change["path"]] = {"path": change["path"], "kind": "file", "mode": change["mode"],
                    "sha256": change["new"], "size": len(service.team.snapshots.read(change["new"]))}
        observed = service.team.snapshots.capture(target, service.team.profile(data))
        if {f["path"]: f for f in observed["manifest"]["files"]} != expected_files:
            raise ContractError("Recovery copy differs from the frozen original scoped edits")
        # Recheck every retained input after context preparation, before metadata commit.
        final = inspect(service, data, task_id, workbench_id)
        if any(final[k] != report[k] for k in ("candidate", "draft_candidate", "worker_candidates")):
            raise ContractError("Retained input changed before instruction recovery commit")
        if service.team.teams.signal(team_id):
            raise ContractError("Team stopped during instruction reconciliation")
        replacement = {"id": pending["id"], "task_id": task_id, "request_id": pending["request_id"],
            "directory": pending["directory"], "base": pending["base"], "status": "DRAFT",
            "instruction_reconciled_from": workbench_id}
        host["workbenches"][replacement["id"]] = replacement
        host["workers"][task_id] = {**pending["host_wait"], "request_id": pending["request_id"]}
        for worker in host.get("specialists", {}).values():
            if worker["workbench_id"] == workbench_id:
                worker.setdefault("instruction_rebindings", []).append({"workbench_id": workbench_id,
                    "request_id": worker["request_id"], "base": worker["base"]})
                worker.update(workbench_id=replacement["id"], request_id=pending["request_id"])
        original["status"] = "INSTRUCTION_RECONCILED"
        provenance = {k: deepcopy(pending[k]) for k in ("original", "old_request", "request_id", "candidate",
                      "draft_candidate", "worker_candidates", "instruction_changes", "host_wait")}
        provenance["replacement"] = replacement["id"]
        host.setdefault("instruction_refresh_history", []).append(provenance)
        host.pop("refresh_pending")
        service.team.teams.save(data, "host.instruction_refresh_completed")
        return {"team_id": team_id, "workbench": deepcopy(replacement), "replayed": False, "provenance": provenance}
