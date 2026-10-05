"""A local operator-supervised vertical slice of the native design.

It owns its edit protocol, journal, check artifacts, and completion decisions.
It does not isolate arbitrary programs or authenticate a different OS principal.
"""

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
import time
from uuid import uuid4

from reference.core import (GateContext, Usage, assess_completion, canonical_digest,
                            stop_decision, updated_failure_count, updated_stall_count)
from .contracts import ContractError, load, validate, validate_step
from .processes import execute, process_alive, process_group_alive, process_identity, terminate_group
from .project import local_capabilities
from .recovery import recovery_policy
from .store import Store, utc_now
from .workspace import Snapshots, byte_digest, matches, safe_path

ACTIVE = {"NEW", "PREPARING", "PLANNING", "EXECUTING", "VERIFYING", "REVIEWING", "CHECKPOINTING"}
TRANSITIONS = {
    "NEW": {"PREPARING"},
    "PREPARING": {"PLANNING", "VERIFYING", "CHECKPOINTING"},
    "PLANNING": {"EXECUTING", "VERIFYING", "CHECKPOINTING"},
    "EXECUTING": {"VERIFYING", "CHECKPOINTING"},
    "VERIFYING": {"CHECKPOINTING"},
    "REVIEWING": {"VERIFYING", "CHECKPOINTING"},
    "CHECKPOINTING": {"PLANNING", "REVIEWING", "SUCCEEDED", "AWAITING_INPUT", "BLOCKED", "STALLED", "BUDGET_EXHAUSTED", "PAUSED", "CANCELLED", "FAILED"},
    "AWAITING_INPUT": {"PREPARING"}, "BLOCKED": {"PREPARING"}, "STALLED": {"PREPARING"},
    "PAUSED": {"PREPARING"},
}


class Controller:
    def __init__(self, store: Store):
        self.store = store
        self.snapshots = Snapshots(store.directory)

    def runtime_capabilities(self) -> dict:
        return local_capabilities()

    def allow_unattended(self) -> bool:
        return False

    def initialize_data(self, data: dict) -> None:
        pass

    def edit_action_id(self, data: dict, step: dict) -> str:
        return "edit-" + uuid4().hex

    def decision_limits(self, data: dict) -> dict:
        return data["task"]["limits"]

    def spend_supported(self, data: dict) -> bool:
        return False

    def gate_context(self, data: dict, snapshot: str, environment: str, digests: list[str]) -> GateContext:
        return GateContext(data["state"]["contract_digest"], snapshot, environment, frozenset(digests),
                           policy_clear=True, effects_reconciled=not data["state"]["outstanding_action_ids"])

    def external_status(self, data: dict, records: list[dict]) -> str | None:
        return "BLOCKED" if any(c["type"] != "command" for c in data["task"]["checks"]) else None

    def stop_signal(self, data: dict) -> str | None:
        if self.store.cancelled(data["run_id"]):
            return "CANCELLED"
        if self.store.paused(data["run_id"]):
            return "PAUSED"
        return None

    def pause(self, run_id: str, reason: str) -> dict:
        self.store.pause(run_id, reason)
        try:
            with self.store.writer(run_id) as data:
                if data["state"]["status"] in ACTIVE:
                    self.checkpoint(data, "PAUSED", reason)
                return data
        except ContractError as exc:
            if "Another controller" not in str(exc) and "Another run" not in str(exc):
                raise
            return self.store.get(run_id)

    def transition(self, data: dict, status: str, reason: str | None = None) -> None:
        current = data["state"]["status"]
        if status not in TRANSITIONS.get(current, set()):
            raise ContractError(f"Illegal transition: {current} -> {status}")
        data["state"]["status"] = status
        data["state"]["reason"] = reason
        if status != "CHECKPOINTING":
            data["state"]["pending_status"] = None
        self.store.save(data, "state.changed", {"from": current, "to": status, "reason": reason})

    def _assert_contract(self, data: dict) -> None:
        if canonical_digest(load(Path(data["task_path"]), "task")) != data["state"]["contract_digest"]:
            raise ContractError("Task file changed since acceptance; start an explicitly revised task instead")
        if canonical_digest(load(Path(data["profile_path"]), "project")) != data["profile_digest"]:
            raise ContractError("Project profile changed since acceptance; previous snapshot policy is frozen")
        if "execution_tools_digest" in data:
            from .execution_tools import configured_tools
            config = configured_tools(Path(data["workspace"]), data["profile"])
            if (canonical_digest(config) if config else None) != data["execution_tools_digest"]:
                raise ContractError("Execution tool configuration changed since acceptance")
        if data.get("scenario_inputs_digest") is not None:
            from .scenarios import task_inputs_digest
            if task_inputs_digest(Path(data["workspace"])) != data["scenario_inputs_digest"]:
                raise ContractError("Scenario spec, answers or instructions changed since acceptance; prepare a new reviewed task")

    def _usage(self, data: dict) -> Usage:
        state = data["state"]
        return Usage(iterations=state["iteration"], wall_seconds=math.ceil(data["elapsed_ms"] / 1000),
                     stalled_iterations=state["stalled_iterations"], repeated_failure_count=state["repeated_failure_count"],
                     tokens=state["usage"]["tokens"], cost_microunits=state["usage"]["cost_microunits"],
                     cost_currency=state["usage"]["cost_currency"])

    def remaining_seconds(self, data: dict) -> float:
        active = max(0, time.time_ns() // 1000000 - data["operation_started_ms"]) if data["operation_started_ms"] is not None else 0
        return max(0.0, data["task"]["limits"]["max_wall_seconds"] - (data["elapsed_ms"] + active) / 1000)

    def verification_reserve(self, data: dict) -> int:
        checks = sum(check.get("timeout_seconds", 0) for check in data["task"]["checks"])
        return max(checks, data["task"]["limits"].get("verification_reserve_seconds", checks))

    def _preflight(self, data: dict, *, implementation: bool = False, dispatch: bool = False) -> bool:
        self._assert_contract(data)
        limits = data["task"]["limits"]
        # Final usage cannot provide a conservative reservation for another CLI
        # request. Reject a configured hard spend cap even when past usage is known.
        if (limits["max_tokens"] is not None or limits["max_cost_microunits"] is not None) and not self.spend_supported(data):
            self.checkpoint(data, "AWAITING_INPUT", "This runtime cannot reserve a hard token/cost budget")
            return False
        usage = self._usage(data)
        # A dispatched final iteration may still verify and finalize its result.
        # Do not dispatch another implementation after the iteration boundary.
        if not implementation and not dispatch:
            usage = Usage(wall_seconds=usage.wall_seconds, tokens=usage.tokens,
                          cost_microunits=usage.cost_microunits, cost_currency=usage.cost_currency)
        signal = self.stop_signal(data)
        if signal:
            self.checkpoint(data, signal, "Operator requested " + signal.lower())
            return False
        decision = stop_decision(self.decision_limits(data), usage)
        if decision:
            self.checkpoint(data, decision.outcome, "; ".join(decision.reasons))
            return False
        if implementation and self.remaining_seconds(data) <= self.verification_reserve(data):
            self.checkpoint(data, "BUDGET_EXHAUSTED", "Remaining time is reserved for required verification")
            return False
        return True

    @contextmanager
    def timed_operation(self, data: dict, kind: str):
        if data["operation_started_ms"] is not None:
            yield
            return
        started = time.monotonic_ns()
        data["operation_started_ms"] = time.time_ns() // 1000000
        self.store.save(data, "operation.started", {"kind": kind})
        try:
            yield
        finally:
            data["elapsed_ms"] += max(1, (time.monotonic_ns() - started) // 1000000)
            data["operation_started_ms"] = None
            data["state"]["usage"]["wall_seconds"] = math.ceil(data["elapsed_ms"] / 1000)
            self.store.save(data, "operation.completed", {"kind": kind})

    def start(self, root: Path, task_path: Path, profile_path: Path, *, baseline: bool = True,
              run_id: str | None = None) -> dict:
        root = root.resolve()
        if not root.is_dir():
            raise ContractError("Project directory must already exist")
        if self.store.directory.is_relative_to(root):
            raise ContractError("State store must be outside the implementer's project workspace")
        task, profile = load(task_path, "task"), load(profile_path, "project")
        scenario_digest = None
        from .scenarios import configured, status as scenario_status, task_inputs_digest
        if configured(root):
            setup = scenario_status(root, task)
            if not setup["ready_for_controller"]:
                raise ContractError("Scenario is " + setup["phase"] + "; the host coordinator must finish intake and prepare the task from .loop/start.md")
            scenario_digest = task_inputs_digest(root)
        if task.get("extensions", {}).get("scaffold"):
            raise ContractError("Replace the generated task scaffold before starting")
        if task["autonomy"] == "unattended" and not self.allow_unattended():
            raise ContractError("Local controller has no unattended isolation; choose manual or assisted supervision")
        unavailable = {name for name in task["required_capabilities"]["runtime"] if self.runtime_capabilities().get(name) is not True}
        if unavailable:
            raise ContractError("Required runtime capabilities unavailable: " + ", ".join(sorted(unavailable)))
        if not {"read_workspace", "run_checks"}.issubset(task["authorization"]["allowed_actions"]):
            raise ContractError("Task must authorize workspace reading and its required checks")
        run_id = run_id or "run-" + uuid4().hex
        self.store.run_dir(run_id)  # Validate a caller-reserved identity before persistence.
        state = {"schema_version": "0.1", "task_id": task["task_id"], "contract_digest": canonical_digest(task),
                 "status": "NEW", "pending_status": None, "snapshot_digest": None, "environment_digest": None,
                 "iteration": 0, "stalled_iterations": 0, "repeated_failure_count": 0, "event_sequence": 0,
                 "lease_token": 0, "checkpoint": None,
                 "progress": {"passed_criteria": [], "confirmed_fact_ids": [], "resolved_dependency_ids": []},
                 "usage": {"wall_seconds": 0, "tokens": None, "cost_microunits": None, "cost_currency": task["limits"]["cost_currency"]},
                 "outstanding_action_ids": [], "reason": None, "next_action": "Inspect baseline and select the smallest useful step.",
                 "updated_at": utc_now(), "extensions": {"supervision": "operator", "isolation": False,
                    "requested_autonomy": task["autonomy"], "effective_autonomy": "manual",
                    "downgrade_reasons": ["Local collector shares the implementer's OS authority"] if task["autonomy"] == "assisted" else []}}
        data = {"run_id": run_id, "workspace": str(root), "task_path": str(task_path.resolve()),
                "profile_path": str(profile_path.resolve()), "task": task, "profile": profile,
                "profile_digest": canonical_digest(profile), "state": state, "elapsed_ms": 0,
                "operation_started_ms": None, "pending_edit": None, "checkpoint_snapshot": None,
                "pending_process": None, "charged_step_digest": None,
                "known_tokens": 0, "usage_complete": True,
                "adapter_failure": {"category": None, "count": 0},
                "baseline": [], "selected_evidence": [], "seen_progress": [], "facts": {}, "last_failure": None,
                "steps": [], "last_gate": None, "last_recovery": None}
        if scenario_digest is not None:
            data["scenario_inputs_digest"] = scenario_digest
        from .execution_tools import configured_tools
        config = configured_tools(root, profile)
        data["execution_tools_digest"] = canonical_digest(config) if config else None
        self.initialize_data(data)
        self.store.create(data)
        with self.store.writer(run_id) as data:
            self.transition(data, "PREPARING")
            if not self._preflight(data):
                return data
            with self.timed_operation(data, "prepare"):
                from .provision import ensure_dependencies
                ensure_dependencies(self, data)
                snapshot = self.snapshots.capture(root, profile)
                data["checkpoint_snapshot"] = snapshot
                state = data["state"]
                state["snapshot_digest"] = snapshot["digest"]
                state["environment_digest"] = self.environment_digest(data)
                if baseline:
                    data["baseline"] = self.collect_checks(data, snapshot, baseline=True)
                    records = self.store.evidence(run_id, data["baseline"])
                    self._check_artifacts(records)
                    current = self.snapshots.capture(root, profile)
                    context = self.gate_context(data, current["digest"], self.environment_digest(data), data["baseline"])
                    decision = assess_completion(task, records, context)
                    data["last_gate"] = {"outcome": decision.outcome, "reasons": list(decision.reasons)}
            if self.stop_signal(data):
                self.checkpoint(data, self.stop_signal(data), "Stopped during baseline")
            elif baseline and decision.outcome == "PASS":
                data["selected_evidence"] = data["baseline"]
                state["environment_digest"] = context.environment_digest
                data["seen_progress"] = sorted(self._progress(data, records, data["baseline"]))
                self.checkpoint(data, "SUCCEEDED", "Baseline already satisfies every required check", expected_snapshot=context.snapshot_digest)
            else:
                stop = stop_decision(self.decision_limits(data), self._usage(data))
                self.checkpoint(data, stop.outcome if stop else "PLANNING",
                                "; ".join(stop.reasons) if stop else "Baseline saved; awaiting an agent step")
            return data

    def environment_digest(self, data: dict) -> str:
        executables = []
        import shutil
        for check in data["task"]["checks"]:
            if check["type"] == "command":
                name = check["argv"][0]
                if "/" in name and not Path(name).is_absolute():
                    cwd = Path(data["workspace"]) / check.get("cwd", ".")
                    candidate = cwd / name
                    resolved = str(candidate) if candidate.is_file() else None
                else:
                    resolved = shutil.which(name)
                item = {"command": check["argv"][0], "resolved": resolved}
                if resolved and Path(resolved).is_file():
                    item["sha256"] = byte_digest(Path(resolved).read_bytes())
                executables.append(item)
        # Relevant dependencies and fixtures are part of the full-tree snapshot.
        # External services and unlisted environment variables remain unsupported.
        from .execution_tools import dependency_identity, tool_runtime_identity
        return canonical_digest({"platform": platform.platform(), "python": sys.version,
                                 "profile_digest": data["profile_digest"], "executables": executables,
                                 "dependencies": dependency_identity(Path(data["workspace"]), data["profile"]),
                                 "execution_tools": tool_runtime_identity(Path(data["workspace"]), data["profile"])})

    def collect_checks(self, data: dict, snapshot: dict, *, baseline: bool = False) -> list[str]:
        records = []
        check_root = self.store.run_dir(data["run_id"]) / ("baseline-" if baseline else "verify-")
        check_root = check_root.with_name(check_root.name + uuid4().hex)
        self.snapshots.materialize(snapshot, check_root)
        from .execution_tools import configured_tools, output_identity
        from .provision import copy_dependencies
        config = configured_tools(Path(data["workspace"]), data["profile"])
        dependency_copy = copy_dependencies(Path(data["workspace"]), check_root, config, data["profile"])
        environment = self.environment_digest(data)
        for check in data["task"]["checks"]:
            if check["type"] != "command":
                continue
            remaining = self.remaining_seconds(data)
            if remaining <= 0 or self.stop_signal(data):
                break
            started = utc_now()
            logs = self.store.run_dir(data["run_id"]) / "artifacts" / uuid4().hex
            cwd_text = check.get("cwd", ".")
            cwd = check_root if cwd_text == "." else safe_path(check_root, cwd_text)
            data["current_check"] = {"id": check["id"], "workspace": str(check_root), "cwd": cwd_text}
            result = self.launch(data, check["argv"], cwd, logs, timeout=min(check["timeout_seconds"], remaining), kind="check")
            data["current_check"] = None
            after = self.snapshots.capture(check_root, data["profile"])
            mutated_inputs = after["digest"] != snapshot["digest"]
            if dependency_copy is not None and output_identity(check_root, config["provision"], data["profile"]) != dependency_copy:
                mutated_inputs = True
            verdict = "pass" if result.outcome == "completed" and result.exit_code == 0 else "fail"
            if result.outcome in {"timeout", "cancelled", "unavailable"} or mutated_inputs:
                verdict = "inconclusive"
            record = {"schema_version": "0.1", "evidence_id": "evidence-" + uuid4().hex,
                      "task_id": data["task"]["task_id"], "contract_digest": data["state"]["contract_digest"],
                      "snapshot_digest": snapshot["digest"], "environment_digest": environment,
                      "check_id": check["id"], "check_digest": canonical_digest(check),
                      "executor_run_id": "check-" + uuid4().hex, "started_at": started, "finished_at": utc_now(),
                      "result": verdict, "exit_code": result.exit_code,
                      "artifacts": [{"uri": path.as_uri(), "sha256": byte_digest(path.read_bytes())}
                                    for path in (result.stdout, result.stderr)],
                      "summary": f"{check['id']}: {result.outcome}, exit={result.exit_code}, input_mutation={mutated_inputs}",
                      "extensions": {"baseline": baseline, "outcome": result.outcome,
                                     "input_mutation": mutated_inputs, "elapsed_ms": result.elapsed_ms}}
            validate("evidence", record)
            digest = self.store.record(data["run_id"], record)
            records.append(digest)
            self.store.save(data, "evidence.recorded", {"digest": digest, "baseline": baseline})
            if mutated_inputs or result.outcome == "cancelled":
                break
        return records

    def checkpoint(self, data: dict, status: str, reason: str | None, *, expected_snapshot: str | None = None) -> None:
        snapshot = self.snapshots.capture(Path(data["workspace"]), data["profile"])
        if status == "SUCCEEDED" and snapshot["digest"] != expected_snapshot:
            status, reason = "PLANNING", "Candidate changed before final checkpoint; obtain fresh evidence"
            data["selected_evidence"] = []
            data["state"]["progress"]["passed_criteria"] = []
        data["checkpoint_snapshot"] = snapshot
        data["state"]["snapshot_digest"] = snapshot["digest"]
        data["state"]["checkpoint"] = str(self.store.run_dir(data["run_id"]) / "checkpoint.json")
        data["state"]["pending_status"] = status
        if data["state"]["status"] != "CHECKPOINTING":
            self.transition(data, "CHECKPOINTING", reason)
        path = Path(data["state"]["checkpoint"])
        from .workspace import atomic_write
        atomic_write(path, (json.dumps(snapshot, indent=2) + "\n").encode("utf-8"))
        self.store.save(data, "checkpoint.saved", {"snapshot_digest": snapshot["digest"], "next_status": status})
        self.transition(data, status, reason)

    def _recover(self, data: dict) -> None:
        if data["state"]["status"] == "NEW":
            self.transition(data, "PREPARING")
        if data["operation_started_ms"] is not None:
            # Crash-time work has unknown duration; charge the full observed gap
            # conservatively rather than resetting elapsed time after restart.
            data["elapsed_ms"] += max(0, time.time_ns() // 1000000 - data["operation_started_ms"])
            data["operation_started_ms"] = None
            data["state"]["usage"]["wall_seconds"] = math.ceil(data["elapsed_ms"] / 1000)
        process = data.get("pending_process")
        if process:
            if process.get("pid") is not None:
                identity = process_identity(process["pid"])
                if identity is None and (process_alive(process["pid"]) or process_group_alive(process["pid"])):
                    self.checkpoint(data, "AWAITING_INPUT", "Interrupted process is still present; ownership cannot be inspected")
                    raise ContractError("Reconcile the interrupted process before resuming")
                if identity is not None and process["identity"] is None:
                    self.checkpoint(data, "AWAITING_INPUT", "Interrupted process ownership cannot be confirmed")
                    raise ContractError("Reconcile the interrupted process before resuming")
                if identity is not None and identity != process["identity"]:
                    self.checkpoint(data, "AWAITING_INPUT", "PID now identifies a different process; do not terminate it")
                    raise ContractError("Reconcile the interrupted process before resuming")
                if identity is not None and identity == process["identity"]:
                    terminate_group(process["pid"])
            else:
                self.checkpoint(data, "AWAITING_INPUT", "Process dispatch had an unknown effect before its PID was recorded")
                raise ContractError("Reconcile the interrupted dispatch before resuming")
            if process["kind"] == "agent":
                data["usage_complete"] = False
                data["state"]["usage"]["tokens"] = None
            data["pending_process"] = None
            data["state"]["outstanding_action_ids"] = [item for item in data["state"]["outstanding_action_ids"]
                                                         if item != process["action_id"]]
            self.store.save(data, "process.reconciled", {"action_id": process["action_id"]})
        pending = data["pending_edit"]
        if pending:
            try:
                self.snapshots.apply_prepared(Path(data["workspace"]), data["task"], pending["changes"])
            except ContractError:
                data["last_recovery"] = recovery_policy("unknown_effect")
                self.checkpoint(data, "AWAITING_INPUT", "Interrupted edit conflicts with current files; reconcile before replay")
                raise
            data["pending_edit"] = None
            data["state"]["outstanding_action_ids"] = []
            self.store.save(data, "action.reconciled", {"action_id": pending["action_id"]})
        if data["state"]["status"] in {"EXECUTING", "VERIFYING", "REVIEWING", "PREPARING", "CHECKPOINTING"}:
            self.checkpoint(data, "PLANNING", "Interrupted work reconciled; previous evidence must be reverified")
            data["selected_evidence"] = []
        elif data["state"]["status"] in {"BLOCKED", "AWAITING_INPUT", "STALLED", "PAUSED"}:
            self.transition(data, "PREPARING")
            self.transition(data, "PLANNING")
        self.store.save(data, "recovery.completed")

    def resume(self, run_id: str) -> dict:
        with self.store.writer(run_id) as data:
            if data["state"]["status"] in {"SUCCEEDED", "CANCELLED", "BUDGET_EXHAUSTED", "FAILED"}:
                raise ContractError("Terminal run requires an explicit new/revised task; budgets are not reset on resume")
            self._assert_contract(data)
            self.store.clear_pause(run_id)
            self._recover(data)
            if self._preflight(data):
                self.checkpoint(data, "PLANNING", "Resume reconciled; current workspace inspected")
            return data

    def reconcile_process(self, run_id: str, action_id: str, note: str) -> dict:
        """Record a specific operator attestation for an unknown dispatch effect.

        This neither kills an unowned process nor accepts model-written evidence.
        The operator must inspect/stop the earlier action before calling it.
        """
        if not note.strip():
            raise ContractError("Operator reconciliation requires a concrete inspection note")
        with self.store.writer(run_id) as data:
            self._assert_contract(data)
            process = data.get("pending_process")
            provision = data.get("provisioning")
            setup = provision and provision.get("action_id", provision["id"]) == action_id
            external = next((row for row in data.get("native", {}).get("execution_attempts", {}).values()
                             if row.get("action_id", row["id"]) == action_id and row["status"] == "RUNNING"), None)
            if (process and process["action_id"] != action_id) or not (process or setup or external):
                raise ContractError("Action does not match the interrupted process")
            pid = process.get("pid") if process else None
            if pid is not None:
                identity = process_identity(pid)
                if ((identity is not None and identity == process["identity"] and process_group_alive(pid))
                        or (identity is None and (process_alive(pid) or process_group_alive(pid)))):
                    raise ContractError("Earlier process/group is still present; inspect and stop it before attesting")
            if process and process["kind"] == "agent":
                data["usage_complete"] = False
                data["state"]["usage"]["tokens"] = None
            if setup:
                if self.snapshots.capture(Path(data["workspace"]), data["profile"])["digest"] != provision["source"]:
                    raise ContractError("Restore the source changed by dependency setup before reconciling")
                provision.update(status="RETRY_ALLOWED", reconciliation_note=note)
            if external:
                external.update(status="RETRY_ALLOWED", reconciliation_note=note)
            data["pending_process"] = None
            data["state"]["outstanding_action_ids"] = [item for item in data["state"]["outstanding_action_ids"] if item != action_id]
            data["selected_evidence"] = []
            self.store.save(data, "operator.reconciled_process", {"action_id": action_id, "note": note})
            return data

    def context(self, run_id: str) -> dict:
        data = self.store.get(run_id)
        self._assert_contract(data)
        snapshot = self.snapshots.capture(Path(data["workspace"]), data["profile"])
        sources, used, omitted = [], 0, []
        priority = data["profile"]["context_paths"]
        files = sorted(snapshot["manifest"]["files"], key=lambda item: (item["path"] not in priority,
                       not matches(item["path"], data["task"]["scope"]["write_allow"]), item["path"]))
        limit = max(0, data["profile"]["context_max_bytes"] - len(json.dumps(data["task"]).encode("utf-8")) - 12000)
        for item in files:
            if item["kind"] != "file":
                omitted.append(item["path"])
                continue
            content = self.snapshots.read(item["sha256"])
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                omitted.append(item["path"])
                continue
            if used + len(content) > limit:
                omitted.append(item["path"])
                continue
            used += len(content)
            sources.append({"path": item["path"], "sha256": item["sha256"], "content": text})
        state = dict(data["state"], checkpoint=snapshot["digest"])
        context = {"schema_version": "0.2", "mode": "proposal_only", "run_id": run_id, "task": data["task"],
                "contract_digest": state["contract_digest"], "base_snapshot_digest": snapshot["digest"],
                "state": state, "sources": sources, "omitted_paths": omitted,
                "baseline": self._summaries(data, data["baseline"]),
                "recent_evidence": self._summaries(data, data["selected_evidence"]),
                "confirmed_facts": data["facts"], "failure_policy": data["last_recovery"],
                "previous_steps": data["steps"][-20:],
                "project": {"languages": data["profile"]["languages"], "conventions": data["profile"]["conventions"]},
                "context_policy": {"max_bytes": data["profile"]["context_max_bytes"],
                                   "omitted_source_count": len(omitted), "omitted_history_count": 0},
                "instructions": "Use task and repository conventions in proposal-only mode. The controller owns effects and handoff. Return one AgentStep matching task extensions.agent_step_version (default 0.2) and the supplied schema, with exact source digests. Version 0.3 permits explicit deletion and canonical base64 bytes. Never edit files, run tools, or delegate. Request verification; the controller decides success."}
        # Measure the actual compact UTF-8 interchange representation, including
        # JSON escaping and retained evidence. Never truncate the task contract.
        budget = data["profile"]["context_max_bytes"]
        size = lambda: len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        context["confirmed_facts"] = dict(context["confirmed_facts"])
        while size() > budget:
            if context["sources"]:
                context["omitted_paths"].append(context["sources"].pop()["path"])
                context["context_policy"]["omitted_source_count"] += 1
            elif context["baseline"]:
                context["baseline"].pop(0)
                context["context_policy"]["omitted_history_count"] += 1
            elif context["recent_evidence"]:
                context["recent_evidence"].pop(0)
                context["context_policy"]["omitted_history_count"] += 1
            elif context["confirmed_facts"]:
                context["confirmed_facts"].pop(next(iter(context["confirmed_facts"])))
                context["context_policy"]["omitted_history_count"] += 1
            elif context["previous_steps"]:
                context["previous_steps"].pop(0)
                context["context_policy"]["omitted_history_count"] += 1
            elif context["omitted_paths"]:
                context["omitted_paths"].pop()
            else:
                raise ContractError("Context limit cannot fit the mandatory task/state; increase context_max_bytes")
        return context

    def _summaries(self, data: dict, digests: list[str]) -> list[dict]:
        result = []
        records = self.store.evidence(data["run_id"], digests)
        self._check_artifacts(records)
        for digest, record in zip(digests, records):
            output = []
            for artifact in record["artifacts"]:
                from urllib.parse import unquote, urlparse
                path = Path(unquote(urlparse(artifact["uri"]).path))
                output.append(path.read_text(encoding="utf-8", errors="replace")[-4000:])
            result.append({"digest": digest, "check_id": record["check_id"], "result": record["result"],
                           "summary": record["summary"], "output_tail": "\n".join(output)})
        return result

    def prepare_proposal(self, data: dict, snapshot: dict, step: dict) -> list[dict]:
        """Admit proposed effects without touching the project; retain blobs."""
        from .file_changes import change_bytes
        if any(previous["step_id"] == step["step_id"] for previous in data["steps"]):
            raise ContractError("Step ID was already consumed; do not replay a completed proposal")
        if not set(step["evidence_refs"]).issubset(data["selected_evidence"] + data["baseline"]):
            raise ContractError("Step references evidence the controller did not collect")
        if step["intent"] != "act":
            return []
        if any(matches(change["path"], data["profile"]["snapshot"]["exclude"]) for change in step["changes"]):
            raise ContractError("The broker cannot edit inputs excluded from snapshot/verification")
        payload_sizes = {change["path"]: len(change_bytes(change) or b"") for change in step["changes"]}
        if any(size > data["profile"]["snapshot"]["max_file_bytes"] for size in payload_sizes.values()):
            raise ContractError("Proposed file exceeds snapshot limits")
        current_sizes = {item["path"]: item.get("size", 0) for item in snapshot["manifest"]["files"]}
        predicted = sum(current_sizes.values()) + sum(
            payload_sizes[change["path"]] - current_sizes.get(change["path"], 0)
            for change in step["changes"])
        if predicted > data["profile"]["snapshot"]["max_total_bytes"]:
            raise ContractError("Proposal would exceed the total snapshot byte limit")
        return self.snapshots.prepare_changes(Path(data["workspace"]), data["task"], step["changes"])

    def submit(self, run_id: str, step: dict, *, agent_capabilities: dict | None = None) -> dict:
        with self.store.writer(run_id) as data:
            if data["state"]["status"] not in {"PLANNING", "AWAITING_INPUT", "BLOCKED", "STALLED"}:
                raise ContractError("Run must be planning or resumable before submitting a step")
            self._assert_contract(data)
            available = agent_capabilities if agent_capabilities is not None else {"text_input": True, "structured_output": True}
            missing = {name for name in data["task"]["required_capabilities"]["agent"] if available.get(name) is not True}
            if missing:
                raise ContractError("Required agent capabilities unavailable: " + ", ".join(sorted(missing)))
            if data["state"]["status"] != "PLANNING":
                self._recover(data)
            if any(previous["step_id"] == step.get("step_id") for previous in data["steps"]):
                raise ContractError("Step ID was already consumed; do not replay a completed proposal")
            snapshot = self.snapshots.capture(Path(data["workspace"]), data["profile"])
            validate_step(step, data["task"], snapshot["digest"])
            if not set(step["evidence_refs"]).issubset(data["selected_evidence"] + data["baseline"]):
                raise ContractError("Step references evidence the controller did not collect")
            charged = canonical_digest(step) == data.get("charged_step_digest")
            if step["intent"] in {"need_input", "blocked"}:
                if not charged:
                    data["usage_complete"] = False
                    data["state"]["usage"]["tokens"] = None
                data["steps"].append({"step_id": step["step_id"], "summary": step["summary"], "intent": step["intent"]})
                data["state"]["next_action"] = step["blocker"]["resumption_condition"]
                self.checkpoint(data, "AWAITING_INPUT" if step["intent"] == "need_input" else "BLOCKED",
                                step["blocker"]["description"])
                return data
            if not self._preflight(data, implementation=step["intent"] == "act" and not charged, dispatch=not charged):
                return data
            if step["intent"] == "act":
                prepared = self.prepare_proposal(data, snapshot, step)
                if not charged:
                    data["state"]["iteration"] += 1
                    data["usage_complete"] = False
                    data["state"]["usage"]["tokens"] = None
                action_id = self.edit_action_id(data, step)
                data["pending_edit"] = {"action_id": action_id, "changes": prepared}
                data["state"]["outstanding_action_ids"] = [action_id]
                self.transition(data, "EXECUTING")
                self.store.save(data, "action.requested", {"action_id": action_id, "step_id": step["step_id"]})
                with self.timed_operation(data, "edit"):
                    self.snapshots.apply_prepared(Path(data["workspace"]), data["task"], prepared)
                    data["pending_edit"] = None
                    data["state"]["outstanding_action_ids"] = []
                    self.store.save(data, "action.completed", {"action_id": action_id})
            elif not charged:
                data["state"]["iteration"] += 1
                data["usage_complete"] = False
                data["state"]["usage"]["tokens"] = None
                self.store.save(data, "verification.requested", {"step_id": step["step_id"]})
            data["charged_step_digest"] = None
            data["steps"].append({"step_id": step["step_id"], "summary": step["summary"], "intent": step["intent"]})
            data["state"]["next_action"] = step["next_action"]
            self._verify(data)
            return data

    def verify(self, run_id: str) -> dict:
        with self.store.writer(run_id) as data:
            if data["state"]["status"] != "PLANNING":
                raise ContractError("Resume to planning before requesting fresh verification")
            if self._preflight(data):
                self._verify(data)
            return data

    def _verify(self, data: dict) -> None:
        if not self._preflight(data):
            return
        self.transition(data, "VERIFYING")
        with self.timed_operation(data, "verify"):
            from .provision import ensure_dependencies
            ensure_dependencies(self, data)
            snapshot = self.snapshots.capture(Path(data["workspace"]), data["profile"])
            digests = self.collect_checks(data, snapshot)
            records = self.store.evidence(data["run_id"], digests)
            self._check_artifacts(records)
            current = self.snapshots.capture(Path(data["workspace"]), data["profile"])
            context = self.gate_context(data, current["digest"], self.environment_digest(data), digests)
            decision = assess_completion(data["task"], records, context)
            data["selected_evidence"] = digests
            data["last_gate"] = {"outcome": decision.outcome, "reasons": list(decision.reasons)}
            current_records = [(digest, record) for digest, record in zip(digests, records)
                               if record["snapshot_digest"] == current["digest"] and record["environment_digest"] == context.environment_digest]
            data["state"]["environment_digest"] = context.environment_digest
            observed = self._progress(data, [record for _, record in current_records], [digest for digest, _ in current_records])
            data["state"]["stalled_iterations"] = updated_stall_count(data["state"]["stalled_iterations"],
                                                      frozenset(observed), frozenset(data["seen_progress"]))
            data["seen_progress"] = sorted(set(data["seen_progress"]) | set(observed))
        if self.stop_signal(data):
            self.checkpoint(data, self.stop_signal(data), "Operator stopped verification")
        elif decision.outcome == "PASS":
            self.checkpoint(data, "SUCCEEDED", "All required checks passed on the delivered candidate", expected_snapshot=context.snapshot_digest)
        else:
            reason = "; ".join(decision.reasons)
            status = "PLANNING"
            if self.external_status(data, records):
                status = self.external_status(data, records)
                reason += "; required non-command evidence needs an external evaluator"
            elif any(record["extensions"]["outcome"] == "unavailable" for record in records):
                status = "BLOCKED"
                data["last_recovery"] = recovery_policy("dependency")
            elif decision.outcome == "REJECT":
                status = "BLOCKED"
            breaker = stop_decision(self.decision_limits(data), self._usage(data))
            if breaker:
                status, reason = breaker.outcome, "; ".join(breaker.reasons)
            self.checkpoint(data, status, reason)

    def _check_artifacts(self, records: list[dict]) -> None:
        from urllib.parse import unquote, urlparse
        for record in records:
            for artifact in record["artifacts"]:
                parsed = urlparse(artifact["uri"])
                path = Path(unquote(parsed.path))
                if parsed.scheme != "file" or not path.resolve().is_relative_to(self.store.directory):
                    raise ContractError("Evidence artifact is outside the collector store")
                if byte_digest(path.read_bytes()) != artifact["sha256"]:
                    raise ContractError("Evidence artifact failed integrity verification")

    def _progress(self, data: dict, records: list[dict], digests: list[str]) -> set[str]:
        passed = {record["check_id"] for record in records if record["result"] == "pass"}
        criteria = [criterion["id"] for criterion in data["task"]["criteria"] if set(criterion["check_ids"]).issubset(passed)]
        data["state"]["progress"]["passed_criteria"] = criteria
        observed = {"criterion-" + name for name in criteria}
        signature = None
        for digest, record in zip(digests, records):
            if record["result"] != "pass":
                from urllib.parse import unquote, urlparse
                text = "\n".join(Path(unquote(urlparse(artifact["uri"]).path)).read_text(errors="replace") for artifact in record["artifacts"])
                text = re.sub(r"/[^\s\"]*/(?:verify|baseline)-[a-f0-9]+", "<workspace>", text)
                text = re.sub(r"\d+\.\d+s", "<elapsed>", text)
                fingerprint = canonical_digest({"check": record["check_id"], "output": text[-2000:], "outcome": record["extensions"]["outcome"]})
                fact_id = "failure-" + fingerprint[7:23]
                observed.add(fact_id)
                data["facts"][fact_id] = {"statement": record["summary"], "evidence_digest": digest}
                signature = signature or fingerprint
        data["state"]["progress"]["confirmed_fact_ids"] = sorted(data["facts"])
        data["state"]["repeated_failure_count"] = updated_failure_count(data["last_failure"], signature,
                                                           data["state"]["repeated_failure_count"])
        data["last_failure"] = signature
        if signature:
            data["last_recovery"] = recovery_policy("code")
        return observed

    def launch(self, data: dict, argv: list[str], cwd: Path, logs: Path, *, timeout: float,
               kind: str, input_text: str | None = None, max_output_bytes: int | None = None):
        action_id = "process-" + uuid4().hex
        data["pending_process"] = {"action_id": action_id, "kind": kind, "pid": None, "identity": None}
        data["state"]["outstanding_action_ids"].append(action_id)
        data["last_process_action_id"] = action_id
        if kind == "provision":
            data["provisioning"]["action_id"] = action_id
        if kind == "evaluator" and data.get("active_executor"):
            data["native"]["execution_attempts"][data["active_executor"]]["action_id"] = action_id
        self.store.save(data, "process.requested", {"action_id": action_id, "kind": kind})

        def started(pid: int) -> None:
            data["pending_process"].update(pid=pid, identity=process_identity(pid))
            self.store.save(data, "process.started", {"action_id": action_id})

        result = execute(argv, cwd, logs, timeout=timeout, input_text=input_text,
                         cancelled=lambda: self.stop_signal(data) is not None, on_started=started,
                         max_output_bytes=max_output_bytes,
                         env_overrides={"PYTHONDONTWRITEBYTECODE": "1"} if kind in {"check", "evaluator", "provision"} else None)
        data["pending_process"] = None
        data["state"]["outstanding_action_ids"].remove(action_id)
        self.store.save(data, "process.completed", {"action_id": action_id, "outcome": result.outcome})
        return result
