"""Pure controller decisions, not a runner, evidence collector, or sandbox.

Production callers must validate JSON Schemas and authenticate the task, runtime,
evidence records, artifacts, and gate context outside the implementer's workspace.
The defensive checks here do not replace those services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Iterable


@dataclass(frozen=True)
class Decision:
    outcome: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModeDecision:
    mode: str | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateContext:
    """Trusted CALLER inputs. Never construct this from agent assertions.

    verified_record_digests must come from a collector that authenticates record
    origin and checks artifact integrity. Independence is separately attested.
    """

    approved_contract_digest: str
    snapshot_digest: str
    environment_digest: str
    verified_record_digests: frozenset[str] = frozenset()
    independent_record_digests: frozenset[str] = frozenset()
    policy_clear: bool = False
    effects_reconciled: bool = False
    blocking_findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Usage:
    iterations: int = 0
    wall_seconds: int = 0
    stalled_iterations: int = 0
    repeated_failure_count: int = 0
    tokens: int | None = None
    cost_microunits: int | None = None
    cost_currency: str | None = None


def _check_json(value: Any) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > 9007199254740991:
            raise ValueError("v0.1 JSON integers must be exactly representable across backends")
        return
    if type(value) is str:
        value.encode("utf-8", errors="strict")
        return
    if type(value) is list:
        for item in value:
            _check_json(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            _check_json(key)
            _check_json(item)
        return
    raise ValueError("v0.1 canonical JSON accepts no floats or non-JSON values")


def canonical_digest(value: Any) -> str:
    """SHA-256 of the canonical integer-only JSON defined in verification.md."""
    _check_json(value)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def strict_json_loads(raw: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_number(raw_number: str) -> Any:
        raise ValueError(f"Non-integer JSON number: {raw_number}")

    value = json.loads(raw, object_pairs_hook=pairs, parse_float=reject_number,
                       parse_constant=reject_number)
    _check_json(value)
    return value


def validate_task_semantics(task: dict[str, Any]) -> None:
    """Cross-field checks in addition to (not instead of) JSON Schema validation."""
    if type(task) is not dict:
        raise ValueError("Task must be an object")
    if task.get("schema_version") not in {"0.1", "0.2"} or type(task.get("task_id")) is not str or not task["task_id"]:
        raise ValueError("Task needs a supported version and nonempty task ID")
    if type(task.get("revision")) is not int or task["revision"] < 1:
        raise ValueError("Task needs a positive revision")
    criteria, checks = task.get("criteria"), task.get("checks")
    if type(criteria) is not list or not criteria or type(checks) is not list or not checks:
        raise ValueError("Nonempty criteria and checks are required")
    if any(type(item) is not dict for item in criteria + checks):
        raise ValueError("Criteria and checks must be objects")
    criterion_ids = [item.get("id") for item in criteria]
    check_ids = [item.get("id") for item in checks]
    for label, ids in (("criterion", criterion_ids), ("check", check_ids)):
        if any(type(item) is not str or not item for item in ids):
            raise ValueError(f"Every {label} needs a nonempty ID")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate {label} ID")
    for criterion in criteria:
        refs = criterion.get("check_ids")
        if type(refs) is not list or not refs or any(type(ref) is not str for ref in refs):
            raise ValueError("Every criterion needs check references")
        if len(refs) != len(set(refs)) or not set(refs).issubset(check_ids):
            raise ValueError("Duplicate or unknown criterion check reference")
    for check in checks:
        kind = check.get("type")
        if kind not in {"command", "interaction", "review", "human", "artifact"}:
            raise ValueError("Unknown check type")
        if "independent" in check and type(check["independent"]) is not bool:
            raise ValueError("independent must be a Boolean")
        if check.get("independent") is True and kind != "review":
            raise ValueError("Only review checks can require independence")
        if kind == "command":
            argv = check.get("argv")
            if type(argv) is not list or not argv or any(type(x) is not str or not x for x in argv):
                raise ValueError("A command requires a nonempty argv array")
            if type(check.get("timeout_seconds")) is not int or check["timeout_seconds"] < 1:
                raise ValueError("A command requires a positive timeout")
            if "procedure" in check:
                raise ValueError("A command uses argv, not a procedure")
        else:
            procedure = check.get("procedure")
            if type(procedure) is not list or not procedure or any(type(x) is not str or not x for x in procedure):
                raise ValueError("A non-command requires a procedure")
            if "argv" in check or "timeout_seconds" in check:
                raise ValueError("A non-command cannot include command arguments")


def negotiate_mode(requested: str, adapter: dict[str, Any], runtime: dict[str, Any],
                   required_agent: Iterable[str] = (),
                   required_runtime: Iterable[str] = ()) -> ModeDecision:
    if requested not in {"manual", "assisted", "unattended"}:
        return ModeDecision(None, ("Unknown requested mode",))
    agent_caps = adapter.get("capabilities", {})
    runtime_caps = runtime.get("capabilities", {})
    if type(agent_caps) is not dict or type(runtime_caps) is not dict:
        return ModeDecision(None, ("Capabilities must be objects",))
    missing = [f"agent:{name}" for name in set(required_agent) | {"text_input"}
               if agent_caps.get(name) is not True]
    missing += [f"runtime:{name}" for name in set(required_runtime)
                if runtime_caps.get(name) is not True]
    if missing:
        return ModeDecision(None, tuple("Missing required capability " + x for x in sorted(missing)))
    if requested == "manual":
        return ModeDecision("manual")
    assisted = ("snapshot_capture", "trusted_evidence")
    unattended = assisted + ("bounded_execution", "isolated_workspace",
                             "policy_enforcement", "durable_state", "run_cancellation")
    reasons: list[str] = []
    if requested == "unattended":
        missing = [f"runtime:{name}" for name in unattended if runtime_caps.get(name) is not True]
        if agent_caps.get("headless_execution") is not True:
            missing.append("agent:headless_execution")
        if not missing:
            return ModeDecision("unattended")
        reasons.append("Unattended unavailable: " + ", ".join(missing))
    missing = [name for name in assisted if runtime_caps.get(name) is not True]
    if not missing:
        return ModeDecision("assisted", tuple(reasons))
    reasons.append("Assisted unavailable: runtime:" + ", runtime:".join(missing))
    return ModeDecision("manual", tuple(reasons))


def assess_completion(task: dict[str, Any], evidence: Iterable[dict[str, Any]],
                      context: GateContext) -> Decision:
    try:
        validate_task_semantics(task)
        contract_digest = canonical_digest(task)
    except (TypeError, ValueError, UnicodeError) as exc:
        return Decision("REJECT", ("Invalid task: " + str(exc),))
    if contract_digest != context.approved_contract_digest:
        return Decision("REJECT", ("Task differs from the authorized contract",))
    if context.policy_clear is not True:
        return Decision("REJECT", ("Candidate scope and policy are not cleared",))
    if context.effects_reconciled is not True:
        return Decision("REJECT", ("Outstanding effects are not reconciled",))
    if context.blocking_findings:
        return Decision("REJECT", ("Blocking review findings remain",))
    checks = {check["id"]: check for check in task["checks"]}
    selected: dict[str, dict[str, Any]] = {}
    record_digests: dict[str, str] = {}
    ids: set[str] = set()
    for record in evidence:
        if type(record) is not dict:
            return Decision("REJECT", ("Evidence must be an object",))
        check_id, evidence_id = record.get("check_id"), record.get("evidence_id")
        if type(check_id) is not str or check_id not in checks:
            return Decision("REJECT", ("Evidence references an unknown check",))
        if type(evidence_id) is not str or not evidence_id:
            return Decision("REJECT", ("Evidence needs an ID",))
        if check_id in selected or evidence_id in ids:
            return Decision("REJECT", ("Ambiguous duplicate evidence",))
        try:
            record_digest = canonical_digest(record)
        except (TypeError, ValueError, UnicodeError) as exc:
            return Decision("REJECT", ("Invalid evidence: " + str(exc),))
        if record_digest not in context.verified_record_digests:
            return Decision("REJECT", ("Evidence is not externally authenticated: " + check_id,))
        if record.get("task_id") != task.get("task_id"):
            return Decision("REJECT", ("Evidence belongs to a different task",))
        artifacts = record.get("artifacts")
        if type(artifacts) is not list or not artifacts or any(
            type(a) is not dict or type(a.get("uri")) is not str or not a["uri"]
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(a.get("sha256"))) for a in artifacts
        ):
            return Decision("REJECT", ("Evidence artifacts are missing or malformed",))
        try:
            started = datetime.fromisoformat(record["started_at"])
            finished = datetime.fromisoformat(record["finished_at"])
            if started.tzinfo is None or finished.tzinfo is None or finished < started:
                raise ValueError("Timestamps need offsets and ordered start/end")
        except (KeyError, TypeError, ValueError) as exc:
            return Decision("REJECT", ("Invalid evidence timestamps: " + str(exc),))
        ids.add(evidence_id)
        selected[check_id] = record
        record_digests[check_id] = record_digest
    stale_or_missing: list[str] = []
    failed: list[str] = []
    for check_id, check in checks.items():
        record = selected.get(check_id)
        if record is None:
            stale_or_missing.append("Missing evidence: " + check_id)
            continue
        expected = {"contract_digest": contract_digest, "check_digest": canonical_digest(check),
                    "snapshot_digest": context.snapshot_digest,
                    "environment_digest": context.environment_digest}
        mismatches = [key for key, value in expected.items() if record.get(key) != value]
        if mismatches:
            stale_or_missing.append("Stale evidence for " + check_id + ": " + ", ".join(mismatches))
            continue
        if check.get("independent") is True and record_digests[check_id] not in context.independent_record_digests:
            stale_or_missing.append("Independent review not attested: " + check_id)
            continue
        result = record.get("result")
        if type(result) is not str or result not in {"pass", "fail", "inconclusive"}:
            return Decision("REJECT", ("Invalid evidence result: " + check_id,))
        exit_code = record.get("exit_code")
        if "exit_code" not in record or (exit_code is not None and type(exit_code) is not int):
            return Decision("REJECT", ("Missing or invalid exit code: " + check_id,))
        if check["type"] != "command" and exit_code is not None:
            return Decision("REJECT", ("Non-command evidence has an exit code: " + check_id,))
        if result == "fail" or (check["type"] == "command" and result == "pass" and exit_code != 0):
            failed.append("Required check failed: " + check_id)
        elif result == "inconclusive":
            stale_or_missing.append("Inconclusive evidence: " + check_id)
    if failed:
        return Decision("REVISE", tuple(failed + stale_or_missing))
    if stale_or_missing:
        return Decision("NEED_EVIDENCE", tuple(stale_or_missing))
    return Decision("PASS")


def stop_decision(limits: dict[str, Any], usage: Usage, *, cancelled: bool = False) -> Decision | None:
    """Check observed counters. Dispatch-time reservations need a real broker."""
    if cancelled:
        return Decision("CANCELLED", ("User cancelled the run",))
    required = ("max_iterations", "max_wall_seconds", "max_stalled_iterations", "max_repeat_failure")
    optional = ("max_tokens", "max_cost_microunits")
    if any(type(limits.get(key)) is not int or limits[key] < 1 for key in required) or any(
        key not in limits or (limits[key] is not None and (type(limits[key]) is not int or limits[key] < 1))
        for key in optional
    ):
        return Decision("FAILED", ("Invalid budget configuration",))
    values = (usage.iterations, usage.wall_seconds, usage.stalled_iterations, usage.repeated_failure_count)
    if any(type(value) is not int or value < 0 for value in values) or any(
        value is not None and (type(value) is not int or value < 0)
        for value in (usage.tokens, usage.cost_microunits)
    ):
        return Decision("FAILED", ("Invalid usage counters",))
    caps = (("iterations", usage.iterations, limits["max_iterations"]),
            ("elapsed time", usage.wall_seconds, limits["max_wall_seconds"]),
            ("tokens", usage.tokens, limits["max_tokens"]))
    for name, value, cap in caps:
        if cap is not None and value is not None and value >= cap:
            return Decision("BUDGET_EXHAUSTED", (name + " limit reached",))
    if limits["max_tokens"] is not None and usage.tokens is None:
        return Decision("AWAITING_INPUT", ("Hard token limit requires supported accounting",))
    if limits["max_cost_microunits"] is not None:
        if usage.cost_microunits is None or usage.cost_currency != limits.get("cost_currency"):
            return Decision("AWAITING_INPUT", ("Hard cost limit requires accounting in the configured currency",))
        if usage.cost_microunits >= limits["max_cost_microunits"]:
            return Decision("BUDGET_EXHAUSTED", ("cost limit reached",))
    if usage.repeated_failure_count >= limits["max_repeat_failure"]:
        return Decision("STALLED", ("Repeated failure limit reached",))
    if usage.stalled_iterations >= limits["max_stalled_iterations"]:
        return Decision("STALLED", ("No verified progress at stall limit",))
    return None


def updated_stall_count(previous: int, observed_ids: frozenset[str],
                        already_seen_ids: frozenset[str]) -> int:
    """IDs must represent facts/progress authenticated by the controller."""
    return 0 if observed_ids - already_seen_ids else previous + 1


def updated_failure_count(previous_signature: str | None, new_signature: str | None,
                          previous_count: int) -> int:
    if new_signature is None:
        return 0
    return previous_count + 1 if new_signature == previous_signature else 1
