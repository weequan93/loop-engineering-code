"""Concurrent isolated drafts; original controllers integrate and verify serially."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from .contracts import ContractError, load, relative_path
from .native_workbench import diff, directory
from .team_automation import encode
from .workspace import atomic_write, safe_path, check_write_scope


def enabled(data):
    return "native_host" in data and bool(data["plan"].get("native_parallel"))


def validate_paths(plan):
    for task in plan["tasks"]:
        for path in task.get("draft_paths", []):
            relative_path(path)
            if any(c in path for c in "*?[") or path.split("/")[0].casefold() in {".loop", ".git"}:
                raise ContractError("Parallel draft ownership needs exact non-control file paths")


def can_admit(team, data, task_id):
    active = [key for key, row in data["records"].items() if key != task_id and
              row["status"] in {"RUNNING", "HANDOFF", "REJECTED"}]
    if not active:
        return True
    if any(data["records"][key]["status"] != "RUNNING" for key in active):
        return False
    policy = load(Path(data["workspace"]) / ".loop/team-policy.json", "team-policy")
    limit = min(policy["max_parallel"], data["plan"]["native_parallel"]["max_parallel"])
    if len(active) >= limit:
        return False
    paths = set(data["tasks"][task_id].get("draft_paths", []))
    def conflict(left, right):
        left, right = left.casefold(), right.casefold()
        return left == right or left.startswith(right + "/") or right.startswith(left + "/")
    return bool(paths) and all(data["tasks"][key].get("draft_paths") and
        not any(conflict(a, b) for a in paths for b in data["tasks"][key]["draft_paths"]) for key in active)


def assert_import(data, task_id, changes):
    if not enabled(data):
        return
    paths = data["tasks"][task_id].get("draft_paths")
    if paths is not None and any(c["path"] not in paths for c in changes):
        raise ContractError("Native proposal exceeds its frozen parallel draft ownership")
    if any(key != task_id and row["status"] in {"HANDOFF", "REJECTED"} for key, row in data["records"].items()):
        raise ContractError("Receive the preceding integrated handoff before another import")


def changed_paths(service, data, record):
    base = json.loads(service.team.snapshots.read(record["base"]))
    current = service.team.snapshots.capture(service.project, service.team.profile(data))
    old = {f["path"]: f for f in base["manifest"]["files"]}
    new = {f["path"]: f for f in current["manifest"]["files"]}
    return current, sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))


def refresh(service, team_id, task_id, workbench_id, reviewed_changes):
    """After host inspection, rebase a stable draft using an idempotent journal.

    Preserves the original child, wait deadline and counters. New context is
    explicit; old results do not become acceptance of the integrated candidate.
    """
    from .native_workers import assert_submittable
    from .native_verification import assert_no_running
    data = service._data(team_id, native=True)
    service.operations.assert_idle(data)
    if service.handshake()["restart_required"]:
        raise ContractError("Reconnect the changed framework before draft refresh")
    with service.team.teams.writer(team_id) as data:
        service.team._assert_inputs(data)
        if not enabled(data) or data["status"] != "ACTIVE" or service.team.teams.signal(team_id):
            raise ContractError("Draft refresh requires an active opted-in parallel team")
        host, record = data["native_host"], data["records"][task_id]
        if host.get("rework_pending") or host.get("stage_repair"):
            raise ContractError("Recover the existing repair before refreshing a draft")
        original = host.get("workbenches", {}).get(workbench_id)
        if not original or original["task_id"] != task_id:
            raise ContractError("Unknown task workbench")
        previous = next((r for r in host["workbenches"].values() if r.get("refreshed_from") == workbench_id), None)
        if previous:
            return {"team_id": team_id, "workbench": previous, "replayed": True}
        assert_submittable(service, data, task_id)
        assert_no_running(data, task_id)
        child = service.team._child(data, task_id)
        if (record["status"] != "RUNNING" or child["state"]["status"] != "PLANNING" or child.get("pending_edit") or
                child.get("pending_process") or child.get("selected_evidence") or
                any(a["status"] in {"RUNNING", "RESULT"} for a in child.get("native", {}).get("execution_attempts", {}).values())):
            raise ContractError("Only a quiescent, unverified planning draft can refresh")
        pending = host.get("refresh_pending")
        if pending and (pending["task_id"], pending["original"]) != (task_id, workbench_id):
            raise ContractError("Recover the original draft refresh first")
        current, changed = changed_paths(service, data, original)
        if type(reviewed_changes) is not list or sorted(set(reviewed_changes)) != changed:
            raise ContractError("Inspect and supply the exact changed project paths before refreshing")
        if pending is None:
            if not record.get("request") or record["request"]["id"] != original["request_id"] or original["status"] != "DRAFT":
                raise ContractError("Refresh needs the original unsubmitted request")
            count = sum(r.get("refreshed_from") is not None and r["task_id"] == task_id for r in host["workbenches"].values())
            if count >= data["plan"]["native_parallel"]["max_refreshes"] or len(host["workbenches"]) >= 64:
                raise ContractError("Frozen draft refresh allowance exhausted")
            candidate, changes = diff(service, data, original)
            assert_import(data, task_id, changes)
            # Conflicting edits are rejected together before writing even a new draft.
            prepared = service.team.snapshots.prepare_changes(service.project, child["task"], changes)
            parent = Path(tempfile.mkdtemp(prefix="loop-native-refresh-", dir=directory(original).parent.parent))
            pending = {"task_id": task_id, "original": workbench_id, "candidate": current["digest"],
                "base": service.team.snapshots.put(encode(current)), "prepared": prepared,
                "directory": str(parent / "project"), "id": "workbench-" + uuid4().hex,
                "host_wait": deepcopy(host["workers"][task_id]), "draft_candidate": candidate["digest"],
                "reviewed_changes": changed}
            host["refresh_pending"] = pending
            service.team.teams.save(data, "host.refresh_reserved")
        if current["digest"] != pending["candidate"]:
            raise ContractError("Project changed during draft refresh; preserve the original journal")
        target = Path(pending["directory"])
        if target.is_symlink() or target.resolve() != target:
            raise ContractError("Refresh directory was redirected")
        # Recover partial materialization without overwriting unrecognized files.
        manifest = {f["path"]: f for f in current["manifest"]["files"]}
        if not target.exists():
            service.team.snapshots.materialize(current, target)
        else:
            present = service.team.snapshots.capture(target, service.team.profile(data))
            desired = {c["path"]: c["new"] for c in pending["prepared"]}
            for item in present["manifest"]["files"]:
                old = manifest.get(item["path"])
                if item != old and item["sha256"] != desired.get(item["path"]):
                    raise ContractError("Refresh copy contains unknown changes")
            for name, item in manifest.items():
                if not safe_path(target, name).exists() and desired.get(name, "unchanged") is not None:
                    atomic_write(safe_path(target, name), service.team.snapshots.read(item["sha256"]), item["mode"])
        service.team.snapshots.apply_prepared(target, child["task"], pending["prepared"])
        controller = service.team._controller(data, task_id)
        if not pending.get("request_id"):
            controller.resume(child["run_id"])
            packet = service.team._decorate(data, task_id, controller.context(child["run_id"]))
            pending["request_id"] = packet["team_assignment"]["request_id"]
            service.team.teams.save(data, "host.refresh_context_prepared")
        if service.team.snapshots.capture(service.project, service.team.profile(data))["digest"] != pending["candidate"]:
            raise ContractError("Project changed during draft refresh; retain the journal")
        if service.team.teams.signal(team_id):
            raise ContractError("Team stopped during refresh; retain its journal")
        replacement = {"id": pending["id"], "task_id": task_id, "request_id": pending["request_id"],
            "directory": pending["directory"], "base": pending["base"], "status": "DRAFT", "refreshed_from": workbench_id}
        host["workbenches"][replacement["id"]] = replacement
        host["workers"][task_id] = {**pending["host_wait"], "request_id": pending["request_id"]}
        original["status"] = "REFRESHED"
        host.pop("refresh_pending")
        service.team.teams.save(data, "host.refresh_completed")
        return {"team_id": team_id, "workbench": replacement, "replayed": False}
