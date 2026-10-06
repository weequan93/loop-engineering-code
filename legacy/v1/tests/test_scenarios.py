"""Offline scenario setup and host-handoff tests; no model/team dispatch."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from loop_engineering.cli import main
from loop_engineering.contracts import ContractError, ROOT, load, validate
from loop_engineering.controller import Controller
from loop_engineering.project import diagnose, initialize
from loop_engineering.scenarios import MAX_INPUT_BYTES, context, status, task_inputs_digest
from loop_engineering.store import Store
from loop_engineering.workspace import byte_digest

FIXED = 'def slug(text: str) -> str:\n    if not text.strip():\n        return ""\n    return "-".join(text.lower().split(" "))\n'
DEVELOPMENT_ROLES = {"coordinator", "requirements_reviewer", "designer", "architect",
                     "frontend", "backend", "tester", "security", "performance", "reviewer", "acceptance",
                     "integrator", "devops", "database", "documentation", "reliability"}


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="loop-scenario-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.spec = self.base / "requirements.md"
        self.spec.write_text("# 需求\n纯空白输入返回空字符串，其他行为保持兼容。\n", encoding="utf-8")

    def initialize(self, **kwargs):
        return initialize(self.root, scenario="development", spec_file=self.spec, **kwargs)

    def cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(args)
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def write(self, relative, value):
        (self.root / relative).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def prepare_task(self, *, select_team=True):
        """Deterministic coordinator fixture, not a claim of spec parsing."""
        for name in ("slug.py", "tests"):
            source = ROOT / "examples/slug-project" / name
            if source.is_dir():
                shutil.copytree(source, self.root / name)
            else:
                shutil.copyfile(source, self.root / name)
        task = load(ROOT / "examples/slug-project/task.json", "task")
        for check in task["checks"]:
            check["argv"][0] = sys.executable
        self.write(".loop/task.json", task)
        if select_team:
            self.select_team()
        return task

    def select_team(self, *, all_roles=False):
        """Declared offline assignments, not semantic selection or real agents."""
        manifest = load(self.root / ".loop/scenario.json", "scenario")
        if "team_selection" not in manifest:
            return None
        value = {"schema_version": "1.0", "basis_digest": status(self.root)["team"]["basis_digest"], "decisions": []}
        for role in manifest["roles"]:
            role_id = role["id"]
            active = all_roles or role_id in {"coordinator", "backend", "reviewer", "acceptance"}
            covered = not active and role_id in {"requirements_reviewer", "architect", "tester", "integrator"}
            value["decisions"].append({
                "role": role_id, "state": "active" if active else "covered" if covered else "inactive",
                "reason": "Exercise the full team in an offline fixture." if all_roles else
                          "Own the scoped library fix." if active else
                          "Coordinator covers this small change." if covered else "Outside this focused library fixture.",
                "sources": [".loop/spec.md"], "assignee": f"fixture:{role_id}" if active else None,
                "covered_by": "coordinator" if covered else None,
                "handoff_to": ["acceptance" if role_id == "coordinator" else "coordinator"] if active or covered else []})
        self.write(".loop/team.json", value)
        return value

    def questions(self, answers=None, *, blocking=True):
        value = {"schema_version": "1.0", "questions": [{"id": "compatibility", "question": "保留非空输入行为吗？",
                 "reason": "确定兼容性边界。", "blocking": blocking, "answers": answers or []}]}
        self.write(".loop/questions.json", value)
        return value

    def controller(self):
        store = Store(self.base / "state")
        return Controller(store)

    def start(self, engine, *, baseline=False):
        return engine.start(self.root, self.root / ".loop/task.json", self.root / ".loop/project.json", baseline=baseline)

    def test_init_and_context_are_offline_and_keep_the_original_spec(self):
        self.write("package.json", {"scripts": {"test": "touch MUST_NOT_RUN"}})
        with patch("subprocess.Popen", side_effect=AssertionError("setup must not execute processes")):
            result = self.initialize()
            report = diagnose(self.root)
            bundle = context(self.root)
        self.assertEqual((self.root / ".loop/spec.md").read_bytes(), self.spec.read_bytes())
        self.assertEqual(result["execution"], "host_orchestrated")
        self.assertEqual({role["id"] for role in bundle["scenario"]["roles"]}, DEVELOPMENT_ROLES)
        self.assertEqual(bundle["role"]["id"], "coordinator")
        self.assertEqual((bundle["task"]["kind"], bundle["task"]["workflow"]), ("feature", "standard"))
        self.assertEqual(report["scenario"]["phase"], "PLANNING")
        self.assertTrue(report["ok"])
        self.assertFalse(report["ready_to_start"])
        self.assertFalse((self.root / "MUST_NOT_RUN").exists())
        self.assertFalse((self.root / ".loop/engine.json").exists())

    def test_existing_project_and_custom_role_files_are_preserved(self):
        initialize(self.root)
        (self.root / "AGENTS.md").write_text("Existing host instructions\n")
        (self.root / "LOOP.md").write_text("Custom portable loop\n")
        original_task = (self.root / ".loop/task.json").read_bytes()
        original_pointer = (self.root / ".loop/agent-instructions.md").read_bytes()
        self.initialize()
        (self.root / ".loop/agents/backend.md").write_text("Custom backend conventions\n")
        self.questions(["保留"])
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.initialize()
        self.assertEqual(result["created"], [])
        self.assertEqual(result["updated"], [])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual((self.root / ".loop/task.json").read_bytes(), original_task)
        self.assertEqual((self.root / ".loop/agent-instructions.md").read_bytes(), original_pointer)

    def test_empty_spec_can_be_supplied_later_without_replacing_the_task(self):
        initialize(self.root, scenario="development")
        self.assertEqual(status(self.root)["phase"], "AWAITING_SPEC")
        original = (self.root / ".loop/task.json").read_bytes()
        result = self.initialize()
        self.assertEqual(result["updated"], [".loop/spec.md"])
        self.assertEqual(status(self.root)["phase"], "PLANNING")
        self.assertEqual((self.root / ".loop/task.json").read_bytes(), original)

    def test_different_spec_rejected_before_any_missing_files_are_created(self):
        self.initialize()
        (self.root / ".loop/agents/backend.md").unlink()
        self.spec.write_text("A different requested product\n")
        with self.assertRaisesRegex(ContractError, "Existing spec differs"):
            self.initialize()
        self.assertFalse((self.root / ".loop/agents/backend.md").exists())
        self.assertIn("纯空白", (self.root / ".loop/spec.md").read_text())

    def test_missing_files_from_interrupted_setup_are_recovered(self):
        self.initialize()
        self.questions(["Yes", "Correction: preserve every existing nonempty result"])
        (self.root / ".loop/agents/frontend.md").unlink()
        self.assertFalse(diagnose(self.root)["ok"])
        before = (self.root / ".loop/questions.json").read_bytes()
        result = self.initialize()
        self.assertEqual(result["created"], [".loop/agents/frontend.md"])
        self.assertEqual((self.root / ".loop/questions.json").read_bytes(), before)
        self.assertTrue(diagnose(self.root)["ok"])

    def test_invalid_specs_leave_the_project_untouched(self):
        for value in (b"  \n", b"\xff", b"bad\0spec", b"x" * (MAX_INPUT_BYTES + 1)):
            with self.subTest(value=value[:10]):
                self.spec.write_bytes(value)
                with self.assertRaises(ContractError):
                    self.initialize()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_symlinked_spec_is_rejected(self):
        linked = self.base / "linked.md"
        linked.symlink_to(self.spec)
        with self.assertRaises(ContractError):
            initialize(self.root, scenario="development", spec_file=linked)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_symlinked_role_directory_rejected_before_other_setup_writes(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / ".loop").mkdir()
        (self.root / ".loop/agents").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ContractError, "Symlink"):
            self.initialize()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.root / "LOOP.md").exists())
        self.assertFalse((self.root / ".loop/project.json").exists())

    def test_invalid_init_combinations_fail_before_writes(self):
        options = ({"scenario": "missing"}, {"spec_file": self.spec},
                   {"scenario": "development", "native": True})
        for kwargs in options:
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                initialize(self.root, **kwargs)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_init_preserves_existing_native_route(self):
        initialize(self.root, native=True)
        before = (self.root / ".loop/engine.json").read_bytes()
        with self.assertRaisesRegex(ContractError, "coding host"):
            self.initialize()
        self.assertEqual((self.root / ".loop/engine.json").read_bytes(), before)
        self.assertFalse((self.root / ".loop/scenario.json").exists())

    def test_manifest_rejects_ambiguous_roles_and_invalid_dependencies(self):
        manifest = load(ROOT / "templates/scenarios/development/scenario.json", "scenario")
        changes = [
            lambda x: x["roles"].append(deepcopy(x["roles"][0])),
            lambda x: x.update(coordinator="missing"),
            lambda x: x["roles"][0].update(instructions="../../outside.md"),
            lambda x: x["workflow"][0].update(roles=["missing"]),
            lambda x: x["workflow"][0].update(depends_on=["deliver"]),
            lambda x: x["workflow"].append(deepcopy(x["workflow"][0])),
            lambda x: x["roles"][0].pop("activation"),
            lambda x: x["roles"][0]["activation"].update(required=False),
            lambda x: x["roles"][1]["activation"].update(covered_by=["missing"]),
            lambda x: x["roles"][1]["activation"].update(covered_by=["requirements_reviewer"]),
            lambda x: x["roles"][1]["activation"].update(covered_by=["reviewer"]),
            lambda x: next(r for r in x["roles"] if r["id"] == "reviewer")["activation"].update(covered_by=["coordinator"]),
        ]
        for change in changes:
            value = deepcopy(manifest)
            change(value)
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate("scenario", value)

    def test_clarification_history_and_readiness_survive_reinitialization(self):
        self.initialize()
        self.prepare_task()
        self.questions()
        self.assertEqual(status(self.root)["phase"], "AWAITING_INPUT")
        self.assertFalse(diagnose(self.root)["ready_to_start"])
        value = self.questions(["Keep old behavior", "Also preserve repeated spaces"])
        self.select_team()
        self.initialize()
        self.assertEqual(load(self.root / ".loop/questions.json"), value)
        report = diagnose(self.root)
        self.assertEqual(report["scenario"]["phase"], "TASK_PREPARED")
        self.assertTrue(report["ready_to_start"])

    def test_optional_question_does_not_block_prepared_task(self):
        self.initialize()
        self.prepare_task()
        self.questions(blocking=False)
        self.select_team()
        self.assertTrue(status(self.root)["ready_for_controller"])

    def test_duplicate_questions_and_blank_answers_fail_closed(self):
        self.initialize()
        original = self.questions()
        changes = [lambda x: x["questions"].append(deepcopy(x["questions"][0])),
                   lambda x: x["questions"][0].update(answers=["  "]),
                   lambda x: x["questions"][0].update(blocking="false"),
                   lambda x: x["questions"][0].update(done=True)]
        for change in changes:
            value = deepcopy(original)
            change(value)
            self.write(".loop/questions.json", value)
            with self.assertRaises(ContractError):
                context(self.root)
            self.assertFalse(diagnose(self.root)["ok"])

    def test_specialist_context_keeps_shared_requirements_and_only_own_role(self):
        self.initialize()
        (self.root / "AGENTS.md").write_text("Keep existing interfaces\n")
        self.questions(["Keep compatibility"])
        coordinator = context(self.root)
        worker = context(self.root, "backend")
        paths = {item["path"] for item in worker["documents"]}
        self.assertTrue({"AGENTS.md", ".loop/spec.md", ".loop/questions.json", ".loop/workflow.md",
                         ".loop/schemas/task.schema.json", ".loop/agents/backend.md"}.issubset(paths))
        self.assertEqual(len([p for p in paths if p.startswith(".loop/agents/")]), 1)
        self.assertEqual([i["path"] for i in coordinator["documents"] if i["path"].startswith(".loop/agents/")],
                         [".loop/agents/coordinator.md"])
        self.assertEqual({item["id"] for item in coordinator["scenario"]["roles"]}, DEVELOPMENT_ROLES)
        self.assertEqual(worker["scenario_inputs_digest"], coordinator["scenario_inputs_digest"])
        with self.assertRaisesRegex(ContractError, "Unknown scenario role"):
            context(self.root, "not-a-role")

    def test_professional_roles_export_complete_bounded_contexts(self):
        self.spec.write_text(self.spec.read_text() + "\nDetailed user constraints. " * 160)
        self.initialize()
        self.questions(["Use the existing product's compatibility requirements"])
        self.select_team(all_roles=True)
        for role_id in DEVELOPMENT_ROLES:
            with self.subTest(role=role_id):
                bundle = context(self.root, role_id)
                self.assertEqual(bundle["role"]["id"], role_id)
                self.assertEqual(bundle["scenario"], load(self.root / ".loop/scenario.json"))
                self.assertEqual(bundle["scenario_source"], {"path": ".loop/scenario.json",
                                 "sha256": byte_digest((self.root / ".loop/scenario.json").read_bytes())})
                self.assertLessEqual(len(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode()),
                                     bundle["project"]["context_max_bytes"])
                documents = {item["path"]: item["content"] for item in bundle["documents"]}
                self.assertEqual(documents[".loop/spec.md"], self.spec.read_text())
                self.assertIn(f".loop/agents/{role_id}.md", documents)
                self.assertIn(".loop/questions.json", documents)
                self.assertIn(".loop/team.json", documents)
                self.assertIn("## Assignment contract", documents[f".loop/agents/{role_id}.md"])
                self.assertEqual(bundle["status"]["team"]["state"], "ready")
                if role_id in {"security", "reviewer", "acceptance"}:
                    self.assertTrue(bundle["role"]["independent"])
                self.assertEqual([path for path in documents if path.startswith(".loop/agents/")],
                                 [f".loop/agents/{role_id}.md"])

    def test_development_acceptance_follows_all_quality_assessments(self):
        self.initialize()
        phases = {phase["id"]: phase for phase in context(self.root)["scenario"]["workflow"]}
        self.assertEqual(phases["team_selection"]["depends_on"], ["requirements_review"])
        self.assertEqual(phases["design"]["depends_on"], ["team_selection"])
        self.assertIn("designer", phases["design"]["roles"])
        self.assertTrue({"architect", "security", "performance"}.issubset(phases["design"]["roles"]))
        self.assertEqual(set(phases["acceptance"]["depends_on"]),
                         {"verify", "security_review", "performance_test", "review", "delivery_readiness"})
        self.assertEqual(phases["integrate"]["depends_on"], ["implement"])
        self.assertEqual(phases["verify"]["depends_on"], ["integrate"])
        self.assertEqual(set(phases["delivery_readiness"]["roles"]), {"devops", "documentation", "reliability"})
        self.assertEqual(phases["deliver"]["depends_on"], ["acceptance"])

    def test_context_limit_refuses_to_drop_spec_or_instructions(self):
        self.initialize()
        profile = load(self.root / ".loop/project.json", "project")
        profile["context_max_bytes"] = 1024
        self.write(".loop/project.json", profile)
        with self.assertRaisesRegex(ContractError, "context_max_bytes"):
            context(self.root)

    def test_task_requires_current_team_selection_without_dispatch(self):
        self.initialize()
        self.prepare_task(select_team=False)
        with patch("subprocess.Popen", side_effect=AssertionError("team validation must not dispatch")):
            report = diagnose(self.root)
            self.assertTrue(report["ok"])
            self.assertFalse(report["ready_to_start"])
            self.assertEqual(report["scenario"]["phase"], "TEAM_SELECTION")
            self.assertEqual(report["scenario"]["team"]["state"], "unselected")
            engine = self.controller()
            with self.assertRaisesRegex(ContractError, "TEAM_SELECTION"):
                self.start(engine)
            self.assertEqual(engine.store.runs(), [])
            selected = self.select_team()
            report = diagnose(self.root)
            self.assertTrue(report["ready_to_start"])
            self.assertEqual(report["scenario"]["team"]["covered_roles"]["integrator"], "coordinator")
            self.initialize()
        self.assertEqual(load(self.root / ".loop/team.json"), selected)

    def test_incomplete_or_pending_team_cannot_start(self):
        self.initialize()
        self.prepare_task()
        selected = load(self.root / ".loop/team.json")
        missing = deepcopy(selected)
        missing["decisions"] = [item for item in missing["decisions"] if item["role"] != "documentation"]
        pending = deepcopy(selected)
        item = next(item for item in pending["decisions"] if item["role"] == "reviewer")
        item.update(state="pending", assignee=None, handoff_to=[], reason="Separate evaluator unavailable.")
        for value, role in ((missing, "documentation"), (pending, "reviewer")):
            with self.subTest(role=role):
                self.write(".loop/team.json", value)
                report = status(self.root)
                self.assertEqual(report["team"]["state"], "pending")
                self.assertEqual(report["team"]["pending_roles"], [role])
                self.assertFalse(report["ready_for_controller"])

    def test_team_rejects_invalid_coverage_independence_and_handoffs(self):
        self.initialize()
        original = self.select_team()
        def role(value, role_id):
            return next(item for item in value["decisions"] if item["role"] == role_id)
        changes = [
            lambda x: role(x, "reviewer").update(state="inactive", assignee=None, handoff_to=[]),
            lambda x: role(x, "reviewer").update(state="covered", assignee=None, covered_by="coordinator"),
            lambda x: role(x, "reviewer").update(assignee=" fixture:backend "),
            lambda x: role(x, "integrator").update(covered_by="tester"),
            lambda x: role(x, "integrator").update(covered_by="backend"),
            lambda x: role(x, "coordinator").update(handoff_to=["missing"]),
            lambda x: role(x, "coordinator").update(handoff_to=["coordinator"]),
            lambda x: role(x, "backend").update(handoff_to=["frontend"]),
            lambda x: role(x, "backend").update(role="unknown"),
        ]
        for change in changes:
            value = deepcopy(original)
            change(value)
            self.write(".loop/team.json", value)
            with self.subTest(value=value), self.assertRaises(ContractError):
                context(self.root)
            self.assertFalse(diagnose(self.root)["ok"])

    def test_team_record_rejects_ambiguous_or_empty_declarations(self):
        self.initialize()
        original = self.select_team()
        changes = [
            lambda x: x["decisions"][1].update(role=x["decisions"][0]["role"]),
            lambda x: x["decisions"][0].update(reason=" "),
            lambda x: x["decisions"][0].update(sources=[" "]),
            lambda x: x["decisions"][0].update(sources=[]),
            lambda x: x["decisions"][0].update(assignee=" "),
            lambda x: x["decisions"][0].update(covered_by="backend"),
            lambda x: x["decisions"][0].update(handoff_to=[]),
            lambda x: x.update(basis_digest="invalid"),
            lambda x: x.update(approved=True),
        ]
        for change in changes:
            value = deepcopy(original)
            change(value)
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate("team", value)

    def test_selection_becomes_stale_after_spec_answers_or_roster_change(self):
        self.initialize()
        self.prepare_task()
        selected = (self.root / ".loop/team.json").read_bytes()
        for name in (".loop/spec.md", ".loop/questions.json", ".loop/scenario.json"):
            path = self.root / name
            before = path.read_bytes()
            if name.endswith("spec.md"):
                path.write_text(path.read_text() + "\nAlso support another input.\n")
            elif name.endswith("questions.json"):
                self.questions(["A new actual answer"])
            else:
                manifest = load(path)
                manifest["description"] += " Updated scope guidance."
                self.write(name, manifest)
            with self.subTest(name=name):
                report = status(self.root)
                self.assertEqual(report["team"]["state"], "stale")
                self.assertEqual(report["phase"], "TEAM_SELECTION")
                self.assertEqual((self.root / ".loop/team.json").read_bytes(), selected)
            path.write_bytes(before)
        self.assertTrue(status(self.root)["ready_for_controller"])

    def test_selection_is_frozen_with_controller_inputs(self):
        self.initialize()
        self.prepare_task()
        engine = self.controller()
        data = self.start(engine)
        team = load(self.root / ".loop/team.json")
        team["decisions"][0]["reason"] += " Changed assignment reasoning."
        self.write(".loop/team.json", team)
        self.assertEqual(status(self.root)["team"]["state"], "ready")
        with self.assertRaisesRegex(ContractError, "Scenario spec, answers or instructions changed"):
            engine.context(data["run_id"])

    def test_missing_or_symlinked_team_fails_closed_and_setup_preserves_boundaries(self):
        self.initialize()
        path = self.root / ".loop/team.json"
        original = path.read_bytes()
        path.unlink()
        self.assertFalse(diagnose(self.root)["ok"])
        path.symlink_to(self.spec)
        before = self.spec.read_bytes()
        with self.assertRaisesRegex(ContractError, "Symlink"):
            context(self.root)
        with self.assertRaises(ContractError):
            self.initialize()
        self.assertEqual(self.spec.read_bytes(), before)
        path.unlink()
        self.initialize()
        self.assertEqual(path.read_bytes(), original)

    def test_old_installed_roster_retains_legacy_readiness_and_is_not_upgraded(self):
        self.initialize()
        old = load(self.root / ".loop/scenario.json")
        old.pop("team_selection")
        old["roles"] = [role for role in old["roles"] if role["id"] in {"coordinator", "backend"}]
        for role in old["roles"]:
            role.pop("activation")
        old["workflow"] = [{"id": "implement", "title": "Legacy host plan", "roles": ["coordinator", "backend"], "depends_on": []}]
        self.write(".loop/scenario.json", old)
        (self.root / ".loop/team.json").unlink()
        (self.root / ".loop/schemas/team.schema.json").unlink()
        self.prepare_task()
        self.assertNotIn("team", status(self.root))
        self.assertTrue(diagnose(self.root)["ready_to_start"])
        self.initialize()
        self.assertEqual(load(self.root / ".loop/scenario.json"), old)
        bundle = context(self.root)
        self.assertNotIn("team", bundle["status"])
        self.assertNotIn(".loop/team.json", [item["path"] for item in bundle["documents"]])

    def test_missing_or_escaping_instructions_fail_context_and_doctor(self):
        self.initialize()
        path = self.root / ".loop/agents/backend.md"
        path.unlink()
        path.symlink_to(self.spec)
        with self.assertRaisesRegex(ContractError, "Symlink"):
            context(self.root)
        self.assertFalse(diagnose(self.root)["ok"])

    def test_controller_waits_for_spec_questions_and_task(self):
        initialize(self.root, scenario="development")
        engine = self.controller()
        with self.assertRaisesRegex(ContractError, "AWAITING_SPEC"):
            self.start(engine)
        self.initialize()
        with self.assertRaisesRegex(ContractError, "PLANNING"):
            self.start(engine)
        self.prepare_task()
        self.questions()
        with self.assertRaisesRegex(ContractError, "AWAITING_INPUT"):
            self.start(engine)
        self.assertEqual(engine.store.runs(), [])

    def test_host_prepared_task_reaches_real_controller_acceptance(self):
        self.initialize()
        self.prepare_task()
        self.questions(["Keep existing behavior"])
        self.select_team()
        engine = self.controller()
        data = self.start(engine, baseline=True)
        run_id = data["run_id"]
        self.assertNotEqual(data["state"]["status"], "SUCCEEDED")
        bundle = engine.context(run_id)
        source = next(item for item in bundle["sources"] if item["path"] == "slug.py")
        proposal = {"schema_version": "0.2", "step_id": "offline-specialist-fixture", "task_id": bundle["task"]["task_id"],
                    "contract_digest": bundle["contract_digest"], "base_snapshot_digest": bundle["base_snapshot_digest"],
                    "intent": "act", "criterion_ids": [item["id"] for item in bundle["task"]["criteria"]],
                    "summary": "Scripted specialist fixture", "expected_observation": "Behavior and compatibility checks pass",
                    "changes": [{"path": "slug.py", "expected_sha256": source["sha256"], "new_content": FIXED}],
                    "evidence_refs": [], "blocker": None, "next_action": None}
        final = engine.submit(run_id, proposal)
        self.assertEqual(final["state"]["status"], "SUCCEEDED")
        self.assertTrue(all(item["result"] == "pass" for item in engine.store.evidence(run_id, final["selected_evidence"])))
        self.assertEqual(engine.store.replay(run_id)["scenario_inputs_digest"], task_inputs_digest(self.root))

    def test_requirement_or_role_change_cannot_silently_reuse_a_run(self):
        self.initialize()
        self.prepare_task()
        engine = self.controller()
        data = self.start(engine)
        for name in (".loop/spec.md", ".loop/agents/backend.md", ".loop/workflow.md",
                     ".loop/agents/requirements_reviewer.md", ".loop/agents/designer.md",
                     ".loop/agents/security.md", ".loop/agents/performance.md", ".loop/agents/acceptance.md",
                     ".loop/agents/integrator.md", ".loop/agents/devops.md", ".loop/agents/database.md",
                     ".loop/agents/documentation.md", ".loop/agents/reliability.md"):
            path = self.root / name
            before = path.read_text()
            path.write_text(before + "\nChanged requirement or instruction\n")
            with self.subTest(name=name), self.assertRaisesRegex(ContractError, "Scenario spec, answers or instructions changed"):
                engine.context(data["run_id"])
            path.write_text(before)
        self.questions(["A newly recorded decision"])
        with self.assertRaisesRegex(ContractError, "Scenario spec, answers or instructions changed"):
            engine.verify(data["run_id"])

    def test_progress_handoff_does_not_change_the_frozen_requirements(self):
        self.initialize()
        self.prepare_task()
        engine = self.controller()
        data = self.start(engine)
        before = task_inputs_digest(self.root)
        (self.root / ".loop/plan.md").write_text("Assigned backend implementation\n")
        (self.root / ".loop/handoff.md").write_text("Next: implement the focused fix\n")
        self.assertEqual(task_inputs_digest(self.root), before)
        self.assertEqual(engine.context(data["run_id"])["run_id"], data["run_id"])

    def test_cli_discovery_context_export_and_exclusive_output(self):
        code, result, _ = self.cli(["scenarios"])
        self.assertEqual(code, 0)
        self.assertFalse(result["automatic_dispatch"])
        self.assertEqual([item["id"] for item in result["scenarios"]], ["development"])
        code, result, _ = self.cli(["init", str(self.root), "--scenario", "development", "--spec", str(self.spec)])
        self.assertEqual(code, 0)
        self.assertEqual(result["entrypoint"], str(self.root / ".loop/start.md"))
        self.assertEqual(self.cli(["doctor", str(self.root)])[0], 0)
        output = self.base / "reviewer-context.json"
        args = ["scenario-context", str(self.root), "--role", "reviewer", "--output", str(output)]
        self.assertEqual(self.cli(args)[0], 0)
        self.assertEqual(load(output)["role"]["id"], "reviewer")
        before = output.read_bytes()
        self.assertEqual(self.cli(args)[0], 2)
        self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
