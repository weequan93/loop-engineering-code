"""Explicit same-ledger approval routing; never grants an execution capability.

Only the official CLI reviewer decides individual approval requests. A successful
help probe means the route exists, not that a product operation is permitted.
"""

from copy import deepcopy
from pathlib import Path
import tempfile

from .contracts import ContractError
from .processes import execute
from .native_workers import text

MODES = {"never", "auto-review"}
MAX_UPDATES = 4


def mode(value):
    if not isinstance(value, str) or value not in MODES:
        raise ContractError("Supervisor approval mode must be never or auto-review")
    return value


def inspect_cli(driver, approval_mode):
    """Bounded local help only: no login, model, session or provider call."""
    mode(approval_mode)
    with tempfile.TemporaryDirectory(prefix="loop-cli-capability-") as directory:
        result = execute([driver.executable, "--help"], Path(directory), Path(directory) / "logs",
                         timeout=10, max_output_bytes=65536)
        help_text = result.stdout.read_text(encoding="utf-8", errors="replace")
    if result.outcome != "completed" or result.exit_code != 0:
        raise ContractError("CLI capability inspection failed; no host dispatch")
    required = {"--no-daemon", "--sandbox", "workspace-write", "--ask-for-approval"}
    if approval_mode == "auto-review":
        required.add("--approve-for-me")
    if any(flag not in help_text for flag in required):
        raise ContractError("CLI does not advertise the requested guarded approval route")
    return {"approval_mode": approval_mode, "sandbox": "workspace-write",
            "route_available": True, "execution_capability_verified": False,
            "basis": "Bounded local CLI help; actual approvals, process cleanup and checks remain pending"}


def update(supervisor, identity, update_id, expected_revision, expected_candidate,
           approval_mode, authorization, host_idle_confirmed):
    """CAS an operator-authorized runtime route at a collected checkpoint.

    No model-visible MCP tool: this is an operator lifecycle action, not a way
    for a worker to approve its own permissions. The journal remains signed.
    """
    mode(approval_mode)
    text(update_id, "runtime update ID", 64)
    text(authorization, "actual runtime authorization", 8192)
    text(expected_candidate, "expected current candidate", 256)
    if type(expected_revision) is not int or expected_revision < 1 or host_idle_confirmed is not True:
        raise ContractError("Runtime update requires the observed revision and actual stopped-writer confirmation")
    request = dict(update_id=update_id, expected_revision=expected_revision,
                   expected_candidate=expected_candidate, approval_mode=approval_mode,
                   authorization=authorization)
    with supervisor._run_lock():
        data = supervisor._data(identity)
        history = data.get("runtime_updates", [])
        previous = next((row for row in history if row["update_id"] == update_id), None)
        if previous:
            if any(previous.get(key) != value for key, value in request.items()):
                raise ContractError("Runtime update ID is bound to a different request")
            return {**data, "runtime_update_replayed": True}
        if (data["revision"] != expected_revision or data.get("pending") or data.get("process") or
                data["signal"] or data["status"] not in {"READY", "BLOCKED", "WAITING", "STALLED"} or
                data["deadline_epoch"] <= supervisor.clock()):
            raise ContractError("Runtime update requires the original unexpired collected supervisor")
        if len(history) >= MAX_UPDATES:
            raise ContractError("Bounded runtime update history exhausted")
        if supervisor.service.handshake()["restart_required"]:
            raise ContractError("Reconnect framework before runtime route update")
        stop = supervisor._external_stop(data)
        if stop:
            raise ContractError("Original project/team stop prevents runtime update")
        team = supervisor.service._data(data["team_id"], native=True)
        supervisor.service.operations.assert_idle(team)
        host = team["native_host"]
        if any(row["status"] not in {"STOPPED", "COLLECTED"} or row.get("pending_worker_import")
               for row in host.get("specialists", {}).values()):
            raise ContractError("Actual specialist writers/imports must be collected before runtime update")
        from .native_verification import assert_no_running
        for task_id, row in team["records"].items():
            assert_no_running(team, task_id)
            if not row.get("child_run_id"):
                continue
            child = supervisor.service.team._child(team, task_id)
            if (child.get("pending_process") or child.get("pending_edit") or
                    child.get("operation_started_ms") is not None or child["state"]["outstanding_action_ids"] or
                    any(a["status"] in {"RUNNING", "RESULT"}
                        for a in child.get("native", {}).get("execution_attempts", {}).values())):
                raise ContractError("Original child effects need reconciliation before runtime update")
        candidate = supervisor.service.team.snapshots.capture(
            supervisor.service.project, supervisor.service.team.profile(team))["digest"]
        if candidate != expected_candidate:
            raise ContractError("Runtime update candidate changed; inspect actual effects")
        from .native_supervisor import CodexSupervisorDriver
        driver = supervisor.driver or CodexSupervisorDriver(data["policy"]["executable"], data["policy"]["model"])
        capability = driver.preflight(approval_mode)
        # Help inspection consumes original lifetime; repeat stop/candidate/CAS gates.
        if supervisor.clock() >= data["deadline_epoch"] or supervisor._external_stop(data):
            raise ContractError("Original lifetime or stop changed during runtime preflight")
        latest_candidate = supervisor.service.team.snapshots.capture(
            supervisor.service.project, supervisor.service.team.profile(team))["digest"]
        if latest_candidate != expected_candidate:
            raise ContractError("Current candidate changed during runtime preflight")

        def apply(row):
            if row["revision"] != expected_revision or row.get("pending") or row.get("process") or row["signal"]:
                raise ContractError("Supervisor changed during runtime preflight")
            before = row["policy"].get("approval_mode", "never")
            row["policy"]["approval_mode"] = approval_mode
            row.setdefault("runtime_updates", []).append({**deepcopy(request), "previous_mode": before,
                "session_id": row["session_id"], "deadline_epoch": row["deadline_epoch"],
                "turns": row["turns"], "dispatch_ms": row["dispatch_ms"], "capability": capability})
            # This does not resume a supervisor, erase its blocker or modify a task.
            row["runtime_capability"] = capability

        return supervisor.journal.update(identity, "supervisor.runtime_route_updated", apply)
