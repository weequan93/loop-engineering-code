"""Durable host observations; never independent evidence or an effect executor."""

from copy import deepcopy
from pathlib import Path
import tempfile
from uuid import uuid4

from reference.core import canonical_digest
from .contracts import ContractError, validate
from .native_workers import text, assert_submittable
from .native_workbench import directory
from .workspace import safe_path

ASSURANCE = "Host-reported actions and environment with controller-captured artifact bytes. Not independent evidence, runtime attestation or formal task acceptance."


def validate_scenarios(task):
    from .verification_dependencies import validate_dependencies
    validate_dependencies(task)
    scenarios = task.get("extensions", {}).get("verification_scenarios")
    if scenarios is None:
        return
    validate("verification-scenarios", scenarios)
    ids = {row["id"] for row in scenarios}
    if len(ids) != len(scenarios):
        raise ContractError("Verification scenario IDs must be unique")
    checks = {c["id"] for c in task["checks"]}
    if any(s["check_id"] not in checks or not set(s["depends_on"]).issubset(ids) for s in scenarios):
        raise ContractError("Verification scenarios need existing checks and dependencies")
    pending = set(ids)
    while pending:
        ready = {s["id"] for s in scenarios if s["id"] in pending and not set(s["depends_on"]) & pending}
        if not ready:
            raise ContractError("Verification scenarios have a dependency cycle")
        pending -= ready


def _sessions(data):
    return data.get("native_host", {}).get("verification_sessions", {})


def unresolved(attempt):
    return attempt["status"] == "RUNNING" or attempt.get("safe_to_retry") is False


def _current(service, data, session):
    record = data["records"][session["task_id"]]
    if not record.get("request") or record["request"]["id"] != session["request_id"]:
        return False
    latest = next((s for s in reversed(list(_sessions(data).values())) if s["workbench_id"] == session["workbench_id"]), None)
    if latest and latest["id"] != session["id"]:
        return False
    if session.get("runtime_environment") and service.team._controller(data, session["task_id"]).environment_digest(
            service.team._child(data, session["task_id"])) != session["runtime_environment"]:
        return False
    workbench = data["native_host"]["workbenches"][session["workbench_id"]]
    return service.team.snapshots.capture(directory(workbench), service.team.profile(data))["digest"] == session["candidate"]


def _view(service, data, session):
    try:
        current, problem = _current(service, data, session), None
    except (OSError, ValueError) as exc:
        current, problem = False, str(exc)[:512]
    rows, now = [], service.clock()
    passed = _passed(session) if current else set()
    for scenario in session["scenarios"]:
        attempts = [a for a in session["attempts"] if a["scenario_id"] == scenario["id"]]
        latest = attempts[-1] if attempts else session.get("reused", {}).get(scenario["id"])
        total = sum(a["scenario_id"] == scenario["id"] for s in _sessions(data).values()
                    if s["task_id"] == session["task_id"] for a in s["attempts"])
        status = latest["status"] if latest else "PENDING"
        if not current:
            status = "STALE"
        elif latest and latest["status"] == "RUNNING" and now >= latest["deadline_epoch"]:
            status = "RECONCILIATION_REQUIRED"
        rows.append({**scenario, "status": status, "attempt": deepcopy(latest),
                     "attempts_remaining": max(0, scenario["max_attempts"] - total),
                     "dependencies_ready": set(scenario["depends_on"]).issubset(passed)})
    pending = next((a for a in session["attempts"] if unresolved(a)), None)
    return {"id": session["id"], "task_id": session["task_id"], "workbench_id": session["workbench_id"],
            "request_id": session["request_id"], "owner": session["owner"], "environment": session["environment"],
            "candidate": session["candidate"], "current": current, "problem": problem,
            "reuse_problem": session.get("reuse_problem"), "reused_scenarios": list(session.get("reused", {})),
            "artifacts_directory": session["artifacts_directory"], "scenarios": rows,
            "observed_pass": len(passed), "total": len(rows), "unresolved_attempt": deepcopy(pending),
            "last_observation_epoch": session["updated_epoch"], "assurance": ASSURANCE}


def _passed(session):
    return set(session.get("reused", {})) | {a["scenario_id"] for a in session["attempts"] if a["status"] == "OBSERVED_PASS"}


def progress(service, team_id, session_id=None):
    data = service._data(team_id, native=True)
    sessions = _sessions(data)
    if session_id is not None:
        if session_id not in sessions:
            raise ContractError("Unknown verification session")
        selected = [sessions[session_id]]
    else:
        # All unresolved actions remain visible; old completed sessions are compacted only in the view.
        selected = [s for s in sessions.values() if any(unresolved(a) for a in s["attempts"])]
        selected += [s for s in list(sessions.values())[-8:] if s not in selected]
    return {"team_id": team_id, "sessions": [_view(service, data, s) for s in selected], "assurance": ASSURANCE}


def begin(service, team_id, task_id, workbench_id, owner, environment):
    text(owner, "verification owner", 256)
    text(environment, "observed verification environment", 2048)
    service._active(team_id)
    with service.team.teams.writer(team_id) as data:
        service.team._assert_inputs(data)
        if service.team.teams.signal(team_id) or data["status"] != "ACTIVE":
            raise ContractError("Stopped team cannot begin verification")
        workbench = data["native_host"].get("workbenches", {}).get(workbench_id)
        record = data["records"].get(task_id, {})
        if (not workbench or workbench["task_id"] != task_id or workbench["status"] != "DRAFT"
                or not record.get("request") or record["request"]["id"] != workbench["request_id"]):
            raise ContractError("Verification needs a current unsubmitted workbench")
        service.team._assert_request_candidate(data, task_id)
        assert_submittable(service, data, task_id)
        task = service.team._child(data, task_id)["task"]
        scenarios = task.get("extensions", {}).get("verification_scenarios")
        if not scenarios:
            raise ContractError("Declare verification scenarios in the task before freezing")
        snapshot = service.team.snapshots.capture(directory(workbench), service.team.profile(data))
        candidate = snapshot["digest"]
        runtime_environment = service.team._controller(data, task_id).environment_digest(service.team._child(data, task_id))
        sessions = data["native_host"].setdefault("verification_sessions", {})
        for session in reversed(list(sessions.values())):
            if session["workbench_id"] != workbench_id:
                continue
            if session["owner"] != owner:
                raise ContractError("Verification session owner is frozen")
            if (session["candidate"], session["environment"], session.get("runtime_environment")) == (
                    candidate, environment, runtime_environment):
                return _view(service, data, session)
            break
        if any(unresolved(a) for s in sessions.values() for a in s["attempts"]):
            raise ContractError("Reconcile the original verification action before another session")
        if len(sessions) >= 64:
            raise ContractError("Verification session history limit reached")
        identity = "verification-" + uuid4().hex
        artifacts = Path(tempfile.mkdtemp(prefix=identity + "-", dir=directory(workbench).parent)).resolve()
        session = {"id": identity, "task_id": task_id, "workbench_id": workbench_id,
                   "request_id": record["request"]["id"], "owner": owner, "environment": environment,
                   "candidate": candidate, "scenarios": deepcopy(scenarios), "attempts": [],
                   "artifacts_directory": str(artifacts), "updated_epoch": int(service.clock()),
                   "runtime_environment": runtime_environment}
        from .verification_dependencies import bindings, reuse
        session["input_bindings"], session["reuse_problem"] = bindings(task, snapshot, environment, runtime_environment)
        session["reused"] = reuse(service, sessions, session)
        sessions[identity] = session
        service.team.teams.save(data, "host.verification_started")
        return _view(service, data, session)


def _artifacts(service, session, paths):
    if (not isinstance(paths, list) or len(paths) > 8 or any(not isinstance(p, str) for p in paths)
            or len(set(paths)) != len(paths)):
        raise ContractError("Verification needs at most eight distinct artifact paths")
    root = Path(session["artifacts_directory"])
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise ContractError("Verification artifact directory is missing or redirected")
    result = []
    for name in paths:
        path = safe_path(root, name)
        if not path.is_file() or not 0 < path.stat().st_size <= 5 * 1024 * 1024:
            raise ContractError("Verification artifact must be a nonempty file up to 5 MiB")
        with path.open("rb") as stream:
            content = stream.read(5 * 1024 * 1024 + 1)
        if not content or len(content) > 5 * 1024 * 1024:
            raise ContractError("Verification artifact changed size while capturing")
        result.append({"path": name, "sha256": service.team.snapshots.put(content), "bytes": len(content)})
    return result


def update(service, team_id, session_id, scenario_id, owner, action, attempt_id=None,
           observation=None, artifacts=None, safe_to_retry=None):
    if action not in {"start", "pass", "fail", "blocked", "reconcile"}:
        raise ContractError("Unknown verification action")
    text(owner, "verification owner", 256)
    if action != "start":
        text(observation, "actual verification observation", 4096)
    if action == "reconcile" and type(safe_to_retry) is not bool:
        raise ContractError("Reconciliation needs the actual observed state and explicit retry safety")
    if action != "reconcile" and safe_to_retry is not None:
        raise ContractError("Retry safety belongs only to reconciliation")
    artifacts = [] if artifacts is None else artifacts
    if action == "reconcile":
        service._data(team_id, native=True)  # Inspection-only reports can settle stopped effects, without resuming work.
    else:
        service._active(team_id)
    with service.team.teams.writer(team_id) as data:
        service.team._assert_inputs(data)
        if action != "reconcile" and (service.team.teams.signal(team_id) or data["status"] != "ACTIVE"):
            raise ContractError("Stopped team cannot advance verification")
        session = _sessions(data).get(session_id)
        if session is None or session["owner"] != owner:
            raise ContractError("Verification update needs the original session and owner")
        scenario = next((s for s in session["scenarios"] if s["id"] == scenario_id), None)
        if scenario is None:
            raise ContractError("Unknown verification scenario")
        attempts = [a for a in session["attempts"] if a["scenario_id"] == scenario_id]
        latest = attempts[-1] if attempts else None
        payload = canonical_digest({"action": action, "observation": observation,
                                    "artifacts": artifacts, "safe_to_retry": safe_to_retry})
        if action != "start" and latest and latest["id"] == attempt_id and latest.get("report_digest") == payload:
            return _view(service, data, session)
        current = _current(service, data, session)
        now = int(service.clock())
        if action == "start":
            if attempt_id is not None or artifacts or observation is not None:
                raise ContractError("Start creates an attempt ID and cannot claim an observation")
            if latest and latest["status"] == "RUNNING":
                # A repeated lost-ack start returns the same unresolved attempt, never replay permission.
                return _view(service, data, session)
            if not current:
                raise ContractError("Verification candidate is stale; inspect it before starting a new session")
            if any(unresolved(a) for s in _sessions(data).values() for a in s["attempts"]):
                raise ContractError("Reconcile the original running verification action first")
            if scenario_id in session.get("reused", {}) or latest and (latest["status"] == "OBSERVED_PASS" or latest.get("safe_to_retry") is False):
                raise ContractError("Scenario already passed or retry is not confirmed safe")
            passed = _passed(session)
            if not set(scenario["depends_on"]).issubset(passed):
                raise ContractError("Verification scenario dependencies are incomplete")
            count = sum(a["scenario_id"] == scenario_id for s in _sessions(data).values()
                        if s["task_id"] == session["task_id"] for a in s["attempts"])
            if count >= scenario["max_attempts"]:
                raise ContractError("Frozen verification attempt allowance is exhausted")
            session["attempts"].append({"id": "observation-" + uuid4().hex, "scenario_id": scenario_id,
                "status": "RUNNING", "started_epoch": now, "deadline_epoch": now + scenario["timeout_seconds"]})
        else:
            if (not latest or latest["id"] != attempt_id or
                    not (latest["status"] == "RUNNING" or action == "reconcile" and unresolved(latest))):
                raise ContractError("Report must match the original running verification attempt")
            if action != "reconcile" and (not current or now >= latest["deadline_epoch"]):
                raise ContractError("Stale or expired action requires reconciliation, not a passing report")
            captured = _artifacts(service, session, artifacts)
            if action == "pass" and not captured:
                raise ContractError("An observed pass requires actual captured artifacts")
            if action == "pass" and not _current(service, data, session):
                raise ContractError("Candidate changed while capturing verification artifacts")
            latest.update(status={"pass": "OBSERVED_PASS", "fail": "OBSERVED_FAIL", "blocked": "BLOCKED",
                                  "reconcile": "RECONCILED"}[action], observation=observation,
                          artifacts=captured, finished_epoch=now, report_digest=payload)
            if action == "reconcile":
                latest["safe_to_retry"] = safe_to_retry
            elif action in {"fail", "blocked"}:
                latest["safe_to_retry"] = False
        session["updated_epoch"] = now
        service.team.teams.save(data, "host.verification_" + action)
        return _view(service, data, session)


def assert_no_running(data, task_id):
    if any(unresolved(a) for s in _sessions(data).values() if s["task_id"] == task_id for a in s["attempts"]):
        raise ContractError("Reconcile running verification actions before task submission")
