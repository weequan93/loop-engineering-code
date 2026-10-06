"""Implementation inventory and the project's live-evaluation prerequisite.

This is a reviewed project checklist, not a runtime capability attestation or
proof that tests passed. It performs no subprocess, provider, or network calls.
"""

from pathlib import Path

from reference.core import strict_json_loads
from .contracts import ContractError, ROOT, relative_path

REQUIRED_ITEMS = frozenset({
    "task_contracts", "proposal_bridge", "completion_gate", "snapshots_recovery",
    "bounded_processes", "lifecycle_control", "protected_runtime", "typed_broker",
    "authenticated_evidence", "hard_spend_reservations", "native_model_drivers",
    "external_evaluators", "staged_workflow", "replayable_events",
    "context_handoff", "offline_conformance", "scenario_presets", "team_execution", "native_host_supervisor",
})
ITEM_FIELDS = {"id", "title", "definition", "implementation", "offline_coverage",
               "definition_refs", "implementation_refs", "test_refs", "remaining_work"}
STATUSES = {"complete", "partial", "missing"}
POLICY = {
    "live_agent_model_tests": "deferred_until_implementation_complete",
    "automatic_live_dispatch": False,
}


def assess_readiness(inventory: dict, *, root: Path = ROOT) -> dict:
    """Fail closed on omitted requirements, unknown states, or broken references.

    Completion is the maintainer's declaration backed by source/test references.
    A live evaluation additionally needs a current offline test result and an
    operator dispatch; this checker neither runs tests nor starts a backend.
    """
    if (type(inventory) is not dict
            or set(inventory) != {"schema_version", "target", "test_policy", "items"}
            or inventory["schema_version"] != "0.1" or inventory["target"] != "1.0"
            or inventory["test_policy"] != POLICY
            or inventory["test_policy"]["automatic_live_dispatch"] is not False):
        raise ContractError("Invalid implementation inventory or live-test policy")
    items = inventory["items"]
    if type(items) is not list or any(type(item) is not dict for item in items):
        raise ContractError("Readiness items must be objects")
    ids = [item.get("id") for item in items]
    if any(type(value) is not str for value in ids) or len(ids) != len(set(ids)):
        raise ContractError("Readiness item IDs must be unique strings")
    if set(ids) != REQUIRED_ITEMS:
        raise ContractError("Readiness inventory must include every required implementation item")
    root = root.resolve()
    remaining = []
    for item in items:
        if set(item) != ITEM_FIELDS:
            raise ContractError("Unknown or missing readiness item fields")
        if type(item["title"]) is not str or not item["title"].strip():
            raise ContractError("Readiness items need a title")
        for status in ("definition", "implementation", "offline_coverage"):
            if type(item[status]) is not str or item[status] not in STATUSES:
                raise ContractError("Unknown readiness status: " + status)
        for field in ("definition_refs", "implementation_refs", "test_refs"):
            refs = item[field]
            if type(refs) is not list or any(type(ref) is not str for ref in refs) or len(refs) != len(set(refs)):
                raise ContractError("Readiness references must be unique paths")
            for ref in refs:
                path = root / relative_path(ref)
                if not path.resolve().is_relative_to(root) or not path.is_file():
                    raise ContractError("Missing or escaping readiness reference: " + ref)
        for status, refs in (("definition", "definition_refs"), ("implementation", "implementation_refs"),
                             ("offline_coverage", "test_refs")):
            if item[status] == "complete" and not item[refs]:
                raise ContractError("A completed readiness item requires supporting references")
        if item["implementation"] == "complete" and item["definition"] != "complete":
            raise ContractError("Implementation cannot be complete with an unfinished definition")
        if item["offline_coverage"] == "complete" and item["implementation"] != "complete":
            raise ContractError("Offline coverage cannot be complete for unfinished implementation")
        unfinished = [field for field in ("definition", "implementation", "offline_coverage")
                      if item[field] != "complete"]
        work = item["remaining_work"]
        if unfinished:
            if type(work) is not str or not work.strip():
                raise ContractError("Unfinished readiness items need concrete remaining work")
            remaining.append({"id": item["id"], "title": item["title"],
                              "unfinished": unfinished, "remaining_work": work})
        elif work is not None:
            raise ContractError("Completed readiness items must have null remaining_work")
    return {"target": "1.0", "implementation_complete": not remaining,
            "live_evaluation_prerequisite": "met" if not remaining else "deferred",
            "automatic_live_dispatch": False,
            "complete_items": len(items) - len(remaining), "required_items": len(items),
            "remaining": remaining,
            "additional_prerequisites": ["Current full offline validation passes", "Explicit operator dispatch"],
            "assurance": "Reviewed inventory and existing file references; no runtime attestation or test execution"}


def load_readiness(path: Path = ROOT / "docs/implementation-status.json", *, root: Path = ROOT) -> dict:
    return assess_readiness(strict_json_loads(path.read_text(encoding="utf-8")), root=root)
