"""Project tools for a coordinator running in an App or CLI.

The host owns conversations and delegation; the existing manual controllers
own proposal effects, actual checks, frozen inputs and completion. Host model
calls have unknown usage, outside the automatic scheduler's ledger. Explicitly
registered independent reviews use their own bounded controller dispatch.
"""

from copy import deepcopy
import html
import json
from pathlib import Path
from datetime import datetime, timezone
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

from reference.core import canonical_digest, strict_json_loads
from .contracts import ContractError, ROOT, load
from .project import initialize
from .progress_view import ProgressView
from .native_flow import next_action, turn_boundary
from .native_preflight import cached_result_imports, compact_preflight, repair_brief, verification_preflight
from .scenarios import context as scenario_context, inputs, task_inputs_digest
from .spec_setup import intake, request_context, validate_result
from .team_automation import TeamAutomation, PLAN_RESPONSE, SPEC_RESPONSE, encode
from .team_engine import TeamController
from .team_memory import fit_request, fit_task_request, pending_report
from .workspace import atomic_write, byte_digest, safe_path

HOST_CAPABILITIES = {"text_input": True, "structured_output": True,
                     "headless_execution": False, "usage_reporting": False}


class NativeHostService:
    def __init__(self, project, store, *, clock=None):
        self.project = Path(project).resolve()
        if not self.project.is_dir() or store.directory.is_relative_to(self.project):
            raise ContractError("Native host needs an existing project and external private state")
        self.team = TeamController(store)
        self.helper = TeamAutomation(self.team, SimpleNamespace(engine=None))
        self.clock = clock or time.time
        from .native_session import fingerprint
        self.loaded_fingerprint = fingerprint()
        self.workbench_parent = None
        self._closed = threading.Event()
        self._watched = set()
        self._watch_lock = threading.Lock()
        self._watcher = None
        self._view = ProgressView()
        from .native_operations import NativeOperations
        self.operations = NativeOperations(self)

    def close(self):
        self._closed.set()
        self.operations.close()
        if self._watcher:
            self._watcher.join(timeout=2)
        self._view.close()

    def _data(self, team_id, *, native=False):
        data = self.team.teams.get(team_id)
        if Path(data["workspace"]).resolve() != self.project:
            raise ContractError("Team is outside the configured project")
        if native and ("native_host" not in data or "automation" in data):
            raise ContractError("This tool needs a native-host team; automatic teams retain their accounted route")
        return data

    def _active(self, team_id):
        if self.handshake()["restart_required"]:
            raise ContractError("Framework files changed after MCP startup; reconnect before further mutations")
        data = self._data(team_id, native=True)
        from .native_project import assert_running
        assert_running(self, data)
        self.operations.assert_idle(data)
        if self.team.teams.signal(team_id) or data["status"] not in {"ACTIVE", "AWAITING_INPUT"}:
            raise ContractError("Team is stopped; inspect progress before explicitly resuming")
        if data["native_host"].get("stage_repair"):
            raise ContractError("Recover the original stage configuration repair before other work")
        if data["native_host"].get("rework_pending"):
            raise ContractError("Recover the original native rework journal before other work")
        if data["native_host"].get("refresh_pending"):
            raise ContractError("Recover the original native draft refresh before other work")
        if data.get("pending_control"):
            with self.team.teams.writer(team_id) as current:
                if self.team.teams.signal(team_id) or current["status"] not in {"ACTIVE", "AWAITING_INPUT"}:
                    raise ContractError("Stopped team cannot recover planning writes")
                self.helper._apply_control(current)
                self.team._apply_signal(current)
            data = self._data(team_id, native=True)
            if self.team.teams.signal(team_id):
                raise ContractError("Team stopped during planning recovery")
        self.team._assert_inputs(data)
        return data

    def begin(self, brief=None):
        if brief is not None:
            intake(brief)  # Validate before any initialization writes.
        related = [r for r in self.team.teams.runs() if r["workspace"] == str(self.project)]
        existing = [r for r in related if r["status"] not in {"COMPLETE", "CANCELLED"}]
        if existing:
            data = self._data(existing[0]["team_id"])
            if "native_host" not in data:
                raise ContractError("An existing controller team owns the project: " + data["team_id"] +
                    "; inspect and explicitly stop it before changing execution mode. Its records and budget are preserved.")
            if brief is not None and brief != data.get("specification", {}).get("brief"):
                raise ContractError("The initial brief is frozen; record clarifications as actual answers")
            if data["status"] in {"ACTIVE", "AWAITING_INPUT"} and not self.team.teams.signal(data["team_id"]):
                self._active(data["team_id"])
            return self.progress(data["team_id"])
        self._check_previous_effects(related)
        if related:
            previous = self._data(related[0]["team_id"])
            from .native_project import inspect as inspect_project
            project = inspect_project(self, previous) if "native_host" in previous else None
            if project is not None:
                if brief is not None:
                    raise ContractError("A registered project queue cannot accept a replacement brief")
                return self.progress(project["latest_team_id"])
        self._check_spend_policy()
        initialize(self.project, scenario="development")
        _, documents = inputs(self.project)
        plan = load(self.project / ".loop/workflow-tasks.json", "team-workflow")
        if plan["tasks"]:
            if brief is not None:
                raise ContractError("A prepared workflow cannot accept a replacement brief")
            data = self.team.build(self.project)
            data["stage"] = "EXECUTION"
        else:
            data = {"workspace": str(self.project), "plan": plan, "tasks": {}, "records": {}, "dependencies": {},
                "bound_files": {".loop/project.json": byte_digest((self.project / ".loop/project.json").read_bytes())},
                "scenario_digest": task_inputs_digest(self.project), "status": "ACTIVE", "reason": None,
                "stage": "SPEC" if brief or not documents[".loop/spec.md"].strip() else "INTAKE"}
            if data["stage"] == "SPEC":
                data["specification"] = intake(brief, documents.get(".loop/setup.md"))
        data["native_host"] = {"repair_rounds": 0, "max_repairs": 3, "request": None,
            "usage": "unknown; host conversation and delegation calls are outside the controller ledger"}
        data["bound_files"][".loop/team-policy.json"] = byte_digest((self.project / ".loop/team-policy.json").read_bytes())
        data["initial_snapshot"] = self.team.snapshots.capture(self.project, self.team.profile(data))
        self.team.teams.create(data)
        return self.progress(data["team_id"])

    def _check_spend_policy(self):
        path = safe_path(self.project, ".loop/team-policy.json")
        if path.exists():
            policy = load(path, "team-policy")
            if policy["max_tokens"] is not None or policy["max_cost_microunits"] is not None:
                raise ContractError("Native host cannot enforce a hard team token/cost cap; retain the accounted provider route or explicitly review that policy")

    def _check_previous_effects(self, related):
        run_ids = set()
        for row in related:
            data = self._data(row["team_id"])
            from .native_verification import unresolved
            if any(unresolved(a) for session in data.get("native_host", {}).get("verification_sessions", {}).values()
                   for a in session["attempts"]):
                raise ContractError("Reconcile previous host verification effects before starting another batch")
            if data.get("pending_control") or any(op["status"] in {"ADMITTED", "RESPONDING"} or op.get("pid")
                    for op in data.get("automation", {}).get("operations", {}).values()):
                raise ContractError("Previous team has unreconciled operations or planning writes; inspect its existing state before starting a new mode")
            run_ids.update(run_id for record in data["records"].values()
                for run_id in (record["child_run_id"], record.get("integration_run_id")) if run_id)
        for row in self.team.store.runs():
            child = self.team.store.get(row["run_id"])
            if Path(child["workspace"]).resolve() != self.project and row["run_id"] not in run_ids:
                continue
            if (child.get("pending_process") or child.get("pending_edit")
                    or child.get("provisioning", {}).get("status") in {"RUNNING", "FAILED", "CANCELLED"}
                    or any(a["status"] == "RUNNING" for a in child.get("native", {}).get("execution_attempts", {}).values())):
                raise ContractError("Previous coding effects need reconciliation before changing execution mode")

    def stage_request(self, team_id):
        data = self._active(team_id)
        if data.get("stage") not in {"SPEC", "INTAKE"}:
            raise ContractError("Planning is finished; request a ready task")
        questions = load(self.project / ".loop/questions.json", "questions")
        pending = [q for q in questions["questions"] if q["blocking"] and not q["answers"]]
        if pending:
            return {"waiting_for_user": True, "questions": pending, "progress": self.progress(team_id)}
        schema = SPEC_RESPONSE if data["stage"] == "SPEC" else PLAN_RESPONSE
        context = (request_context(self.team, data) if data["stage"] == "SPEC" else
                   self.helper._inputs(data, planning=True))
        from .native_project import inspect as inspect_project
        project = inspect_project(self, data)
        if project is not None:
            context["project_supervision"] = {key: project[key] for key in
                ("root_team_id", "objective", "authorization", "original_controller_budget_seconds",
                 "controller_budget_seconds", "remaining_controller_seconds", "host_usage")}
            context["project_supervision"]["latest_budget_amendment"] = (
                project["budget_amendments"][-1] if project["budget_amendments"] else None)
            context["project_supervision"]["instruction"] = (
                "Follow the actual registered authorization and scope for this successor. Preserve prior accepted batches, "
                "answers and evidence. Submit the new requirements and plan; do not replay the previous workflow. "
                "Raise material external decisions early; queue authorization never grants paid calls, permissions or missing evidence. "
                "A separately recorded budget amendment changes only the cumulative controller cap; retain original scope, per-child limits and evidence gates.")
        if data["stage"] == "INTAKE":
            manifest = context.pop("source_manifest")
            context["source_coverage"] = {"files": len(manifest["files"]), "manifest_digest": canonical_digest(manifest),
                "note": "Bounded excerpts only; the host can inspect omitted project files with its own read tools. Sources remain bound to the candidate."}
            context["preparation_feedback"] = data.get("preparation_feedback", [])[-3:]
        packet = {"kind": "native-host-stage-request", "role": "requirements_reviewer" if data["stage"] == "SPEC" else "coordinator",
            "context": context, "response_schema": deepcopy(schema),
            "request_id": "request-" + uuid4().hex,
            "instruction": "The current App/CLI coordinator owns dialogue and actual delegation. Return a grounded spec/questions or a prepared plan matching this schema. Use native host agents when available, separate role results and preserve project milestone stop points. Loop tools alone apply proposals and run checks. Required independent/human/UI/artifact evidence needs its actual registered evaluator; role names and this chat cannot attest it. Do not launch team-run or a nested coding CLI for ordinary work."}
        if data["stage"] == "INTAKE":
            packet["workflow_schema"] = load(ROOT / "schemas/team-workflow.schema.json")
            packet["selection_schema"] = load(ROOT / "schemas/team.schema.json")
            packet["task_schema"] = load(ROOT / "schemas/task.schema.json")
            packet["verification_scenarios_schema"] = load(ROOT / "schemas/verification-scenarios.schema.json")
            packet["engine_template"] = load(ROOT / "templates/engine.json")
            packet["preferred_submission"] = {"tool": "loop_plan_submit",
                "instruction": "Submit selection/workflow/contracts as JSON objects. The tool serializes them and uses the same frozen-request validation. Submit the actual plan before dispatching implementation; do not stop after announcing that you will prepare it."}
        packet["verification_preflight"] = compact_preflight(verification_preflight(self, data))
        from .native_readiness import PLANNING_GUIDANCE
        packet["execution_readiness"] = deepcopy(PLANNING_GUIDANCE)
        fit_request(packet, self.team.profile(data)["context_max_bytes"])
        with self.team.teams.writer(team_id) as current:
            self.team._assert_inputs(current)
            if self.team.teams.signal(team_id):
                raise ContractError("Stopped team cannot prepare a request")
            candidate = self.team.snapshots.capture(self.project, self.team.profile(current))["digest"]
            if candidate != context["candidate"]:
                raise ContractError("Project changed while preparing context; request fresh context")
            saved = current["native_host"]["request"]
            if saved and saved["candidate"] == candidate and saved["stage"] == current["stage"]:
                return strict_json_loads(self.team.snapshots.read(saved["blob"]).decode())
            current["native_host"]["request"] = {"id": packet["request_id"], "candidate": candidate,
                "stage": current["stage"], "blob": self.team.snapshots.put(encode(packet))}
            self.team.teams.save(current, "host.stage_requested")
        self.progress(team_id)
        return packet

    def plan_submit(self, team_id, request_id, selection, workflow, contracts, summary):
        data = self._active(team_id)
        if data["stage"] != "INTAKE":
            raise ContractError("Typed plan submission needs an intake planning request")
        response = {"selection_json": json.dumps(selection, ensure_ascii=False),
            "workflow_json": json.dumps(workflow, ensure_ascii=False), "summary": summary,
            "contracts": [{"path": item["path"], "task_json": json.dumps(item["task"], ensure_ascii=False),
                "engine_path": item["engine_path"],
                "engine_json": json.dumps(item["engine"], ensure_ascii=False) if item["engine"] is not None else None}
                for item in contracts], "questions": []}
        return self.stage_submit(team_id, request_id, response)

    def plan_preflight(self, team_id, contracts):
        from .native_readiness import preview
        from .native_project import preflight_budget
        return preflight_budget(self, team_id, contracts, preview(self, team_id, contracts))

    def handshake(self):
        from .native_session import compatibility
        return compatibility(self)

    def heartbeat(self, team_id, owner, activity, task_id=None, ttl_seconds=120):
        from .native_session import heartbeat
        return heartbeat(self, team_id, owner, activity, task_id, ttl_seconds)

    def workbench_refresh(self, team_id, task_id, workbench_id, reviewed_changes):
        from .native_parallel import refresh
        return refresh(self, team_id, task_id, workbench_id, reviewed_changes)

    def workbench_reconcile_instructions(self, team_id, task_id, workbench_id, reviewed_changes=None, dry_run=False):
        from .native_instruction_recovery import reconcile
        return reconcile(self, team_id, task_id, workbench_id, reviewed_changes, dry_run)

    def repair_plan(self, team_id, dry_run=False):
        from .native_plan_repair import repair
        if type(dry_run) is not bool:
            raise ContractError("Plan repair dry_run must be a boolean")
        result = repair(self, team_id, dry_run=dry_run)
        return result if dry_run else self.progress(team_id)

    def rework(self, team_id, task_id, check_id, evidence_digest):
        from .native_rework import rework
        rework(self, team_id, task_id, check_id, evidence_digest)
        return self.progress(team_id)

    def handoff_rework(self, team_id, task_id, handoff_id, dry_run=False):
        from .native_handoff_rework import inspect, rework
        if type(dry_run) is not bool:
            raise ContractError("Handoff rework dry_run must be a boolean")
        if dry_run:
            return inspect(self, self._data(team_id, native=True), task_id, handoff_id)
        rework(self, team_id, task_id, handoff_id)
        return self.progress(team_id)

    def verification_begin(self, team_id, task_id, workbench_id, owner, environment):
        from .native_verification import begin
        return begin(self, team_id, task_id, workbench_id, owner, environment)

    def verification_update(self, team_id, session_id, scenario_id, owner, action, attempt_id=None,
                            observation=None, artifacts=None, safe_to_retry=None):
        from .native_verification import update
        return update(self, team_id, session_id, scenario_id, owner, action, attempt_id,
                      observation, artifacts, safe_to_retry)

    def verification_progress(self, team_id, session_id=None):
        from .native_verification import progress
        return progress(self, team_id, session_id)

    def next(self, team_id):
        self._data(team_id, native=True)
        state = self.progress(team_id)
        action = state["next_action"]
        if action["kind"] in {"prepare_spec", "prepare_plan", "prepare_task"} or (
                action["kind"] == "review_failed" and action["tool"] == "loop_workbench_prepare" and action["continue_work"]):
            packet = (self.workbench_prepare(**action["arguments"]) if action["tool"] == "loop_workbench_prepare"
                      else self.stage_request(team_id))
            packet.update(team_id=team_id, next_action=action, dashboard_url=self.dashboard_url(team_id))
            packet["verification_preflight"] = compact_preflight(state["verification_preflight"])
            fit_request(packet, self.team.profile(self._data(team_id))["context_max_bytes"])
            return packet
        return state

    def project_supervise(self, team_id, objective, authorization, milestones, controller_budget_seconds):
        from .native_project import register
        return register(self, team_id, objective, authorization, milestones, controller_budget_seconds)

    def project_advance(self, team_id):
        from .native_project import advance
        return advance(self, team_id)

    def project_control(self, team_id, action):
        from .native_project import control
        return control(self, team_id, action)

    def project_budget_amend(self, team_id, amendment_id, expected_controller_budget_seconds,
                             controller_budget_seconds, authorization, reason):
        from .native_project import amend_budget
        return amend_budget(self, team_id, amendment_id, expected_controller_budget_seconds,
                            controller_budget_seconds, authorization, reason)

    def planning_activity(self, team_id, request_id, activity_id, role, title, agent_id, status, detail):
        data = self._active(team_id)
        saved = data["native_host"]["request"]
        if data["stage"] not in {"SPEC", "INTAKE"} or not saved or saved["id"] != request_id:
            raise ContractError("Planning activity needs the current bound stage request")
        manifest, _ = inputs(self.project)
        if role not in {r["id"] for r in manifest["roles"]}:
            raise ContractError("Unknown planning role")
        if status not in {"RUNNING", "COMPLETE", "BLOCKED"} or not all(
                type(v) is str and v.strip() for v in (activity_id, title, agent_id, detail)):
            raise ContractError("Planning activity needs an actual host report")
        if any(len(v.encode()) > limit for v, limit in ((activity_id, 128), (title, 512), (agent_id, 256), (detail, 4096))):
            raise ContractError("Planning activity exceeds its bounded report size")
        with self.team.teams.writer(team_id) as current:
            self.team._assert_inputs(current)
            if self.team.teams.signal(team_id) or current["native_host"]["request"] != saved:
                raise ContractError("Stopped or replaced planning request cannot record activity")
            if self.team.snapshots.capture(self.project, self.team.profile(current))["digest"] != saved["candidate"]:
                raise ContractError("Project changed; refresh planning context before reporting activity")
            activities = current["native_host"].setdefault("activities", {})
            if activity_id not in activities and len(activities) >= 32:
                raise ContractError("Planning activity limit reached; update an existing activity")
            activities[activity_id] = {"activity_id": activity_id, "request_id": request_id,
                "stage": current["stage"], "candidate": saved["candidate"], "role": role,
                "title": title, "agent_id": agent_id, "status": status, "detail": detail,
                "updated_at": datetime.now(timezone.utc).isoformat(), "assurance": "host_declared_planning_only"}
            self.team.teams.save(current, "host.planning_activity_recorded")
            self.team._apply_signal(current)
        return self.progress(team_id)

    def stage_submit(self, team_id, request_id, response):
        from jsonschema import Draft202012Validator
        data = self._active(team_id)
        saved = data["native_host"]["request"]
        if not saved or saved["id"] != request_id or saved["stage"] != data["stage"]:
            raise ContractError("Stage response has no matching current request")
        packet = strict_json_loads(self.team.snapshots.read(saved["blob"]).decode())
        candidate = self.team.snapshots.capture(self.project, self.team.profile(data))["digest"]
        if candidate != saved["candidate"]:
            raise ContractError("Project sources changed; request fresh context before submitting")
        questions = load(self.project / ".loop/questions.json", "questions")
        try:
            errors = list(Draft202012Validator(packet["response_schema"]).iter_errors(response))
            if errors or len(encode(response)) > 1048576:
                raise ContractError("Stage response does not satisfy its schema/byte limit")
            if data["stage"] == "SPEC":
                waiting = validate_result(response, packet["context"], questions)
                writes = {".loop/questions.json": encode(questions)}
                if response["spec_markdown"].strip():
                    writes[".loop/spec-draft.md"] = response["spec_markdown"].encode()
                if not waiting:
                    writes[".loop/spec.md"] = response["spec_markdown"].encode()
            else:
                values, waiting = self.helper._preparation_result(data, response, questions)
                if not waiting:
                    from .native_project import assert_plan_budget
                    assert_plan_budget(self, data, values)
                    from .native_readiness import effective_budget
                    from .execution_tools import configured_tools
                    tools = configured_tools(self.project, self.team.profile(data))
                    for name, value in values.items():
                        if name.startswith(".loop/tasks/") and "checks" in value and value.get("extensions", {}).get("verification_plan"):
                            budget = effective_budget(self, value, None, tools)
                            if not budget["fits"]:
                                raise ContractError("Effective check time plus repair reserve exceeds the task wall limit: " + value["task_id"])
                writes = {name: encode(value) for name, value in values.items()}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            with self.team.teams.writer(team_id) as current:
                self.team._assert_inputs(current)
                if self.team.teams.signal(team_id) or current["native_host"]["request"] != saved:
                    raise ContractError("Stopped or replaced request cannot record a planning rejection") from exc
                if self.team.snapshots.capture(self.project, self.team.profile(current))["digest"] != candidate:
                    raise ContractError("Sources changed while validating the response") from exc
                current["native_host"]["repair_rounds"] += 1
                current["native_host"]["request"] = None
                if current["native_host"]["repair_rounds"] >= current["native_host"]["max_repairs"]:
                    current.update(status="BLOCKED", reason="Native planning repair limit reached: " + str(exc))
                current.setdefault("specification_feedback" if data["stage"] == "SPEC" else "preparation_feedback", []).append(
                    str(exc)[:1500] if data["stage"] == "SPEC" else {"round": current["native_host"]["repair_rounds"], "error": str(exc)[:1500]})
                self.team.teams.save(current, "host.stage_rejected")
            self.progress(team_id)
            raise
        with self.team.teams.writer(team_id) as current:
            self.team._assert_inputs(current)
            if self.team.teams.signal(team_id):
                raise ContractError("Stopped team cannot install a stage response")
            if current["native_host"]["request"] != saved:
                raise ContractError("Another stage request replaced this response")
            if self.team.snapshots.capture(self.project, self.team.profile(current))["digest"] != candidate:
                raise ContractError("Sources changed before installation")
            _, frozen = inputs(self.project)
            current["pending_unchanged_inputs"] = {n: byte_digest(t.encode()) for n, t in frozen.items() if n not in writes}
            current["pending_unchanged_inputs"].update({n: d for n, d in current["bound_files"].items() if n not in writes})
            current["pending_source_digest"] = candidate
            current["pending_control"] = [{"path": name,
                "old": byte_digest(safe_path(self.project, name).read_bytes()) if safe_path(self.project, name).exists() else None,
                "new": self.team.snapshots.put(content)} for name, content in writes.items()]
            stage = current["stage"]
            if stage == "SPEC":
                current["specification"].update(state="COLLECTING_PENDING" if waiting else "READY_PENDING",
                    draft=response["spec_markdown"], summary=response["summary"], sources=response["sources"])
            pending_report(current, response, stage=stage, role=packet["role"], candidate=candidate)
            current["stage"] = stage if waiting else "INTAKE" if stage == "SPEC" else "EXECUTION"
            current.update(status="AWAITING_INPUT" if waiting else "ACTIVE", reason="Waiting for actual user answers" if waiting else None)
            current["native_host"]["request"] = None
            self.team.teams.save(current, "host.stage_files_prepared")
            self.helper._apply_control(current)
            self.team._apply_signal(current)
        return self.progress(team_id)

    def answer(self, team_id, question_id, answer):
        self._active(team_id)
        self.helper.answer(team_id, question_id, answer)
        with self.team.teams.writer(team_id) as current:
            self.team._apply_signal(current)
        return self.progress(team_id)

    def task_request(self, team_id, task_id):
        data = self._active(team_id)
        result = self.team.request(team_id, task_id)
        context = result["task_context"]
        task = context.get("bundle", context)["task"]
        version = task.get("extensions", {}).get("agent_step_version", "0.2")
        result["response_schema"] = load(ROOT / f"schemas/step-v{version}.schema.json")
        from .native_flow import CONTINUATION_INSTRUCTION
        result["continuation_instruction"] = CONTINUATION_INSTRUCTION
        limit = self.team.profile(data)["context_max_bytes"]
        fit_task_request(result, limit, error=
            "Mandatory native task context and proposal schema exceed context_max_bytes")
        with self.team.teams.writer(team_id) as current:
            self.team._assert_inputs(current)
            if self.team.teams.signal(team_id) or current["status"] != "ACTIVE":
                raise ContractError("Stopped team cannot register a host wait")
            request_id = result["team_assignment"]["request_id"]
            if current["records"][task_id]["request"]["id"] != request_id:
                raise ContractError("Task request changed before host wait registration")
            workers = current["native_host"].setdefault("workers", {})
            if workers.get(task_id, {}).get("request_id") != request_id:
                timeout = load(self.project / ".loop/team-policy.json", "team-policy")["timeout_seconds"]
                now = int(self.clock())
                workers[task_id] = {"request_id": request_id, "started_epoch": now,
                    "deadline_epoch": now + timeout, "timeout_seconds": timeout}
                self.team.teams.save(current, "host.wait_registered")
                self.team._apply_signal(current)
        current = self._active(team_id)
        if current["records"][task_id]["request"]["id"] != result["team_assignment"]["request_id"]:
            raise ContractError("Task request was replaced before dispatch")
        result["host_wait"] = self.host_wait(current, task_id)
        fit_task_request(result, limit, error=
            "Mandatory native task context and proposal schema exceed context_max_bytes")
        self.progress(team_id)
        return result

    def host_wait(self, data, task_id):
        request = data["records"].get(task_id, {}).get("request")
        worker = data.get("native_host", {}).get("workers", {}).get(task_id)
        if not request:
            return {"status": "NONE"}
        if not worker or worker["request_id"] != request["id"]:
            return {"status": "UNREGISTERED", "summary": "宿主提案尚未登记等待期限；重新请求任务并准备工作副本。"}
        now = self.clock()
        return {**worker, "status": "EXPIRED" if now >= worker["deadline_epoch"] else "WAITING",
            "elapsed_seconds": max(0, int(now - worker["started_epoch"])),
            "remaining_seconds": max(0, int(worker["deadline_epoch"] - now)),
            "summary": "宿主提案等待已超时；停止空等，检查已写草稿，由主 agent 接手或重新派发有界工作。" if now >= worker["deadline_epoch"] else "等待宿主交付实际草稿或提案。"}

    def workbench_prepare(self, team_id, task_id):
        from .native_workbench import prepare
        return prepare(self, team_id, task_id)

    def workbench_status(self, team_id, task_id):
        from .native_workbench import status
        return status(self, team_id, task_id)

    def workbench_submit(self, team_id, task_id, workbench_id, summary):
        from .native_workbench import submit
        return submit(self, team_id, task_id, workbench_id, summary)

    def worker_assign(self, team_id, task_id, workbench_id, assignment_id, role, title, goal, write_paths, depends_on):
        from .native_workers import assign
        return assign(self, team_id, task_id, workbench_id, assignment_id, role, title, goal, write_paths, depends_on)

    def worker_update(self, team_id, worker_id, agent_id, status, detail):
        from .native_workers import update
        return update(self, team_id, worker_id, agent_id, status, detail)

    def worker_collect(self, team_id, worker_id, summary):
        from .native_workers import collect
        return collect(self, team_id, worker_id, summary)

    def worker_board(self, team_id):
        from .native_workers import board
        return board(self, team_id)

    def role_context(self, team_id, role):
        data = self._data(team_id)
        self.team._assert_inputs(data)
        result = {"context": scenario_context(self.project, role)}
        fit_request(result, self.team.profile(data)["context_max_bytes"])
        return result

    def task_submit(self, team_id, task_id, request_id, step):
        from .native_workers import assert_submittable
        assert_submittable(self, self._active(team_id), task_id)
        self.team.submit(team_id, task_id, request_id, step, agent_capabilities=HOST_CAPABILITIES)
        return self.progress(team_id)

    def task_submit_background(self, team_id, task_id, request_id, step):
        return self.operations.start("task_submit", dict(team_id=team_id, task_id=task_id, request_id=request_id, step=step))

    def workbench_submit_background(self, team_id, task_id, workbench_id, summary):
        return self.operations.start("workbench_submit", dict(team_id=team_id, task_id=task_id,
            workbench_id=workbench_id, summary=summary))

    def evaluation_background(self, team_id, task_id, check_id, retry=False):
        return self.operations.start("evaluation", dict(team_id=team_id, task_id=task_id, check_id=check_id, retry=retry))

    def receive(self, team_id, task_id, handoff_id, role, accept, note):
        self._active(team_id)
        self.team.receive(team_id, task_id, handoff_id, role, accept=accept, note=note)
        return self.progress(team_id)

    def collect(self, team_id, task_id):
        self._active(team_id)
        self.team.collect(team_id, task_id)
        return self.progress(team_id)

    def control(self, team_id, action):
        self._data(team_id)
        if action == "resume":
            data = self._data(team_id, native=True)
            if data["native_host"].get("stage_repair"):
                from .native_plan_repair import resume
                resume(self, team_id)
            if data.get("pending_control"):
                with self.team.teams.writer(team_id) as current:
                    if current["status"] in {"COMPLETE", "CANCELLED", "BLOCKED"} or self.team.teams.signal(team_id) == "CANCELLED":
                        raise ContractError("Terminal team cannot recover planning writes")
                    self.helper._apply_control(current)
                    self.team._apply_signal(current)
            self.team.resume(team_id)
            with self.team.teams.writer(team_id) as current:
                if current.get("stage") in {"SPEC", "INTAKE"} and not self.team.teams.signal(team_id):
                    questions = load(self.project / ".loop/questions.json", "questions")
                    if any(q["blocking"] and not q["answers"] for q in questions["questions"]):
                        current.update(status="AWAITING_INPUT", reason="Waiting for actual user answers")
                        self.team.teams.save(current, "host.questions_restored")
        elif action in {"pause", "cancel"}:
            self.team.stop(team_id, cancel=action == "cancel")
        else:
            raise ContractError("Unknown team control action")
        return self.progress(team_id)

    def memory(self, team_id, role="coordinator", task_id=None):
        self._data(team_id)
        return self.team.memory(team_id, role=role, task_id=task_id)

    def evaluation(self, team_id, task_id, check_id=None, envelope=None):
        controller, run_id = self._evaluator(team_id, task_id)
        if envelope is None:
            return controller.evaluation_request(run_id, check_id)
        child = self.team.store.get(run_id)
        attempts = child["native"].get("execution_attempts", {}).values()
        if child["pending_process"] or child["pending_edit"] or any(a["status"] == "RUNNING" for a in attempts):
            raise ContractError("Evaluator import requires reconciliation of the original unresolved effect")
        if child["operation_started_ms"] is not None:
            if not any(a["status"] == "RESULT" and a.get("envelope") == envelope for a in attempts):
                raise ContractError("Interrupted evaluation operation must be recovered before importing another result")
            recovered = controller.resume(run_id)
            if recovered["state"]["status"] != "PLANNING":
                raise ContractError("Cached evaluator result cannot be consumed after recovery stopped by signal or budget")
        controller.import_evaluation(run_id, envelope)
        return self._finish_evaluation(team_id, task_id, controller, run_id)

    def _evaluator(self, team_id, task_id):
        data = self._active(team_id)
        if task_id not in data["records"] or not data["records"][task_id]["child_run_id"]:
            raise ContractError("Evaluation needs an admitted task")
        from .native_workers import assert_submittable
        assert_submittable(self, data, task_id)
        controller = self.team._controller(data, task_id)
        run_id = data["records"][task_id]["child_run_id"]
        if not hasattr(controller, "evaluation_request"):
            raise ContractError("This task has no registered native evaluator")
        return controller, run_id

    def execute_evaluation(self, team_id, task_id, check_id, retry=False):
        if type(retry) is not bool:
            raise ContractError("Review retry must be a boolean")
        controller, run_id = self._evaluator(team_id, task_id)
        child = self.team.store.get(run_id)
        check = next((item for item in child["task"]["checks"] if item["id"] == check_id), None)
        if check and check["type"] == "review":
            from .native_review import run
            run(self, team_id, task_id, check_id, retry=retry)
        else:
            if retry:
                raise ContractError("Explicit retry is supported only for a registered review")
            controller.execute_evaluation(run_id, check_id)
        return self._finish_evaluation(team_id, task_id, controller, run_id)

    def _finish_evaluation(self, team_id, task_id, controller, run_id):
        self._active(team_id)  # An evaluator result cannot implicitly unpause a team.
        child = self.team.store.get(run_id)
        if child["state"]["status"] != "SUCCEEDED":
            if child["state"]["status"] != "PLANNING":
                controller.resume(run_id)
            controller.verify(run_id)
        self.team.collect(team_id, task_id)
        return self.progress(team_id)

    def progress(self, team_id=None):
        if team_id is None:
            related = [r for r in self.team.teams.runs() if r["workspace"] == str(self.project)]
            return {"project": str(self.project), "teams": [self.progress(r["team_id"]) for r in related[:20]],
                    "omitted_older_teams": max(0, len(related) - 20)}
        data = self._data(team_id)
        state = self.team.status(team_id)
        counts = {s: sum(r["status"] == s for r in state["tasks"].values())
                  for s in ("PENDING", "RUNNING", "HANDOFF", "COMPLETE", "REJECTED")}
        state.update(mode="native_host" if "native_host" in data else "automatic_controller" if "automation" in data else "prepared_manual",
            task_counts={"total": len(state["tasks"]), **counts},
            role_tasks=[{"task_id": key, "role": data["tasks"][key]["role"], "phase": data["tasks"][key]["phase"],
                         "status": r["status"], "reason": r["reason"]} for key, r in state["tasks"].items()],
            host_usage="unknown; host conversations/delegation are outside controller accounting",
            last_checkpoint_at=data["updated_at"], check_results=[], evidence_problems=[], pending_evaluations=[],
            current_summary=data.get("specification", {}).get("summary") or data.get("specification", {}).get("brief"))
        state["verification_preflight"] = verification_preflight(self, data)
        from .native_session import activity_status
        state["host_activity"] = activity_status(self, data)
        state["compatibility"] = self.handshake()
        state["operations"] = self.operations.snapshot(data) if "native_host" in data else []
        state["verification_sessions"] = self.verification_progress(team_id)["sessions"] if "native_host" in data else []
        from .native_plan_repair import inspect
        state["plan_configuration"] = inspect(self, data)
        state["verification_preflight"]["problems"].extend(state["plan_configuration"]["problems"])
        routes = {(item["task_id"], item["check_id"]): item
                  for item in state["verification_preflight"]["checks"]}
        host = data.get("native_host", {})
        state["refresh_pending"] = ({k: host["refresh_pending"][k] for k in ("task_id", "original", "candidate", "reviewed_changes")}
                                    if host.get("refresh_pending") else None)
        if state["refresh_pending"] and host["refresh_pending"].get("kind"):
            state["refresh_pending"]["kind"] = host["refresh_pending"]["kind"]
        state["rework_pending"] = ({k: host["rework_pending"][k] for k in
            ("kind", "task_id", "check_id", "evidence_digest", "handoff_id", "receipt_digest", "repair_task", "affected_tasks")
            if k in host["rework_pending"]}
            if host.get("rework_pending") else None)
        state["rework_rounds"] = len(host.get("rework_history", []))
        saved = host.get("request")
        candidate_current = bool(saved and self.team.snapshots.capture(self.project, self.team.profile(data))["digest"] == saved["candidate"])
        questions = state["pending_questions"]
        state["planning"] = {"questions_total": len(questions),
            "questions_answered": sum(bool(q["answers"]) for q in questions),
            "questions_remaining": sum(q["blocking"] and not q["answers"] for q in questions),
            "request_ready": bool(saved), "request_candidate_current": candidate_current,
            "activities": [{**item, "current": bool(candidate_current and saved["id"] == item["request_id"]
                and state["stage"] == item["stage"])} for item in host.get("activities", {}).values()],
            "assurance": "Host-declared planning activity; never counted as verified implementation or independent evidence"}
        state["worker_drafts"] = []
        state["dispatch_board"] = self.worker_board(team_id) if "native_host" in data else {"workers": []}
        for key, record in data["records"].items():
            state["tasks"][key]["controller_created"] = bool(record["child_run_id"] and self.team._exists(record["child_run_id"]))
            state["tasks"][key]["host_wait"] = self.host_wait(data, key)
            if "native_host" in data and any(r["task_id"] == key for r in host.get("workbenches", {}).values()):
                state["worker_drafts"].extend(self.workbench_status(team_id, key)["workbenches"])
            if not record["child_run_id"] or not self.team._exists(record["child_run_id"]):
                continue
            integrated = bool(record.get("integration_run_id") and self.team._exists(record["integration_run_id"]))
            child = self.team._child(data, key, verification=integrated)
            state["tasks"][key]["controller_reason"] = child["state"].get("reason")
            state["tasks"][key]["pending_edit"] = child["pending_edit"]
            try:
                controller = self.team._controller(data, key, verification=integrated)
                evidence = self.team.store.evidence(child["run_id"], child["selected_evidence"])
                controller._check_artifacts(evidence)
                state["check_results"].extend({"task_id": key, "check_id": ev["check_id"], "result": ev["result"],
                    "candidate": ev["snapshot_digest"], "summary": ev["summary"][:512]} for ev in evidence)
                outcomes = {}
                imports = {}
                if "native" in child:
                    snapshot = self.team.snapshots.capture(Path(child["workspace"]), child["profile"])
                    environment = controller.environment_digest(child)
                    checks = {check["id"]: check for check in child["task"]["checks"]}
                    for check_id, digest in child["native"]["external_by_check"].items():
                        result = self.team.store.evidence(child["run_id"], [digest])[0]
                        controller._check_artifacts([result])
                        if not controller._origin(child, digest, result):
                            raise ContractError("External check result has no authenticated evaluator origin")
                        check = checks.get(check_id)
                        if check and all(result[field] == expected for field, expected in (
                                ("snapshot_digest", snapshot["digest"]), ("environment_digest", environment),
                                ("contract_digest", child["state"]["contract_digest"]), ("check_digest", canonical_digest(check)))):
                            outcomes[check_id] = {"result": result["result"], "evaluation_summary": result["summary"],
                                "evidence_digest": digest, "candidate": result["snapshot_digest"],
                                "environment": result["environment_digest"],
                                "findings": deepcopy(result["extensions"].get("findings", []))}
                            if result["result"] == "fail":
                                outcomes[check_id].update(repair_brief(self, data, key))
                    state["tasks"][key]["evaluation_outcomes"] = outcomes
                    unresolved = child["pending_process"] or child["pending_edit"] or any(
                        attempt["status"] == "RUNNING" for attempt in child["native"].get("execution_attempts", {}).values())
                    if not unresolved:
                        imports = cached_result_imports(controller, child, snapshot["digest"], environment)
                    preparation = child["native"].get("review_preparation")
                    state["tasks"][key]["review_preparation"] = (deepcopy(preparation) if preparation and
                        child["operation_started_ms"] is not None and not imports else None)
                needs_evaluation = child["state"]["status"] in {"REVIEWING", "BLOCKED"} or (
                    child["state"]["status"] == "PLANNING" and (imports or any(
                        outcome["result"] in {"fail", "inconclusive"} for outcome in outcomes.values())))
                if "native" in child and needs_evaluation and record["status"] == "RUNNING":
                    passed = {check_id for check_id, outcome in outcomes.items() if outcome["result"] == "pass"}
                    state["pending_evaluations"].extend({"task_id": key, "check_id": check["id"], "type": check["type"],
                        **routes.get((key, check["id"]), {"executor_registered": False, "authority_registered": False,
                            "route": "missing_authority", "summary": "当前冻结检查的执行入口无法确认。"}),
                        **outcomes.get(check["id"], {"result": None, "findings": []}), **imports.get(check["id"], {})}
                        for check in child["task"]["checks"] if check["type"] != "command" and check["id"] not in passed)
            except (OSError, ValueError) as exc:
                state["evidence_problems"].append({"task_id": key, "error": str(exc)[:512]})
        if "native_host" in data and not state["rework_pending"]:
            from .native_rework import inspect as inspect_rework
            for evaluation in state["pending_evaluations"]:
                if evaluation.get("result") == "fail" and evaluation.get("repair_requires_reviewed_plan"):
                    evaluation["native_rework"] = inspect_rework(self, data, evaluation["task_id"],
                        evaluation["check_id"], evaluation["evidence_digest"])
        from .native_project import inspect as inspect_project
        state["project_supervision"] = inspect_project(self, data) if "native_host" in data else None
        state["handoff_rework"] = []
        if "native_host" in data and data["status"] == "BLOCKED" and not state["rework_pending"]:
            from .native_handoff_rework import inspect as inspect_handoff
            for key, record in data["records"].items():
                if record["status"] == "REJECTED" and record.get("handoff"):
                    state["handoff_rework"].append(inspect_handoff(self, data, key, record["handoff"]["id"]))
        state["completion_scope"] = "batch"
        state["next_action"] = next_action(state, data)
        state["turn_boundary"] = turn_boundary(state["next_action"])
        from .native_supervisor_store import existing_summary
        state["host_supervisor"] = existing_summary(self.team.store, self.project)
        with self.team.store.connect() as connection:
            rows = connection.execute("SELECT envelope FROM team_events WHERE id=? ORDER BY revision DESC LIMIT 8", (team_id,)).fetchall()
        state["recent_events"] = [{"event": p["event"], "revision": p["data"]["revision"], "at": p["data"]["updated_at"]}
            for (raw,) in rows for p in [self.team.teams._verify(raw, team_id)]]
        path = self.team.store.run_dir(team_id) / "progress.html"
        state["dashboard_path"] = str(path)
        state["dashboard_url"] = self._view.url(team_id)
        state["dashboard_problem"] = self._view.problem
        atomic_write(path, render_progress(state).encode())
        return state

    def watch_progress(self, team_id):
        self._data(team_id)
        with self._watch_lock:
            if self._closed.is_set():
                raise ContractError("The native host connection is closed")
            self._view.register(team_id, self.team.store.run_dir(team_id) / "progress.html")
            self._watched.add(team_id)
            if self._watcher is None:
                self._watcher = threading.Thread(target=self._refresh, daemon=True)
                self._watcher.start()

    def dashboard_url(self, team_id):
        return self._view.url(team_id)

    def _refresh(self):
        while not self._closed.wait(1):
            with self._watch_lock:
                ids = list(self._watched)
            for team_id in ids:
                try:
                    self.progress(team_id)
                except (OSError, ValueError, KeyError):
                    pass  # Keep the last actual snapshot; never invent success.


def render_progress(state):
    e = lambda v: html.escape(str(v if v is not None else "—"))
    labels = {"native_host": "对话模式", "automatic_controller": "自动控制器", "prepared_manual": "已准备任务",
        "SPEC": "需求收集", "INTAKE": "团队与任务规划", "EXECUTION": "任务执行",
        "ACTIVE": "可继续", "AWAITING_INPUT": "等待用户回答", "PAUSED": "已暂停", "CANCELLED": "已取消",
        "BLOCKED": "需要处理阻塞", "PENDING": "待开始", "RUNNING": "执行中", "HANDOFF": "等待交接",
        "COMPLETE": "已完成", "REJECTED": "已退回", "coordinator": "协调", "requirements_reviewer": "需求评审",
        "designer": "设计", "architect": "架构", "frontend": "前端", "backend": "后端", "tester": "测试",
        "security": "安全", "performance": "性能", "reviewer": "独立评审", "acceptance": "需求验收",
        "integrator": "整合", "devops": "构建与交付", "database": "数据库", "documentation": "文档", "reliability": "可靠性",
        "pass": "已通过", "fail": "未通过", "inconclusive": "待验证", "DRAFT": "代码草稿", "SUBMITTED": "已提交",
        "PREPARED": "待派发", "DRAFT_READY": "待收集", "COLLECTED": "草稿已收集", "STOPPED": "工作者已停止",
        "OBSERVED_PASS": "观察通过，待正式检查", "OBSERVED_FAIL": "观察失败", "STALE": "候选已变化",
        "RECONCILIATION_REQUIRED": "需要核对原动作", "RECONCILED": "原动作已核对"}
    label = lambda v: e(labels.get(v, v))
    validity = lambda v: "有效" if v is True else "需更新" if v is False else "待验证"
    progress_label = {"PAUSED": "等待恢复", "CANCELLED": "已取消", "BLOCKED": "需要处理阻塞"}.get(state["status"], "进行中的任务")
    task_status = lambda t: e(progress_label) if t["status"] == "RUNNING" and state["status"] in {"PAUSED", "CANCELLED", "BLOCKED"} else label(t["status"])
    rows = "".join(f"<tr><td>{e(t['task_id'])}</td><td>{label(t['role'])}</td><td>{e(t['phase'])}</td><td>{task_status(t)}</td><td>{e(t['reason'])}</td></tr>" for t in state["role_tasks"])
    if not rows:
        rows = '<tr><td colspan="5">正式任务尚未登记；主 agent 需要提交经过校验的任务计划。</td></tr>'
    questions = "".join(f"<li>{e(q['question'])}</li>" for q in state["pending_questions"] if q["blocking"] and not q["answers"])
    events = "".join(f"<li>{e(v['at'])} · {e(v['event'])}</li>" for v in state["recent_events"])
    checks = "".join(f"<tr><td>{e(v['task_id'])}</td><td>{e(v['check_id'])}</td><td>{label(v['result'])}</td><td>{e(v['summary'])}</td></tr>" for v in state["check_results"])
    preflight = state.get("verification_preflight", {"checks": [], "problems": []})
    route_labels = {"controller_command": "原控制器命令", "registered_executor": "已注册自动执行器",
                    "registered_review": "已注册独立评审执行器", "signed_import": "外部签名导入，需实际评估结果",
                    "missing_authority": "缺少有效评估权威"}
    route_rows = "".join(f"<tr><td>{e(v['task_id'])}</td><td>{e(v['check_id'])}</td><td>{e(v['type'])}</td><td>{e(route_labels.get(v['route'], v['route']))}</td><td>{e('; '.join(v['problems']) or v['summary'])}</td></tr>" for v in preflight["checks"])
    route_problems = "".join(f"<li>{e(v['task_id'])}：{e(v['problem'])}</li>" for v in preflight["problems"])
    route_problems += "".join(f"<li>{e(v['task_id'])} · {e(v['kind'])}：{e(v['detail'])}</li>" for v in preflight.get("readiness_issues", []))
    preflight_section = (f'<section><h2>正式检查与执行入口</h2><table><tr><th>任务</th><th>检查 ID</th><th>类型</th><th>实际匹配入口</th><th>条件或缺口</th></tr>{route_rows}</table><ul>{route_problems}</ul><small>开工前可核对正式检查和当前入口；登记不代表评估者已连接或检查已通过。已授权的开发可以继续，专业咨询不能替代签名评审或终验。</small></section>' if route_rows or route_problems else '')
    problems = "".join(f"<li>{e(v['task_id'])}：{e(v['error'])}</li>" for v in state["evidence_problems"])
    counts = state["task_counts"]
    planning = state["planning"]
    activity_rows = "".join(f"<tr><td>{e(a['title'])}</td><td>{label(a['role'])}</td><td>{e(a['agent_id'])}</td><td>{label(a['status']) if a['current'] else '历史规划，需重新核对'}</td><td>{e(a['detail'])}</td></tr>" for a in planning["activities"])
    if not activity_rows:
        activity_rows = '<tr><td colspan="5">主 agent 尚未登记规划分工；页面不会自动读取宿主内部的子 agent 活动。</td></tr>'
    planning_section = (f'<section><h2>需求与规划</h2><p>已回答问题：{planning["questions_answered"]} / {planning["questions_total"]}；剩余必须回答：{planning["questions_remaining"]}</p><table><tr><th>规划工作</th><th>角色</th><th>Agent</th><th>状态</th><th>实际汇报</th></tr>{activity_rows}</table><small>规划活动由宿主汇报，规划完成不等于开发或验收通过。</small></section>'
        if state["stage"] in {"SPEC", "INTAKE"} or planning["activities"] else '')
    waits = "".join(f"<li>{e(key)}：{e(t['host_wait'].get('summary'))} 已等待 {e(t['host_wait'].get('elapsed_seconds'))} 秒；期限 {e(t['host_wait'].get('timeout_seconds'))} 秒。</li>"
        for key, t in state["tasks"].items() if t.get("request") and t.get("host_wait", {}).get("status") in {"WAITING", "EXPIRED", "UNREGISTERED"})
    drafts = "".join(f"<tr><td>{e(r['task_id'])}</td><td>{e(r.get('changed_file_count'))}</td><td>{label(r['status']) if r['current'] else '历史草稿'}</td><td>{e(r.get('problem') or ', '.join(r.get('changed_files', [])))}</td></tr>" for r in state["worker_drafts"])
    draft_section = f'<section><h2>实际代码草稿与等待期限</h2><ul>{waits}</ul><table><tr><th>任务</th><th>修改文件数</th><th>状态</th><th>文件或问题</th></tr>{drafts}</table><small>草稿位于独立副本；文件已写入不等于原项目已集成或检查通过。等待超时要求当前宿主处理，Loop 不会自行中断模型。</small></section>' if waits or drafts else ''
    board = state.get("dispatch_board", {"workers": []})
    worker_rows = "".join(f"<tr><td>{e(w['title'])}<br><small>{e(w['goal'])}</small></td><td>{label(w['role'])}</td><td>{e(w['agent_id'] or '待宿主派发')}</td><td>{label(w['status'])}{' · 已超时' if w['expired'] else ''}{' · 历史请求' if not w['current'] else ''}</td><td>{e(', '.join(w['write_paths']) or '只读咨询')}<br><small>已修改：{e(w['changed_file_count'])}</small></td><td>{e(w.get('problem') or w.get('summary') or w['detail'])}</td></tr>" for w in board["workers"])
    worker_section = (f'<section><h2>谁在做什么 · 专业分工</h2><p>已占专业槽位：{e(board["occupied_slots"])} / {e(board["max_parallel"])}；可派发：{e(board["available_slots"])}。主线程负责接口、依赖、派发与回收。</p><table><tr><th>具体工作</th><th>专业</th><th>实际 Agent</th><th>状态</th><th>文件归属</th><th>结果或阻塞</th></tr>{worker_rows}</table><small>身份与汇报由宿主登记，文件数来自实际草稿；专业草稿收集不等于原任务验收通过。还需遵守宿主实际可用槽位。</small></section>' if worker_rows else '')
    operation_rows = "".join(f"<tr><td>{e(o['task_id'])}</td><td>{e(o['kind'])}</td><td>{e('需要恢复' if o['recovery_required'] else o['status'])}</td><td>{e(round(o['elapsed_seconds'], 1))} 秒</td><td>{e(o.get('error') or '')}</td></tr>" for o in state.get("operations", []))
    operation_section = (f'<section><h2>后台检查与恢复</h2><table><tr><th>任务</th><th>操作</th><th>状态</th><th>实际经过</th><th>问题</th></tr>{operation_rows}</table><small>操作返回不代表验收通过。此处是操作经过时间，不是模型 token 或费用；原检查、预算和交接仍需满足。</small></section>' if operation_rows else '')
    verification_rows = "".join(f"<tr><td>{e(s['task_id'])}</td><td>{e(s['owner'])}</td><td>{e(c['surface'])} · {e(c['title'])}</td><td>{label(c['status'])}</td><td>{e((c['attempt'] or {}).get('observation', '等待实际操作'))}</td><td>{e(datetime.fromtimestamp(s['last_observation_epoch'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC'))}</td></tr>"
        for s in state.get("verification_sessions", []) for c in s["scenarios"])
    verification_section = (f'<section><h2>逐项验证与中断恢复</h2><table><tr><th>任务</th><th>实际负责人</th><th>验证场景</th><th>状态</th><th>观察或阻塞</th><th>最后观察时间</th></tr>{verification_rows}</table><small>宿主报告与实际保存的附件；不是独立评估或正式验收。无新观察时不推断宿主仍在运行。已过期或结果未知的操作需先核对，不能盲目重放。</small></section>' if verification_rows else '')
    activity = state.get("host_activity", {})
    activity_names = {"unknown": "尚无宿主活动报告", "recent_report": "近期收到宿主活动报告", "stale_report": "宿主活动报告已过期，需核对", "stopped": "团队已停止推进"}
    activity_section = (f'<section><h2>宿主活动与耗时</h2><p>{e(activity_names.get(activity.get("state"), "未知"))}</p><p>{e((activity.get("report") or {}).get("activity", ""))}</p><p>本批实际经过 {e(round(activity.get("team_elapsed_seconds", 0)))} 秒；控制器累计记录 {e(round(activity.get("recorded_controller_seconds", 0), 1))} 秒。宿主模型用量未知。</p><small>页面刷新不代表 agent 仍在工作，也不会延长其期限。活动过期需核对原任务，不自动重派。</small></section>')
    boundary = state.get("turn_boundary")
    boundary_section = (f'<section><h2>主线程是否应继续</h2><p>{e("应继续执行" if not boundary["may_end_turn"] else "当前存在结束或等待门槛")}</p><p>{e(boundary["reason"])}</p><p>下一步：{e(boundary["next_action_kind"])} · {e(boundary["next_tool"])}</p><small>这是控制器当前指令，不是宿主仍在运行的证明。等待门槛不代表产品验收通过，也不证明全部工程已经耗尽。</small></section>' if boundary else '')
    supervisor = state.get("host_supervisor")
    supervisor_section = (f'<section><h2>持续执行监督器</h2><p>{e(supervisor["id"])} · {e(supervisor["status"])}</p><p>{e(supervisor["reason"])}</p><p>已派发 {e(supervisor["turns"])} 轮；当前团队 {e(supervisor["team_id"])}</p><small>此处为保存的运行状态；使用 host-supervisor-status 核对本机进程。监督器报告不能替代原检查和正式验收。</small></section>' if supervisor else '')
    project = state.get("project_supervision")
    project_section = (f'<section><h2>项目目标与后续批次</h2><p>{e(project["objective"])}</p><p>已建立后续批次：{e(project["successor_batches"])} / {e(project["max_successor_batches"])}；当前批次：{e(project["latest_team_id"])} · {label(project["latest_status"])}</p><p>续批链控制器累计：{e(project["recorded_controller_seconds"])} / {e(project["controller_budget_seconds"])} 秒；原额度 {e(project["original_controller_budget_seconds"])} 秒；已授权扩额 {e(len(project["budget_amendments"]))} 次。宿主 token 与费用未知。</p><p>下一步：{e(state["next_action"]["summary"])}</p><small>批次验收完成不等于整个产品完成。队列只覆盖已声明且已授权的后续合同；更早批次保留各自原账本，真实外部条件仍需验证。</small></section>' if project else '')
    metric = f"{counts['COMPLETE']} / {counts['total']}" if counts["total"] else "计划待提交"
    summary = state['current_summary'] or (state['scope'] if counts['total'] else state['next_action']['summary'])
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="3"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Loop · 项目进度</title>
<style>body{{font:16px system-ui;background:#f5f7fb;color:#16243b;margin:0}}main{{max-width:1080px;margin:40px auto;padding:24px}}section{{background:white;border:1px solid #dce3ee;border-radius:16px;padding:24px;margin:20px 0;overflow-x:auto}}h1{{font-size:32px}}small{{color:#53647c}}table{{width:100%;border-collapse:collapse;text-align:left}}td,th{{padding:12px 8px;border-bottom:1px solid #e7ecf4;overflow-wrap:anywhere}}.metrics{{display:flex;gap:28px;flex-wrap:wrap}}strong{{font-size:28px}}li{{padding:5px}}</style>
<main><small>LOOP ENGINEERING · {label(state['mode'])}</small><h1>项目进度</h1><p>阶段：{label(state['stage'])}　状态：{label(state['status'])}　记录版本：{e(state['revision'])}</p>
<section class="metrics"><div><strong>{metric}</strong><br>已完成正式任务</div><div><strong>{counts['RUNNING']}</strong><br>{e(progress_label)}</div><div><strong>{counts['HANDOFF']}</strong><br>等待交接</div></section>
<section><h2>当前情况</h2><p>{e(summary)}</p><p>{e(state['reason'] or state['input_problem'] or state['next_action']['summary'])}</p><p>下一步：{e(state['next_action']['summary'])}</p><p>任务输入：{validity(state['inputs_current'])}　最终候选：{validity(state['final_candidate_current'])}</p><ul>{questions}</ul></section>
{planning_section}
{worker_section}
{operation_section}
{activity_section}
{boundary_section}
{supervisor_section}
{project_section}
{verification_section}
{draft_section}
<section><h2>团队任务</h2><table><tr><th>任务</th><th>角色</th><th>阶段</th><th>状态</th><th>说明</th></tr>{rows}</table></section>
{preflight_section}
<section><h2>实测检查记录</h2><small>结果绑定各自记录中的候选版本；历史通过不能证明后来修改的代码通过。</small><table><tr><th>任务</th><th>检查</th><th>结果</th><th>说明</th></tr>{checks}</table><ul>{problems}</ul></section>
<section><h2>最近记录</h2><ul>{events}</ul><small>最后控制器记录：{e(state['last_checkpoint_at'])}。页面每 3 秒刷新，MCP 连接期间由服务更新；关闭连接后只保留最后快照。模型费用和全局调用量未知。</small></section></main></html>'''
