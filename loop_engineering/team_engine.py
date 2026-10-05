"""Supervised, serial team batches backed by existing coding controllers.

Every task needs a real task contract, a verified controller result and explicit
recipient receipts. This module does not infer a plan, grant tools, certify host
identities, or turn a role report into authenticated evaluator evidence.
"""

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

from reference.core import canonical_digest, strict_json_loads
from .adapters import drive
from .contracts import ContractError, load, relative_path, validate
from .controller import Controller
from .native_engine import NativeController
from .native_contracts import load_engine
from .scenarios import inputs, read_text, role_assignment, status as scenario_status, task_inputs_digest
from .stages import validate_stages
from .store import Store
from .team_store import TeamStore
from .team_memory import build as memory_context, fit_task_request
from .workspace import Snapshots, byte_digest, check_write_scope, matches, safe_path

PLAN_PATH = ".loop/workflow-tasks.json"


class MissingHandoffOutput(ContractError):
    """Verified task checks passed, but its declared output is absent/empty."""


def ancestors(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    result, visiting = {}, set()
    def visit(key):
        if key in visiting:
            raise ContractError("Team dependency cycle")
        if key not in graph:
            raise ContractError("Unknown team dependency: " + key)
        if key not in result:
            visiting.add(key)
            result[key] = set(graph[key])
            for parent in graph[key]:
                result[key].update(visit(parent))
            visiting.remove(key)
        return result[key]
    for key in graph:
        visit(key)
    return result


def validate_workflow(value: dict) -> None:
    from .native_parallel import validate_paths
    validate_paths(value)
    tasks = value["tasks"]
    ids = [task["id"] for task in tasks]
    if len(ids) != len(set(ids)) or not value["scope"].strip():
        raise ContractError("Team task IDs must be unique and scope must be concrete")
    ancestors({task["id"]: set(task["depends_on"]) for task in tasks})
    if tasks and value["final_task"] not in ids:
        raise ContractError("Team batch needs a defined final verification task")
    if not tasks and value["final_task"] is not None:
        raise ContractError("Empty team scaffold cannot name a final task")
    for task in tasks:
        for name in [task["task_file"], *task["outputs"], *([task["engine_file"]] if task["engine_file"] else [])]:
            relative_path(name)
        if any(path.split("/")[0].casefold() in {".loop", ".git"} for path in task["outputs"]):
            raise ContractError("Team outputs must be versioned workspace artifacts, outside control directories")


class TeamDriver:
    """Add frozen specialist instructions to a normal supervised proposal driver."""
    def __init__(self, base, prepare, check_request):
        self.base, self.prepare, self.check_request = base, prepare, check_request
        self.name, self.capabilities = base.name, base.capabilities

    def command(self, work, artifacts):
        return self.base.command(work, artifacts)

    def response(self, result, artifacts):
        self.check_request()
        return self.base.response(result, artifacts)

    def token_report(self, result):
        return self.base.token_report(result)

    def prepare_context(self, context):
        return self.prepare(self.base.prepare_context(context))


class TeamController:
    def __init__(self, store: Store):
        self.store, self.teams = store, TeamStore(store)
        self.snapshots = Snapshots(store.directory)

    def profile(self, data):
        return load(safe_path(Path(data["workspace"]), ".loop/project.json"), "project")

    def start(self, root: Path, plan_path: Path | None = None) -> dict:
        data = self.build(root, plan_path)
        self.teams.create(data)
        return self.status(data["team_id"])

    def build(self, root: Path, plan_path: Path | None = None) -> dict:
        """Validate a batch without persistence or dispatch, also used by intake."""
        root = root.resolve()
        if self.store.directory.is_relative_to(root):
            raise ContractError("Team state must be outside the project workspace")
        plan_path = (plan_path or root / PLAN_PATH).absolute()
        if not plan_path.resolve().is_relative_to(root):
            raise ContractError("Team plan must be inside the project")
        plan_path = safe_path(root, plan_path.relative_to(root).as_posix())
        plan = strict_json_loads(read_text(plan_path))
        validate("team-workflow", plan)
        if not plan["tasks"]:
            raise ContractError("Replace the team workflow scaffold before starting")
        manifest, documents = inputs(root)
        setup = scenario_status(root)
        if (not documents[".loop/spec.md"].strip() or setup["pending_questions"]
                or setup.get("team", {}).get("state") != "ready"):
            raise ContractError("Team requires a spec, answered blocking questions and current selection")
        selection = strict_json_loads(documents[".loop/team.json"])
        decisions = {item["role"]: item for item in selection["decisions"]}
        roles = {role["id"]: role for role in manifest["roles"]}
        phases = {phase["id"]: phase for phase in manifest["workflow"]}
        phase_ancestors = ancestors({key: set(phase["depends_on"]) for key, phase in phases.items()})
        profile = load(safe_path(root, ".loop/project.json"), "project")
        bound = {plan_path.relative_to(root).as_posix(): byte_digest(plan_path.read_bytes()),
                 ".loop/project.json": byte_digest((root / ".loop/project.json").read_bytes())}
        tasks, records, contracts = {}, {}, {}
        for task in plan["tasks"]:
            role, phase = task["role"], task["phase"]
            if role not in decisions or decisions[role]["state"] not in {"active", "covered"}:
                raise ContractError("Team task requires an active or covered role")
            if phase not in phases or role not in phases[phase]["roles"]:
                allowed = [key for key, item in phases.items() if role in item["roles"]]
                raise ContractError("Team task role does not participate in its declared phase: " + task["id"] +
                                    " role=" + role + " phase=" + phase + "; allowed phases=" + ",".join(allowed))
            if not set(task["handoff_to"]).issubset(decisions[role]["handoff_to"]):
                raise ContractError("Team handoff recipients differ from the selected role contract")
            owner = decisions[role]["covered_by"] or role
            contract_path = safe_path(root, task["task_file"])
            contract = strict_json_loads(read_text(contract_path))
            validate("task", contract)
            if contract["task_id"] != task["id"] or contract.get("extensions", {}).get("scaffold"):
                raise ContractError("Team task contract is a scaffold or has a different task ID")
            if roles[role]["independent"] and (not task["engine_file"] or not any(
                    c["type"] == "review" and c.get("independent") for c in contract["checks"])):
                raise ContractError("Independent roles require a registered native independent review check")
            for name in (task["task_file"], *([task["engine_file"]] if task["engine_file"] else [])):
                path = safe_path(root, name)
                bound[name] = byte_digest(read_text(path).encode("utf-8"))
            if task["engine_file"]:
                try:
                    engine = load_engine(safe_path(root, task["engine_file"]))
                    validate_stages(contract, engine["stages"])
                except ContractError as exc:
                    raise ContractError("Invalid team task stage configuration: " + task["id"] + ": " + str(exc)) from exc
            for name in task["outputs"]:
                safe_path(root, name)
                if matches(name, profile["snapshot"]["exclude"]):
                    raise ContractError("Team output is excluded from verified snapshots: " + name)
            tasks[task["id"]] = {**task, "owner": owner, "assignee": decisions[owner]["assignee"],
                                  "contract_digest": canonical_digest(contract)}
            contracts[task["id"]] = contract
            for name in task.get("draft_paths", []):
                check_write_scope(root, name, contract)
                if matches(name, profile["snapshot"]["exclude"]):
                    raise ContractError("Parallel draft paths must be captured by the project snapshot")
            records[task["id"]] = {"status": "PENDING", "child_run_id": None, "request": None,
                                    "handoff": None, "receipts": {}, "reason": None}
        graph = {key: set(task["depends_on"]) | {other for other, item in tasks.items()
                 if item["phase"] in phase_ancestors[task["phase"]]} for key, task in tasks.items()}
        graph = ancestors(graph)
        for key, task in tasks.items():
            for name in task["outputs"]:
                path = safe_path(root, name)
                if path.exists() and not path.is_file():
                    raise ContractError("Team output must be a regular file path: " + name)
                if path.is_file() and path.stat().st_size > 0:
                    continue
                for producer in [key, *sorted(graph[key])]:
                    try:
                        check_write_scope(root, name, contracts[producer])
                    except ContractError:
                        continue
                    break
                else:
                    raise ContractError("Team output is missing and outside this task/dependencies' write scopes: " +
                                        key + " output=" + name + "; declare an actual relative file path")
            if task.get("repair_task") is not None and task["repair_task"] not in graph[key] | {key}:
                raise ContractError("Repair task must be this task or one of its dependencies")
        if graph[plan["final_task"]] != set(tasks) - {plan["final_task"]}:
            raise ContractError("Final verification must depend on every task in this batch")
        data = {"workspace": str(root), "plan": plan, "tasks": tasks, "records": records,
                "dependencies": {key: sorted(items) for key, items in graph.items()},
                "bound_files": bound, "scenario_digest": task_inputs_digest(root),
                "status": "ACTIVE", "reason": None}
        return data

    def _assert_inputs(self, data):
        root = Path(data["workspace"])
        if task_inputs_digest(root) != data["scenario_digest"]:
            raise ContractError("Team scenario inputs changed; prepare a reviewed new batch")
        for name, digest in data["bound_files"].items():
            if byte_digest(read_text(safe_path(root, name)).encode("utf-8")) != digest:
                raise ContractError("Frozen team plan, contract, engine or profile changed: " + name)

    def _controller(self, data, task_id, *, verification=False):
        record, task = data["records"][task_id], data["tasks"][task_id]
        run_id = record.get("integration_run_id") if verification else record["child_run_id"]
        root = Path(data["workspace"] if verification else record.get("workspace", data["workspace"]))
        if task["engine_file"]:
            probe = None
            if run_id and self._exists(run_id):
                probe = self.store.get(run_id)["native"]["runtime_probe"]
            return NativeController(self.store, safe_path(root, task["engine_file"]), probe=probe)
        return Controller(self.store)

    def _exists(self, run_id):
        with self.store.connect() as connection:
            return connection.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is not None

    def _child(self, data, task_id, *, verification=False):
        record, task = data["records"][task_id], data["tasks"][task_id]
        run_id = record.get("integration_run_id") if verification else record["child_run_id"]
        child = self.store.get(run_id)
        workspace = data["workspace"] if verification else record.get("workspace", data["workspace"])
        if (child["workspace"] != workspace or child["state"]["contract_digest"] != task["contract_digest"]
                or child.get("scenario_inputs_digest") != data["scenario_digest"]):
            raise ContractError("Child controller does not match the accepted team task")
        return child

    def _admit(self, data, task_id):
        self._assert_inputs(data)
        if data.get("native_host", {}).get("rework_pending"):
            raise ContractError("Recover the original native rework journal before task admission")
        if data.get("native_host", {}).get("refresh_pending"):
            raise ContractError("Recover the original native draft refresh before task admission")
        if data["status"] != "ACTIVE" or self.teams.signal(data["team_id"]):
            raise ContractError("Team is stopped; inspect status or explicitly resume")
        if task_id not in data["tasks"]:
            raise ContractError("Unknown team task")
        record = data["records"][task_id]
        if record["status"] not in {"PENDING", "RUNNING"}:
            raise ContractError("Task needs handoff resolution or is already complete")
        if any(data["records"][key]["status"] != "COMPLETE" for key in data["dependencies"][task_id]):
            raise ContractError("Task dependencies or phase handoffs are incomplete")
        isolated = data.get("automation", {}).get("isolated", False)
        from .native_parallel import enabled, can_admit
        parallel = enabled(data)
        if parallel and not can_admit(self, data, task_id):
            raise ContractError("Native parallel admission needs a free slot and disjoint exact draft ownership; finish pending handoffs first")
        if not isolated and not parallel and any(key != task_id and item["status"] in {"RUNNING", "HANDOFF", "REJECTED"}
               for key, item in data["records"].items()):
            raise ContractError("Team execution is serial; finish the current task and handoff")
        if isolated and record["status"] == "PENDING" and sum(r["status"] == "RUNNING" for r in data["records"].values()) >= data["automation"]["policy"]["max_parallel"]:
            raise ContractError("Team parallel workspace limit reached")
        if record["child_run_id"] is None:
            record.update(status="RUNNING", child_run_id="run-" + uuid4().hex)
            self.teams.save(data, "task.child_reserved")
        if isolated:
            from .team_parallel import isolate
            isolate(self, data, task_id)
        controller = self._controller(data, task_id)
        if not self._exists(record["child_run_id"]):
            # Identity was journaled before creation: a crash cannot create a
            # second child with fresh budgets when this operation is retried.
            root = Path(record.get("workspace", data["workspace"]))
            controller.start(root, safe_path(root, data["tasks"][task_id]["task_file"]),
                             root / ".loop/project.json", baseline=False, run_id=record["child_run_id"])
        self._child(data, task_id)
        if record["status"] == "PENDING" and record.get("native_rework_id"):
            record["status"] = "RUNNING"
            self.teams.save(data, "task.rework_admitted")
        return controller

    def _instructions(self, data, task_id, snapshot):
        task = data["tasks"][task_id]
        root = Path(data["workspace"])
        manifest, documents = inputs(root)
        assignment = role_assignment(manifest, documents, task["role"], include_covered=False)
        names = ["LOOP.md", ".loop/agent-instructions.md", ".loop/spec.md", ".loop/questions.json", ".loop/workflow.md",
                 *assignment["instruction_paths"]]
        if "agent_protocol" in manifest:
            names.append(manifest["agent_protocol"])
        if "execution_tools" in manifest:
            names.append(manifest["execution_tools"])
        if "AGENTS.md" in documents:
            names.append("AGENTS.md")
        # Local controller sources may omit files for size. Keep all repository
        # instruction files explicitly, including nested instructions in scope.
        instruction_docs = [{"path": name, "content": documents[name]} for name in dict.fromkeys(names)]
        nested = set()
        for name in [item["path"] for item in snapshot["manifest"]["files"]] + task["outputs"]:
            parts = name.split("/")
            nested.update("/".join(parts[:length]) + "/AGENTS.md" for length in range(1, len(parts)))
        for name in sorted(nested - set(names)):
            path = safe_path(root, name)
            if path.exists():
                instruction_docs.append({"path": name, "content": read_text(path)})
        return instruction_docs

    def _decorate(self, data, task_id, context):
        self._assert_inputs(data)
        if self.teams.signal(data["team_id"]):
            raise ContractError("Team stopped before host dispatch")
        task, record = data["tasks"][task_id], data["records"][task_id]
        child = self._child(data, task_id)
        snapshot = Controller(self.store).snapshots.capture(Path(child["workspace"]), child["profile"])
        instruction_docs = self._instructions(data, task_id, snapshot)
        request_id = "request-" + uuid4().hex
        from .native_rework import feedback as native_rework_feedback
        assignment = {"team_id": data["team_id"], "task_id": task_id, "request_id": request_id,
                      "worker_context_id": "worker-" + uuid4().hex,
                      "role": task["role"], "owner": task["owner"], "assignee": task["assignee"],
                      "phase": task["phase"], "outputs": task["outputs"], "handoff_to": task["handoff_to"],
                      "instructions": instruction_docs,
                      "memory": memory_context(self, data, role=task["role"], task_id=task_id, candidate=snapshot["digest"]),
                      "dependencies": [data["records"][key]["handoff"] for key in data["dependencies"][task_id]],
                      "repair_feedback": data.get("automation", {}).get("feedback", [])[-4:] + native_rework_feedback(data, task_id),
                      "authority": "Proposal only: return an AgentStep for the supplied task. Use intent=act for any file changes, and request_verification only with no changes. Use supplied task/contract/snapshot hashes. Do not execute tools, edit files or delegate. Role documents guide analysis; controller scope and checks govern effects. Handoff receipts do not replace evaluator evidence."}
        result = {"schema_version": "1.0", "kind": "team-host-request", "mode": "proposal_only",
                  "task_context": context, "team_assignment": assignment}
        budget = child["profile"]["context_max_bytes"]
        fit_task_request(result, budget, error=
            "Team context exceeds context_max_bytes; preserve instructions and revise the context limit/scope")
        record["request"] = {"id": request_id, "snapshot": snapshot["digest"],
                              "instructions_digest": canonical_digest(instruction_docs),
                              "context_digest": Controller(self.store).snapshots.put(
                                  json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))}
        self.teams.save(data, "host.request_prepared")
        return result

    def request(self, team_id, task_id, *, wait_seconds=0, managed=False):
        with self.teams.writer(team_id, wait_seconds=wait_seconds) as data:
            if "automation" in data and not managed:
                raise ContractError("Automatic teams use team-run; manual worker dispatch cannot bypass the shared budget")
            controller = self._admit(data, task_id)
            record = data["records"][task_id]
            if self._child(data, task_id)["state"]["status"] != "PLANNING":
                raise ContractError("Child is not ready for a proposal; collect, verify or resume it")
            if record["request"] is not None:
                self._assert_request_candidate(data, task_id)
                return strict_json_loads(controller.snapshots.read(record["request"]["context_digest"]).decode("utf-8"))
            return self._decorate(data, task_id, controller.context(record["child_run_id"]))

    def _assert_request_candidate(self, data, task_id):
        record = data["records"][task_id]
        child = self._child(data, task_id)
        current = Controller(self.store).snapshots.capture(Path(child["workspace"]), child["profile"])
        if record["request"] is None or current["digest"] != record["request"]["snapshot"]:
            raise ContractError("Host request is missing or stale; reconcile and prepare fresh context")
        if canonical_digest(self._instructions(data, task_id, current)) != record["request"]["instructions_digest"]:
            raise ContractError("Repository instructions changed during host work; prepare fresh context")

    def submit(self, team_id, task_id, request_id, step, *, collect=True, managed=False, agent_capabilities=None):
        current = self.teams.get(team_id)
        if "automation" in current and not managed:
            raise ContractError("Automatic teams accept only their accounted team-run proposals")
        if "native_host" in current and not collect:
            raise ContractError("Native task effects require serial collection under the team writer")
        if not collect:
            # Automated workers check their isolated roots concurrently. Keep
            # the metadata lock short; the child's workspace writer owns effects.
            with self.teams.writer(team_id, wait_seconds=5) as data:
                controller = self._admit(data, task_id)
                record = data["records"][task_id]
                self._assert_request_candidate(data, task_id)
                if request_id != record["request"]["id"] or step.get("base_snapshot_digest") != record["request"]["snapshot"]:
                    raise ContractError("Response belongs to a different host request or candidate")
                run_id = record["child_run_id"]
            controller.submit(run_id, step, agent_capabilities=agent_capabilities)
            with self.teams.writer(team_id, wait_seconds=5) as data:
                data["records"][task_id]["request"] = None
                self.teams.save(data, "host.response_processed")
                self._apply_signal(data)
            return self.status(team_id)
        with self.teams.writer(team_id) as data:
            controller = self._admit(data, task_id)
            record = data["records"][task_id]
            self._assert_request_candidate(data, task_id)
            if request_id != record["request"]["id"] or step.get("base_snapshot_digest") != record["request"]["snapshot"]:
                raise ContractError("Response belongs to a different host request or candidate")
            if "native_host" in data:
                from .native_parallel import assert_import
                assert_import(data, task_id, step.get("changes", []))
                from types import SimpleNamespace
                from .native_workers import assert_submittable
                assert_submittable(SimpleNamespace(team=self), data, task_id)
                from .native_verification import assert_no_running
                assert_no_running(data, task_id)
                if record.get("repair_from_candidate") == self.snapshots.capture(
                        Path(data["workspace"]), self.profile(data))["digest"]:
                    child = self._child(data, task_id)
                    changes = self.snapshots.prepare_changes(Path(data["workspace"]), child["task"], step.get("changes", []))
                    if not any(c["old"] != c["new"] and not matches(c["path"], child["profile"]["snapshot"]["exclude"])
                               for c in changes):
                        raise ContractError("Repair owner must deliver a changed candidate before verification")
            controller.submit(record["child_run_id"], step, agent_capabilities=agent_capabilities)
            record["request"] = None
            self.teams.save(data, "host.response_processed")
            if not self._apply_signal(data) and collect:
                self._collect(data, task_id)
        return self.status(team_id)

    def drive(self, team_id, task_id, driver, *, timeout=180):
        with self.teams.writer(team_id) as data:
            if "automation" in data:
                raise ContractError("Use team-run for accounted automatic team dispatch")
            if data["tasks"].get(task_id, {}).get("engine_file"):
                raise ContractError("Native tasks use the file/evaluator bridge; a legacy command cannot bypass managed accounting")
            controller = self._admit(data, task_id)
            record = data["records"][task_id]
            if record["request"] is not None:
                raise ContractError("A host request is outstanding; submit it or explicitly resume before command dispatch")
            wrapped = TeamDriver(driver, lambda context: self._decorate(data, task_id, context),
                                 lambda: (self._assert_inputs(data), self._assert_request_candidate(data, task_id)))
            drive(controller, record["child_run_id"], wrapped, turns=1, timeout=timeout)
            record["request"] = None
            self.teams.save(data, "host.command_returned")
            if not self._apply_signal(data):
                self._collect(data, task_id)
        return self.status(team_id)

    def _verified(self, data, task_id):
        integrated = bool(data["records"][task_id].get("integration_run_id"))
        child = self._child(data, task_id, verification=integrated)
        controller = self._controller(data, task_id, verification=integrated)
        controller._assert_contract(child)
        if child["state"]["status"] != "SUCCEEDED":
            raise ContractError("Task has no successful controller verification")
        snapshot = controller.snapshots.capture(Path(data["workspace"]), child["profile"])
        if (snapshot["digest"] != child["state"]["snapshot_digest"]
                or controller.environment_digest(child) != child["state"]["environment_digest"]):
            raise ContractError("Verified candidate or environment changed; reverify before handoff")
        evidence = self.store.evidence(child["run_id"], child["selected_evidence"])
        if not evidence:
            raise ContractError("Verified task is missing authenticated evidence")
        controller._check_artifacts(evidence)
        handoff = data["records"][task_id]["handoff"]
        if handoff is not None and (handoff["candidate"] != snapshot["digest"] or handoff["evidence"] != child["selected_evidence"]):
            raise ContractError("Handoff is stale relative to the controller result")
        return child, snapshot

    def _collect(self, data, task_id):
        record = data["records"][task_id]
        child = self._child(data, task_id)
        if record.get("repair_from_candidate") and self.snapshots.capture(
                Path(data["workspace"]), self.profile(data))["digest"] == record["repair_from_candidate"]:
            raise ContractError("Repair owner must deliver a changed candidate before handoff")
        if child["state"]["status"] != "SUCCEEDED":
            record["reason"] = child["state"].get("reason")
            self.teams.save(data, "task.awaiting_controller")
            return
        if "native_host" in data:
            from types import SimpleNamespace
            from .native_workers import assert_submittable
            assert_submittable(SimpleNamespace(team=self), data, task_id)
        if data.get("automation", {}).get("isolated"):
            from .team_parallel import integrate
            integrate(self, data, task_id)
            child = self._child(data, task_id, verification=True)
            if child["state"]["status"] != "SUCCEEDED":
                record["reason"] = child["state"].get("reason")
                self.teams.save(data, "task.awaiting_integrated_verification")
                return
        child, snapshot = self._verified(data, task_id)
        files = {item["path"]: item for item in snapshot["manifest"]["files"] if item["kind"] == "file"}
        outputs = []
        for path in data["tasks"][task_id]["outputs"]:
            if path not in files or not Controller(self.store).snapshots.read(files[path]["sha256"]):
                raise MissingHandoffOutput("Required handoff output is missing or empty: " + path)
            outputs.append({"path": path, "sha256": files[path]["sha256"]})
        record.update(status="HANDOFF", request=None, reason=None,
                      handoff={"id": "handoff-" + uuid4().hex, "task_id": task_id,
                               "child_run_id": child["run_id"], "candidate": snapshot["digest"],
                               "outputs": outputs, "evidence": child["selected_evidence"]})
        self.teams.save(data, "handoff.prepared")

    def collect(self, team_id, task_id):
        with self.teams.writer(team_id) as data:
            self._assert_inputs(data)
            if data["status"] != "ACTIVE" or self.teams.signal(team_id):
                raise ContractError("Team is stopped")
            if task_id not in data["records"] or data["records"][task_id]["status"] != "RUNNING":
                raise ContractError("Only a running task can produce a handoff")
            self._collect(data, task_id)
        return self.status(team_id)

    def receive(self, team_id, task_id, handoff_id, role, *, accept, note):
        if type(accept) is not bool or not note.strip():
            raise ContractError("Handoff receipt needs a decision and an inspection note")
        with self.teams.writer(team_id) as data:
            self._assert_inputs(data)
            if data["status"] != "ACTIVE" or self.teams.signal(team_id):
                raise ContractError("Team is stopped")
            if task_id not in data["records"]:
                raise ContractError("Unknown team task")
            record, task = data["records"][task_id], data["tasks"][task_id]
            if record["status"] != "HANDOFF" or record["handoff"]["id"] != handoff_id:
                raise ContractError("Stale or unavailable handoff")
            if role not in task["handoff_to"] or role in record["receipts"]:
                raise ContractError("Unexpected recipient or duplicate receipt")
            self._verified(data, task_id)
            record["receipts"][role] = {"accepted": accept, "note": note}
            if not accept:
                record.update(status="REJECTED", reason=note)
                data.update(status="BLOCKED", reason="Handoff rejected; prepare a reviewed revision after resolving findings")
            elif set(record["receipts"]) == set(task["handoff_to"]):
                record["status"] = "COMPLETE"
                if all(item["status"] == "COMPLETE" for item in data["records"].values()):
                    self._verified(data, data["plan"]["final_task"])
                    data["status"] = "COMPLETE"
            self.teams.save(data, "handoff.received")
        return self.status(team_id)

    def _apply_signal(self, data):
        signal = self.teams.signal(data["team_id"])
        if signal:
            data["status"] = signal
            self.teams.save(data, "team.stopped")
        return signal

    def stop(self, team_id, *, cancel=False):
        current = self.teams.get(team_id)
        if current["status"] in {"COMPLETE", "CANCELLED"}:
            raise ContractError("Team is already terminal")
        self.teams.signal(team_id, "CANCELLED" if cancel else "PAUSED")
        for item in current["records"].values():
            for run_id in (item["child_run_id"], item.get("integration_run_id")):
                if run_id and self._exists(run_id) and item["status"] == "RUNNING":
                    if cancel:
                        self.store.cancel(run_id)
                    else:
                        self.store.pause(run_id, "Team paused")
        try:
            with self.teams.writer(team_id) as data:
                self._apply_signal(data)
        except ContractError as exc:
            if "Another operation" not in str(exc):
                raise
        return self.status(team_id)

    def resume(self, team_id):
        with self.teams.writer(team_id) as data:
            self._assert_inputs(data)
            if data["status"] in {"COMPLETE", "CANCELLED", "BLOCKED"} or self.teams.signal(team_id) == "CANCELLED":
                raise ContractError("Terminal/rejected team needs an explicit reviewed new batch")
            for task_id, record in data["records"].items():
                if record["status"] == "RUNNING" and self._exists(record["child_run_id"]):
                    pending_draft = any(w["task_id"] == task_id and w.get("pending_worker_import") is not None and
                        record["request"] and w["request_id"] == record["request"]["id"]
                        for w in data.get("native_host", {}).get("specialists", {}).values())
                    if pending_draft:
                        # Resume preserves this request so the host can replay
                        # the frozen draft import explicitly, without any new
                        # automatic file effect or renewed dispatch deadline.
                        self._assert_request_candidate(data, task_id)
                    for integrated in (False, True):
                        if integrated and not record.get("integration_run_id"):
                            continue
                        if integrated and not self._exists(record["integration_run_id"]):
                            continue
                        controller = self._controller(data, task_id, verification=integrated)
                        child = self._child(data, task_id, verification=integrated)
                        if child["state"]["status"] != "SUCCEEDED":
                            controller.resume(child["run_id"])
                    pending_refresh = data.get("native_host", {}).get("refresh_pending", {}).get("task_id") == task_id
                    if "automation" not in data and not pending_draft and not pending_refresh:
                        record["request"] = None
            self.teams.signal(team_id, "")
            data.update(status="ACTIVE", reason=None)
            self.teams.save(data, "team.resumed")
        return self.status(team_id)

    def memory(self, team_id, *, role="coordinator", task_id=None, max_bytes=None):
        data = self.teams.get(team_id)
        self._assert_inputs(data)
        return memory_context(self, data, role=role, task_id=task_id, max_bytes=max_bytes)

    def status(self, team_id):
        data = self.teams.get(team_id)
        fresh, problem = True, None
        try:
            self._assert_inputs(data)
        except (OSError, ValueError) as exc:
            fresh, problem = False, str(exc)
        signal = self.teams.signal(team_id)
        current = None
        if data["status"] == "COMPLETE":
            try:
                self._verified(data, data["plan"]["final_task"])
                current = fresh
            except (OSError, ValueError):
                current = False
        occupied = any(r["status"] in {"RUNNING", "HANDOFF", "REJECTED"} for r in data["records"].values())
        if data.get("automation", {}).get("isolated"):
            occupied = sum(r["status"] == "RUNNING" for r in data["records"].values()) >= data["automation"]["policy"]["max_parallel"]
        records = deepcopy(data["records"])
        for task_id, record in records.items():
            if record["child_run_id"] and self._exists(record["child_run_id"]):
                child = self._child(data, task_id)
                record["controller_status"] = child["state"]["status"]
                record["usage"] = child["state"]["usage"]
                record["provisioning"] = child.get("provisioning")
                record["pending_process"] = child.get("pending_process")
                record["executors"] = child.get("native", {}).get("execution_attempts", {})
            if record.get("integration_run_id") and self._exists(record["integration_run_id"]):
                integrated = self._child(data, task_id, verification=True)
                record["integration_status"] = integrated["state"]["status"]
                record["integration_usage"] = integrated["state"]["usage"]
        ready = [key for key, record in data["records"].items() if record["status"] == "PENDING"
                 and all(data["records"][parent]["status"] == "COMPLETE" for parent in data["dependencies"][key])]
        from .native_parallel import enabled, can_admit
        if enabled(data):
            ready = [key for key in ready if can_admit(self, data, key)]
            occupied = False
        budget = None
        operations = []
        if "automation" in data:
            from .team_policy import balance
            budget = balance(data)
            operations = [{"operation_id": key, **{name: row.get(name) for name in ("status", "phase", "pid", "child_run_id", "error")}}
                          for key, row in data["automation"]["operations"].items() if row["status"] in {"ADMITTED", "RESPONDING"}]
        return {"team_id": team_id, "status": signal or data["status"], "revision": data["revision"], "team_budget": budget,
                "stage": data.get("stage", "EXECUTION"), "pending_operations": operations,
                "requirements_setup": ({key: data["specification"].get(key) for key in
                    ("role", "state", "summary", "sources")} if "specification" in data else None),
                "pending_questions": (load(safe_path(Path(data["workspace"]), ".loop/questions.json"), "questions")["questions"]
                    if data.get("stage") in {"SPEC", "INTAKE"} else []),
                "scope": data["plan"]["scope"], "inputs_current": fresh, "input_problem": problem,
                "reason": data["reason"], "ready_tasks": ready if fresh and not occupied and not signal and data["status"] == "ACTIVE" else [],
                "tasks": records, "final_candidate_current": current,
                "assurance": "Controller-verified workflow with host-declared receipts; completion covers this workflow's accepted contracts. Automatic teams use cumulative accounting and isolated workers with serial integration. Host identities and external usage are not independently attested."}
