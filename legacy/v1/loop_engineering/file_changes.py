"""Canonical bounded file payloads; path and task authority stay in the broker."""

import base64
import binascii
import re

from .contracts import ContractError

MAX_BINARY_BYTES = 1048576


def change_bytes(change):
    legacy = {"path", "expected_sha256", "new_content"}
    modern = {"path", "expected_sha256", "operation", "encoding", "content"}
    if type(change) is not dict or set(change) not in (legacy, modern):
        raise ContractError("Invalid file change fields")
    expected = change["expected_sha256"]
    if expected is not None and (type(expected) is not str or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected)):
        raise ContractError("Invalid file precondition hash")
    if set(change) == legacy:
        if type(change["new_content"]) is not str:
            raise ContractError("Legacy file content must be UTF-8 text")
        return change["new_content"].encode("utf-8")
    if change["operation"] == "delete":
        if expected is None or change["encoding"] is not None or change["content"] is not None:
            raise ContractError("Deletion requires an existing hash and null encoding/content")
        return None
    if change["operation"] != "write" or type(change["content"]) is not str:
        raise ContractError("A write requires a string content payload")
    if change["encoding"] == "utf-8":
        return change["content"].encode("utf-8")
    if change["encoding"] != "base64" or len(change["content"]) > 4 * ((MAX_BINARY_BYTES + 2) // 3):
        raise ContractError("Invalid encoding or binary payload exceeds 1 MiB")
    try:
        raw = base64.b64decode(change["content"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContractError("Binary content must be canonical base64") from exc
    if len(raw) > MAX_BINARY_BYTES or base64.b64encode(raw).decode("ascii") != change["content"]:
        raise ContractError("Binary content is noncanonical or exceeds 1 MiB")
    return raw


def proposal_version(task):
    version = task.get("extensions", {}).get("agent_step_version", "0.2")
    if version not in {"0.2", "0.3"}:
        raise ContractError("Unsupported task agent_step_version")
    return version


def task_change_bytes(change, task):
    if type(change) is not dict:
        raise ContractError("File change must be an object")
    modern = "operation" in change
    if modern != (proposal_version(task) == "0.3"):
        raise ContractError("File change shape differs from the negotiated task version")
    return change_bytes(change)


def change_from_bytes(path, expected, raw, version):
    if version == "0.2":
        if raw is None:
            raise ContractError("Deletion requires agent_step_version 0.3")
        try:
            return {"path": path, "expected_sha256": expected, "new_content": raw.decode("utf-8")}
        except UnicodeDecodeError as exc:
            raise ContractError("Binary integration requires agent_step_version 0.3") from exc
    content, encoding = None, None
    if raw is not None:
        try:
            content, encoding = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            content, encoding = base64.b64encode(raw).decode("ascii"), "base64"
    result = {"path": path, "expected_sha256": expected, "operation": "delete" if raw is None else "write",
              "encoding": encoding, "content": content}
    change_bytes(result)
    return result
