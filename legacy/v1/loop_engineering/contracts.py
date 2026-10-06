"""Strict interchange validation, shared by the CLI and adapter boundary."""

from pathlib import Path, PurePosixPath
from typing import Any

from reference.core import canonical_digest, strict_json_loads, validate_task_semantics

ROOT = Path(__file__).resolve().parents[1]


class ContractError(ValueError):
    pass


def validate(kind: str, value: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as exc:
        raise ContractError("Install requirements-dev.txt to use the CLI's schema validation") from exc
    version = value.get("schema_version") if isinstance(value, dict) else None
    name = f"{kind}-v0.2" if kind in {"task", "project", "step"} and version == "0.2" else kind
    if kind == "step" and version == "0.3":
        name = "step-v0.3"
    path = ROOT / "schemas" / f"{name}.schema.json"
    if not path.is_file():
        raise ContractError(f"Unsupported {kind} version: {version}")
    schema = strict_json_loads(path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
                    key=lambda error: str(list(error.absolute_path)))
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.absolute_path) or "root"
        raise ContractError(f"Invalid {kind} at {location}: {error.message}")
    canonical_digest(value)
    if kind == "readiness":
        from .readiness import assess_readiness
        assess_readiness(value)
    if kind == "scenario":
        from .scenarios import validate_manifest
        validate_manifest(value)
    if kind == "questions":
        from .scenarios import validate_questions
        validate_questions(value)
    if kind == "team":
        from .scenarios import validate_team
        validate_team(value)
    if kind == "team-workflow":
        from .team_engine import validate_workflow
        validate_workflow(value)
    if kind == "execution-tools":
        from .execution_tools import validate_tools
        validate_tools(value)
    if kind == "task":
        from .file_changes import proposal_version
        proposal_version(value)
        validate_task_semantics(value)
        from .native_readiness import validate_plan
        validate_plan(value)
        from .native_verification import validate_scenarios
        validate_scenarios(value)
        for check in value["checks"]:
            relative_path(check.get("cwd", "."), allow_dot=True)
            if check["type"] == "command" and any("\0" in item for item in check["argv"]):
                raise ContractError("Command arguments cannot contain a NUL byte")
    if kind == "project":
        for command in value["commands"].values():
            if command:
                relative_path(command["cwd"], allow_dot=True)
                if any("\0" in item for item in command["argv"]):
                    raise ContractError("Command arguments cannot contain a NUL byte")
        for path_text in value["context_paths"]:
            relative_path(path_text)


def load(path: Path, kind: str | None = None) -> dict[str, Any]:
    value = strict_json_loads(path.read_text(encoding="utf-8"))
    if kind:
        validate(kind, value)
    return value


def relative_path(text: str, *, allow_dot: bool = False) -> str:
    if not isinstance(text, str) or not text or "\\" in text or "\0" in text or ":" in text:
        raise ContractError("Use a normalized repository-relative POSIX path")
    path = PurePosixPath(text)
    if text == "." and allow_dot:
        return text
    if path.is_absolute() or any(part in {"", ".", ".."} for part in text.split("/")):
        raise ContractError(f"Unsafe relative path: {text}")
    return path.as_posix()


def validate_step(step: dict[str, Any], task: dict[str, Any], snapshot: str) -> None:
    validate("step", step)
    from .file_changes import change_bytes, proposal_version
    if step["schema_version"] != proposal_version(task):
        raise ContractError("Step version differs from the accepted task's agent_step_version")
    for change in step["changes"]:
        change_bytes(change)
    if step["task_id"] != task["task_id"] or step["contract_digest"] != canonical_digest(task):
        raise ContractError("Step belongs to a different or stale task contract")
    if step["base_snapshot_digest"] != snapshot:
        raise ContractError("Step was proposed for an earlier workspace snapshot")
    if not set(step["criterion_ids"]).issubset(item["id"] for item in task["criteria"]):
        raise ContractError("Step references an unknown criterion")
    paths = [relative_path(change["path"]) for change in step["changes"]]
    if len(paths) != len(set(paths)):
        raise ContractError("A step cannot change the same path twice")
    for left in paths:
        if any(right != left and right.startswith(left + "/") for right in paths):
            raise ContractError("A file cannot also be a parent directory in the same step")
    if step["intent"] != "act" and step["changes"]:
        raise ContractError("Only an act step may propose file changes")
    if step["intent"] in {"need_input", "blocked"} and step["blocker"] is None:
        raise ContractError("A waiting step requires a specific blocker and resumption condition")
    if step["intent"] not in {"need_input", "blocked"} and step["blocker"] is not None:
        raise ContractError("A working step cannot carry an unresolved blocker")


def step_schema(task: dict) -> dict:
    from .file_changes import proposal_version
    return load(ROOT / "schemas" / ("step-v" + proposal_version(task) + ".schema.json"))
