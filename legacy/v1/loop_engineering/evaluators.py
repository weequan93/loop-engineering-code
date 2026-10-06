"""Authenticated evaluator interchange for review, human, UI, and artifact checks.

The registered evaluator owns the judgment. The engine validates authority,
procedure identity, candidate, environment, findings, and immutable artifacts.
"""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from reference.core import canonical_digest
from .contracts import ContractError, validate
from .native_contracts import validate_native
from .store import utc_now
from .workspace import atomic_write, byte_digest


def make_request(data: dict, check: dict, snapshot: str, environment: str) -> dict:
    request = {"schema_version": "1.0", "request_id": "evaluation-" + uuid4().hex,
               "task_id": data["task"]["task_id"], "contract_digest": data["state"]["contract_digest"],
               "snapshot_digest": snapshot, "environment_digest": environment,
               "check_id": check["id"], "check_digest": canonical_digest(check),
               "context_id": "review-context-" + uuid4().hex, "procedure": check["procedure"], "created_at": utc_now()}
    validate_native("evaluator_request", request)
    return request


def sign_result(authorities, request_envelope: dict, key_id: str, *, result: str,
                summary: str, artifacts: list[Path], findings: list[dict]) -> dict:
    request = authorities.verify(request_envelope, "evaluation_request", key_id="controller", role="controller")
    validate_native("evaluator_request", request)
    if result not in {"pass", "fail", "inconclusive"} or not summary.strip():
        raise ContractError("Evaluator needs a real verdict and a concrete summary")
    for finding in findings:
        validate_native("finding", finding)
    if len({finding["id"] for finding in findings}) != len(findings):
        raise ContractError("Duplicate finding ID")
    if result == "pass" and any(f["severity"] == "blocking" for f in findings):
        raise ContractError("A blocking finding cannot accompany a passing review")
    refs = []
    for path in artifacts:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 10485760:
            raise ContractError("Evaluator artifact must be a bounded regular file")
        refs.append({"uri": path.resolve().as_uri(), "sha256": byte_digest(path.read_bytes())})
    record = {"schema_version": "0.1", "evidence_id": "evidence-" + uuid4().hex,
              **{name: request[name] for name in ("task_id", "contract_digest", "snapshot_digest", "environment_digest", "check_id", "check_digest")},
              "executor_run_id": request["context_id"], "started_at": request["created_at"], "finished_at": utc_now(),
              "result": result, "exit_code": None, "artifacts": refs, "summary": summary,
              "extensions": {"outcome": "evaluator", "request_id": request["request_id"],
                             "context_id": request["context_id"], "findings": findings}}
    validate("evidence", record)
    return authorities.sign(key_id, "evidence", record)


def admit_result(store, data: dict, envelope: dict, snapshot: str, environment: str) -> tuple[str, dict]:
    record = store.authorities.verify(envelope, "evidence")
    validate("evidence", record)
    check = next((check for check in data["task"]["checks"] if check["id"] == record["check_id"]), None)
    if not check or check["type"] == "command":
        raise ContractError("Evaluator cannot substitute command evidence")
    permitted = next((key for key in data["native"]["config"]["evaluator_keys"] if key["key_id"] == envelope["key_id"]), None)
    if not permitted or envelope["role"] != permitted["role"] or check["id"] not in permitted["check_ids"]:
        raise ContractError("Evaluator key is not registered for this check")
    expected_role = {"review": "reviewer", "human": "human", "interaction": "interaction", "artifact": "artifact"}[check["type"]]
    if envelope["role"] != expected_role:
        raise ContractError("Evaluator role differs from the required check type")
    request_id = record.get("extensions", {}).get("request_id")
    saved = data["native"]["evaluation_requests"].get(request_id)
    if saved is None or saved["consumed"]:
        raise ContractError("Evaluator request is absent or already consumed")
    request = saved["request"]
    bindings = {"task_id": data["task"]["task_id"], "contract_digest": data["state"]["contract_digest"],
                "snapshot_digest": snapshot, "environment_digest": environment,
                "check_id": check["id"], "check_digest": canonical_digest(check)}
    if any(record.get(name) != value or request.get(name) != value for name, value in bindings.items()):
        raise ContractError("Evaluator result is stale or belongs to another check/task")
    if (record["executor_run_id"] != request["context_id"]
            or record["extensions"].get("context_id") != request["context_id"]
            or request["context_id"] == data["native"]["implementer_context_id"]):
        raise ContractError("Evaluator did not use the requested separate context")
    findings = record["extensions"].get("findings", [])
    criteria = {item["id"] for item in data["task"]["criteria"]}
    for finding in findings:
        validate_native("finding", finding)
        if finding["criterion_id"] is not None and finding["criterion_id"] not in criteria:
            raise ContractError("Finding references an unknown criterion")
    if len({finding["id"] for finding in findings}) != len(findings):
        raise ContractError("Duplicate finding ID")
    if record["result"] == "pass" and any(f["severity"] == "blocking" for f in findings):
        raise ContractError("Unresolved blocking review finding")
    # Copy authenticated bytes into collector-owned storage. Re-signing is not
    # used to erase their origin: retain original attestation and an import map.
    imported = deepcopy(record)
    destination = store.run_dir(data["run_id"]) / "artifacts" / uuid4().hex
    from urllib.parse import unquote, urlparse
    for index, artifact in enumerate(record["artifacts"]):
        parsed = urlparse(artifact["uri"])
        source = Path(unquote(parsed.path))
        if parsed.scheme != "file" or parsed.netloc or source.is_symlink() or not source.is_file() or source.stat().st_size > 10485760:
            raise ContractError("Unsupported evaluator artifact")
        if source.resolve().is_relative_to(store.authorities.directory):
            raise ContractError("Authority material is not an evaluator artifact")
        raw = source.read_bytes()
        if byte_digest(raw) != artifact["sha256"]:
            raise ContractError("Evaluator artifact bytes changed after signing")
        target = destination / (str(index) + "-" + source.name)
        atomic_write(target, raw)
        imported["artifacts"][index]["uri"] = target.as_uri()
    # The collector authenticates the import and immutable copied URIs. The
    # original evaluator signature remains separately verifiable and is bound
    # to this imported record by its digest and the authenticated projection.
    imported["extensions"]["origin_digest"] = canonical_digest(record)
    digest = store.record(data["run_id"], imported)
    saved["consumed"] = True
    data["native"]["external_origins"][digest] = envelope
    data["native"]["external_by_check"][check["id"]] = digest
    return digest, imported
