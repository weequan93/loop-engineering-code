"""Integrated native engine: authority, contained checks, reservations, evaluators,
stages, provider-neutral context, revision/pause controls, and recovery.

Capability availability is measured on the deployment host. A local or unavailable
Docker backend cannot enable unattended operation.
"""

from copy import deepcopy
import json
import math
from pathlib import Path
import time
from uuid import uuid4
from functools import wraps
import threading

from reference.core import GateContext, canonical_digest, stop_decision
from .broker import Broker
from .budgets import BudgetError, Ledger
from .contracts import ContractError, load, validate_step
from .controller import ACTIVE, Controller
from .evaluators import admit_result, make_request
from .models import HTTPTransport, ModelDriver, ModelError
from .native_contracts import load_engine, validate_context
from .runtime import DockerRuntime, runtime_for
from .stages import active_stage, admit_stage, statuses, validate_stages
from .team_memory import byte_size, discard_task_optional
from .workspace import byte_digest
from .workspace import safe_path


def metered(method):
    """Charge top-level controller work once, including preparation/checkpoints.

    Process deadlines remain separately enforced. Crash gaps are conservatively
    charged by the durable operation journal; status inspection is read-only.
    """
    @wraps(method)
    def wrapper(self, run_id, *args, **kwargs):
        if getattr(self.meter, "active", False):
            return method(self, run_id, *args, **kwargs)
        started = time.monotonic_ns()
        before = self.store.get(run_id)["elapsed_ms"]
        self.meter.active = True
        try:
            result = method(self, run_id, *args, **kwargs)
        finally:
            self.meter.active = False
            with self.store.writer(run_id) as data:
                wall = max(1, (time.monotonic_ns() - started) // 1000000)
                extra = max(0, wall - (data["elapsed_ms"] - before))
                data["elapsed_ms"] += extra
                data["state"]["usage"]["wall_seconds"] = math.ceil(data["elapsed_ms"] / 1000)
                self.store.save(data, "budget.controller_work", {"operation": method.__name__, "additional_ms": extra})
        if type(result) is dict and result.get("run_id") == run_id and "state" in result:
            return self.store.get(run_id)
        return result
    return wrapper


class NativeController(Controller):
    def __init__(self, store, engine_path: Path, *, runtime=None, probe: dict | None = None):
        super().__init__(store)
        self.engine_path = engine_path.resolve()
        self.config = load_engine(self.engine_path)
        self.runtime = runtime or runtime_for(self.config["runtime"])
        self.probe_result = probe
        self.probe_elapsed_ms = 0
        self.broker = Broker()
        self.meter = threading.local()

    def runtime_capabilities(self) -> dict:
        if self.probe_result is None:
            started = time.monotonic_ns()
            self.probe_result = self.runtime.probe(self.store.directory / "runtime-probes")
            self.probe_elapsed_ms = max(1, (time.monotonic_ns() - started) // 1000000)
        return self.probe_result["capabilities"]

    def allow_unattended(self) -> bool:
        caps = self.runtime_capabilities()
        required = {"isolated_workspace", "trusted_evidence", "bounded_execution", "policy_enforcement", "run_cancellation", "durable_state"}
        return (isinstance(self.runtime, DockerRuntime) and self.config["runtime"]["backend"] == "docker"
                and self.probe_result["verified"] is True and all(caps.get(name) is True for name in required))

    def initialize_data(self, data: dict) -> None:
        self.runtime_capabilities()
        data["elapsed_ms"] += self.probe_elapsed_ms
        data["state"]["usage"]["wall_seconds"] = math.ceil(data["elapsed_ms"] / 1000)
        validate_stages(data["task"], self.config["stages"])
        checks = {c["id"]: c for c in data["task"]["checks"]}
        for key in self.config["evaluator_keys"]:
            if self.store.authorities.key(key["key_id"])["role"] != key["role"] or not set(key["check_ids"]).issubset(checks):
                raise ContractError("Evaluator registration does not match an available authority and task check")
        caps = ModelDriver(self.config["model"], self.config["response_limits"], None).capabilities
        if any(caps.get(name) is not True for name in data["task"]["required_capabilities"]["agent"]):
            raise ContractError("Native model driver lacks a required agent capability")
        data["native"] = {"schema_version": "1.0", "config": deepcopy(self.config),
                          "config_path": str(self.engine_path), "config_digest": canonical_digest(self.config),
                          "runtime_probe": deepcopy(self.probe_result), "reservations": [],
                          "current_model": deepcopy(self.config["model"]), "implementer_context_id": "implementer-" + uuid4().hex,
                          "stage_status": [], "evaluation_requests": {}, "external_by_check": {},
                          "external_origins": {}, "contract_history": [], "unmanaged_usage": False,
                          "instruction_paths": ["AGENTS.md", "LOOP.md", ".loop/agent-instructions.md"]}
        self._update_usage(data)
        verified = self.probe_result["verified"] is True
        requested = data["task"]["autonomy"]
        effective = requested if verified else "manual"
        data["state"]["extensions"].update(isolation=verified, effective_autonomy=effective,
                    supervision="operator" if effective == "manual" else "host",
                    downgrade_reasons=[] if effective == requested else ["Protected runtime conformance is unavailable"])

    def start(self, *args, **kwargs) -> dict:
        started = time.monotonic_ns()
        result = super().start(*args, **kwargs)
        with self.store.writer(result["run_id"]) as data:
            wall = max(1, (time.monotonic_ns() - started) // 1000000)
            data["elapsed_ms"] = max(data["elapsed_ms"], wall)
            data["state"]["usage"]["wall_seconds"] = math.ceil(data["elapsed_ms"] / 1000)
            if data["baseline"] and data["state"]["status"] == "PLANNING":
                data["state"]["environment_digest"] = self.environment_digest(data)
                records = self.store.evidence(data["run_id"], data["baseline"])
                passing = {r["check_id"] for r in records if r["result"] == "pass"
                           and r["snapshot_digest"] == data["state"]["snapshot_digest"]
                           and r["environment_digest"] == data["state"]["environment_digest"]}
                criteria = {c["id"] for c in data["task"]["criteria"] if set(c["check_ids"]).issubset(passing)}
                data["state"]["progress"]["passed_criteria"] = sorted(criteria)
                data["native"]["stage_status"] = statuses(data["native"]["config"]["stages"], criteria, data["state"]["snapshot_digest"])
            self.store.save(data, "preparation.completed", {"wall_ms": wall})
        return self.store.get(result["run_id"])

    @metered
    def resume(self, run_id: str) -> dict:
        return super().resume(run_id)

    @metered
    def verify(self, run_id: str) -> dict:
        return super().verify(run_id)

    def _assert_contract(self, data: dict) -> None:
        super()._assert_contract(data)
        if canonical_digest(load_engine(Path(data["native"]["config_path"]))) != data["native"]["config_digest"]:
            raise ContractError("Native configuration changed; use model-switch or an explicit new run")

    def spend_supported(self, data: dict) -> bool:
        return True  # Atomic reservation admission occurs before every native model dispatch.

    def decision_limits(self, data: dict) -> dict:
        # Read-only verification/finalization remains available at a spend boundary.
        # Ledger admission, rather than observed post-call totals, owns spend limits.
        return dict(data["task"]["limits"], max_tokens=None, max_cost_microunits=None)

    def _preflight(self, data: dict, **kwargs) -> bool:
        balance = Ledger(data["native"]["reservations"]).balance()
        if balance["violated_bound"]:
            self.checkpoint(data, "AWAITING_INPUT", "Provider exceeded a reserved bound; reconcile accounting")
            return False
        limits = data["task"]["limits"]
        if data["native"]["unmanaged_usage"] and (limits["max_tokens"] is not None or limits["max_cost_microunits"] is not None):
            self.checkpoint(data, "AWAITING_INPUT", "Unmanaged proposals have unknown spend; a hard cap cannot certify their usage")
            return False
        return super()._preflight(data, **kwargs)

    def environment_digest(self, data: dict) -> str:
        return canonical_digest({"config": data["native"]["config_digest"],
                                 "runtime": data["native"]["runtime_probe"]["identity"],
                                 "profile": data["profile_digest"], "checks": data["task"]["checks"],
                                 "repository_instructions": self._instructions(data),
                                 "local_environment": super().environment_digest(data)})

    def _instruction_paths(self, snapshot: dict) -> list[str]:
        paths = {"AGENTS.md", "LOOP.md", ".loop/agent-instructions.md"}
        for item in snapshot["manifest"]["files"]:
            parts = item["path"].split("/")
            for length in range(1, len(parts)):
                paths.add("/".join(parts[:length]) + "/AGENTS.md")
        return sorted(paths)

    def _instructions(self, data: dict) -> list[dict]:
        result = []
        for name in data["native"]["instruction_paths"]:
            path = safe_path(Path(data["workspace"]), name)
            if path.exists():
                if not path.is_file() or path.stat().st_size > data["profile"]["snapshot"]["max_file_bytes"]:
                    raise ContractError("Mandatory repository instruction is not a bounded regular file")
                raw = path.read_bytes()
                if raw:
                    result.append({"path": name, "sha256": byte_digest(raw), "content": raw.decode("utf-8")})
        return result

    def checkpoint(self, data: dict, status: str, reason: str | None, **kwargs) -> None:
        with self.timed_operation(data, "checkpoint"):
            super().checkpoint(data, status, reason, **kwargs)

    def _origin(self, data: dict, digest: str, record: dict) -> dict | None:
        envelope = data["native"]["external_origins"].get(digest)
        if envelope is None:
            return None
        original = self.store.authorities.verify(envelope, "evidence")
        permitted = next((key for key in data["native"]["config"]["evaluator_keys"] if key["key_id"] == envelope["key_id"]), None)
        if (not permitted or permitted["role"] != envelope["role"] or record["check_id"] not in permitted["check_ids"]
                or canonical_digest(original) != record["extensions"].get("origin_digest")):
            raise ContractError("Imported evaluator origin differs from registered authority")
        copied = deepcopy(record)
        copied["extensions"].pop("origin_digest", None)
        copied["artifacts"] = original["artifacts"]
        if copied != original or [a["sha256"] for a in record["artifacts"]] != [a["sha256"] for a in original["artifacts"]]:
            raise ContractError("Collector import altered evaluator judgment or artifact identity")
        return envelope

    def gate_context(self, data: dict, snapshot: str, environment: str, digests: list[str]) -> GateContext:
        independent, blocking = set(), []
        records = self.store.evidence(data["run_id"], digests)
        checks = {c["id"]: c for c in data["task"]["checks"]}
        for digest, record in zip(digests, records):
            if checks[record["check_id"]]["type"] != "command":
                origin = self._origin(data, digest, record)
                if not origin:
                    raise ContractError("Non-command evidence has no registered evaluator origin")
                if origin["role"] == "reviewer" and record["executor_run_id"] != data["native"]["implementer_context_id"]:
                    independent.add(digest)
            if record["snapshot_digest"] == snapshot and record["environment_digest"] == environment:
                blocking.extend(f["description"] for f in record["extensions"].get("findings", []) if f["severity"] == "blocking")
        balance = Ledger(data["native"]["reservations"]).balance()
        return GateContext(data["state"]["contract_digest"], snapshot, environment, frozenset(digests),
                           frozenset(independent), policy_clear=not balance["violated_bound"],
                           effects_reconciled=not data["state"]["outstanding_action_ids"],
                           blocking_findings=tuple(blocking) if not any(r["result"] == "fail" for r in records) else ())

    def collect_checks(self, data: dict, snapshot: dict, **kwargs) -> list[str]:
        data["native"]["instruction_paths"] = self._instruction_paths(snapshot)
        data["state"]["snapshot_digest"] = snapshot["digest"]
        digests = super().collect_checks(data, snapshot, **kwargs)
        environment = self.environment_digest(data)
        for digest in data["native"]["external_by_check"].values():
            record = self.store.evidence(data["run_id"], [digest])[0]
            if (record["snapshot_digest"] == snapshot["digest"] and record["environment_digest"] == environment
                    and record["contract_digest"] == data["state"]["contract_digest"]):
                self._origin(data, digest, record)
                digests.append(digest)
        return digests

    def external_status(self, data: dict, records: list[dict]) -> str | None:
        if any(r["result"] == "fail" for r in records):
            return None
        present = {r["check_id"] for r in records if r["result"] == "pass"}
        missing = [c for c in data["task"]["checks"] if c["type"] != "command" and c["id"] not in present]
        if not missing:
            return None
        return "REVIEWING" if all(c["type"] == "review" for c in missing) else "BLOCKED"

    def _progress(self, data: dict, records: list[dict], digests: list[str]) -> set[str]:
        result = super()._progress(data, records, digests)
        data["native"]["stage_status"] = statuses(data["native"]["config"]["stages"],
                set(data["state"]["progress"]["passed_criteria"]), data["state"]["snapshot_digest"])
        return result

    def prepare_proposal(self, data: dict, snapshot: dict, step: dict) -> list[dict]:
        if len(json.dumps(step, ensure_ascii=False).encode()) > data["native"]["config"]["response_limits"]["max_response_bytes"]:
            raise ContractError("Native proposal exceeds the configured interchange bound")
        stage = active_stage(data["native"]["config"]["stages"], data["native"]["stage_status"], snapshot["digest"])
        admit_stage(stage, step)
        data["state"]["snapshot_digest"] = snapshot["digest"]
        if step["changes"]:
            action = self.broker.authorize(data, self.broker.proposal(data, "workspace.apply", {"changes": step["changes"]}), actor_id="implementer")
            self.store.save(data, "action.authorized", action)
            data["native"]["pending_typed_edit"] = {"action_id": action["action_id"], "lease_token": action["lease_token"],
                    "paths": [c["path"] for c in step["changes"]], "step_digest": canonical_digest(step)}
        return super().prepare_proposal(data, snapshot, step)

    def edit_action_id(self, data: dict, step: dict) -> str:
        action = data["native"].get("pending_typed_edit")
        if step["changes"] and (not action or action["step_digest"] != canonical_digest(step)
                or action["lease_token"] != data["state"]["lease_token"]):
            raise ContractError("File effects need the current broker authorization")
        return action["action_id"] if action and step["changes"] else super().edit_action_id(data, step)

    def action_result(self, data: dict, action_id: str, outcome: str, effects: dict, artifacts: list[str], usage: dict) -> None:
        from .native_contracts import validate_native
        result = {"schema_version": "1.0", "action_id": action_id, "outcome": outcome,
                  "actual_effects": effects, "artifact_refs": artifacts, "known_usage": usage}
        validate_native("action_result", result)
        self.store.save(data, "broker.action_result", result)

    def _verify(self, data: dict) -> None:
        action = data["native"].get("pending_typed_edit")
        if action and data["pending_edit"] is None:
            self.action_result(data, action["action_id"], "succeeded", {"paths": action["paths"]}, [], {})
            data["native"]["pending_typed_edit"] = None
        super()._verify(data)

    @metered
    def submit(self, run_id: str, step: dict, **kwargs) -> dict:
        data = self.store.get(run_id)
        limits = data["task"]["limits"]
        if data.get("charged_step_digest") != canonical_digest(step):
            if limits["max_tokens"] is not None or limits["max_cost_microunits"] is not None:
                raise ContractError("Hard-cap native runs accept only their reserved native model proposals")
            with self.store.writer(run_id) as current:
                current["native"]["unmanaged_usage"] = True
                self.store.save(current, "usage.unmanaged_proposal")
        return super().submit(run_id, step, **kwargs)

    def launch(self, data: dict, argv: list[str], cwd: Path, logs: Path, *, timeout: float,
               kind: str, input_text: str | None = None, max_output_bytes: int | None = None):
        if kind == "agent":
            raise ContractError("Native runs use direct provider drivers or the manual file bridge")
        if kind != "check":
            return super().launch(data, argv, cwd, logs, timeout=timeout, kind=kind, input_text=input_text,
                                  max_output_bytes=max_output_bytes)
        current = data["current_check"]
        action = self.broker.authorize(data, self.broker.proposal(data, "check.run",
                    {"check_id": current["id"], "argv": argv, "cwd": current["cwd"]}), actor_id="controller")
        self.store.save(data, "action.authorized", action)
        if not isinstance(self.runtime, DockerRuntime):
            result = super().launch(data, argv, cwd, logs, timeout=timeout, kind=kind)
            self.action_result(data, action["action_id"], "succeeded" if result.outcome == "completed" and result.exit_code == 0
                               else "cancelled" if result.outcome == "cancelled" else "failed",
                               {"exit_code": result.exit_code}, [byte_digest(p.read_bytes()) for p in (result.stdout, result.stderr)],
                               {"elapsed_ms": result.elapsed_ms})
            return result
        name = self.runtime.name()
        owner = data["run_id"] + ":" + action["action_id"]
        data["pending_process"] = {"action_id": action["action_id"], "kind": "container", "pid": None,
                                   "identity": None, "container": name, "owner": owner}
        data["state"]["outstanding_action_ids"].append(action["action_id"])
        self.store.save(data, "process.requested", {"container": name, "owner": owner})
        def started(pid):
            data["pending_process"]["pid"] = pid
            self.store.save(data, "process.started", {"container": name})
        result = self.runtime.execute(argv, Path(current["workspace"]), current["cwd"], logs, timeout=timeout,
                    name=name, owner=owner, cancelled=lambda: self.stop_signal(data) is not None, on_started=started)
        data["pending_process"] = None
        data["state"]["outstanding_action_ids"].remove(action["action_id"])
        self.store.save(data, "process.completed", {"action_id": action["action_id"], "outcome": result.outcome})
        self.action_result(data, action["action_id"], "succeeded" if result.outcome == "completed" and result.exit_code == 0
                           else "cancelled" if result.outcome == "cancelled" else "failed",
                           {"exit_code": result.exit_code, "container_removed": True},
                           [byte_digest(p.read_bytes()) for p in (result.stdout, result.stderr)], {"elapsed_ms": result.elapsed_ms})
        return result

    def _recover(self, data: dict) -> None:
        pending = data.get("pending_process")
        if pending and pending["kind"] == "container":
            if not isinstance(self.runtime, DockerRuntime):
                raise ContractError("Recovery needs the original protected backend")
            try:
                self.runtime.reconcile(pending["container"], pending["owner"])
            except (OSError, ValueError) as exc:
                self.checkpoint(data, "AWAITING_INPUT", "Container effect is unresolved: " + str(exc))
                raise
            data["pending_process"] = None
            data["state"]["outstanding_action_ids"].remove(pending["action_id"])
            self.store.save(data, "container.reconciled")
        for row in data["native"]["reservations"]:
            if row["status"] == "held":
                row["status"] = "unknown"
        super()._recover(data)
        self._update_usage(data)
        self.store.save(data, "budget.recovery_reconciled")

    @metered
    def reconcile_process(self, run_id: str, action_id: str, note: str) -> dict:
        if not note.strip():
            raise ContractError("Operator reconciliation requires a concrete inspection note")
        process = self.store.get(run_id).get("pending_process")
        if not process or process.get("kind") != "container":
            return super().reconcile_process(run_id, action_id, note)
        with self.store.writer(run_id) as data:
            self._assert_contract(data)
            process = data.get("pending_process")
            if not process or process["action_id"] != action_id:
                raise ContractError("Action does not match the interrupted container")
            if not isinstance(self.runtime, DockerRuntime):
                raise ContractError("Recovery needs the original protected backend")
            # A note cannot waive an inaccessible daemon or a still-running,
            # unowned container. Retain the journal until removal is confirmed.
            self.runtime.reconcile(process["container"], process["owner"])
            data["pending_process"] = None
            data["state"]["outstanding_action_ids"].remove(action_id)
            data["selected_evidence"] = []
            self.store.save(data, "operator.reconciled_container", {"action_id": action_id, "note": note})
            return data

    def _context(self, data: dict) -> dict:
        bundle = super().context(data["run_id"])
        snapshot = self.snapshots.capture(Path(data["workspace"]), data["profile"])
        if snapshot["digest"] != bundle["base_snapshot_digest"]:
            raise ContractError("Workspace changed during context preparation; retry with a fresh context")
        data["native"]["instruction_paths"] = self._instruction_paths(snapshot)
        instructions = self._instructions(data)
        paths = {item["path"] for item in instructions}
        bundle["sources"] = [item for item in bundle["sources"] if item["path"] not in paths]
        findings = []
        environment = self.environment_digest(data)
        for digest in data["native"]["external_by_check"].values():
            record = self.store.evidence(data["run_id"], [digest])[0]
            self._origin(data, digest, record)
            for finding in record["extensions"].get("findings", []):
                findings.append({"evidence_digest": digest, "check_id": record["check_id"],
                    "snapshot_digest": record["snapshot_digest"], "environment_digest": record["environment_digest"],
                    "current": record["snapshot_digest"] == snapshot["digest"] and record["environment_digest"] == environment,
                    "finding": deepcopy(finding)})
        value = {"schema_version": "1.0", "context_id": data["native"]["implementer_context_id"],
                 "contract_digest": bundle["contract_digest"], "base_snapshot_digest": bundle["base_snapshot_digest"],
                 "bundle": bundle, "repository_instructions": instructions,
                 "active_stage": active_stage(data["native"]["config"]["stages"], data["native"]["stage_status"], snapshot["digest"]),
                 "budget": Ledger(data["native"]["reservations"]).balance(), "runtime": data["native"]["runtime_probe"],
                 "pending_findings": findings}
        while byte_size(value) > data["profile"]["context_max_bytes"]:
            if not discard_task_optional(value):
                raise ContractError("Mandatory task/state/repository instructions cannot fit the context limit")
        validate_context(value)
        return value

    @metered
    def context(self, run_id: str) -> dict:
        data = self.store.get(run_id)
        if data["operation_started_ms"] is not None:
            return self._context(data)
        with self.store.writer(run_id) as data:
            self._assert_contract(data)
            with self.timed_operation(data, "context"):
                return self._context(data)

    @metered
    def evaluation_request(self, run_id: str, check_id: str) -> dict:
        with self.store.writer(run_id) as data:
            self._assert_contract(data)
            if data["state"]["status"] in {"SUCCEEDED", "CANCELLED", "FAILED"}:
                raise ContractError("Request evaluation for an active/recoverable task")
            with self.timed_operation(data, "evaluation_request"):
                check = next((c for c in data["task"]["checks"] if c["id"] == check_id), None)
                if not check or check["type"] == "command":
                    raise ContractError("Request must identify a non-command check")
                snapshot = self.snapshots.capture(Path(data["workspace"]), data["profile"])
                request = make_request(data, check, snapshot["digest"], self.environment_digest(data))
                data["native"]["evaluation_requests"][request["request_id"]] = {"request": request, "consumed": False}
                self.store.save(data, "review.requested", {"request_id": request["request_id"], "check_id": check_id})
                return self.store.authorities.sign("controller", "evaluation_request", request)

    @metered
    def import_evaluation(self, run_id: str, envelope: dict) -> dict:
        from .native_contracts import validate_native
        validate_native("attestation", envelope)
        record = self.store.authorities.verify(envelope, "evidence")
        request_id = record.get("extensions", {}).get("request_id")
        if type(request_id) is not str or not request_id:
            raise ContractError("Evaluator result has no request identity")
        with self.store.writer(run_id) as data:
            self._assert_contract(data)
            if data["state"]["status"] in {"SUCCEEDED", "CANCELLED", "FAILED"}:
                raise ContractError("Terminal revision cannot import new completion evidence")
            with self.timed_operation(data, "evaluation_import"):
                snapshot = self.snapshots.capture(Path(data["workspace"]), data["profile"])
                data["state"]["snapshot_digest"] = snapshot["digest"]
                action = self.broker.authorize(data, self.broker.proposal(data, "evaluator.import",
                     {"request_id": request_id,
                      "evidence_digest": canonical_digest(envelope["payload"])}), actor_id="operator")
                self.store.save(data, "review.import_requested", action)
                digest, record = admit_result(self.store, data, envelope, snapshot["digest"], self.environment_digest(data))
                data["selected_evidence"] = []
                self.store.save(data, "review.recorded", {"digest": digest, "action": action})
                self.action_result(data, action["action_id"], "succeeded", {"evidence_digest": digest},
                                   [a["sha256"] for a in record["artifacts"]], {})
                return data

    @metered
    def execute_evaluation(self, run_id: str, check_id: str) -> dict:
        from .external_execution import execute_evaluation
        return execute_evaluation(self, run_id, check_id)

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

    @metered
    def amend(self, run_id: str, task_path: Path, note: str) -> dict:
        if not note.strip():
            raise ContractError("Authorized amendment needs a concrete operator note")
        task = load(task_path, "task")
        with self.store.writer(run_id) as data:
            if (canonical_digest(load(Path(data["profile_path"]), "project")) != data["profile_digest"]
                    or canonical_digest(load_engine(Path(data["native"]["config_path"]))) != data["native"]["config_digest"]):
                raise ContractError("An amendment cannot silently change the frozen profile or native configuration")
            if data["state"]["status"] in {"CANCELLED", "FAILED"}:
                raise ContractError("Cancelled/failed runs require an explicit new run")
            if data["state"]["status"] in ACTIVE or data["pending_edit"] or data["pending_process"]:
                raise ContractError("Pause/reconcile the task before an amendment")
            if task["task_id"] != data["task"]["task_id"] or task["revision"] != data["task"]["revision"] + 1:
                raise ContractError("Amendment must keep task ID and increment revision exactly once")
            if task["limits"]["cost_currency"] != data["task"]["limits"]["cost_currency"]:
                raise ContractError("A run cannot silently change accounting currency")
            if task["autonomy"] == "unattended" and not self.allow_unattended():
                raise ContractError("Amendment cannot grant unavailable unattended capability")
            if any(self.runtime_capabilities().get(c) is not True for c in task["required_capabilities"]["runtime"]):
                raise ContractError("Amendment requires an unavailable runtime capability")
            validate_stages(task, data["native"]["config"]["stages"])
            data["native"]["contract_history"].append({"revision": data["task"]["revision"], "digest": data["state"]["contract_digest"], "note": note})
            data.update(task=task, task_path=str(task_path.resolve()), selected_evidence=[], baseline=[], last_gate=None)
            data["state"].update(contract_digest=canonical_digest(task), status="PREPARING", pending_status=None)
            data["state"]["progress"]["passed_criteria"] = []
            data["native"].update(stage_status=[], external_by_check={}, evaluation_requests={}, external_origins={})
            self.store.clear_pause(run_id)
            self.store.save(data, "contract.revised", {"revision": task["revision"], "note": note})
            self.checkpoint(data, "PLANNING", "Authorized revision accepted; cumulative usage preserved")
            return data

    @metered
    def switch_model(self, run_id: str, model_config: dict) -> dict:
        candidate = deepcopy(self.config); candidate["model"] = model_config
        from .native_contracts import validate_native
        validate_native("engine", candidate)
        with self.store.writer(run_id) as data:
            self._assert_contract(data)
            if (data["state"]["status"] in ACTIVE - {"PLANNING"} or data["pending_edit"] or data["pending_process"]
                    or data["state"]["outstanding_action_ids"]):
                raise ContractError("Reconcile/pause a dispatch before switching models")
            capabilities = ModelDriver(model_config, data["native"]["config"]["response_limits"], None).capabilities
            if any(capabilities.get(c) is not True for c in data["task"]["required_capabilities"]["agent"]):
                raise ContractError("Model switch lacks a required agent capability")
            if model_config["pricing"] and model_config["pricing"]["currency"] != data["task"]["limits"]["cost_currency"]:
                raise ContractError("Model switch cannot mix budget currencies")
            data["native"]["current_model"] = deepcopy(model_config)
            data["native"]["implementer_context_id"] = "implementer-" + uuid4().hex
            self.store.save(data, "model.switched", {"provider": model_config["provider"], "model": model_config["model"]})
            return data

    def _update_usage(self, data: dict) -> None:
        balance = Ledger(data["native"]["reservations"]).balance()
        data["known_tokens"] = balance["known_tokens"]
        data["usage_complete"] = balance["actual_tokens"] is not None and not data["native"]["unmanaged_usage"]
        data["state"]["usage"].update(tokens=balance["actual_tokens"] if data["usage_complete"] else None,
                cost_microunits=balance["actual_cost_microunits"] if not data["native"]["unmanaged_usage"] else None)

    @metered
    def drive(self, run_id: str, *, turns: int = 1, transport=None) -> dict:
        if type(turns) is not int or turns < 1:
            raise ContractError("Turns must be a positive integer")
        for _ in range(turns):
            step = None
            with self.store.writer(run_id) as data:
                if data["state"]["status"] != "PLANNING" or not self._preflight(data, implementation=True, dispatch=True):
                    return data
                with self.timed_operation(data, "native_turn"):
                    limits = data["native"]["config"]["response_limits"]
                    config = data["native"]["current_model"]
                    driver = ModelDriver(config, limits, transport or HTTPTransport(self, data, config))
                    required = data["task"]["required_capabilities"]["agent"]
                    if any(driver.capabilities.get(c) is not True for c in required):
                        raise ContractError("Selected driver lacks required capabilities")
                    request_id = "request-" + uuid4().hex
                    reserved = False
                    try:
                        context = self._context(data)
                        payload = driver.prepare(context)
                        available = self.remaining_seconds(data) - self.verification_reserve(data)
                        if available <= 0:
                            raise BudgetError("No implementation time remains before verification reserve")
                        timeout = max(1, min(limits["timeout_seconds"], math.floor(available)))
                        quote = driver.quote(payload, timeout=timeout)
                        available = self.remaining_seconds(data) - self.verification_reserve(data)
                        if available < 1:
                            raise BudgetError("Token-count preparation consumed implementation time")
                        ledger = Ledger(data["native"]["reservations"])
                        ledger.reserve(request_id, quote, data["task"]["limits"], final_tokens=limits["final_reserve_tokens"],
                                       final_cost=limits["final_reserve_cost_microunits"])
                        reserved = True
                        data["state"]["iteration"] += 1
                        self._update_usage(data)
                        self.transition(data, "EXECUTING")
                        self.store.save(data, "budget.reserved", {"request_id": request_id, "quote": quote,
                                                               "adapter": driver.describe()})
                        response = driver.respond(payload, timeout=max(1, min(limits["timeout_seconds"], math.floor(available))))
                        usage = driver.usage(response)
                        ledger.settle(request_id, usage)
                        self._update_usage(data)
                        self.store.save(data, "budget.reconciled", {"request_id": request_id, "usage": usage})
                        if "usage_reporting" in required and usage is None:
                            raise ModelError("capability", "Required usage reporting is absent")
                        step = driver.step(response)
                        snapshot = self.snapshots.capture(Path(data["workspace"]), data["profile"])
                        validate_step(step, data["task"], snapshot["digest"])
                        if snapshot["digest"] != context["base_snapshot_digest"]:
                            raise ModelError("invalid_output", "Candidate changed while model response was outstanding")
                        if self._instructions(data) != context["repository_instructions"]:
                            raise ModelError("invalid_output", "Repository instructions changed during model dispatch")
                        self.prepare_proposal(data, snapshot, step)
                        data["charged_step_digest"] = canonical_digest(step)
                        data["adapter_failure"] = {"category": None, "count": 0}
                        stop, reason = "PLANNING", "Reserved native proposal ready for guarded application"
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        if reserved:
                            row = next(r for r in data["native"]["reservations"] if r["request_id"] == request_id)
                            if row["status"] == "held":
                                row["status"] = "unknown"
                            self._update_usage(data)
                        category = ("budget" if isinstance(exc, BudgetError) else exc.category if isinstance(exc, ModelError) else "invalid_output")
                        prior = data["adapter_failure"]
                        count = prior["count"] + 1 if prior["category"] == category else 1
                        data["adapter_failure"] = {"category": category, "count": count}
                        step = None
                        signal = self.stop_signal(data)
                        stop = signal or ("BUDGET_EXHAUSTED" if category == "budget" and ("Insufficient" in str(exc) or "time" in str(exc))
                                          else "AWAITING_INPUT" if category in {"authentication", "permission", "budget"}
                                          else "PLANNING" if category in {"invalid_output", "transient"} and count == 1 else "BLOCKED")
                        reason = str(exc)
                        self.store.save(data, "model.error", {"category": category, "request_id": request_id, "reserved": reserved})
                self.checkpoint(data, stop, reason)
            if step is not None:
                data = self.submit(run_id, step, agent_capabilities=driver.capabilities)
            if data["state"]["status"] != "PLANNING":
                return data
        return self.store.get(run_id)
