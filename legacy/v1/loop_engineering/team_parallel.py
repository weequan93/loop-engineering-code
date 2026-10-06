"""Private snapshot workspaces and journaled, preconditioned serial integration."""

from pathlib import Path
from uuid import uuid4

from .contracts import ContractError
from .scenarios import inputs, read_text
from .workspace import atomic_write, byte_digest, safe_path
from .file_changes import change_from_bytes, proposal_version


def isolate(team, data, task_id):
    record = data["records"][task_id]
    root = Path(data["workspace"])
    if "workspace" not in record:
        snapshot = team.snapshots.capture(root, team.profile(data))
        record.update(workspace=str(team.store.run_dir(data["team_id"]) / ("workspace-" + uuid4().hex)), base_snapshot=snapshot)
        team.teams.save(data, "team.workspace_reserved")
    target, snapshot = Path(record["workspace"]), record["base_snapshot"]
    if not target.exists():
        team.snapshots.materialize(snapshot, target)
    elif not team._exists(record["child_run_id"]):
        # Complete a partial materialization only from the saved manifest.
        for item in snapshot["manifest"]["files"]:
            path = safe_path(target, item["path"])
            if path.exists() and byte_digest(path.read_bytes()) != item["sha256"]:
                raise ContractError("Interrupted team workspace contains an unknown effect")
            if not path.exists():
                atomic_write(path, team.snapshots.read(item["sha256"]), item["mode"])
        if team.snapshots.capture(target, team.profile(data))["digest"] != snapshot["digest"]:
            raise ContractError("Interrupted team workspace differs from its reserved candidate")
    if not team._exists(record["child_run_id"]):
        _, docs = inputs(root)
        names = set(docs) | set(data["bound_files"]) | {".loop/task.json"}
        # Preserve excluded nested instructions as well as planned output paths.
        for document in team._instructions(data, task_id, snapshot):
            names.add(document["path"])
        for name in sorted(names):
            content = read_text(safe_path(root, name)).encode()
            path = safe_path(target, name)
            if path.exists() and path.read_bytes() != content:
                raise ContractError("Team workspace control/instruction bytes changed")
            if not path.exists():
                atomic_write(path, content)
    return target


def integrate(team, data, task_id):
    record = data["records"][task_id]
    child = team._child(data, task_id)
    if child["state"]["status"] != "SUCCEEDED":
        return
    controller = team._controller(data, task_id)
    controller._assert_contract(child)
    branch = team.snapshots.capture(Path(child["workspace"]), child["profile"])
    if branch["digest"] != child["state"]["snapshot_digest"]:
        raise ContractError("Parallel worker candidate changed after verification")
    root = Path(data["workspace"])
    if not record.get("integration_run_id"):
        old = {f["path"]: f for f in record["base_snapshot"]["manifest"]["files"]}
        new = {f["path"]: f for f in branch["manifest"]["files"]}
        changes = []
        for name in sorted(set(old) | set(new)):
            if old.get(name) == new.get(name):
                continue
            before, after = old.get(name), new.get(name)
            if (after and after["kind"] != "file" or before and before["kind"] != "file"
                    or before and after and before["mode"] != after["mode"]):
                raise ContractError("Parallel merge does not support symlink or mode changes")
            changes.append(change_from_bytes(name, before["sha256"] if before else None,
                           team.snapshots.read(after["sha256"]) if after else None, proposal_version(child["task"])))
        with team.store.workspace_writer(root):
            prepared = team.snapshots.prepare_changes(root, child["task"], changes)
        record.update(integration_run_id="run-" + uuid4().hex, pending_merge=prepared)
        team.teams.save(data, "team.merge_prepared")
    if record.get("pending_merge") is not None:
        with team.store.workspace_writer(root):
            team.snapshots.apply_prepared(root, child["task"], record["pending_merge"])
        record["pending_merge"] = None
        team.teams.save(data, "team.merge_applied")
    integrated = team._controller(data, task_id, verification=True)
    if not team._exists(record["integration_run_id"]):
        integrated.start(root, safe_path(root, data["tasks"][task_id]["task_file"]), root / ".loop/project.json",
                         baseline=False, run_id=record["integration_run_id"])
    result = team._child(data, task_id, verification=True)
    if result["state"]["status"] == "PLANNING":
        integrated.verify(result["run_id"])
