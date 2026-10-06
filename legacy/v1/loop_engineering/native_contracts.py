"""Validated native contracts; provider-specific objects stay at the boundary."""

from pathlib import Path
from functools import lru_cache

from reference.core import canonical_digest, strict_json_loads
from .contracts import ContractError, ROOT, relative_path, validate


@lru_cache(maxsize=None)
def _validator(kind: str):
    from jsonschema import Draft202012Validator, FormatChecker
    schema = strict_json_loads((ROOT / "schemas/native.schema.json").read_text())
    if kind not in schema["$defs"]:
        raise ContractError("Unknown native contract")
    selected = dict(schema, **{"$ref": "#/$defs/" + kind})
    return Draft202012Validator(selected, format_checker=FormatChecker())


def validate_native(kind: str, value: dict) -> None:
    errors = list(_validator(kind).iter_errors(value))
    if errors:
        raise ContractError("Invalid " + kind + ": " + errors[0].message)
    canonical_digest(value)


def load_engine(path: Path) -> dict:
    config = strict_json_loads(path.read_text(encoding="utf-8"))
    validate_native("engine", config)
    if config["runtime"]["backend"] == "docker" and config["runtime"]["image"] is None:
        raise ContractError("Docker execution requires an explicit immutable image identity")
    ids = [item["key_id"] for item in config["evaluator_keys"]]
    if len(ids) != len(set(ids)):
        raise ContractError("Evaluator key IDs must be unique")
    return config


def validate_context(value: dict) -> None:
    validate_native("context", value)
    bundle = value["bundle"]
    validate("task", bundle["task"])
    validate("state", bundle["state"])
    if value["contract_digest"] != canonical_digest(bundle["task"]):
        raise ContractError("Context contract identity differs from its task")
    if (bundle["contract_digest"] != value["contract_digest"]
            or bundle["state"]["contract_digest"] != value["contract_digest"]
            or bundle["state"]["task_id"] != bundle["task"]["task_id"]):
        raise ContractError("Context state/bundle identities differ from the accepted task")
    if value["base_snapshot_digest"] != bundle["base_snapshot_digest"]:
        raise ContractError("Context snapshot identities disagree")
    from .workspace import byte_digest
    for items in (bundle["sources"], value["repository_instructions"]):
        if len({item["path"] for item in items}) != len(items):
            raise ContractError("Duplicate context source/instruction path")
        for item in items:
            relative_path(item["path"])
            if byte_digest(item["content"].encode("utf-8")) != item["sha256"]:
                raise ContractError("Context content differs from its supplied byte hash")
