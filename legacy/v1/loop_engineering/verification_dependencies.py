"""Conservative reuse of host observations, never relabeling formal evidence.

The accepted task declares the dependency graph. Every captured file must have
exactly one owner, otherwise no scenario is reusable. Semantics of the declared
graph need review; filesystem coverage alone cannot establish independence.
"""

from copy import deepcopy

from reference.core import canonical_digest
from .contracts import ContractError, validate, relative_path
from .workspace import matches


def validate_dependencies(task):
    graph = task.get("extensions", {}).get("verification_dependencies")
    scenarios = task.get("extensions", {}).get("verification_scenarios", [])
    if graph is None:
        if any(s.get("input_modules") for s in scenarios):
            raise ContractError("Scenario input modules require a declared verification dependency graph")
        return
    validate("verification-dependencies", graph)
    nodes = {n["id"]: n for n in graph["modules"]}
    if len(nodes) != len(graph["modules"]):
        raise ContractError("Verification dependency module IDs must be unique")
    for node in nodes.values():
        for path in node["paths"]:
            relative_path(path)
        if not set(node["depends_on"]).issubset(nodes):
            raise ContractError("Verification dependency module is missing")
    for scenario in scenarios:
        if not set(scenario.get("input_modules", [])).issubset(nodes):
            raise ContractError("Scenario references an unknown dependency module")
    pending = set(nodes)
    while pending:
        ready = {k for k in pending if not set(nodes[k]["depends_on"]) & pending}
        if not ready:
            raise ContractError("Verification module dependency cycle")
        pending -= ready


def bindings(task, snapshot, environment, runtime_environment):
    graph = task.get("extensions", {}).get("verification_dependencies")
    if graph is None:
        return {}, "No dependency graph declared; observations remain bound to the full candidate"
    nodes = {n["id"]: n for n in graph["modules"]}
    files = {key: [] for key in nodes}
    for item in snapshot["manifest"]["files"]:
        owners = [key for key, node in nodes.items() if matches(item["path"], node["paths"])]
        if len(owners) != 1:
            return {}, "Incomplete or ambiguous dependency coverage: " + item["path"]
        files[owners[0]].append(item)
    result = {}
    for scenario in task.get("extensions", {}).get("verification_scenarios", []):
        closure = set(scenario.get("input_modules", []))
        if not closure:
            continue
        pending = list(closure)
        while pending:
            for dependency in nodes[pending.pop()]["depends_on"]:
                if dependency not in closure:
                    closure.add(dependency); pending.append(dependency)
        result[scenario["id"]] = canonical_digest({"contract": task, "scenario": scenario,
            "environment": environment, "runtime_environment": runtime_environment,
            "modules": {key: sorted(files[key], key=lambda f: f["path"]) for key in sorted(closure)}})
    return result, None


def reuse(service, sessions, session):
    """Retain original attempt/candidate/artifacts and provenance, without a new pass."""
    reused = {}
    for scenario in session["scenarios"]:
        identity = scenario["id"]
        binding = session.get("input_bindings", {}).get(identity)
        if not binding:
            continue
        for old in reversed(list(sessions.values())):
            if (old["task_id"] != session["task_id"] or old["owner"] != session["owner"] or
                    old.get("input_bindings", {}).get(identity) != binding):
                continue
            attempts = [a for a in old["attempts"] if a["scenario_id"] == identity]
            latest = attempts[-1] if attempts else old.get("reused", {}).get(identity)
            # A subsequent actual failed/reconciled observation supersedes an older pass.
            if latest is None:
                continue
            if latest["status"] != "OBSERVED_PASS":
                break
            for artifact in latest["artifacts"]:
                # CAS read checks actual bytes against their digest.
                service.team.snapshots.read(artifact["sha256"])
            reused[identity] = {**deepcopy(latest), "reused_from": latest.get("reused_from") or
                {"session_id": old["id"], "candidate": old["candidate"], "attempt_id": latest["id"]}}
            break
    # A dependent scenario can only carry forward if all its prerequisite observations do.
    while True:
        invalid = {s["id"] for s in session["scenarios"] if s["id"] in reused and
                   not set(s["depends_on"]).issubset(reused)}
        if not invalid:
            return reused
        for identity in invalid:
            del reused[identity]
