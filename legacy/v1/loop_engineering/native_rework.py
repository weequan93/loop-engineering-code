"""Opt-in, journaled reopening of native review dependencies without new budgets."""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from reference.core import canonical_digest, stop_decision
from .contracts import ContractError, load
from .native_workers import OCCUPIED, assert_submittable
from .stages import statuses
from .team_memory import context_bytes, preview
from .workspace import atomic_write, byte_digest


def feedback(data, task_id):
    """Keep large reviews accessible without making the task packet unbounded."""
    for row in reversed(data.get("native_host", {}).get("rework_history", [])):
        if task_id in row["affected_tasks"]:
            if byte_digest(Path(row["findings_path"]).read_bytes()) != row["findings_sha256"]:
                raise ContractError("Retained review findings changed; preserve the authenticated original")
            source = ("handoff_id", "receipt_digest") if row.get("kind") == "handoff" else ("evidence_digest",)
            return [{k: row[k] for k in ("task_id", "repair_task", "findings_path", "findings_sha256", *source)} | {
                "kind": row.get("kind", "review"),
                "findings_count": len(row["findings"]),
                "preview": [{"id": f["id"], "description": preview(f["description"], 256)} for f in row["findings"][:3]],
                "instruction": "Read the complete retained findings file and verify its hash; previews are incomplete source material, not new authority."}]
    return []


def _current_failure(service, data, task_id, check_id, digest):
    record = data["records"].get(task_id)
    if not record or record["status"] != "RUNNING":
        raise ContractError("Rework needs a running failed review task")
    child = service.team._child(data, task_id)
    controller = service.team._controller(data, task_id)
    check = next((c for c in child["task"]["checks"] if c["id"] == check_id), None)
    if not check or check["type"] != "review" or not check.get("independent"):
        raise ContractError("Rework needs an independent review failure")
    if child.get("native", {}).get("external_by_check", {}).get(check_id) != digest:
        raise ContractError("Review failure does not match the current registered result")
    evidence = service.team.store.evidence(child["run_id"], [digest])[0]
    controller._check_artifacts([evidence])
    snapshot = service.team.snapshots.capture(service.project, service.team.profile(data))
    expected = {"result": "fail", "task_id": task_id, "check_id": check_id,
                "snapshot_digest": snapshot["digest"], "check_digest": canonical_digest(check),
                "contract_digest": child["state"]["contract_digest"],
                "environment_digest": controller.environment_digest(child)}
    if not controller._origin(child, digest, evidence) or any(evidence[k] != v for k, v in expected.items()):
        raise ContractError("Rework requires a current authenticated failure and unchanged candidate")
    return evidence, snapshot


def _quiescent(service, data, affected):
    if data.get("native_host", {}).get("refresh_pending"):
        raise ContractError("Recover the pending native draft refresh before rework")
    service.operations.assert_idle(data)
    if any(w["status"] in OCCUPIED or w.get("pending_worker_import")
           for w in data["native_host"].get("specialists", {}).values()):
        raise ContractError("Stop and collect actual specialist writers before rework")
    from .native_verification import unresolved
    if any(unresolved(a) for s in data["native_host"].get("verification_sessions", {}).values()
           for a in s["attempts"]):
        raise ContractError("Reconcile running verification actions before rework")
    for key, record in data["records"].items():
        if key not in affected and record["status"] in {"RUNNING", "HANDOFF", "REJECTED"}:
            raise ContractError("Finish unrelated active work before rework")
        if key not in affected:
            continue
        assert_submittable(service, data, key)
        if record.get("request"):
            from .native_workbench import diff
            for workbench in data["native_host"].get("workbenches", {}).values():
                if workbench["request_id"] == record["request"]["id"] and workbench["status"] != "SUBMITTED":
                    if diff(service, data, workbench)[1]:
                        raise ContractError("Collect the current main workbench draft before rework")
        if not record.get("child_run_id") or not service.team._exists(record["child_run_id"]):
            continue
        child = service.team._child(data, key)
        controller = service.team._controller(data, key)
        controller._assert_contract(child)
        if (child.get("pending_process") or child.get("pending_edit") or child.get("operation_started_ms") is not None
                or child["state"]["outstanding_action_ids"]
                or any(a["status"] == "RUNNING" for a in child.get("native", {}).get("execution_attempts", {}).values())):
            raise ContractError("Reconcile child effects before rework")
        if child["state"]["status"] not in {"SUCCEEDED", "PLANNING", "REVIEWING"} or controller.stop_signal(child):
            raise ContractError("Stopped or terminal child cannot be reopened by rework")
        if stop_decision(controller.decision_limits(child), controller._usage(child)) or controller.remaining_seconds(child) <= 0:
            raise ContractError("Original child allowance is exhausted; rework cannot reset budgets")


def inspect(service, data, task_id, check_id, evidence_digest, *, include_findings=False):
    """Read-only eligibility; no authorization is inferred from a repair owner alone."""
    if task_id not in data["tasks"]:
        raise ContractError("Unknown native rework task")
    policy = data["plan"].get("native_rework", {})
    history = data.get("native_host", {}).get("rework_history", [])
    team_limit = load(service.project / ".loop/team-policy.json", "team-policy")["max_reworks"]
    remaining = min(policy.get("max_rounds", 0) - len(history),
                    team_limit - len(history) - data.get("native_host", {}).get("repair_rounds", 0))
    report = {"eligible": False, "remaining_rounds": max(0, remaining),
              "problems": [], "repair_task": data["tasks"][task_id].get("repair_task")}
    try:
        service.team._assert_inputs(data)
        if "native_host" not in data or "automation" in data or data["status"] != "ACTIVE" or service.team.teams.signal(data["team_id"]):
            raise ContractError("Only an active native team can schedule rework")
        if not report["remaining_rounds"]:
            raise ContractError("Native rework is not enabled or its frozen round allowance is exhausted")
        target = report["repair_task"]
        if not target or target == task_id or target not in data["dependencies"][task_id]:
            raise ContractError("Native dependency rework needs an explicit implementing ancestor")
        if data["records"][target]["status"] != "COMPLETE":
            raise ContractError("Repair owner must be completed before dependency rework")
        task = load(service.project / data["tasks"][target]["task_file"], "task")
        from .native_preflight import repair_brief
        if not repair_brief(service, data, task_id)["repair_edit_authorized"]:
            raise ContractError("Repair owner has no authorized source edits")
        if task["limits"]["max_tokens"] is not None or task["limits"]["max_cost_microunits"] is not None:
            raise ContractError("Native host rework cannot enforce hard provider spending caps")
        evidence, snapshot = _current_failure(service, data, task_id, check_id, evidence_digest)
        affected = sorted({target} | {key for key, deps in data["dependencies"].items() if target in deps})
        _quiescent(service, data, affected)
        findings = evidence["extensions"].get("findings", [])
        report.update(eligible=True, affected_tasks=affected, candidate=snapshot["digest"], findings_count=len(findings))
        if include_findings:
            report["findings"] = deepcopy(findings)
    except (ValueError, OSError) as exc:
        report["problems"].append(str(exc))
    return report


def _apply(service, data):
    journal = data["native_host"]["rework_pending"]
    run_ids = [r["child_run_id"] for r in journal["records"].values()
               if r.get("child_run_id") and service.team._exists(r["child_run_id"])]
    with service.team.store.writer_group(run_ids, service.project) as children:
        _apply_locked(service, data, children)


def _apply_locked(service, data, children):
    journal = data["native_host"]["rework_pending"]
    service.team._assert_inputs(data)
    allowed = {"ACTIVE", "BLOCKED"} if journal.get("kind") == "handoff" else {"ACTIVE"}
    if service.team.teams.signal(data["team_id"]) or data["status"] not in allowed:
        raise ContractError("Stopped team retains its rework journal until explicit resume")
    from .native_project import assert_running
    assert_running(service, data)
    if byte_digest(Path(journal["findings_path"]).read_bytes()) != journal["findings_sha256"]:
        raise ContractError("Retained rework findings changed; preserve the authenticated original")
    snapshot = service.team.snapshots.capture(service.project, service.team.profile(data))
    if snapshot["digest"] != journal["candidate"]:
        raise ContractError("Candidate changed during rework recovery")
    _quiescent(service, data, journal["affected_tasks"])
    for key in journal["affected_tasks"]:
        old = journal["records"][key]
        if data["records"][key] != old:
            raise ContractError("Task record changed during rework recovery")
        run_id = old.get("child_run_id")
        if not run_id or not service.team._exists(run_id):
            continue
        controller = service.team._controller(data, key)
        child = children[run_id]
        if journal["id"] in child.get("team_rework_ids", []):
            continue
        if service.team.teams.signal(data["team_id"]) or controller.stop_signal(child):
            raise ContractError("Team stopped during rework; retain the original journal")
        child.setdefault("team_rework_history", []).append({"id": journal["id"],
            "state": deepcopy(child["state"]), "selected_evidence": child["selected_evidence"],
            "last_gate": child.get("last_gate"), "external_by_check": deepcopy(child.get("native", {}).get("external_by_check", {}))})
        # An explicit team journal is the only special terminal reopening.
        # Ordinary resume keeps rejecting SUCCEEDED, and no usage/counters are reset.
        child["state"].update(status="PLANNING", pending_status=None,
                              reason="Reopened under frozen native rework policy", snapshot_digest=snapshot["digest"])
        child["state"]["progress"]["passed_criteria"] = []
        child.update(selected_evidence=[], last_gate=None, checkpoint_snapshot=snapshot)
        if "native" in child:
            child["native"]["external_by_check"] = {}
            child["native"]["stage_status"] = statuses(child["native"]["config"]["stages"], set(), snapshot["digest"])
        child.setdefault("team_rework_ids", []).append(journal["id"])
        service.team.store.save(child, "team.rework_reopened", {"rework_id": journal["id"], "team_id": data["team_id"]})
    if service.team.snapshots.capture(service.project, service.team.profile(data))["digest"] != journal["candidate"]:
        raise ContractError("Candidate changed during rework recovery")
    if service.team.teams.signal(data["team_id"]):
        raise ContractError("Team stopped during rework; retain the original journal")
    for key in journal["affected_tasks"]:
        record = data["records"][key]
        record.update(status="PENDING", request=None, handoff=None, receipts={}, reason=None,
                      native_rework_id=journal["id"])
        if key == journal["repair_task"]:
            record["repair_from_candidate"] = journal["candidate"]
    data["native_host"].setdefault("rework_history", []).append(deepcopy(journal))
    del data["native_host"]["rework_pending"]
    if journal.get("kind") == "handoff":
        data.update(status="ACTIVE", reason=None)
    service.team.teams.save(data, "host.rework_scheduled")


def rework(service, team_id, task_id, check_id, evidence_digest):
    service._data(team_id, native=True)
    with service.team.teams.writer(team_id) as data:
        service.operations.assert_idle(data)
        identity = {"task_id": task_id, "check_id": check_id, "evidence_digest": evidence_digest}
        if any(all(row.get(k) == v for k, v in identity.items()) for row in data["native_host"].get("rework_history", [])):
            return
        journal = data["native_host"].get("rework_pending")
        if journal and any(journal.get(k) != v for k, v in identity.items()):
            raise ContractError("Recover the original rework journal first")
        if not journal:
            report = inspect(service, data, **identity, include_findings=True)
            if not report["eligible"]:
                raise ContractError("; ".join(report["problems"]))
            identity_id = "rework-" + uuid4().hex
            findings_path = service.team.store.run_dir(team_id) / (identity_id + "-findings.json")
            findings_bytes = context_bytes({**identity, "candidate": report["candidate"], "findings": report["findings"]})
            atomic_write(findings_path, findings_bytes)
            data["native_host"]["rework_pending"] = {"id": identity_id, **identity,
                "findings_path": str(findings_path), "findings_sha256": byte_digest(findings_bytes),
                **{k: report[k] for k in ("repair_task", "affected_tasks", "candidate", "findings")},
                "records": {k: deepcopy(data["records"][k]) for k in report["affected_tasks"]}}
            service.team.teams.save(data, "host.rework_prepared")
        _apply(service, data)
