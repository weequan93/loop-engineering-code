"""Owned, journaled dependency setup and verified copies for check workspaces."""

import os
from pathlib import Path
import shutil
from uuid import uuid4

from .contracts import ContractError
from .execution_tools import configured_tools, input_identity, output_identity
from .workspace import safe_path


def ensure_dependencies(controller, data):
    root = Path(data["workspace"])
    config = configured_tools(root, data["profile"])
    if not config or not config["provision"]["steps"]:
        return
    if (data["task"]["autonomy"] == "unattended" or data.get("native", {}).get("config", {}).get("runtime", {}).get("backend", "local") != "local"):
        raise ContractError("Dependency provisioning requires the supervised local runtime; prepare protected-image dependencies separately")
    policy = config["provision"]
    before_inputs = input_identity(root, policy)
    record = data.get("provisioning")
    current_outputs = output_identity(root, policy, data["profile"])
    if record and record["status"] == "COMPLETE" and record["inputs"] == before_inputs:
        if current_outputs != record["outputs"]:
            raise ContractError("Installed dependency bytes changed; inspect and explicitly reconcile provisioning")
        return
    if record and record["status"] in {"RUNNING", "FAILED", "CANCELLED"}:
        raise ContractError("Dependency setup needs explicit reconciliation; inspect provisioning logs and action ID")
    history = [*data.get("provision_history", [])]
    if record:
        history.append(record)
    if len(history) >= 8:
        raise ContractError("Dependency setup attempt limit reached")
    source = controller.snapshots.capture(root, data["profile"])["digest"]
    record = {"id": "provision-" + uuid4().hex, "status": "RUNNING", "inputs": before_inputs,
              "source": source, "outputs": None, "steps": []}
    data.update(provisioning=record, provision_history=history)
    controller.store.save(data, "dependencies.setup_requested", {"id": record["id"]})
    for step in policy["steps"]:
        if controller.stop_signal(data) or controller.remaining_seconds(data) <= 0:
            record["status"] = "CANCELLED"
            controller.store.save(data, "dependencies.setup_stopped")
            raise ContractError("Dependency setup stopped by signal or deadline")
        cwd = root if step["cwd"] == "." else safe_path(root, step["cwd"])
        if not cwd.is_dir():
            raise ContractError("Dependency setup working directory is absent")
        logs = controller.store.run_dir(data["run_id"]) / (record["id"] + "-" + step["id"])
        result = controller.launch(data, step["argv"], cwd, logs,
                    timeout=min(step["timeout_seconds"], controller.remaining_seconds(data)),
                    kind="provision", max_output_bytes=10485760)
        record["action_id"] = data.get("last_process_action_id")
        record["steps"].append({"id": step["id"], "argv": step["argv"], "outcome": result.outcome,
                               "exit_code": result.exit_code, "elapsed_ms": result.elapsed_ms,
                               "stdout": str(result.stdout), "stderr": str(result.stderr)})
        controller.store.save(data, "dependencies.step_completed", record["steps"][-1])
        if controller.snapshots.capture(root, data["profile"])["digest"] != source or input_identity(root, policy) != before_inputs:
            record["status"] = "FAILED"
            controller.store.save(data, "dependencies.source_mutated")
            raise ContractError("Dependency setup modified source/lock inputs; inspect actual changes before resuming")
        if result.outcome != "completed" or result.exit_code != 0:
            record["status"] = "CANCELLED" if result.outcome == "cancelled" else "FAILED"
            controller.store.save(data, "dependencies.setup_failed")
            raise ContractError("Dependency setup did not complete successfully: " + step["id"])
    if any(not safe_path(root, name).is_dir() for name in policy["output_dirs"]):
        record["status"] = "FAILED"
        controller.store.save(data, "dependencies.outputs_missing")
        raise ContractError("Dependency setup did not create its declared output directories")
    record["outputs"] = output_identity(root, policy, data["profile"])
    record["status"] = "COMPLETE"
    controller.store.save(data, "dependencies.setup_completed", {"inputs": before_inputs, "outputs": record["outputs"]})


def copy_dependencies(source, target, config, profile):
    source, target = source.resolve(), target.resolve()
    if not config or not config["provision"]["steps"]:
        return None
    policy = config["provision"]
    before = output_identity(source, policy, profile)
    for name in policy["output_dirs"]:
        origin, destination = safe_path(source, name), safe_path(target, name)
        if not origin.is_dir() or destination.exists():
            raise ContractError("Dependency copy needs existing outputs and a fresh destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(origin, destination, symlinks=True)
        for directory, subdirs, names in os.walk(destination, followlinks=False):
            for item in [*subdirs, *names]:
                path = Path(directory) / item
                if path.is_symlink() and os.path.isabs(os.readlink(path)):
                    old_target = Path(os.readlink(path)).resolve()
                    if not old_target.is_relative_to(source):
                        raise ContractError("Dependency link escapes the source workspace")
                    path.unlink(); path.symlink_to(target / old_target.relative_to(source))
    after = output_identity(target, policy, profile)
    if after != before or output_identity(source, policy, profile) != before:
        raise ContractError("Dependencies changed during check workspace materialization")
    return after
