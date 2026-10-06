"""Repeatable local module-collaboration corpus; semantic judgments are fixtures."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from loop_engineering.contracts import ROOT, load
from loop_engineering.project import initialize
from loop_engineering.scenarios import status
from loop_engineering.store import Store
from loop_engineering.team_automation import TeamAutomation
from loop_engineering.team_engine import TeamController
from loop_engineering.team_host import TeamHost
from loop_engineering.workspace import byte_digest
from .notes_solutions import API, INCOMPATIBLE_API, STORAGE, VIEWS


class CorpusTransport:
    def __init__(self, *, inject_regression=False):
        self.inject_regression = inject_regression
        self.requests = []
        self.backend_attempts = 0

    def post(self, operation, payload, **kwargs):
        if operation == "count":
            return {"input_tokens": 10}
        request = json.loads(payload["input"])
        self.requests.append(request)
        kind = request["kind"]
        if kind == "team-recipient-request":
            response = {"accept": True, "note": "Explicit offline recipient fixture; actual controller records retained"}
        elif kind == "team-evaluator-request":
            response = {"result": "pass", "summary": "Explicit offline judgment fixture; not a live security/review assessment", "findings": []}
        else:
            context = request["task_context"]
            bundle = context.get("bundle", context)
            task_id = bundle["task"]["task_id"]
            proposed = {"data-schema": ("storage.py", STORAGE), "note-view": ("views.py", VIEWS),
                        "notes-api": ("api.py", API)}.get(task_id)
            if task_id == "notes-api":
                self.backend_attempts += 1
                if self.inject_regression and self.backend_attempts == 1:
                    proposed = ("api.py", INCOMPATIBLE_API)
            changes = []
            if proposed:
                path, content = proposed
                source = next(item for item in bundle["sources"] if item["path"] == path)
                changes = [{"path": path, "expected_sha256": source["sha256"], "new_content": content}]
            response = {"schema_version": "0.2", "step_id": request["team_assignment"]["request_id"],
                        "task_id": task_id, "contract_digest": context["contract_digest"],
                        "base_snapshot_digest": context["base_snapshot_digest"],
                        "intent": "act" if changes else "request_verification",
                        "criterion_ids": [item["id"] for item in bundle["task"]["criteria"]],
                        "summary": "Deterministic scoped module proposal", "expected_observation": "Actual unchanged checks verify the interface",
                        "changes": changes, "evidence_refs": [], "blocker": None, "next_action": None}
        return {"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 10},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(response)}]}]}


def run_notes_case(*, inject_regression=False):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="loop-notes-corpus-") as directory:
        base = Path(directory)
        root = base / "project"
        shutil.copytree(ROOT / "examples/notes-project", root, ignore=shutil.ignore_patterns("__pycache__"))
        initialize(root, scenario="development", spec_file=root / "spec.md")
        profile = load(root / ".loop/project.json")
        # Seven roles retain original-source and predecessor evidence at final
        # acceptance. Select an explicit bounded budget before contract freeze.
        profile["context_max_bytes"] = 120000
        (root / ".loop/project.json").write_text(json.dumps(profile, indent=2) + "\n")
        store = Store(base / "state")
        team = TeamController(store)
        def write(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, indent=2) + "\n")
        manifest = load(root / ".loop/scenario.json")
        active = {"coordinator", "database", "frontend", "backend", "security", "performance", "reviewer", "acceptance"}
        covered = {"requirements_reviewer", "architect", "tester", "integrator", "documentation"}
        selection = {"schema_version": "1.0", "basis_digest": status(root)["team"]["basis_digest"], "decisions": []}
        for role in manifest["roles"]:
            name = role["id"]
            state = "active" if name in active else "covered" if name in covered else "inactive"
            selection["decisions"].append({"role": name, "state": state, "reason": "Explicit local collaboration corpus assignment",
                                           "sources": [".loop/spec.md"], "assignee": "offline:"+name if state=="active" else None,
                                           "covered_by": "coordinator" if state=="covered" else None,
                                           "handoff_to": ["acceptance" if name=="coordinator" else "coordinator"] if state!="inactive" else []})
        write(root / ".loop/team.json", selection)
        definitions = [
            ("data-schema", "database", "implement", [], "storage.py", "StorageTests"),
            ("note-view", "frontend", "implement", [], "views.py", "ViewTests"),
            ("notes-api", "backend", "implement", ["data-schema", "note-view"], "api.py", None),
            ("security-notes", "security", "security_review", ["notes-api"], None, "SecurityTests"),
            ("workload-notes", "performance", "performance_test", ["notes-api"], None, "WorkloadTests"),
            ("review-notes", "reviewer", "review", ["notes-api"], None, None),
            ("accept-notes", "acceptance", "acceptance", ["security-notes", "workload-notes", "review-notes"], None, None),
        ]
        workflow = {"schema_version": "1.0", "workflow_id": "notes-collaboration", "revision": 1,
                    "scope": "Local persistence, API, HTML, security checks and bounded workload; judgments are offline fixtures",
                    "final_task": "accept-notes", "tasks": []}
        for task_id, role, phase, dependencies, path, test_class in definitions:
            contract = load(ROOT / "templates/task.json")
            contract.update(task_id=task_id, kind="feature", workflow="standard", objective="Verify notes requirements within the " + role + " responsibility",
                            assumptions=["Local bounded fixture; no deployed browser or service evidence"], dependencies=[],
                            non_goals=["No deployment or actual browser/load evidence claim"],
                            compatibility_requirements=["Keep tests unchanged and the shared id/title contract"], extensions={})
            contract["scope"] = {"write_allow": [path] if path else ["api.py"],
                                 "write_deny": ["tests/**", ".loop/**", ".git/**"] + ([] if path else ["api.py", "storage.py", "views.py"])}
            if path is None:
                contract["authorization"]["allowed_actions"] = ["read_workspace", "run_checks"]
            argv = [sys.executable, "-m", "unittest", "tests.test_notes" + ("." + test_class if test_class else ""), "-v"]
            contract["criteria"] = [{"id": "behavior", "description": "Actual scoped behavioral checks pass", "check_ids": ["behavior"]}]
            contract["checks"] = [{"id": "behavior", "type": "command", "description": "Execute unchanged external fixture checks",
                                   "argv": argv, "cwd": ".", "timeout_seconds": 20}]
            contract["limits"].update(max_wall_seconds=180, max_iterations=4, max_tokens=None, max_cost_microunits=None,
                                      verification_reserve_seconds=20)
            engine_path = None
            if role in {"security", "reviewer", "acceptance"}:
                contract["criteria"].append({"id": "assessment", "description": "Separate required assessment passes", "check_ids": ["independent"]})
                contract["checks"].append({"id": "independent", "type": "review", "description": "Separate candidate-bound assessment",
                                           "independent": True, "procedure": ["Inspect actual source, requirement coverage and retained check output"]})
                engine = load(ROOT / "templates/engine.json")
                key_id = "offline-" + role
                store.authorities.create(key_id, "reviewer")
                engine["evaluator_keys"] = [{"key_id": key_id, "role": "reviewer", "check_ids": ["independent"]}]
                engine_path = ".loop/tasks/" + task_id + "-engine.json"
                write(root / engine_path, engine)
            contract_path = ".loop/tasks/" + task_id + ".json"
            write(root / contract_path, contract)
            workflow["tasks"].append({"id": task_id, "role": role, "phase": phase, "depends_on": dependencies,
                                      "task_file": contract_path, "engine_file": engine_path,
                                      "outputs": [path] if path else ["storage.py", "views.py", "api.py"],
                                      "handoff_to": ["coordinator"], "repair_task": "notes-api" if path is None else None})
        write(root / ".loop/workflow-tasks.json", workflow)
        tests_before = {path.relative_to(root).as_posix():byte_digest(path.read_bytes()) for path in (root/"tests").rglob("*.py")}
        baseline = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root,
                                  capture_output=True, text=True, timeout=20)
        provider = load(ROOT / "templates/engine.json")
        provider["model"]["model"] = "offline-corpus-fixture"
        write(base / "provider.json", provider)
        transport = CorpusTransport(inject_regression=inject_regression)
        auto = TeamAutomation(team, TeamHost(team, engine=base / "provider.json", transport=transport))
        team_id = auto.start(root)["team_id"]
        result = auto.run(team_id)
        final = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root,
                               capture_output=True, text=True, timeout=20)
        data = team.teams.audit(team_id)
        failed_records = 0
        for task_id in data["records"]:
            record = data["records"][task_id]
            if record["child_run_id"]:
                child = store.replay(record["child_run_id"])
                with store.connect() as connection:
                    digests = [row[0] for row in connection.execute("SELECT digest FROM evidence WHERE run_id=?", (child["run_id"],))]
                failed_records += sum(item["result"] == "fail" for item in store.evidence(child["run_id"], digests))
            if record.get("integration_run_id"):
                store.replay(record["integration_run_id"])
        protected = all(byte_digest((root/name).read_bytes())==digest for name,digest in tests_before.items())
        return {"case": "notes-regression-recovery" if inject_regression else "notes-collaboration",
                "baseline_failed": baseline.returncode != 0, "final_oracle_passed": final.returncode == 0,
                "final_oracle_log_sha256": byte_digest((final.stdout+final.stderr).encode()),
                "protected_tests_unchanged": protected, "status": result["status"], "reason": result["reason"],
                "verified_success": result["status"]=="COMPLETE" and result["final_candidate_current"] and final.returncode==0 and protected,
                "role_ids": [item[1] for item in definitions], "backend_attempts": transport.backend_attempts,
                "actual_failed_check_records": failed_records,
                "independent_requests": len([request for request in transport.requests if request["kind"]=="team-evaluator-request"]),
                "dispatches": result["team_budget"]["dispatches"], "elapsed_ms": round((time.monotonic()-started)*1000),
                "journal_audit": "pass", "local_workload_recorded": "LOCAL_WORKLOAD count=500" in final.stdout+final.stderr,
                "live_model_calls": 0, "judgments": "deterministic offline fixtures", "browser_or_deployed_load_claim": False}
