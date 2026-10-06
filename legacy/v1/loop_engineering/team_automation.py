"""Continuous supervised coordination, clarification, isolated work and rework.

Run is an explicit bounded dispatch. Missing human/evaluator/accounting abilities
stop progress. Existing task scopes and checks are never weakened by rework.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import threading
import time

from reference.core import canonical_digest, strict_json_loads
from .budgets import BudgetError, Ledger
from .contracts import ROOT, ContractError, load, validate, validate_step
from .evaluators import sign_result
from .processes import process_alive, process_group_alive
from .scenarios import context as scenario_context, inputs, role_assignment, task_inputs_digest, _team_status
from .stages import validate_stages
from .team_engine import MissingHandoffOutput, TeamController
from .team_host import TeamHost
from .team_memory import build as memory_context, commit_report, fit_request, pending_report
from .team_policy import enable, policy_file, remaining
from .workspace import atomic_write, byte_digest, safe_path


def object_schema(properties):
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(properties)}


TEXT = {"type": "string", "minLength": 1}
PLAN_RESPONSE = object_schema({
    "selection_json": TEXT, "workflow_json": {"type": ["string", "null"]}, "summary": TEXT,
    "contracts": {"type": "array", "items": object_schema({"path": TEXT, "task_json": TEXT,
        "engine_path": {"type": ["string", "null"]}, "engine_json": {"type": ["string", "null"]}})},
    "questions": {"type": "array", "items": object_schema({"id": TEXT, "question": TEXT, "reason": TEXT, "blocking": {"type": "boolean"}})}
})
RECEIPT_RESPONSE = object_schema({"accept": {"type": "boolean"}, "note": TEXT})
REVIEW_RESPONSE = object_schema({"result": {"enum": ["pass", "fail", "inconclusive"]}, "summary": TEXT,
    "findings": {"type": "array", "items": object_schema({"id": TEXT, "severity": {"enum": ["blocking", "note"]},
                    "description": TEXT, "criterion_id": {"type": ["string", "null"]}})}})
SPEC_RESPONSE = object_schema({
    "status": {"enum": ["ready", "needs_input"]}, "summary": TEXT,
    "spec_markdown": {"type": "string", "maxLength": 1048576},
    "sources": {"type": "array", "maxItems": 100, "items": TEXT},
    "questions": {"type": "array", "maxItems": 3, "items": object_schema({
        "id": TEXT, "question": TEXT, "reason": TEXT, "blocking": {"type": "boolean"}})}
})


class TeamAutomation:
    def __init__(self, team: TeamController, host):
        self.team, self.host = team, host

    def _stage_request(self, team_id, slot, packet):
        """Freeze memory with intake requests until their response is consumed.

        Team revisions/accounting advance during dispatch. Rebuilding a memory
        packet after a response crash must not turn a cached result into a new
        paid request. Failed, reconciled calls receive fresh failure context.
        """
        with self.team.teams.writer(team_id, wait_seconds=5) as data:
            self.team._assert_inputs(data)
            if self.team.teams.signal(team_id):
                raise ContractError("Stopped team cannot prepare a stage request")
            fit_request(packet, self.team.profile(data)["context_max_bytes"])
            candidate = self.team.snapshots.capture(Path(data["workspace"]), self.team.profile(data))["digest"]
            if candidate != packet["context"]["candidate"]:
                raise ContractError("Project changed before stage request; prepare fresh context")
            binding = {"scenario_digest": data["scenario_digest"], "candidate": candidate,
                "feedback": canonical_digest([data.get("preparation_feedback", []), data.get("specification_feedback", [])]),
                "host": self.host.describe(packet)}
            saved = data.get(slot)
            if saved and saved["binding"] == binding:
                digest = canonical_digest(saved["packet"])
                failed = any(op["request_digest"] == digest and op["status"] in {"FAILED", "CONSUMED"}
                             for op in data["automation"]["operations"].values())
                if not failed:
                    return deepcopy(saved["packet"])
            data[slot] = {"binding": binding, "packet": deepcopy(packet)}
            self.team.teams.save(data, "team.stage_context_saved")
            return packet

    def start(self, root: Path, policy=None, *, specification=False, brief=None):
        root = root.resolve()
        chosen = load(policy or policy_file(root), "team-policy")
        if self.team.store.directory.is_relative_to(root):
            raise ContractError("Team state must be outside the project")
        manifest, documents = inputs(root)
        plan = load(safe_path(root, ".loop/workflow-tasks.json"), "team-workflow")
        collect_spec = specification or not documents[".loop/spec.md"].strip()
        if collect_spec and plan["tasks"]:
            raise ContractError("Requirements setup cannot change a prepared workflow; stop it and prepare a new setup")
        if plan["tasks"]:
            data = self.team.build(root)
            data["stage"] = "EXECUTION"
        else:
            data = {"workspace": str(root), "plan": plan, "tasks": {}, "records": {}, "dependencies": {},
                    "bound_files": {".loop/project.json": byte_digest(read_bytes(root / ".loop/project.json"))},
                    "scenario_digest": task_inputs_digest(root), "status": "ACTIVE", "reason": None,
                    "stage": "SPEC" if collect_spec else "INTAKE"}
        if collect_spec:
            from .spec_setup import intake
            data["specification"] = intake(brief, documents.get(".loop/setup.md"))
        elif brief is not None:
            raise ContractError("A brief is only accepted for requirements setup")
        enable(data, chosen)
        data["initial_snapshot"] = self.team.snapshots.capture(root, self.team.profile(data))
        self.team.teams.create(data)
        return self.team.status(data["team_id"])

    def begin_spec(self, team_id, *, brief=None):
        """Give an unfinished planning-only team the setup assignment, in place."""
        from .spec_setup import intake
        with self.team.teams.writer(team_id) as data:
            self.team._assert_inputs(data)
            if "specification" in data:
                if brief is not None and brief != data["specification"]["brief"]:
                    raise ContractError("The setup brief is frozen; use team-answer for actual clarifications")
                return
            if (data.get("stage") != "INTAKE" or data["status"] not in {"ACTIVE", "AWAITING_INPUT"}
                    or self.team.teams.signal(team_id) or data.get("pending_control") or data["tasks"]
                    or "automation" not in data):
                raise ContractError("Only an unfinished planning team can enter requirements setup")
            if any(row["status"] in {"ADMITTED", "RESPONDING"} for row in data["automation"]["operations"].values()):
                raise ContractError("Reconcile the outstanding operation before requirements setup")
            root = Path(data["workspace"])
            if load(root / ".loop/workflow-tasks.json", "team-workflow")["tasks"]:
                raise ContractError("Requirements setup cannot change a prepared workflow")
            _, documents = inputs(root)
            data["specification"] = intake(brief, documents.get(".loop/setup.md"))
            data.update(stage="SPEC", status="ACTIVE", reason=None)
            self.team.teams.save(data, "team.requirements_setup_assigned")

    def prepare_spec(self, team_id):
        from .spec_setup import request_context, validate_result
        data = self.team.teams.get(team_id)
        self.team._assert_inputs(data)
        root = Path(data["workspace"])
        questions = load(root / ".loop/questions.json", "questions")
        if any(q["blocking"] and not q["answers"] for q in questions["questions"]):
            self._state(team_id, "AWAITING_INPUT", "Requirements agent needs answers to recorded questions")
            return
        context = request_context(self.team, data)
        request = {"kind": "team-specification-request", "role": "requirements_reviewer", "context": context,
                   "instruction": "Perform only the setup requirements-collection assignment. Read supplied docs first, incorporate actual answers, and return a grounded spec or up to three material questions. Do not invent requirements, source contents, user answers or acceptance evidence. No task contracts, code changes or development dispatch are authorized here."}
        request = self._stage_request(team_id, "specification_request", request)
        context = request["context"]
        response = self.host.invoke(team_id, request, SPEC_RESPONSE)
        try:
            waiting = validate_result(response, context, questions)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            with self.team.teams.writer(team_id) as current:
                self.team._assert_inputs(current)
                auto = current["automation"]
                if auto["reworks"] >= auto["policy"]["max_reworks"]:
                    raise BudgetError("Requirements preparation repair budget exhausted: " + str(exc)) from exc
                auto["reworks"] += 1
                current.setdefault("specification_feedback", []).append(str(exc)[:1500])
                current.pop("specification_request", None)
                self.team.teams.save(current, "team.specification_repair_requested")
            return
        with self.team.teams.writer(team_id) as current:
            self.team._assert_inputs(current)
            if self.team.teams.signal(team_id):
                raise ContractError("Stopped team cannot install requirements")
            snapshot = self.team.snapshots.capture(root, self.team.profile(current))
            if snapshot["digest"] != context["candidate"]:
                raise ContractError("Project documents changed during requirements collection; retry with fresh sources")
            writes = {".loop/questions.json": encode(questions)}
            if response["spec_markdown"].strip():
                writes[".loop/spec-draft.md"] = response["spec_markdown"].encode()
            if not waiting:
                writes[".loop/spec.md"] = response["spec_markdown"].encode()
            current["specification"].update(state="COLLECTING_PENDING" if waiting else "READY_PENDING", summary=response["summary"],
                                           draft=response["spec_markdown"], sources=response["sources"])
            current["pending_control"] = [{"path": name,
                "old": byte_digest(read_bytes(safe_path(root, name))) if safe_path(root, name).exists() else None,
                "new": self.team.snapshots.put(content)} for name, content in writes.items()]
            _, frozen = inputs(root)
            current["pending_unchanged_inputs"] = {name: byte_digest(text.encode())
                for name, text in frozen.items() if name not in writes}
            current["pending_unchanged_inputs"].update({name: digest for name, digest in current["bound_files"].items() if name not in writes})
            current["pending_source_digest"] = context["candidate"]
            pending_report(current, response, stage="SPEC", role="requirements_reviewer", candidate=context["candidate"])
            current.pop("specification_request", None)
            current["stage"] = "SPEC" if waiting else "INTAKE"
            self.team.teams.save(current, "team.specification_files_prepared")
            self._apply_control(current)
        if waiting:
            self._state(team_id, "AWAITING_INPUT", "Requirements agent needs answers to recorded questions")

    def _inputs(self, data, *, role="coordinator", workspace=None, planning=False, task_id=None, independent=False):
        root = Path(workspace or data["workspace"])
        profile = self.team.profile(data)
        snap = self.team.snapshots.capture(root, profile)
        docs = []
        used = 0
        for item in snap["manifest"]["files"]:
            if item["kind"] != "file":
                raise ContractError("Team coordination requires regular snapshot files")
            raw = self.team.snapshots.read(item["sha256"])
            try:
                content = raw.decode()
            except UnicodeDecodeError:
                continue
            if used + len(raw) <= max(0, profile["context_max_bytes"] // 3):
                docs.append({"path": item["path"], "sha256": item["sha256"], "content": content})
                used += len(raw)
        manifest, documents = inputs(Path(data["workspace"]))
        assignment = role_assignment(manifest, documents, role)
        names = ["LOOP.md", ".loop/spec.md", ".loop/questions.json", ".loop/team.json", ".loop/workflow.md", ".loop/agent-instructions.md", *assignment["instruction_paths"]]
        if "agent_protocol" in manifest:
            names.append(manifest["agent_protocol"])
        if "execution_tools" in manifest:
            names.append(manifest["execution_tools"])
        if "AGENTS.md" in documents:
            names.append("AGENTS.md")
        material = scenario_context(Path(data["workspace"]), role) if planning else {
            "role": assignment["role"], "role_assignment": assignment,
            "instructions": [{"path": name, "content": documents[name]} for name in dict.fromkeys(names)]}
        return {"scenario": material, "candidate": snap["digest"],
                "memory": memory_context(self.team, data, role=role, task_id=task_id,
                    candidate=snap["digest"], independent=independent, documents=documents),
                "sources": docs, "source_manifest": snap["manifest"]}

    def _apply_control(self, data):
        pending = data.get("pending_control")
        if not pending:
            return
        root = Path(data["workspace"])
        for name, digest in data.get("pending_unchanged_inputs", {}).items():
            if byte_digest(read_bytes(safe_path(root, name))) != digest:
                raise ContractError("Frozen input changed during requirements installation: " + name)
        if data.get("pending_source_digest"):
            snapshot = self.team.snapshots.capture(root, self.team.profile(data))
            if snapshot["digest"] != data["pending_source_digest"]:
                raise ContractError("Project sources changed during requirements installation")
        for item in pending:
            path = safe_path(root, item["path"])
            current = byte_digest(path.read_bytes()) if path.exists() else None
            if current not in {item["old"], item["new"]}:
                raise ContractError("Coordinator control update conflicts with a user change")
        for item in pending:
            atomic_write(safe_path(root, item["path"]), self.team.snapshots.read(item["new"]))
        data["pending_control"] = None
        data.pop("pending_unchanged_inputs", None)
        data.pop("pending_source_digest", None)
        if data.get("specification", {}).get("state") in {"COLLECTING_PENDING", "READY_PENDING"}:
            data["specification"]["state"] = "READY" if data["specification"]["state"] == "READY_PENDING" else "COLLECTING"
        data["scenario_digest"] = task_inputs_digest(root)
        if data["stage"] == "EXECUTION":
            prepared = self.team.build(root)
            for name in ("plan", "tasks", "records", "dependencies", "bound_files", "scenario_digest"):
                data[name] = prepared[name]
        commit_report(data)
        self.team.teams.save(data, "team.coordinator_files_applied")

    def prepare(self, team_id):
        data = self.team.teams.get(team_id)
        if data.get("pending_control"):
            with self.team.teams.writer(team_id) as current:
                self._apply_control(current)
            return
        self.team._assert_inputs(data)
        root = Path(data["workspace"])
        questions = load(root / ".loop/questions.json", "questions")
        if any(q["blocking"] and not q["answers"] for q in questions["questions"]):
            self._state(team_id, "AWAITING_INPUT", "Answer the recorded blocking questions")
            return
        request = {"kind": "team-coordinator-request", "context": self._inputs(data, planning=True),
                   "instruction": "Interpret the spec and inspect the repository. Ask only material unanswered questions. Return current role decisions and a dependency-ordered workflow with real task contracts, meaningful actual checks, concrete outputs and a final integrated acceptance task. Include independent reviewer/acceptance checks when required. Use provided task schemas; JSON encode contracts and selection. Do not invent user answers or mark required assessments inapplicable to avoid missing capabilities. Task and engine paths must be under .loop/tasks/. Set repair_task to the appropriate implementing dependency for review/test/acceptance tasks. Preserve existing project/test work.",
                   "policy": data["automation"]["policy"], "engine_template": load(ROOT / "templates/engine.json"),
                   "workflow_schema": load(ROOT / "schemas/team-workflow.schema.json"),
                   "host": self.host.describe(),
                   "execution_contract": "You prepare contracts for the actual team-run scheduler. It dispatches each worker/recipient as a separate host request and performs registered code reviews with separate fresh requests and private signing authorities. Assign supported code-review/acceptance roles to this host (adapter_id:role), instead of marking them pending merely because you cannot delegate tools yourself. You must only return the plan; the scheduler owns execution. This actual route replaces portable-mode delegation assumptions in scenario context. Required human/browser/load/artifact execution still needs its actual executor.",
                   "preparation_feedback": [{"round": row["round"], "error": row["error"][:600]}
                                            for row in data.get("preparation_feedback", [])[-8:]],
                   "review_configuration": "The preparation layer supplies native runtime/evaluator configuration and a required separate review check for independent roles. Keep your meaningful command/spec criteria; you may omit engine bodies for these defaults. Do not remove human/UI/load/artifact requirements.",
                   "available_evaluation": "Actual bounded command checks and separate code-review requests; human, browser, load/security and artifact procedures need their own registered executor. Keep required unavailable procedures pending."}
        if getattr(self.host, "desktop", False):
            request["execution_contract"] += " Desktop workers consume native MCP queued requests under operator supervision."
            if not self.host.review_codex:
                request["available_evaluation"] = "Actual command checks are available. Desktop cannot attest fresh independent model review; a separately configured reviewer host is required. Keep required reviews pending until that host is configured."
                request["execution_contract"] = "The scheduler queues Desktop work through native MCP. No separate independent reviewer is configured; do not assert that this chat satisfies independent review. Required unavailable procedures remain pending."
        from .execution_tools import configured_tools
        tools = configured_tools(root, self.team.profile(data))
        registered = [{"check_id": plan["check_id"], "kind": plan["kind"],
                       "check_type": "interaction" if plan["kind"] == "browser" else "artifact"}
                      for plan in (tools or {}).get("executors", [])]
        request["registered_execution_tools"] = registered
        request["execution_contract"] += " Supervised browser/HTTP checks execute only when their exact check_id/type matches a frozen registered_execution_tools entry. Human judgment and unspecified scanning/visual/performance procedures remain pending. Set task extensions.agent_step_version to 0.3 when deletion or binary file proposals are needed; the default text schema is 0.2."
        if registered:
            request["available_evaluation"] += " These actual local executor plans are registered: " + json.dumps(registered)
        request = self._stage_request(team_id, "preparation_request", request)
        response = self.host.invoke(team_id, request, PLAN_RESPONSE)
        try:
            writes, waiting = self._preparation_result(data, response, questions)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            with self.team.teams.writer(team_id) as current:
                self.team._assert_inputs(current)
                auto = current['automation']
                if auto['reworks'] >= auto['policy']['max_reworks']:
                    raise BudgetError('Coordinator preparation repair budget exhausted: ' + str(exc)) from exc
                auto['reworks'] += 1
                current.setdefault('preparation_feedback', []).append({
                    'round': auto['reworks'], 'error': str(exc)[:1500],
                    'response_digest': canonical_digest(response)})
                current.pop("preparation_request", None)
                self.team.teams.save(current, 'team.preparation_repair_requested')
            return
        with self.team.teams.writer(team_id) as current:
            self.team._assert_inputs(current)
            current["pending_control"] = [{"path": name, "old": byte_digest(read_bytes(safe_path(root, name))) if safe_path(root, name).exists() else None,
                                           "new": self.team.snapshots.put(encode(value))} for name, value in writes.items()]
            pending_report(current, response, stage="INTAKE", role="coordinator", candidate=request["context"]["candidate"])
            current.pop("preparation_request", None)
            current["stage"] = "INTAKE" if waiting else "EXECUTION"
            self.team.teams.save(current, "team.coordinator_files_prepared")
            self._apply_control(current)
        if waiting:
            self._state(team_id, "AWAITING_INPUT", "Coordinator needs answers to the recorded questions")

    def _preparation_result(self, data, response, questions):
        root = Path(data['workspace'])
        writes = {}
        known = {q["id"] for q in questions["questions"]}
        for question in response["questions"]:
            if question["id"] in known:
                raise ContractError("Coordinator cannot replace an existing question or answer")
            known.add(question["id"])
            questions["questions"].append({**question, "answers": []})
        validate("questions", questions)
        writes[".loop/questions.json"] = questions
        waiting = any(q["blocking"] and not q["answers"] for q in questions["questions"])
        if not waiting:
            selection = strict_json_loads(response["selection_json"])
            validate("team", selection)
            manifest, documents = inputs(root)
            documents[".loop/questions.json"] = encode(questions).decode()
            from .scenarios import selection_basis
            selection["basis_digest"] = selection_basis(manifest, documents)
            documents[".loop/team.json"] = encode(selection).decode()
            selected = _team_status(manifest, documents)
            if selected["state"] != "ready":
                raise ContractError("Coordinator team selection still has pending responsibilities: " + ", ".join(selected["pending_roles"]))
            plan = strict_json_loads(response["workflow_json"] or "null")
            validate("team-workflow", plan)
            if not plan["tasks"]:
                raise ContractError("Coordinator returned an empty workflow")
            by_id = {t["id"]: t for t in plan["tasks"]}
            for entry in response["contracts"]:
                name = entry["path"]
                if not name.startswith(".loop/tasks/"):
                    raise ContractError("Coordinator contracts must be under .loop/tasks/")
                safe_path(root, name)
                contract = strict_json_loads(entry["task_json"])
                validate("task", contract)
                from .native_readiness import validate_decisions
                validate_decisions(contract, questions)
                task = by_id.get(contract["task_id"])
                if not task or task["task_file"] != name or name in writes:
                    raise ContractError("Coordinator contract does not match a unique planned task")
                role_definition = next((role for role in manifest["roles"] if role["id"] == task["role"]), None)
                if role_definition is None:
                    raise ContractError("Unknown planned role: " + task["role"])
                original_contract = deepcopy(contract)
                added_review_criterion = None
                if role_definition["independent"] and not any(c["type"] == "review" and c.get("independent") for c in contract["checks"]):
                    check_id = "team-independent-" + task["role"]
                    while check_id in {c["id"] for c in contract["checks"]} | {c["id"] for c in contract["criteria"]}:
                        check_id += "-next"
                    contract["checks"].append({"id": check_id, "type": "review", "independent": True,
                        "description": "Required separate " + task["role"] + " assessment of the actual candidate",
                        "procedure": ["Inspect the exact candidate, accepted task/spec and actual check artifacts in a separate context.",
                                      "Apply the assigned professional role instructions. Report concrete blocking findings; pass only when the stated requirements are met.",
                                      "Code inspection does not substitute for required human, UI, scan, load or artifact execution."]})
                    contract["criteria"].append({"id": check_id, "description": "Required independent " + task["role"] + " assessment passes",
                                                "check_ids": [check_id]})
                    added_review_criterion = check_id
                    validate("task", contract)
                writes[name] = contract
                needs_native = any(c["type"] != "command" for c in contract["checks"])
                if entry["engine_path"] or task["engine_file"] or needs_native or entry["engine_json"] is not None:
                    engine_name = entry["engine_path"] or task["engine_file"] or ".loop/tasks/" + task["id"] + "-engine.json"
                    if not engine_name.startswith(".loop/tasks/") or task["engine_file"] and engine_name != task["engine_file"]:
                        raise ContractError("Coordinator engine path differs from its planned task")
                    safe_path(root, engine_name)
                    task["engine_file"] = engine_name
                    explicit_engine = entry["engine_json"] is not None
                    config = strict_json_loads(entry["engine_json"]) if explicit_engine else deepcopy(
                        self.host.engine or load(ROOT / "templates/engine.json"))
                    from .native_contracts import validate_native
                    validate_native("engine", config)
                    try:
                        if explicit_engine:
                            # Reject an invalid declared graph before adding the
                            # preparation layer's independent assessment.
                            validate_stages(original_contract, config["stages"])
                            if added_review_criterion and contract["workflow"] == "staged":
                                stage_id = added_review_criterion
                                while stage_id in {stage["id"] for stage in config["stages"]}:
                                    stage_id += "-next"
                                config["stages"].append({"id": stage_id,
                                    "depends_on": [stage["id"] for stage in config["stages"]],
                                    "criterion_ids": [added_review_criterion],
                                    "write_allow": deepcopy(contract["scope"]["write_allow"]),
                                    "write_deny": ["**"]})
                        elif contract["workflow"] == "staged":
                            # A generic engine template has no task-specific
                            # graph. Own every final criterion, including review.
                            config["stages"] = [{"id": "delivery", "depends_on": [],
                                "criterion_ids": [criterion["id"] for criterion in contract["criteria"]],
                                "write_allow": deepcopy(contract["scope"]["write_allow"]),
                                "write_deny": deepcopy(contract["scope"]["write_deny"])}]
                        validate_stages(contract, config["stages"])
                    except ContractError as exc:
                        raise ContractError("Invalid team task stage configuration: " + task["id"] + ": " + str(exc)) from exc
                    keys = {}
                    for check in contract["checks"]:
                        if check["type"] != "command":
                            role = {"review": "reviewer", "human": "human", "interaction": "interaction", "artifact": "artifact"}[check["type"]]
                            key_id = "team-" + task["role"] + "-" + role
                            try:
                                self.team.store.authorities.create(key_id, role)
                            except FileExistsError:
                                if self.team.store.authorities.key(key_id)["role"] != role:
                                    raise ContractError("Existing evaluator key has a different role")
                            keys.setdefault(key_id, {"key_id": key_id, "role": role, "check_ids": []})["check_ids"].append(check["id"])
                    config["evaluator_keys"] = list(keys.values())
                    if engine_name in writes or engine_name in {t["task_file"] for t in plan["tasks"]}:
                        raise ContractError("Coordinator engine path collides with another control file")
                    writes[engine_name] = config
            if any(task["task_file"] not in writes or task["engine_file"] and task["engine_file"] not in writes for task in plan["tasks"]):
                raise ContractError("Coordinator did not provide every referenced contract/engine")
            writes.update({".loop/team.json": selection, ".loop/workflow-tasks.json": plan,
                           ".loop/task.json": writes[plan["tasks"][0]["task_file"]]})
            self._validate_preparation(data, writes)
        return writes, waiting

    def answer(self, team_id, question_id, answer):
        if not answer.strip():
            raise ContractError("A human answer must be nonblank")
        with self.team.teams.writer(team_id) as data:
            if data.get("stage") not in {"SPEC", "INTAKE"} or data["status"] != "AWAITING_INPUT":
                raise ContractError("Only an intake clarification can be answered through this command")
            self.team._assert_inputs(data)
            root = Path(data["workspace"])
            value = load(root / ".loop/questions.json", "questions")
            question = next((q for q in value["questions"] if q["id"] == question_id), None)
            if question is None:
                raise ContractError("Unknown team question")
            question["answers"].append(answer)
            validate("questions", value)
            data["pending_control"] = [{"path": ".loop/questions.json", "old": byte_digest(read_bytes(root / ".loop/questions.json")),
                                       "new": self.team.snapshots.put(encode(value))}]
            self.team.teams.save(data, "team.human_answer_prepared")
            self._apply_control(data)
            waiting = any(q["blocking"] and not q["answers"] for q in value["questions"])
            data.update(status="AWAITING_INPUT" if waiting else "ACTIVE",
                        reason="Waiting for actual user answers" if waiting else None)
            self.team.teams.save(data, "team.human_answer_recorded")
        return self.team.status(team_id)

    def _validate_preparation(self, data, writes):
        """Validate every proposed control file together before project writes."""
        from tempfile import TemporaryDirectory
        root = Path(data["workspace"])
        snapshot = self.team.snapshots.capture(root, self.team.profile(data))
        with TemporaryDirectory(prefix="plan-check-", dir=self.team.store.run_dir(data["team_id"])) as directory:
            target = Path(directory) / "project"
            self.team.snapshots.materialize(snapshot, target)
            _, docs = inputs(root)
            for name, content in docs.items():
                atomic_write(safe_path(target, name), content.encode())
            for name in (".loop/task.json", ".loop/project.json"):
                atomic_write(safe_path(target, name), read_bytes(safe_path(root, name)))
            for name, value in writes.items():
                atomic_write(safe_path(target, name), encode(value))
            self.team.build(target)

    def _state(self, team_id, status, reason):
        with self.team.teams.writer(team_id, wait_seconds=5) as data:
            if self.team.teams.signal(team_id):
                status = self.team.teams.signal(team_id)
            data.update(status=status, reason=reason)
            self.team.teams.save(data, "team.scheduler_checkpoint")

    def reconcile(self, team_id, operation_id, note):
        if not note.strip():
            raise ContractError("Reconciliation needs an actual inspection note")
        with self.team.teams.writer(team_id) as data:
            row = data["automation"]["operations"].get(operation_id)
            if not row or row["status"] not in {"ADMITTED", "RESPONDING"}:
                raise ContractError("Unknown or already resolved team operation")
            pid = row["pid"]
            if pid and (process_alive(pid) or process_group_alive(pid)):
                raise ContractError("Stop and inspect the earlier process before reconciliation")
            reservation = next((r for r in data["automation"]["reservations"] if r["request_id"] == operation_id), None)
            if reservation and reservation["status"] == "held":
                Ledger(data["automation"]["reservations"]).settle(operation_id, None)
            row.update(status="FAILED", reconciliation_note=note, pid=None, identity=None)
            run_id = row.get("child_run_id")
            if run_id and self.team._exists(run_id):
                with self.team.store.writer(run_id) as child:
                    if "native" in child:
                        held = next((r for r in child["native"]["reservations"] if r["request_id"] == operation_id), None)
                        if held and held["status"] == "held":
                            Ledger(child["native"]["reservations"]).settle(operation_id, None)
                            TeamHost._sync_native(child)
                    if child.get("team_operation_id") == operation_id:
                        child["elapsed_ms"] += max(1, time.time_ns() // 1000000 - child["operation_started_ms"])
                        child.update(operation_started_ms=None, team_operation_id=None)
                        child["state"]["usage"]["wall_seconds"] = (child["elapsed_ms"] + 999) // 1000
                    self.team.store.save(child, "team.child_operation_reconciled")
            data.update(status="ACTIVE", reason=None)
            self.team.teams.save(data, "team.operation_reconciled")
        return self.team.status(team_id)

    def rework(self, team_id, failed_task, note):
        with self.team.teams.writer(team_id) as data:
            self.team._assert_inputs(data)
            if self.team.teams.signal(team_id):
                raise ContractError("Stopped team cannot schedule rework")
            auto = data["automation"]
            if auto["reworks"] >= auto["policy"]["max_reworks"]:
                raise BudgetError("Team rework budget exhausted")
            task = data["tasks"][failed_task]
            target = task.get("repair_task") or failed_task
            affected = {target} | {key for key, deps in data["dependencies"].items() if target in deps}
            if any(r["status"] in {"ADMITTED", "RESPONDING"} for r in auto["operations"].values()):
                raise ContractError("Reconcile all active operations before rework")
            auto["reworks"] += 1
            auto["feedback"].append({"failed_task": failed_task, "repair_task": target, "round": auto["reworks"], "finding": note[:12000]})
            for key in sorted(affected):
                record = data["records"][key]
                auto["history"].append({"task": key, "round": auto["reworks"], "record": deepcopy(record)})
                for run_id in (record["child_run_id"], record.get("integration_run_id")):
                    if run_id and self.team._exists(run_id) and self.team.store.get(run_id)["pending_process"]:
                        raise ContractError("Reconcile a child process before rework")
                data["records"][key] = {"status": "PENDING", "child_run_id": None, "request": None,
                                       "handoff": None, "receipts": {}, "reason": None}
            data.update(status="ACTIVE", reason=None)
            self.team.teams.save(data, "team.rework_scheduled")

    def _review_material(self, data, task_id, child):
        initial = data.get("initial_snapshot")
        material = {"initial_snapshot": initial, "original_changed_sources": [], "verified_dependencies": []}
        if initial is not None:
            current = self.team.snapshots.capture(Path(child["workspace"]), child["profile"])
            before = {f["path"]: f for f in initial["manifest"]["files"]}
            after = {f["path"]: f for f in current["manifest"]["files"]}
            material["observed_changed_paths"] = sorted(
                name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
            for name in material["observed_changed_paths"]:
                item = before.get(name)
                if item and item["kind"] == "file":
                    raw = self.team.snapshots.read(item["sha256"])
                    material["original_changed_sources"].append({"path": name, "sha256": item["sha256"],
                        "content": raw[:4000].decode("utf-8", errors="replace"), "truncated": len(raw) > 4000})
        for dependency in data["dependencies"][task_id]:
            record = data["records"][dependency]
            if record["status"] != "COMPLETE" or not record["handoff"]:
                continue
            if record["handoff"]["candidate"] != child["state"]["snapshot_digest"]:
                continue
            predecessor, snapshot = self.team._verified(data, dependency)
            controller = self.team._controller(data, dependency,
                verification=bool(record.get("integration_run_id")))
            material["verified_dependencies"].append({"task_id": dependency,
                "task_contract": predecessor["task"], "candidate": snapshot["digest"],
                "environment": predecessor["state"]["environment_digest"],
                "checks": self.team.store.evidence(predecessor["run_id"], predecessor["selected_evidence"]),
                "observations": controller._summaries(predecessor, predecessor["selected_evidence"])})
        return material

    def _evaluate(self, team_id, task_id, *, integrated=False):
        data = self.team.teams.get(team_id)
        child = self.team._child(data, task_id, verification=integrated)
        if "native" not in child:
            return None
        controller = self.team._controller(data, task_id, verification=integrated)
        command_ids = {c["id"] for c in child["task"]["checks"] if c["type"] == "command"}
        passed = {e["check_id"] for e in self.team.store.evidence(child["run_id"], child["selected_evidence"])
                  if e["result"] == "pass" and e["snapshot_digest"] == child["state"]["snapshot_digest"]}
        if not command_ids.issubset(passed):
            return None
        for check in child["task"]["checks"]:
            if check["type"] == "command":
                continue
            saved_evidence = child["native"]["external_by_check"].get(check["id"])
            if saved_evidence:
                record = self.team.store.evidence(child["run_id"], [saved_evidence])[0]
                if record["snapshot_digest"] == child["state"]["snapshot_digest"] and record["environment_digest"] == controller.environment_digest(child):
                    controller._check_artifacts([record])
                    if record["result"] == "inconclusive":
                        self._state(team_id, "AWAITING_INPUT", record["summary"])
                        return "waiting"
                    if record["result"] != "pass":
                        return record["summary"] + " " + json.dumps(record["extensions"].get("findings", []))
                    continue
            if check["type"] == "human":
                self._state(team_id, "AWAITING_INPUT", "Actual human evaluator required for " + check["id"])
                return "waiting"
            if check["type"] != "review":
                from .external_execution import executor_for
                if executor_for(child, check["id"]) is None:
                    self._state(team_id, "AWAITING_INPUT", "An actual registered interaction/artifact executor is required")
                    return "waiting"
                record = controller.execute_evaluation(child["run_id"], check["id"])
                if record["result"] == "inconclusive":
                    self._state(team_id, "AWAITING_INPUT", record["summary"])
                    return "waiting"
                if record["result"] != "pass":
                    return record["summary"]
                child = self.team._child(self.team.teams.get(team_id), task_id, verification=integrated)
                continue
            cache_key = child["run_id"] + ":" + check["id"]
            saved = self.team.teams.get(team_id)["records"][task_id].get("review_requests", {}).get(cache_key)
            if saved and (saved["request"]["payload"]["snapshot_digest"] != child["state"]["snapshot_digest"]
                          or saved["request"]["payload"]["environment_digest"] != controller.environment_digest(child)):
                saved = None
            if saved is None:
                request = controller.evaluation_request(child["run_id"], check["id"])
                context = controller.context(child["run_id"])
                saved = {"request": request, "packet": {"kind": "team-evaluator-request", "role": data["tasks"][task_id]["role"],
                    "request": request["payload"], "context": context,
                    "inspection": self._inputs(data, role=data["tasks"][task_id]["role"], workspace=child["workspace"],
                        task_id=task_id, independent=True),
                    "comparison_and_checks": self._review_material(data, task_id, child),
                    "instruction": "Perform the stated independent review on this exact candidate. Inspect the actual code and retained check results. Return blocking findings when requirements are unmet; do not invent browser/load/human evidence."}}
                with self.team.teams.writer(team_id, wait_seconds=5) as current:
                    current["records"][task_id].setdefault("review_requests", {})[cache_key] = saved
                    self.team.teams.save(current, "team.review_context_saved")
            request = saved["request"]
            response = self.host.invoke(team_id, saved["packet"], REVIEW_RESPONSE,
                final=task_id == data["plan"]["final_task"])
            artifact = self.team.store.run_dir(team_id) / ("review-" + request["payload"]["request_id"] + ".json")
            atomic_write(artifact, encode(response))
            key = next(k for k in child["native"]["config"]["evaluator_keys"] if check["id"] in k["check_ids"] and k["role"] == "reviewer")
            signed = sign_result(self.team.store.authorities, request, key["key_id"], result=response["result"],
                                 summary=response["summary"], artifacts=[artifact], findings=response["findings"])
            controller.import_evaluation(child["run_id"], signed)
            if response["result"] != "pass":
                return json.dumps(response, ensure_ascii=False)
        if child["state"]["status"] != "SUCCEEDED":
            controller.resume(child["run_id"])
            controller.verify(child["run_id"])
        return None

    def _worker(self, team_id, task_id):
        data = self.team.teams.get(team_id)
        record = data["records"][task_id]
        if record["child_run_id"] and self.team._exists(record["child_run_id"]):
            child = self.team._child(data, task_id)
            if child["pending_process"] or child["pending_edit"]:
                self.team._controller(data, task_id).resume(child["run_id"])
            if child["state"]["status"] == "SUCCEEDED":
                return None
            if child["state"]["status"] in {"BUDGET_EXHAUSTED", "CANCELLED", "FAILED"}:
                raise BudgetError("Child task is terminal; team budget and checks cannot be reset")
            if child["state"]["status"] != "PLANNING":
                if child["steps"] and child["steps"][-1]["intent"] in {"need_input", "blocked"}:
                    self._state(team_id, "AWAITING_INPUT", child["state"]["reason"] or "Worker needs actual input")
                    return "waiting"
                failed = self._evaluate(team_id, task_id)
                if failed:
                    return failed
                child = self.team._child(self.team.teams.get(team_id), task_id)
                if child["state"]["status"] == "SUCCEEDED":
                    return None
                if child["state"]["status"] != "PLANNING":
                    return child["state"].get("reason") or "Task requires attention"
        request = self.team.request(team_id, task_id, wait_seconds=5, managed=True)
        run_id = request["task_context"].get("run_id") or self.team.teams.get(team_id)["records"][task_id]["child_run_id"]
        from .contracts import step_schema
        response = self.host.invoke(team_id, request, step_schema(self.team.store.get(run_id)["task"]),
                                    final=task_id == data["plan"]["final_task"], child_run_id=run_id)
        child = self.team.store.get(run_id)
        try:
            validate_step(response, child["task"], request["task_context"]["base_snapshot_digest"])
        except ContractError as exc:
            # A fully received, accounted proposal can be corrected without
            # resetting its child or retrying an ambiguous dispatch. Validate
            # before effects; stale live inputs still require reconciliation.
            with self.team.teams.writer(team_id, wait_seconds=5) as current:
                self.team._assert_inputs(current)
                self.team._assert_request_candidate(current, task_id)
                if self.team.teams.signal(team_id):
                    raise ContractError("Stopped team cannot correct a proposal")
                auto, record = current["automation"], current["records"][task_id]
                if auto["reworks"] >= auto["policy"]["max_reworks"]:
                    raise BudgetError("Team invalid-proposal correction budget exhausted")
                auto["reworks"] += 1
                auto["feedback"].append({"failed_task": task_id, "repair_task": task_id,
                    "round": auto["reworks"], "finding": "Invalid proposal: " + str(exc)[:12000]})
                record.setdefault("rejected_proposals", []).append({"request": deepcopy(record["request"]),
                    "response_digest": canonical_digest(response), "finding": str(exc)[:12000]})
                record["request"] = None
                self.team.teams.save(current, "team.invalid_proposal_rejected")
            return None
        self.team.submit(team_id, task_id, request["team_assignment"]["request_id"], response,
                         collect=False, managed=True, agent_capabilities=self.host.capabilities)
        return None

    def _handoffs(self, team_id):
        data = self.team.teams.get(team_id)
        for task_id in data["tasks"]:
            record = self.team.teams.get(team_id)["records"][task_id]
            if record["status"] == "RUNNING" and record["child_run_id"]:
                child = self.team._child(self.team.teams.get(team_id), task_id)
                if child["state"]["status"] != "SUCCEEDED":
                    failed = self._evaluate(team_id, task_id)
                    if failed == "waiting":
                        return False
                    if failed:
                        self.rework(team_id, task_id, failed)
                        return True
                    child = self.team._child(self.team.teams.get(team_id), task_id)
                    if child["state"]["status"] != "SUCCEEDED":
                        continue
                try:
                    report = self.team.collect(team_id, task_id)
                except MissingHandoffOutput as exc:
                    self.rework(team_id, task_id, str(exc))
                    return True
                record = report["tasks"][task_id]
                if record["status"] == "RUNNING" and record.get("integration_run_id"):
                    integrated_child = self.team._child(self.team.teams.get(team_id), task_id, verification=True)
                    if integrated_child["state"]["status"] in {"BUDGET_EXHAUSTED", "CANCELLED", "FAILED"}:
                        raise BudgetError("Integrated verification is terminal; its budget cannot be reset")
                    command_ids = {c["id"] for c in integrated_child["task"]["checks"] if c["type"] == "command"}
                    actual = self.team.store.evidence(integrated_child["run_id"], integrated_child["selected_evidence"])
                    failures = [e for e in actual if e["check_id"] in command_ids and e["result"] != "pass"]
                    if failures:
                        self.rework(team_id, task_id, "Integrated checks failed: " + json.dumps(failures, ensure_ascii=False))
                        return True
                    failed = self._evaluate(team_id, task_id, integrated=True)
                    if failed == "waiting":
                        return False
                    if failed:
                        self.rework(team_id, task_id, failed)
                        return True
                    try:
                        self.team.collect(team_id, task_id)
                    except MissingHandoffOutput as exc:
                        self.rework(team_id, task_id, str(exc))
                        return True
                    record = self.team.teams.get(team_id)["records"][task_id]
            if record["status"] != "HANDOFF":
                continue
            for role in data["tasks"][task_id]["handoff_to"]:
                if role in record["receipts"]:
                    continue
                current = self.team.teams.get(team_id)
                child, snapshot = self.team._verified(current, task_id)
                controller = self.team._controller(current, task_id,
                    verification=bool(current["records"][task_id].get("integration_run_id")))
                base = current["records"][task_id].get("base_snapshot")
                changed = None
                if base is not None:
                    before = {f["path"]: f for f in base["manifest"]["files"]}
                    after = {f["path"]: f for f in snapshot["manifest"]["files"]}
                    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
                response = self.host.invoke(team_id, {"kind": "team-recipient-request", "role": role,
                    "handoff": record["handoff"], "context": self._inputs(self.team.teams.get(team_id), role=role, task_id=task_id),
                    "phase_scope": {"task_id": task_id, "phase": current["tasks"][task_id]["phase"],
                        "task_contract": child["task"], "downstream_tasks": [
                            {"id": key, "role": task["role"], "phase": task["phase"],
                             "status": current["records"][key]["status"]}
                            for key, task in current["tasks"].items() if task_id in current["dependencies"][key]]},
                    "verification": {"candidate": snapshot["digest"], "environment": child["state"]["environment_digest"],
                        "checks": self.team.store.evidence(child["run_id"], child["selected_evidence"]),
                        "observations": controller._summaries(child, child["selected_evidence"]),
                        "observed_changed_paths": changed, "change_scope": "Declared verified snapshots; excluded files are outside this comparison"},
                    "instruction": "Inspect the actual outputs, task contract, verified check records and retained output excerpts for this phase's current integrated candidate. Accept only a usable complete handoff for this task's accepted criteria; otherwise reject with actionable findings. Scheduled downstream review/acceptance tasks run after this receipt and remain required for overall completion. This receipt cannot replace independent or human evaluation."},
                    RECEIPT_RESPONSE, final=task_id == data["plan"]["final_task"])
                self.team.receive(team_id, task_id, record["handoff"]["id"], role, accept=response["accept"], note=response["note"])
                if not response["accept"]:
                    self.rework(team_id, task_id, response["note"])
                    return True
        return True

    def run(self, team_id, *, max_cycles=100, setup_only=False):
        if type(max_cycles) is not int or max_cycles < 1:
            raise ContractError("Scheduler cycles must be positive")
        done = threading.Event()
        def deadline_watch():
            while not done.wait(.1):
                data = self.team.teams.get(team_id)
                if remaining(data) <= 0 and data["status"] not in {"COMPLETE", "CANCELLED"}:
                    self.team.stop(team_id, cancel=True)
                    break
        watcher = threading.Thread(target=deadline_watch, daemon=True)
        with self.team.store._lock(self.team.store.run_dir(team_id) / "scheduler.lock", "Another scheduler owns this team"):
            watcher.start()
            try:
                for _ in range(max_cycles):
                    data = self.team.teams.get(team_id)
                    if self.team.teams.signal(team_id) or data["status"] in {"COMPLETE", "CANCELLED", "PAUSED", "BUDGET_EXHAUSTED"}:
                        break
                    if data.get("pending_control"):
                        with self.team.teams.writer(team_id) as current:
                            self._apply_control(current)
                            if current["status"] == "AWAITING_INPUT" and "specification" in current:
                                current.update(status="ACTIVE", reason=None)
                                self.team.teams.save(current, "team.specification_installation_recovered")
                        continue
                    if data["status"] == "AWAITING_INPUT":
                        break
                    if setup_only and data.get("stage") != "SPEC":
                        break
                    if data.get("stage") == "SPEC":
                        self.prepare_spec(team_id)
                        # A generated spec is a reviewable handoff. Even team-run
                        # stops here; the operator continues this same team ID.
                        if self.team.teams.get(team_id).get("stage") != "SPEC":
                            break
                        continue
                    if data.get("stage") == "INTAKE":
                        self.prepare(team_id)
                        continue
                    if data["status"] == "BLOCKED":
                        failed = next((key for key, r in data["records"].items() if r["status"] == "REJECTED"), None)
                        if failed:
                            self.rework(team_id, failed, data["records"][failed]["reason"])
                            continue
                        break
                    report = self.team.status(team_id)
                    active = [key for key, r in data["records"].items() if r["status"] == "RUNNING" and not r.get("integration_run_id")]
                    ready = report["ready_tasks"]
                    chosen = (active + [key for key in ready if key not in active])[:data["automation"]["policy"]["max_parallel"]]
                    if chosen:
                        # Admission/context mutation is short and serial; inference
                        # and local checks execute concurrently in isolated roots.
                        for key in chosen:
                            if data["records"][key]["status"] == "PENDING":
                                self.team.request(team_id, key, managed=True)
                        with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
                            futures = {key: pool.submit(self._worker, team_id, key) for key in chosen}
                            findings = [(key, future.result()) for key, future in futures.items()]
                        for key, finding in findings:
                            if finding == "waiting":
                                return self.team.status(team_id)
                            if finding:
                                self.rework(team_id, key, finding)
                                break
                    if not self._handoffs(team_id):
                        break
                    latest = self.team.status(team_id)
                    if not chosen and not latest["ready_tasks"] and latest["status"] != "COMPLETE" and not any(
                            r["status"] in {"RUNNING", "HANDOFF"} for r in latest["tasks"].values()):
                        self._state(team_id, "AWAITING_INPUT", "No runnable task; inspect dependencies and pending capabilities")
                        break
            except BudgetError as exc:
                self._state(team_id, "BUDGET_EXHAUSTED", str(exc))
            except (OSError, ValueError, KeyError) as exc:
                self._state(team_id, "AWAITING_INPUT", str(exc))
            finally:
                done.set()
                watcher.join(timeout=2)
                # The owned process can observe the deadline before the watcher
                # gets its next time slice. Persist the same stop in that case.
                latest = self.team.teams.get(team_id)
                if remaining(latest) <= 0 and latest["status"] not in {"COMPLETE", "CANCELLED"}:
                    self.team.stop(team_id, cancel=True)
        return self.team.status(team_id)


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def read_bytes(path):
    from .scenarios import read_text
    return read_text(path).encode()
