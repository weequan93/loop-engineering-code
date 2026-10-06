"""Recover a rejected native receipt under the existing frozen rework allowance."""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from reference.core import canonical_digest
from .contracts import ContractError, load
from .native_rework import _apply, _quiescent
from .team_memory import context_bytes
from .workspace import atomic_write, byte_digest


def inspect(service, data, task_id, handoff_id, *, include_findings=False):
    """Inspect signed controller receipts; they are not independent verdicts."""
    report = {"task_id": task_id, "handoff_id": handoff_id, "eligible": False,
              "remaining_rounds": 0, "problems": []}
    try:
        service._data(data["team_id"], native=True)
        service.team._assert_inputs(data)
        if service.handshake()["restart_required"]:
            raise ContractError("Reconnect changed framework code before handoff rework")
        if data["status"] != "BLOCKED" or service.team.teams.signal(data["team_id"]):
            raise ContractError("Handoff rework needs the original blocked native team without a stop signal")
        from .native_project import assert_running, inspect as inspect_project
        assert_running(service, data)
        project = inspect_project(service, data)
        if project and (project["latest_team_id"] != data["team_id"] or not project["remaining_controller_seconds"]):
            raise ContractError("Handoff rework needs the current project batch and remaining cumulative allowance")
        if data["native_host"].get("rework_pending"):
            raise ContractError("Recover the original native rework journal first")
        history = data["native_host"].get("rework_history", [])
        team_limit = load(service.project / ".loop/team-policy.json", "team-policy")["max_reworks"]
        remaining = min(data["plan"].get("native_rework", {}).get("max_rounds", 0) - len(history),
                        team_limit - len(history) - data["native_host"].get("repair_rounds", 0))
        report["remaining_rounds"] = max(0, remaining)
        if remaining <= 0:
            raise ContractError("Native rework is not enabled or its frozen round allowance is exhausted")
        record = data["records"].get(task_id)
        if not record or record["status"] != "REJECTED" or not record.get("handoff") or record["handoff"]["id"] != handoff_id:
            raise ContractError("Handoff rework needs the exact current rejected handoff")
        receipts = record["receipts"]
        if any(role not in data["tasks"][task_id]["handoff_to"] or type(receipt.get("accepted")) is not bool
               or not isinstance(receipt.get("note"), str) or not receipt["note"].strip()
               for role, receipt in receipts.items()):
            raise ContractError("Rejected handoff has an invalid recipient receipt")
        rejected = {role: receipt for role, receipt in receipts.items() if receipt["accepted"] is False}
        if not rejected:
            raise ContractError("Handoff rework needs an actual rejected recipient receipt")
        child, snapshot = service.team._verified(data, task_id)
        from .native_preflight import repair_brief
        brief = repair_brief(service, data, task_id)
        target = brief["repair_task"]
        if not brief["repair_edit_authorized"]:
            raise ContractError("Handoff repair owner has no authorized source edits")
        if target != task_id and (target not in data["dependencies"][task_id] or
                                 data["records"][target]["status"] != "COMPLETE"):
            raise ContractError("Handoff repair owner must be this task or its explicit completed ancestor")
        owner = load(service.project / data["tasks"][target]["task_file"], "task")
        if owner["limits"]["max_tokens"] is not None or owner["limits"]["max_cost_microunits"] is not None:
            raise ContractError("Native handoff rework cannot enforce hard provider spending caps")
        affected = sorted({target} | {key for key, deps in data["dependencies"].items() if target in deps})
        _quiescent(service, data, affected)
        findings = [{"id": "handoff-" + role, "severity": "blocking", "description": receipt["note"],
                     "recipient": role, "source": "host-declared rejected receipt"}
                    for role, receipt in sorted(rejected.items())]
        report.update(eligible=True, repair_task=target, repair_owner=brief["repair_owner"],
                      affected_tasks=affected, candidate=snapshot["digest"], findings_count=len(findings),
                      receipt_digest=canonical_digest(receipts),
                      assurance="Authenticated controller receipt history, not independent evaluation evidence. Original checks and new acceptance remain required.")
        if include_findings:
            report["findings"] = findings
    except (ValueError, OSError) as exc:
        report["problems"].append(str(exc))
    return report


def rework(service, team_id, task_id, handoff_id):
    service._data(team_id, native=True)
    if service.handshake()["restart_required"]:
        raise ContractError("Reconnect changed framework code before handoff rework")
    with service.team.teams.writer(team_id) as data:
        from .native_project import assert_running
        assert_running(service, data)
        service.operations.assert_idle(data)
        identity = {"kind": "handoff", "task_id": task_id, "handoff_id": handoff_id}
        if any(all(row.get(key) == value for key, value in identity.items())
               for row in data["native_host"].get("rework_history", [])):
            return
        journal = data["native_host"].get("rework_pending")
        if journal and any(journal.get(key) != value for key, value in identity.items()):
            raise ContractError("Recover the original native rework journal first")
        if not journal:
            report = inspect(service, data, task_id, handoff_id, include_findings=True)
            if not report["eligible"]:
                raise ContractError("; ".join(report["problems"]))
            rework_id = "rework-" + uuid4().hex
            findings_path = service.team.store.run_dir(team_id) / (rework_id + "-findings.json")
            findings_bytes = context_bytes({**identity, "candidate": report["candidate"],
                "receipt_digest": report["receipt_digest"], "receipts": data["records"][task_id]["receipts"],
                "findings": report["findings"], "assurance": report["assurance"]})
            atomic_write(findings_path, findings_bytes)
            data["native_host"]["rework_pending"] = {
                "id": rework_id, **identity, "findings_path": str(findings_path),
                "findings_sha256": byte_digest(findings_bytes),
                **{key: report[key] for key in ("repair_task", "affected_tasks", "candidate", "findings", "receipt_digest")},
                "team_status_before": data["status"], "team_reason_before": data["reason"],
                "records": {key: deepcopy(data["records"][key]) for key in report["affected_tasks"]},
            }
            service.team.teams.save(data, "host.handoff_rework_prepared")
        _apply(service, data)
