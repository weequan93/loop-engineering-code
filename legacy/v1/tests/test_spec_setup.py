"""Requirements setup with fake provider responses and actual durable writes."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from loop_engineering.cli import main
from loop_engineering.contracts import ContractError, load
from loop_engineering.project import diagnose
from loop_engineering.team_automation import TeamAutomation
from tests import test_team_automation as fixture


SPEC = "# Library requirements\n\nWhitespace-only input returns an empty string; preserve other slug behavior.\n"


class RequirementsSetupTests(unittest.TestCase):
    def setUp(self):
        self.owner = fixture.AutomationTests()
        self.owner.setUp()
        self.addCleanup(self.owner.doCleanups)
        self.owner.coordinator_response()
        self.root, self.auto, self.team = self.owner.root, self.owner.auto, self.owner.team
        (self.root / ".loop/spec.md").write_text("")
        (self.root / "docs").mkdir()
        (self.root / "docs/requirements.md").write_text(SPEC)
        self.response = {"status": "ready", "summary": "Requirements extracted from the actual supplied document",
                         "spec_markdown": SPEC, "sources": ["docs/requirements.md"], "questions": []}
        original = self.owner.respond
        def respond(request):
            if request["kind"] == "team-specification-request":
                return deepcopy(self.response)
            return original(request)
        self.owner.respond = respond

    def start(self, **kwargs):
        result = self.auto.start(self.root, self.owner.policy_path, **kwargs)
        self.team_id = result["team_id"]
        return result

    def ask(self):
        self.response.update(status="needs_input", spec_markdown="", sources=[], questions=[
            {"id": "scope", "question": "Should existing nonempty behavior remain?", "reason": "Define compatibility", "blocking": True}])

    def test_static_setup_exports_agent_without_any_dispatch(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("subprocess.Popen", side_effect=AssertionError("No live dispatch")), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["setup", str(self.root)])
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertFalse(result["automatic_dispatch"])
        self.assertEqual(result["role"], "requirements_reviewer")
        self.assertTrue((self.root / ".loop/setup.md").is_file())
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")

    def test_empty_spec_admits_the_setup_stage_without_dispatch(self):
        with patch("subprocess.Popen", side_effect=AssertionError("Admission is offline")):
            result = self.start()
        self.assertEqual(result["stage"], "SPEC")
        self.assertEqual(result["requirements_setup"]["role"], "requirements_reviewer")
        self.assertEqual(result["team_budget"]["dispatches"], 0)

    def test_document_first_preparation_stops_before_development(self):
        original = (self.root / "slug.py").read_bytes()
        self.start()
        result = self.auto.run(self.team_id)
        self.assertEqual(result["stage"], "INTAKE", result["reason"])
        self.assertEqual(result["requirements_setup"]["state"], "READY")
        self.assertEqual((self.root / ".loop/spec.md").read_text(), SPEC)
        self.assertEqual((self.root / "slug.py").read_bytes(), original)
        self.assertEqual(result["tasks"], {})
        self.assertEqual(result["team_budget"]["dispatches"], 1)
        request = self.owner.transport.requests[0]
        self.assertEqual(request["role"], "requirements_reviewer")
        instructions = {s["path"]: s["content"] for s in request["context"]["instructions"]}
        self.assertEqual(instructions[".loop/agent-protocol.md"], (self.root / ".loop/agent-protocol.md").read_text())
        sources = {s["path"]: s for s in request["context"]["source_documents"]}
        self.assertEqual(sources["docs/requirements.md"]["content"], SPEC)
        self.assertNotIn("source_manifest", request["context"])

    def test_questions_actual_answers_and_resume_keep_one_budget(self):
        self.ask()
        self.start()
        result = self.auto.run(self.team_id)
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertEqual(result["pending_questions"][0]["answers"], [])
        self.assertEqual(diagnose(self.root)["scenario"]["phase"], "AWAITING_INPUT")
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")
        self.auto.answer(self.team_id, "scope", "Yes, preserve nonempty behavior.")
        self.response.update(status="ready", spec_markdown=SPEC, sources=["docs/requirements.md", "answer:scope:1"], questions=[])
        restarted = TeamAutomation(self.team, self.owner.host)
        result = restarted.run(self.team_id, setup_only=True)
        self.assertEqual(result["team_budget"]["dispatches"], 2)
        self.assertEqual(result["team_budget"]["known_tokens"], 40)
        self.assertEqual(result["requirements_setup"]["state"], "READY")
        self.assertEqual(self.owner.transport.requests[-1]["context"]["answers"][0]["answer"], "Yes, preserve nonempty behavior.")
        self.team.teams.audit(self.team_id)

    def test_spec_handoff_continues_to_actual_checks_in_same_team(self):
        self.start()
        self.auto.run(self.team_id, setup_only=True)
        result = self.auto.run(self.team_id)
        self.assertEqual(result["status"], "COMPLETE", result["reason"])
        self.assertTrue(result["final_candidate_current"])
        self.assertEqual(result["team_budget"]["dispatches"], 6)
        self.assertEqual(len(self.team.teams.runs()), 1)

    def test_setup_only_never_dispatches_coordinator_when_ready(self):
        self.start()
        self.auto.run(self.team_id, setup_only=True)
        result = self.auto.run(self.team_id, setup_only=True)
        self.assertEqual(result["stage"], "INTAKE")
        self.assertEqual(len(self.owner.transport.requests), 1)

    def test_missing_goal_waits_for_a_real_answer(self):
        (self.root / "docs/requirements.md").unlink()
        self.ask()
        self.start()
        result = self.auto.run(self.team_id)
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertEqual(result["team_budget"]["dispatches"], 1)

    def test_ready_cannot_bypass_a_blocking_question(self):
        self.owner.policy["max_reworks"] = 0
        self.owner.write_policy()
        self.ask()
        self.response.update(status="ready", spec_markdown=SPEC, sources=["docs/requirements.md"])
        self.start()
        result = self.auto.run(self.team_id)
        self.assertEqual(result["status"], "BUDGET_EXHAUSTED")
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")
        self.assertEqual(load(self.root / ".loop/questions.json")["questions"], [])

    def test_unread_source_cannot_certify_a_spec(self):
        self.owner.policy["max_reworks"] = 0
        self.owner.write_policy()
        self.response["sources"] = ["missing-product-spec.md"]
        self.start()
        result = self.auto.run(self.team_id)
        self.assertEqual(result["status"], "BUDGET_EXHAUSTED")
        self.assertIn("not supplied", result["reason"])
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")

    def test_repair_feedback_uses_fresh_request_and_shared_limit(self):
        original = self.owner.respond
        def respond(request):
            if request["kind"] == "team-specification-request" and not request["context"]["preparation_feedback"]:
                result = deepcopy(self.response)
                result["sources"] = ["not-provided.md"]
                return result
            return original(request)
        self.owner.respond = respond
        self.start()
        result = self.auto.run(self.team_id)
        self.assertEqual(result["stage"], "INTAKE", result["reason"])
        self.assertEqual(result["team_budget"]["reworks"], 1)
        self.assertEqual(result["team_budget"]["dispatches"], 2)

    def test_budget_exhaustion_does_not_reset_between_answers(self):
        self.owner.policy["max_dispatches"] = 1
        self.owner.write_policy()
        self.ask()
        self.start()
        self.auto.run(self.team_id)
        self.auto.answer(self.team_id, "scope", "Yes.")
        result = self.auto.run(self.team_id)
        self.assertEqual(result["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(result["team_budget"]["dispatches"], 1)

    def test_existing_workflow_refuses_spec_rewrite(self):
        self.owner.fixture.save_plan()
        with self.assertRaisesRegex(ContractError, "prepared workflow"):
            self.start(specification=True)
        self.assertEqual(self.team.teams.runs(), [])

    def test_existing_spec_is_preserved_while_questions_are_pending(self):
        (self.root / ".loop/spec.md").write_text(SPEC)
        self.ask()
        self.response["spec_markdown"] = "# Partial clarification draft\n"
        self.start(specification=True)
        result = self.auto.run(self.team_id, setup_only=True)
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertEqual((self.root / ".loop/spec.md").read_text(), SPEC)
        self.assertEqual((self.root / ".loop/spec-draft.md").read_text(), self.response["spec_markdown"])

    def test_changed_document_prevents_stale_install_and_can_retry(self):
        original = self.owner.respond
        changed = False
        def respond(request):
            nonlocal changed
            if request["kind"] == "team-specification-request" and not changed:
                (self.root / "docs/requirements.md").write_text(SPEC + "\nNew compatibility note.\n")
                changed = True
            return original(request)
        self.owner.respond = respond
        self.start()
        result = self.auto.run(self.team_id)
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")
        self.team.resume(self.team_id)
        result = self.auto.run(self.team_id, setup_only=True)
        self.assertEqual(result["requirements_setup"]["state"], "READY")
        self.assertEqual(result["team_budget"]["dispatches"], 2)

    def test_crash_after_prepared_files_recovers_without_duplicate_dispatch(self):
        self.start()
        with patch.object(self.auto, "_apply_control", side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.auto.run(self.team_id, setup_only=True)
        self.assertEqual(self.team.status(self.team_id)["requirements_setup"]["state"], "READY_PENDING")
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")
        result = TeamAutomation(self.team, self.owner.host).run(self.team_id, setup_only=True)
        self.assertEqual(result["requirements_setup"]["state"], "READY")
        self.assertEqual(result["team_budget"]["dispatches"], 1)

    def test_cancelled_setup_cannot_install_returned_spec(self):
        original = self.owner.respond
        def respond(request):
            result = original(request)
            self.team.stop(self.team_id, cancel=True)
            return result
        self.owner.respond = respond
        self.start()
        result = self.auto.run(self.team_id)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")

    def test_partial_io_failure_rolls_forward_without_duplicate_dispatch(self):
        from loop_engineering.workspace import atomic_write
        self.start()
        calls = 0
        def fail_once(path, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated disk interruption")
            return atomic_write(path, content)
        with patch("loop_engineering.team_automation.atomic_write", side_effect=fail_once):
            result = self.auto.run(self.team_id, setup_only=True)
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertEqual(result["requirements_setup"]["state"], "READY_PENDING")
        result = TeamAutomation(self.team, self.owner.host).run(self.team_id, setup_only=True)
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(result["requirements_setup"]["state"], "READY")
        self.assertEqual((self.root / ".loop/spec.md").read_text(), SPEC)
        self.assertEqual(result["team_budget"]["dispatches"], 1)

    def test_recovery_cannot_accept_changed_frozen_instructions(self):
        self.start()
        with patch.object(self.auto, "_apply_control", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.auto.run(self.team_id, setup_only=True)
        (self.root / ".loop/setup.md").write_text("User changed the setup rules\n")
        result = self.auto.run(self.team_id, setup_only=True)
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertIn("Frozen input changed", result["reason"])
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "")

    def test_recovery_preserves_intervening_user_spec_edit(self):
        self.start()
        with patch.object(self.auto, "_apply_control", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.auto.run(self.team_id, setup_only=True)
        (self.root / ".loop/spec.md").write_text("User-written requirements\n")
        result = self.auto.run(self.team_id, setup_only=True)
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertIn("conflicts with a user change", result["reason"])
        self.assertEqual((self.root / ".loop/spec.md").read_text(), "User-written requirements\n")

    def test_context_omission_is_explicit_and_does_not_expose_secrets(self):
        (self.root / "docs/too-large.md").write_text("long text " * 10000)
        (self.root / ".env").write_text("SECRET=must-not-send")
        self.start()
        self.auto.run(self.team_id, setup_only=True)
        context = self.owner.transport.requests[0]["context"]
        self.assertIn("docs/too-large.md", context["source_coverage"]["omitted_paths"])
        self.assertNotIn("SECRET=must-not-send", json.dumps(context))
        self.assertNotIn("docs/too-large.md", context["allowed_source_refs"])

    def test_brief_is_an_actual_source_and_is_frozen(self):
        self.response["sources"] = ["brief"]
        self.start(specification=True, brief="Whitespace should return empty text.")
        result = self.auto.run(self.team_id, setup_only=True)
        self.assertEqual(result["requirements_setup"]["sources"], ["brief"])
        self.assertEqual(self.owner.transport.requests[0]["context"]["brief"], "Whitespace should return empty text.")
        with self.assertRaisesRegex(ContractError, "brief is frozen"):
            self.auto.begin_spec(self.team_id, brief="A replacement product goal.")

    def test_modified_setup_instructions_invalidate_frozen_team(self):
        self.start()
        (self.root / ".loop/setup.md").write_text("Changed instructions\n")
        result = self.auto.run(self.team_id)
        self.assertEqual(result["status"], "AWAITING_INPUT")
        self.assertEqual(result["team_budget"]["dispatches"], 0)

    def test_setup_dispatch_requires_a_private_state_directory(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["setup", str(self.root), "--adapter", "codex"])
        self.assertEqual(code, 2)
        self.assertIn("--state-dir", stderr.getvalue())

    def test_real_cli_command_wrapper_collects_spec_without_developing(self):
        argv = [sys.executable, "-c", "import sys; sys.stdin.read(); print(" + repr(json.dumps(self.response)) + ")"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["setup", str(self.root), "--state-dir", str(self.team.store.directory),
                         "--adapter", "command", "--argv", json.dumps(argv)])
        self.assertEqual(code, 0, stderr.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["requirements_setup"]["state"], "READY")
        self.assertEqual(result["stage"], "INTAKE")
        self.assertEqual(result["tasks"], {})
        self.assertEqual(result["team_budget"]["dispatches"], 1)

    def test_cli_refuses_active_team_before_changing_legacy_setup(self):
        (self.root / ".loop/setup.md").unlink()
        self.start()
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("subprocess.Popen", side_effect=AssertionError("No dispatch")), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["setup", str(self.root), "--state-dir", str(self.team.store.directory), "--adapter", "codex"])
        self.assertEqual(code, 2)
        self.assertIn("unfinished team", stderr.getvalue())
        self.assertFalse((self.root / ".loop/setup.md").exists())
        self.assertTrue(self.team.status(self.team_id)["inputs_current"])

    def test_existing_planning_team_gets_setup_without_resetting_paid_calls(self):
        (self.root / ".loop/spec.md").write_text(SPEC)
        self.owner.plan_response["questions"] = [{"id": "old-question", "question": "Keep compatibility?",
            "reason": "Original coordinator decision", "blocking": True}]
        self.start()
        result = self.auto.run(self.team_id)
        self.assertEqual(result["stage"], "INTAKE")
        self.assertEqual(result["team_budget"]["dispatches"], 1)
        self.auto.answer(self.team_id, "old-question", "Yes.")
        before = self.team.teams.get(self.team_id)["automation"]
        self.auto.begin_spec(self.team_id)
        after = self.team.teams.get(self.team_id)["automation"]
        self.assertEqual(after, before)
        result = self.auto.run(self.team_id, setup_only=True)
        self.assertEqual(result["requirements_setup"]["state"], "READY")
        self.assertEqual(result["team_budget"]["dispatches"], 2)
        self.assertEqual(load(self.root / ".loop/questions.json")["questions"][0]["answers"], ["Yes."])

    def test_setup_assignment_refuses_execution(self):
        self.owner.fixture.save_plan()
        (self.root / ".loop/spec.md").write_text(SPEC)
        self.owner.fixture.fixture.select_team()
        self.start()
        with self.assertRaisesRegex(ContractError, "planning team"):
            self.auto.begin_spec(self.team_id)

    def test_setup_assignment_refuses_unreconciled_operation(self):
        (self.root / ".loop/spec.md").write_text(SPEC)
        self.start()
        with self.team.teams.writer(self.team_id) as data:
            data["automation"]["operations"]["interrupted-operation"] = {"status": "ADMITTED"}
            self.team.teams.save(data, "team.offline_interrupted_operation")
        with self.assertRaisesRegex(ContractError, "outstanding operation"):
            self.auto.begin_spec(self.team_id)
        self.assertNotIn("specification", self.team.teams.get(self.team_id))

    def test_setup_assignment_preserves_a_pause(self):
        (self.root / ".loop/spec.md").write_text(SPEC)
        self.start()
        self.team.teams.signal(self.team_id, "PAUSED")
        with self.assertRaisesRegex(ContractError, "planning team"):
            self.auto.begin_spec(self.team_id)
        self.assertEqual(self.team.teams.signal(self.team_id), "PAUSED")
        self.assertNotIn("specification", self.team.teams.get(self.team_id))

    def test_cli_can_continue_existing_planning_team_as_requirements_setup(self):
        (self.root / ".loop/spec.md").write_text(SPEC)
        self.start()
        argv = [sys.executable, "-c", "import sys; sys.stdin.read(); print(" + repr(json.dumps(self.response)) + ")"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["setup", "--team-id", self.team_id, "--state-dir", str(self.team.store.directory),
                         "--adapter", "command", "--argv", json.dumps(argv)])
        self.assertEqual(code, 0, stderr.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["team_id"], self.team_id)
        self.assertEqual(result["requirements_setup"]["state"], "READY")
