"""Typed admission for the native engine's deliberately small tool registry."""

from uuid import uuid4

from reference.core import canonical_digest
from .contracts import ContractError, relative_path
from .native_contracts import validate_native
from .workspace import check_write_scope

TOOLS = {"workspace.read": ("read_workspace", "repeatable"),
         "workspace.apply": ("edit_workspace", "reconcile"),
         "check.run": ("run_checks", "repeatable"),
         "evaluator.import": ("run_checks", "reconcile")}


class Broker:
    def authorize(self, data: dict, proposal: dict, *, actor_id: str) -> dict:
        validate_native("action", proposal)
        if (proposal["task_id"] != data["task"]["task_id"]
                or proposal["contract_digest"] != data["state"]["contract_digest"]
                or proposal["candidate_digest"] != data["state"]["snapshot_digest"]):
            raise ContractError("Broker proposal has stale task or candidate identity")
        if type(data["state"]["lease_token"]) is not int or data["state"]["lease_token"] < 1:
            raise ContractError("Broker requires a current fenced writer")
        tool, arguments = proposal["tool"], proposal["arguments"]
        if actor_id not in {"controller", "implementer", "operator"}:
            raise ContractError("Unknown actor role")
        if actor_id == "implementer" and tool == "evaluator.import":
            raise ContractError("Implementer cannot mint or import evaluator authority")
        action, idempotency = TOOLS[tool]
        if action not in data["task"]["authorization"]["allowed_actions"]:
            raise ContractError("Tool effect is not authorized by the accepted task")
        if tool == "workspace.read":
            if set(arguments) != {"path"}:
                raise ContractError("Invalid read arguments")
            relative_path(arguments["path"])
            if arguments["path"].split("/")[0].casefold() in {".loop", ".git"}:
                raise ContractError("Control state is not an implementer tool input")
        elif tool == "workspace.apply":
            if set(arguments) != {"changes"} or type(arguments["changes"]) is not list:
                raise ContractError("Invalid apply arguments")
            from pathlib import Path
            from .file_changes import task_change_bytes
            for change in arguments["changes"]:
                task_change_bytes(change, data["task"])
                check_write_scope(Path(data["workspace"]), change["path"], data["task"])
        elif tool == "check.run":
            if set(arguments) != {"check_id", "argv", "cwd"}:
                raise ContractError("Invalid check arguments")
            check = next((c for c in data["task"]["checks"] if c["id"] == arguments["check_id"]), None)
            if (not check or check["type"] != "command" or arguments["argv"] != check["argv"]
                    or arguments["cwd"] != check.get("cwd", ".")):
                raise ContractError("A check tool cannot replace the approved command or cwd")
        elif set(arguments) != {"request_id", "evidence_digest"}:
            raise ContractError("Invalid evaluator import arguments")
        policy = {"task": proposal["contract_digest"], "candidate": proposal["candidate_digest"],
                  "actor": actor_id, "tool": tool, "arguments": arguments, "lease": data["state"]["lease_token"]}
        result = {"schema_version": "1.0", "action_id": "action-" + uuid4().hex,
                  "proposal_id": proposal["proposal_id"], "task_id": proposal["task_id"],
                  "contract_digest": proposal["contract_digest"], "candidate_digest": proposal["candidate_digest"],
                  "actor_id": actor_id, "lease_token": data["state"]["lease_token"],
                  "policy_decision_id": canonical_digest(policy), "idempotency": idempotency,
                  "tool": tool, "arguments": arguments}
        validate_native("authorized_action", result)
        return result

    def proposal(self, data: dict, tool: str, arguments: dict) -> dict:
        return {"schema_version": "1.0", "proposal_id": "proposal-" + uuid4().hex,
                "task_id": data["task"]["task_id"], "contract_digest": data["state"]["contract_digest"],
                "candidate_digest": data["state"]["snapshot_digest"], "tool": tool, "arguments": arguments}
