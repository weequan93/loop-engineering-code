"""Team-wide reservations and durable operation admission, including rework."""

from copy import deepcopy
from pathlib import Path
import time

from .budgets import BudgetError, Ledger
from .contracts import ROOT, ContractError


def enable(data, policy, *, isolated=True):
    if "automation" in data:
        if data["automation"]["policy"] != policy:
            raise ContractError("Team policy is frozen; resume does not reset its budget")
        return
    data["automation"] = {"policy": deepcopy(policy), "isolated": isolated,
                          "started_ms": time.time_ns() // 1000000,
                          "operations": {}, "reservations": [], "reworks": 0,
                          "feedback": [], "history": []}


def policy_file(root: Path):
    from .workspace import safe_path
    path = safe_path(root, ".loop/team-policy.json")
    return path if path.exists() else ROOT / "templates/scenarios/development/team-policy.json"


def remaining(data):
    auto = data["automation"]
    return max(0, auto["policy"]["max_wall_seconds"] -
               (time.time_ns() // 1000000 - auto["started_ms"]) / 1000)


def admit(data):
    auto = data["automation"]
    if remaining(data) < 1:
        raise BudgetError("Team wall deadline exhausted, including waiting/recovery time")
    if len(auto["operations"]) >= auto["policy"]["max_dispatches"]:
        raise BudgetError("Team dispatch budget exhausted")
    if Ledger(auto["reservations"]).balance()["violated_bound"]:
        raise BudgetError("Team response violated its reserved bound")


def reserve(data, operation_id, quote, *, final=False):
    policy = data["automation"]["policy"]
    return Ledger(data["automation"]["reservations"]).reserve(
        operation_id, quote, policy,
        final_tokens=0 if final else policy["final_reserve_tokens"],
        final_cost=0 if final else policy["final_reserve_cost_microunits"])


def balance(data):
    auto = data["automation"]
    return {**Ledger(auto["reservations"]).balance(),
            "dispatches": len(auto["operations"]), "reworks": auto["reworks"],
            "remaining_wall_seconds": int(remaining(data)), "limits": auto["policy"],
            "scope": "All requirements, coordinator, worker, evaluator and receipt operations in this team, including rework"}
