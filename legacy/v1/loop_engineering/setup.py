"""Explicit, reversible preset reinitialization; coding history is retained."""

import json
import os
from pathlib import Path
from uuid import uuid4

from .contracts import ContractError
from .project import initialize
from .team_store import TeamStore
from .workspace import atomic_write, safe_path


def reinitialize(project: Path, spec: Path, store, *, scenario="development"):
    project = project.resolve()
    if not project.is_dir() or store.directory.is_relative_to(project):
        raise ContractError("Reinitialization requires a project and private state outside it")
    content = spec.read_text(encoding="utf-8")
    if not content.strip() or len(content.encode()) > 2097152:
        raise ContractError("Provide nonempty UTF-8 requirements within 2 MiB")
    from .scenarios import PRESETS
    if scenario not in PRESETS:
        raise ContractError("Unknown scenario")
    teams = TeamStore(store)
    with store.workspace_writer(project):
        unfinished = [item["team_id"] for item in teams.runs()
                      if Path(item["workspace"]).resolve() == project
                      and item["status"] not in {"COMPLETE", "CANCELLED"}]
        if unfinished:
            raise ContractError("Cancel unfinished teams in this state before reinitializing: " + ", ".join(unfinished))
        related_runs = set()
        for item in teams.runs():
            if Path(item["workspace"]).resolve() != project:
                continue
            team = teams.get(item["team_id"])
            for record in team.get("records", {}).values():
                related_runs.update(run_id for run_id in (record.get("child_run_id"), record.get("integration_run_id")) if run_id)
            operations = team.get("automation", {}).get("operations", {})
            if any(op["status"] in {"ADMITTED", "RESPONDING"} or op.get("pid") for op in operations.values()):
                raise ContractError("Stop and reconcile owned team operations before reinitializing")
        for item in store.runs():
            child = store.get(item["run_id"])
            unresolved_setup = child.get("provisioning", {}).get("status") in {"RUNNING", "FAILED", "CANCELLED"}
            unresolved_execution = any(row["status"] == "RUNNING" for row in
                child.get("native", {}).get("execution_attempts", {}).values())
            relevant = Path(child["workspace"]).resolve() == project or item["run_id"] in related_runs
            if relevant and (child.get("pending_process") or child.get("pending_edit") or unresolved_setup or unresolved_execution):
                raise ContractError("Reconcile pending coding effects before reinitializing")
        control = safe_path(project, ".loop")
        if control.exists() and not control.is_dir():
            raise ContractError(".loop must be a regular directory")
        backup = store.directory / "setup-backups" / ("setup-" + uuid4().hex)
        backup.mkdir(parents=True, mode=0o700)
        saved_spec = backup / "requirements.md"
        saved_spec.write_text(content, encoding="utf-8")
        record = {"project": str(project), "scenario": scenario, "status": "PREPARED",
                  "previous_controls": control.exists(), "history": "retained in the supplied private state"}
        journal = backup / "setup.json"
        def save(status):
            record["status"] = status
            atomic_write(journal, (json.dumps(record, indent=2) + "\n").encode())
        save("PREPARED")
        if control.exists():
            os.replace(control, backup / ".loop")
        save("ARCHIVED")
        try:
            result = initialize(project, scenario=scenario, spec_file=saved_spec)
        except BaseException:
            if control.exists():
                os.replace(control, backup / "incomplete-controls")
            if (backup / ".loop").exists():
                os.replace(backup / ".loop", control)
            save("ROLLED_BACK")
            raise
        save("COMPLETE")
        return {**result, "backup_directory": str(backup), "automatic_dispatch": False,
                "prior_history_preserved": True,
                "next": "Review the fresh policy and start a new team; old team IDs and usage remain in private state."}
